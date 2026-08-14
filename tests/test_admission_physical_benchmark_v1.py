from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from experiments.cooperbench.confirmatory_30x3 import admission_physical_v1 as physical


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def handle():\n    return 2\n", encoding="utf-8")
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
                    "what": "handle",
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
                    "what": "handle",
                }
            ]
        },
    )


def _fake_scip(repo, builtin, *, revision, cache_root, force):
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


def test_profiles_are_ordered_causal_stack_with_baselines() -> None:
    assert physical.DEFAULT_ADMISSION_PHYSICAL_PROFILES == (
        physical.AdmissionPhysicalProfile.SERIAL,
        physical.AdmissionPhysicalProfile.NAIVE_PARALLEL,
        physical.AdmissionPhysicalProfile.BROAD_DECLARED,
        physical.AdmissionPhysicalProfile.SYMBOL_PROJECTION,
        physical.AdmissionPhysicalProfile.DEPENDENCY_NARROWING,
        physical.AdmissionPhysicalProfile.REFINED_POLICY,
    )
    assert physical.parse_admission_physical_profiles(
        "refined_policy,serial,refined_policy"
    ) == (
        physical.AdmissionPhysicalProfile.REFINED_POLICY,
        physical.AdmissionPhysicalProfile.SERIAL,
    )
    with pytest.raises(ValueError):
        physical.parse_admission_physical_profiles("")


def test_controlled_profiles_share_warm_scip_graph_and_bind_9e(monkeypatch, tmp_path) -> None:
    repo, revision = _init_repo(tmp_path)
    plan_a, plan_b = _plans()
    calls: list[bool] = []

    def fake_scip(*args, **kwargs):
        calls.append(bool(kwargs["force"]))
        return _fake_scip(*args, **kwargs)

    monkeypatch.setattr(physical, "_required_scip_graph", fake_scip)
    result = physical.build_pair_admission_profiles(
        repo,
        base_commit=revision,
        plan_a=plan_a,
        plan_b=plan_b,
        cache_root=tmp_path / "cache",
    )

    assert calls == [True, False]
    assert set(result) == set(physical._CONTROLLED_PROFILES)
    fingerprints = {
        item["verdict"]["admission_physical_evidence"]["semantic_graph_fingerprint"]
        for item in result.values()
    }
    ablations = {
        item["verdict"]["admission_physical_evidence"]["ablation_fingerprint"]
        for item in result.values()
    }
    assert len(fingerprints) == 1
    assert len(ablations) == 1
    assert all(item["timing"]["scip_cache_hit"] is True for item in result.values())
    assert all(
        item["verdict"]["admission_physical_evidence"]["candidate_blocking_enabled"]
        is False
        for item in result.values()
    )
    assert all(
        item["timing"]["scip_cache_seed_seconds_excluded"] >= 0
        for item in result.values()
    )


def test_refined_policy_can_release_broad_serialization_without_changing_graph(monkeypatch, tmp_path) -> None:
    repo, revision = _init_repo(tmp_path)
    plan_a, plan_b = _plans()
    monkeypatch.setattr(physical, "_required_scip_graph", _fake_scip)

    result = physical.build_pair_admission_profiles(
        repo,
        base_commit=revision,
        plan_a=plan_a,
        plan_b=plan_b,
        cache_root=tmp_path / "cache",
    )

    broad = result[physical.AdmissionPhysicalProfile.BROAD_DECLARED]["verdict"]
    refined = result[physical.AdmissionPhysicalProfile.REFINED_POLICY]["verdict"]
    assert broad["serialized"] is True
    assert refined["serialized"] is False
    assert (
        broad["admission_physical_evidence"]["semantic_graph_fingerprint"]
        == refined["admission_physical_evidence"]["semantic_graph_fingerprint"]
    )


def test_execution_order_is_deterministic_rotation() -> None:
    first = physical._execution_order(
        fingerprint="f" * 64,
        coder_seed=101,
        pair_index=1,
        profiles=physical.DEFAULT_ADMISSION_PHYSICAL_PROFILES,
    )
    second = physical._execution_order(
        fingerprint="f" * 64,
        coder_seed=101,
        pair_index=1,
        profiles=physical.DEFAULT_ADMISSION_PHYSICAL_PROFILES,
    )
    assert first == second
    assert set(first) == set(physical.DEFAULT_ADMISSION_PHYSICAL_PROFILES)


