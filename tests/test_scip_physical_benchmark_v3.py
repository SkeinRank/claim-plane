from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane.core import build_python_dependency_graph
from claim_plane.swarm import SwarmBudgetPolicy, WorkGraph, compute_concurrency_plan
from experiments.cooperbench.confirmatory_30x3 import scip_v3


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, revision


def _plans():
    return (
        {
            "files": [
                {
                    "path": "a.py",
                    "action": "modify",
                    "commitment": "committed",
                    "line_start": 1,
                    "line_end": 2,
                    "what": "alpha",
                }
            ]
        },
        {
            "files": [
                {
                    "path": "b.py",
                    "action": "modify",
                    "commitment": "committed",
                    "line_start": 1,
                    "line_end": 2,
                    "what": "beta",
                }
            ]
        },
    )


def test_parse_scip_v3_profiles_deduplicates_and_preserves_order() -> None:
    assert scip_v3.parse_scip_v3_profiles(
        "scip_cache_blocking,serial,scip_cache_blocking"
    ) == (
        scip_v3.ScipV3Profile.SCIP_CACHE_BLOCKING,
        scip_v3.ScipV3Profile.SERIAL,
    )
    with pytest.raises(ValueError):
        scip_v3.parse_scip_v3_profiles("")


def test_candidate_blocking_can_be_disabled_without_changing_default_metadata() -> None:
    sources = {
        "a.py": "def alpha():\n    return 1\n",
        "b.py": "def beta():\n    return 2\n",
    }
    semantic_graph = build_python_dependency_graph(sources)
    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                {
                    "work_id": "A",
                    "title": "A",
                    "goal": "A",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "symbol",
                                "identifier": "alpha",
                                "metadata": {"path": "a.py", "qualified_identifier": "alpha"},
                            },
                        }
                    ],
                },
                {
                    "work_id": "B",
                    "title": "B",
                    "goal": "B",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "symbol",
                                "identifier": "beta",
                                "metadata": {"path": "b.py", "qualified_identifier": "beta"},
                            },
                        }
                    ],
                },
            ],
        }
    )
    policy = SwarmBudgetPolicy.from_dict(
        {
            "workers": {"max_active": 2, "max_active_per_work_item": 1, "max_work_items": 2},
            "concurrency": {"same_file": "region_safe"},
        }
    )

    default = compute_concurrency_plan(graph, policy, semantic_graph=semantic_graph)
    disabled = compute_concurrency_plan(
        graph,
        policy,
        semantic_graph=semantic_graph,
        candidate_blocking_enabled=False,
    )

    assert "candidate_blocking_enabled" not in default.metadata
    assert disabled.metadata["candidate_blocking_enabled"] is False
    assert disabled.metadata["candidate_blocking"] is None


def test_pair_profile_builder_measures_cold_then_warm_cache(monkeypatch, tmp_path) -> None:
    repo, revision = _init_repo(tmp_path)
    plan_a, plan_b = _plans()
    calls: list[bool] = []

    def fake_required_scip_graph(repo, builtin, *, revision, cache_root, force):
        calls.append(force)
        return builtin, {
            "scip_index_seconds": 0.02 if force else 0.001,
            "scip_decode_graph_seconds": 0.003,
            "graph_merge_seconds": 0.001,
            "scip_cache_hit": not force,
            "scip_cache_key": "a" * 64,
            "scip_artifact_sha256": "b" * 64,
            "scip_artifact_size_bytes": 100,
            "scip_indexer_id": "scip-python",
            "scip_indexer_version": "test",
            "workspace_fingerprint": "c" * 64,
        }

    monkeypatch.setattr(scip_v3, "_required_scip_graph", fake_required_scip_graph)

    result = scip_v3.build_pair_admission_profiles(
        repo,
        base_commit=revision,
        plan_a=plan_a,
        plan_b=plan_b,
        cache_root=tmp_path / "cache",
    )

    assert calls == [True, False]
    cold = result[scip_v3.ScipV3Profile.SCIP_GRAPH_COLD]
    warm = result[scip_v3.ScipV3Profile.SCIP_CACHE_BLOCKING]
    assert cold["timing"]["scip_cache_hit"] is False
    assert warm["timing"]["scip_cache_hit"] is True
    assert cold["verdict"]["scip_v3_evidence"]["candidate_blocking_enabled"] is False
    assert warm["verdict"]["scip_v3_evidence"]["candidate_blocking_enabled"] is True
    assert warm["timing"]["builtin_cache_hit"] is True


def test_mean_active_agents_uses_measured_worker_intervals() -> None:
    row = {
        "serialized": False,
        "physical_timing": {
            "union_seconds": 2.0,
            "agent_a": {"duration_seconds": 2.0},
            "agent_b": {"duration_seconds": 1.0},
        },
    }
    assert scip_v3._mean_active_agents(row) == pytest.approx(1.5)
    assert scip_v3._mean_active_agents({"serialized": True}) == 1.0


def test_v3_schema_and_cli_contract_are_registered() -> None:
    from experiments.cooperbench.cli import build_parser

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "experiments/cooperbench/schemas/scip-ablation-physical-benchmark-v3.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["protocol"]["const"] == scip_v3.SCIP_PHYSICAL_V3_PROTOCOL
    assert schema["properties"]["result_revision"]["const"] == scip_v3.SCIP_PHYSICAL_V3_RESULT_REVISION

    parser = build_parser()
    args = parser.parse_args(
        [
            "confirmatory",
            "scip-v3-run",
            "--cooperbench",
            "/tmp/CooperBench",
            "--pairs",
            "1-6",
        ]
    )
    assert args.seeds == "101"
    assert args.pairs == "1-6"
    assert args.max_parallel_pairs == 6



