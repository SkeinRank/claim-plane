from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_plane import cli
from claim_plane.dogfood import (
    DOGFOOD_GATE_PROTOCOL,
    DOGFOOD_PLAN_PROTOCOL,
    DOGFOOD_RESULT_PROTOCOL,
    DOGFOOD_SUITE_PROTOCOL,
    DOGFOOD_SUMMARY_PROTOCOL,
    DogfoodArm,
    DogfoodPlan,
    DogfoodResult,
    GoldenSuite,
    aggregate_dogfood_results,
    build_dogfood_plan,
    build_dogfood_result,
    evaluate_dogfood_release_gate,
    freeze_golden_suite,
)


def _candidate(*, tasks: int = 20, repositories: int = 5, seeds=(101, 202)):
    repo_rows = [
        {
            "repository_id": f"repo-{index}",
            "clone_url": f"https://example.invalid/repo-{index}.git",
            "base_commit": f"{index + 1:040x}",
            "language": "python",
        }
        for index in range(repositories)
    ]
    classes = ("bugfix", "feature", "test", "documentation")
    risks = ("low", "medium", "high", "critical")
    task_rows = []
    for index in range(tasks):
        prompt = f"Implement frozen task {index} without unrelated changes."
        task_rows.append(
            {
                "task_id": f"task-{index:02d}",
                "repository_id": f"repo-{index % repositories}",
                "prompt": prompt,
                "source_ref": f"issue:{1000 + index}",
                "task_class": classes[index % len(classes)],
                "risk_class": risks[index % len(risks)],
                "acceptance": ["python -m pytest -q"],
                "split": "dogfood",
            }
        )
    return {
        "suite_id": "claim-plane-golden-v1",
        "description": "Frozen single-agent technical-preview task corpus.",
        "selection_seed": 42,
        "coder_seeds": list(seeds),
        "repositories": repo_rows,
        "tasks": task_rows,
    }


def _result(
    plan,
    entry,
    *,
    task_success: bool,
    accepted_delivery: bool,
) -> DogfoodResult:
    return DogfoodResult.from_dict(
        {
            "protocol": DOGFOOD_RESULT_PROTOCOL,
            "execution_id": entry.execution_id,
            "plan_digest": plan.digest,
            "suite_digest": plan.suite_digest,
            "task_id": entry.task_id,
            "repository_id": entry.repository_id,
            "seed": entry.seed,
            "arm": entry.arm.value,
            "outcome": "COMPLETED",
            "evaluation_complete": True,
            "task_success": task_success,
            "accepted_delivery": accepted_delivery,
            "undeclared_mutations": 0 if accepted_delivery else 1,
            "scope_amendments": 1 if entry.arm is DogfoodArm.GUARDED else 0,
            "false_blocks": 0,
            "missed_mutations": 0,
            "human_repairs": 0,
            "retries": 0,
            "wall_time_seconds": 10.0,
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.01,
            "files_changed": 2,
            "lines_added": 5,
            "lines_deleted": 1,
            "public_api_drift": False,
            "dependency_drift": False,
            "evidence_digest": "a" * 64,
        }
    )


def test_release_grade_suite_freezes_prompt_and_repository_inputs() -> None:
    suite = freeze_golden_suite(
        _candidate(),
        frozen_at="2026-08-03T00:00:00Z",
        require_release_grade=True,
    )

    assert suite.protocol == DOGFOOD_SUITE_PROTOCOL
    assert len(suite.tasks) == 20
    assert len(suite.repositories) == 5
    assert len(suite.coder_seeds) == 2
    assert len(suite.digest) == 64
    assert suite.tasks[0].prompt_sha256
    assert (
        GoldenSuite.from_dict(suite.to_dict(), require_release_grade=True).digest
        == suite.digest
    )


def test_release_grade_validation_rejects_small_or_unpinned_corpus() -> None:
    with pytest.raises(ValueError, match="task count"):
        freeze_golden_suite(
            _candidate(tasks=5),
            frozen_at="2026-08-03T00:00:00Z",
            require_release_grade=True,
        )

    candidate = _candidate()
    candidate["repositories"][0]["base_commit"] = "abc1234"
    with pytest.raises(ValueError, match="40-character"):
        freeze_golden_suite(
            candidate,
            frozen_at="2026-08-03T00:00:00Z",
            require_release_grade=True,
        )