def test_stage_transition_summary_counts_released_serialization() -> None:
    rows = []
    for profile, serialized, concurrent in [
        ("broad_declared", True, False),
        ("symbol_projection", True, False),
        ("dependency_narrowing", True, False),
        ("refined_policy", False, True),
    ]:
        rows.append(
            {
                "pair_index": 1,
                "coder_seed": 101,
                "admission_physical_profile": profile,
                "serialized": serialized,
                "physical_concurrency_observed": concurrent,
                "integration_success": True,
                "pair_pass": True,
            }
        )
    transitions = physical._stage_transition_summary(rows)
    assert transitions[-1]["released_serializations"] == 1
    assert transitions[-1]["newly_serialized_pairs"] == 0
    assert transitions[-1]["physical_concurrency_rate_delta"] == pytest.approx(1.0)


def test_speedup_summary_pairs_exact_seed_and_pair() -> None:
    rows = []
    for seed, serial_seconds, refined_seconds in [(101, 100.0, 50.0), (202, 200.0, 200.0)]:
        rows.extend(
            [
                {
                    "pair_index": 1,
                    "coder_seed": seed,
                    "admission_physical_profile": "serial",
                    "integration_success": True,
                    "execution_wall_time_seconds": serial_seconds,
                    "end_to_end_wall_time_seconds": serial_seconds,
                },
                {
                    "pair_index": 1,
                    "coder_seed": seed,
                    "admission_physical_profile": "refined_policy",
                    "integration_success": True,
                    "execution_wall_time_seconds": refined_seconds,
                    "end_to_end_wall_time_seconds": refined_seconds,
                },
            ]
        )
    summary, exclusions = physical._paired_speedup_summary(
        rows,
        (
            physical.AdmissionPhysicalProfile.SERIAL,
            physical.AdmissionPhysicalProfile.REFINED_POLICY,
        ),
        baseline=physical.AdmissionPhysicalProfile.SERIAL,
    )
    refined = summary["refined_policy"]
    assert refined["valid_speedup_observations"] == 2
    assert refined["mean_execution_speedup"] == pytest.approx(1.5)
    assert exclusions == []


def test_result_identity_is_revision_bound() -> None:
    profiles = physical.DEFAULT_ADMISSION_PHYSICAL_PROFILES
    name = physical._result_name(profiles)
    key = "+".join(sorted(profile.value for profile in profiles))
    unrevisioned = f"result-{__import__('hashlib').sha256(key.encode()).hexdigest()[:12]}.json"
    assert physical.ADMISSION_PHYSICAL_V1_RESULT_REVISION == 1
    assert name.startswith("result-")
    assert name != unrevisioned


def test_schema_cli_and_research_script_contract_are_registered() -> None:
    from experiments.cooperbench.cli import build_parser

    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "experiments/cooperbench/schemas/admission-granularity-physical-benchmark-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["protocol"]["const"] == physical.ADMISSION_PHYSICAL_V1_PROTOCOL
    assert schema["properties"]["result_revision"]["const"] == physical.ADMISSION_PHYSICAL_V1_RESULT_REVISION
    assert set(schema["properties"]["profiles"]["items"]["enum"]) == {
        profile.value for profile in physical.DEFAULT_ADMISSION_PHYSICAL_PROFILES
    }

    parser = build_parser()
    args = parser.parse_args(
        [
            "confirmatory",
            "admission-v1-run",
            "--cooperbench",
            "/tmp/CooperBench",
        ]
    )
    assert args.seeds == "101"
    assert args.pairs == "1-6"
    assert args.max_parallel_pairs == 2
    assert "refined_policy" in args.profiles

    script = (root / "scripts/cooperbench-private.sh").read_text(encoding="utf-8")
    assert "admission-v1-smoke" in script
    assert 'admission_v1_run "101,202,303" "1-30"' in script
    assert "CLAIM_PLANE_ADMISSION_V1_MAX_PARALLEL_PAIRS" in script
