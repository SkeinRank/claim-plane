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