def test_suite_digest_detects_prompt_tampering() -> None:
    suite = freeze_golden_suite(
        _candidate(), frozen_at="2026-08-03T00:00:00Z"
    ).to_dict()
    suite["tasks"][0]["prompt"] = "Different task"

    with pytest.raises(ValueError, match="prompt_sha256 mismatch"):
        GoldenSuite.from_dict(suite)


def test_plan_expands_every_task_seed_and_arm_deterministically() -> None:
    suite = freeze_golden_suite(_candidate(), frozen_at="2026-08-03T00:00:00Z")
    first = build_dogfood_plan(
        suite, model="codex-test", created_at="2026-08-03T01:00:00Z"
    )
    second = build_dogfood_plan(
        suite, model="codex-test", created_at="2026-08-03T01:00:00Z"
    )

    assert first.protocol == DOGFOOD_PLAN_PROTOCOL
    assert first.digest == second.digest
    assert len(first.entries) == 20 * 2 * 3
    assert len({entry.execution_id for entry in first.entries}) == len(first.entries)
    assert {entry.arm for entry in first.entries} == set(DogfoodArm)
    assert DogfoodPlan.from_dict(first.to_dict()).digest == first.digest


def test_result_binding_rejects_identity_override() -> None:
    suite = freeze_golden_suite(
        _candidate(tasks=1, repositories=1, seeds=(101,)),
        frozen_at="2026-08-03T00:00:00Z",
    )
    plan = build_dogfood_plan(suite, created_at="2026-08-03T01:00:00Z")
    evaluation = _result(
        plan,
        plan.entries[0],
        task_success=True,
        accepted_delivery=True,
    ).to_dict()
    evaluation.pop("protocol")
    evaluation.pop("execution_id")
    evaluation.pop("plan_digest")
    evaluation.pop("suite_digest")
    evaluation.pop("task_id")
    evaluation.pop("repository_id")
    evaluation.pop("seed")
    evaluation.pop("arm")

    bound = build_dogfood_result(plan, plan.entries[0].execution_id, evaluation)
    assert bound.execution_id == plan.entries[0].execution_id

    tampered = dict(evaluation)
    tampered["task_id"] = "different-task"
    with pytest.raises(ValueError, match="protected field task_id"):
        build_dogfood_result(plan, plan.entries[0].execution_id, tampered)


def test_aggregation_never_fills_missing_measurements() -> None:
    suite = freeze_golden_suite(
        _candidate(tasks=1, repositories=1, seeds=(101,)),
        frozen_at="2026-08-03T00:00:00Z",
    )
    plan = build_dogfood_plan(suite, created_at="2026-08-03T01:00:00Z")
    one = _result(plan, plan.entries[0], task_success=True, accepted_delivery=True)

    summary = aggregate_dogfood_results(
        suite, plan, [one], generated_at="2026-08-03T02:00:00Z"
    )

    assert summary["protocol"] == DOGFOOD_SUMMARY_PROTOCOL
    assert summary["completeness"]["complete"] is False
    assert summary["completeness"]["matched"] == 1
    assert len(summary["completeness"]["missing"]) == 2
    assert summary["arms"][one.arm.value]["evaluated_count"] == 1