def test_agent_failure_is_retained_for_reliability_but_excluded_from_speedup() -> None:
    failed = {
        "agent_execution_failure": True,
        "integration_success": False,
        "execution_wall_time_seconds": 10.0,
        "end_to_end_wall_time_seconds": 12.0,
    }
    validity = scip_v3._measurement_validity(failed)
    assert validity == {
        "execution_outcome": "agent_failure",
        "speedup_eligible": False,
        "speedup_exclusion_reason": "agent_execution_failure",
    }

    serial = {
        "integration_success": True,
        "execution_wall_time_seconds": 100.0,
        "end_to_end_wall_time_seconds": 100.0,
    }
    execution, end_to_end, reason = scip_v3._paired_speedup(serial, failed)
    assert execution is None
    assert end_to_end is None
    assert reason == "agent_execution_failure"


def test_integration_failure_remains_timing_eligible() -> None:
    row = {
        "integration_success": False,
        "execution_wall_time_seconds": 50.0,
        "end_to_end_wall_time_seconds": 50.0,
    }
    validity = scip_v3._measurement_validity(row)
    assert validity["execution_outcome"] == "integration_failure"
    assert validity["speedup_eligible"] is True

    serial = {
        "integration_success": True,
        "execution_wall_time_seconds": 100.0,
        "end_to_end_wall_time_seconds": 100.0,
    }
    execution, end_to_end, reason = scip_v3._paired_speedup(serial, row)
    assert execution == pytest.approx(2.0)
    assert end_to_end == pytest.approx(2.0)
    assert reason is None


def test_profile_summary_excludes_truncated_attempt_from_latency_means() -> None:
    rows = [
        {
            "scip_v3_profile": "scip_cache_blocking",
            "integration_success": True,
            "pair_pass": True,
            "serialized": False,
            "physical_concurrency_observed": True,
            "execution_wall_time_seconds": 100.0,
            "control_plane_wall_time_seconds": 5.0,
            "end_to_end_wall_time_seconds": 105.0,
            "critical_path_seconds": 80.0,
            "mean_active_agents": 1.5,
            "control_plane": {"scip_cache_hit": True},
        },
        {
            "scip_v3_profile": "scip_cache_blocking",
            "agent_execution_failure": True,
            "integration_success": False,
            "pair_pass": False,
            "serialized": True,
            "physical_concurrency_observed": False,
            "execution_wall_time_seconds": 10.0,
            "control_plane_wall_time_seconds": 5.0,
            "end_to_end_wall_time_seconds": 15.0,
            "critical_path_seconds": 5.0,
            "mean_active_agents": 1.0,
            "control_plane": {"scip_cache_hit": True},
        },
    ]
    summary = scip_v3._profile_summary(rows, scip_v3.ScipV3Profile.SCIP_CACHE_BLOCKING)
    assert summary["observations"] == 2
    assert summary["timing_observations"] == 1
    assert summary["excluded_timing_observations"] == 1
    assert summary["execution_outcome_counts"]["agent_failure"] == 1
    assert summary["mean_execution_wall_time_seconds"] == pytest.approx(100.0)
    assert summary["mean_attempt_wall_time_seconds"] == pytest.approx(55.0)


def test_legacy_rows_can_be_annotated_without_rerunning_models() -> None:
    legacy = {
        "agent_execution_failure": True,
        "integration_success": False,
        "error": "Coding agent failed to produce a valid tool action after 4 transport attempts.",
    }
    annotated = scip_v3._annotate_measurement_validity(legacy)
    assert annotated["execution_outcome"] == "agent_failure"
    assert annotated["speedup_eligible"] is False
    assert annotated["speedup_exclusion_reason"] == "agent_execution_failure"



def test_paired_speedup_matches_exact_pair_and_coder_seed() -> None:
    rows = []
    for seed, serial_seconds, profile_seconds in [(101, 100.0, 50.0), (202, 200.0, 200.0)]:
        rows.extend(
            [
                {
                    "pair": "same-pair",
                    "pair_index": 1,
                    "coder_seed": seed,
                    "scip_v3_profile": "serial",
                    "integration_success": True,
                    "execution_wall_time_seconds": serial_seconds,
                    "end_to_end_wall_time_seconds": serial_seconds,
                },
                {
                    "pair": "same-pair",
                    "pair_index": 1,
                    "coder_seed": seed,
                    "scip_v3_profile": "scip_cache_blocking",
                    "integration_success": True,
                    "execution_wall_time_seconds": profile_seconds,
                    "end_to_end_wall_time_seconds": profile_seconds,
                },
            ]
        )

    summary, exclusions = scip_v3._paired_speedup_summary(
        rows,
        (scip_v3.ScipV3Profile.SERIAL, scip_v3.ScipV3Profile.SCIP_CACHE_BLOCKING),
    )
    warm = summary["scip_cache_blocking"]
    assert warm["valid_speedup_observations"] == 2
    assert warm["mean_execution_speedup_vs_serial"] == pytest.approx(1.5)
    assert warm["median_execution_speedup_vs_serial"] == pytest.approx(1.5)
    assert exclusions == []



def test_v3_result_identity_is_revisioned_without_losing_legacy_lookup() -> None:
    profiles = (
        scip_v3.ScipV3Profile.SERIAL,
        scip_v3.ScipV3Profile.SCIP_CACHE_BLOCKING,
    )
    assert scip_v3.SCIP_PHYSICAL_V3_RESULT_REVISION == 2
    assert scip_v3._result_name(profiles) != scip_v3._legacy_result_name(profiles)
    assert scip_v3._result_name(profiles).startswith("result-")
    assert scip_v3._legacy_result_name(profiles).startswith("result-")