def test_release_gate_passes_compensating_reliability_gain() -> None:
    suite = freeze_golden_suite(_candidate(), frozen_at="2026-08-03T00:00:00Z")
    plan = build_dogfood_plan(suite, created_at="2026-08-03T01:00:00Z")
    results = []
    for index, entry in enumerate(plan.entries):
        if entry.arm is DogfoodArm.BARE_CODEX:
            success = index % 5 != 0
            accepted = index % 2 == 0
        elif entry.arm is DogfoodArm.OBSERVE:
            success = index % 5 != 0
            accepted = index % 3 != 0
        else:
            success = index % 4 != 0
            accepted = index % 5 != 0
        results.append(
            _result(
                plan,
                entry,
                task_success=success,
                accepted_delivery=accepted,
            )
        )
    summary = aggregate_dogfood_results(
        suite, plan, results, generated_at="2026-08-03T02:00:00Z"
    )

    gate = evaluate_dogfood_release_gate(
        summary,
        max_task_success_drop=0.01,
        min_accepted_delivery_gain=0.05,
        evaluated_at="2026-08-03T03:00:00Z",
    )

    assert summary["completeness"]["complete"] is True
    assert gate["protocol"] == DOGFOOD_GATE_PROTOCOL
    assert gate["status"] == "PASSED"
    assert gate["release_allowed"] is True


def test_release_gate_blocks_uncompensated_success_regression() -> None:
    summary = {
        "protocol": DOGFOOD_SUMMARY_PROTOCOL,
        "digest": "b" * 64,
        "completeness": {"complete": True},
        "arms": {
            "bare-codex": {
                "task_success_rate": 0.90,
                "accepted_delivery_rate": 0.70,
            },
            "claim-plane-observe": {
                "task_success_rate": 0.90,
                "accepted_delivery_rate": 0.72,
            },
            "claim-plane-guarded": {
                "task_success_rate": 0.75,
                "accepted_delivery_rate": 0.71,
            },
        },
    }

    gate = evaluate_dogfood_release_gate(
        summary,
        max_task_success_drop=0.05,
        min_accepted_delivery_gain=0.05,
        evaluated_at="2026-08-03T03:00:00Z",
    )

    assert gate["status"] == "BLOCKED"
    assert gate["release_allowed"] is False
    assert gate["findings"][0]["code"] == "guarded_success_regression"


def test_cli_freeze_plan_aggregate_and_gate(tmp_path: Path) -> None:
    candidate = _candidate(tasks=1, repositories=1, seeds=(101,))
    candidate_path = tmp_path / "candidate.json"
    suite_path = tmp_path / "suite.json"
    plan_path = tmp_path / "plan.json"
    results_path = tmp_path / "results.json"
    summary_path = tmp_path / "summary.json"
    gate_path = tmp_path / "gate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    assert (
        cli.main(
            [
                "dogfood",
                "freeze",
                str(candidate_path),
                "--out",
                str(suite_path),
                "--frozen-at",
                "2026-08-03T00:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "dogfood",
                "plan",
                str(suite_path),
                "--out",
                str(plan_path),
                "--created-at",
                "2026-08-03T01:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    suite = GoldenSuite.from_dict(json.loads(suite_path.read_text()))
    plan = DogfoodPlan.from_dict(json.loads(plan_path.read_text()))
    results = []
    for index, entry in enumerate(plan.entries):
        evaluation = _result(
            plan, entry, task_success=True, accepted_delivery=True
        ).to_dict()
        for protected in (
            "protocol",
            "execution_id",
            "plan_digest",
            "suite_digest",
            "task_id",
            "repository_id",
            "seed",
            "arm",
        ):
            evaluation.pop(protected)
        evaluation_path = tmp_path / f"evaluation-{index}.json"
        result_path = tmp_path / f"result-{index}.json"
        evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
        assert (
            cli.main(
                [
                    "dogfood",
                    "record",
                    str(plan_path),
                    entry.execution_id,
                    str(evaluation_path),
                    "--out",
                    str(result_path),
                    "--json",
                ]
            )
            == 0
        )
        results.append(json.loads(result_path.read_text()))
    results_path.write_text(json.dumps(results), encoding="utf-8")

    assert (
        cli.main(
            [
                "dogfood",
                "aggregate",
                str(suite_path),
                str(plan_path),
                str(results_path),
                "--out",
                str(summary_path),
                "--generated-at",
                "2026-08-03T02:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "dogfood",
                "gate",
                str(summary_path),
                "--out",
                str(gate_path),
                "--evaluated-at",
                "2026-08-03T03:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    gate = json.loads(gate_path.read_text())
    assert gate["status"] == "PASSED"
    assert suite.digest == plan.suite_digest
