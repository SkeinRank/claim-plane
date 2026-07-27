from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.cooperbench.cli import main as experiment_main
from experiments.cooperbench.paper_6pair.config import (
    FROZEN_PAIRS,
    PAPER_STUDY,
    REFERENCE_SUMMARY,
)
from experiments.cooperbench.paper_6pair.dataset import (
    benchmark_provenance,
    frozen_dataset_digest,
    stable_seed,
    validate_frozen_pairs,
    verify_pair_labels,
)
from experiments.cooperbench.paper_6pair.runner import (
    aggregate_results,
    compare_reference,
)


def test_paper_study_freezes_exact_v85_pair_order() -> None:
    assert PAPER_STUDY.claim_plane_version == "0.2.1"
    assert PAPER_STUDY.planner_policy_version == "planner-v1"
    assert PAPER_STUDY.coder_seeds == (101,)
    assert [pair.key for pair in FROZEN_PAIRS] == [
        "pallets_jinja_task/task1465/feature1+feature8",
        "pallets_jinja_task/task1559/feature1+feature4",
        "pallets_jinja_task/task1621/feature1+feature7",
        "pallets_click_task/task2800/feature1+feature3",
        "pallets_jinja_task/task1559/feature6+feature8",
        "samuelcolvin_dirty_equals_task/task43/feature5+feature7",
    ]
    assert sum(bool(pair.gold_conflict) for pair in FROZEN_PAIRS) == 3


def test_agent_seed_matches_frozen_pair_identity() -> None:
    pair = FROZEN_PAIRS[0]
    first = stable_seed(pair, 0, "A", "implementation")
    second = stable_seed(pair, 0, "A", "implementation")
    other = stable_seed(pair, 0, "B", "implementation")

    assert first == second
    assert first != other
    assert 0 <= first < 2_000_000_000


def _write_task(
    dataset: Path, repo: str, task_id: int, features: tuple[int, ...]
) -> None:
    task = dataset / repo / f"task{task_id}"
    task.mkdir(parents=True)
    (task / "setup.sh").write_text(
        'BASE_COMMIT="abc123"\ngit clone https://example.invalid/repo.git repo\n',
        encoding="utf-8",
    )
    for feature_id in features:
        feature = task / f"feature{feature_id}"
        feature.mkdir()
        (feature / "feature.md").write_text("feature", encoding="utf-8")
        (feature / "feature.patch").write_text("diff", encoding="utf-8")
        (feature / "tests.patch").write_text("diff", encoding="utf-8")


def _fake_dataset(tmp_path: Path) -> Path:
    dataset = tmp_path / "CooperBench" / "dataset"
    needed: dict[tuple[str, int], set[int]] = {}
    for pair in FROZEN_PAIRS:
        needed.setdefault((pair.repo, pair.task_id), set()).update(
            (pair.feature_a, pair.feature_b)
        )
    for (repo, task_id), features in needed.items():
        _write_task(dataset, repo, task_id, tuple(sorted(features)))
    conflicts = [
        {
            "repo": pair.repo,
            "task_id": pair.task_id,
            "f1": pair.feature_a,
            "f2": pair.feature_b,
        }
        for pair in FROZEN_PAIRS
        if pair.gold_conflict
    ]
    (dataset / "gold_conflict_report.json").write_text(
        json.dumps({"conflict_pairs": conflicts}), encoding="utf-8"
    )
    return dataset


def test_dataset_validation_accepts_all_frozen_inputs(tmp_path: Path) -> None:
    dataset = _fake_dataset(tmp_path)
    tasks = validate_frozen_pairs(dataset)
    verify_pair_labels(dataset)

    assert len(tasks) == 5


def test_dataset_validation_rejects_missing_frozen_feature(tmp_path: Path) -> None:
    dataset = _fake_dataset(tmp_path)
    missing = dataset / "pallets_jinja_task" / "task1465" / "feature8"
    for child in missing.iterdir():
        child.unlink()
    missing.rmdir()

    with pytest.raises(RuntimeError, match="task1465"):
        validate_frozen_pairs(dataset)


def test_reference_aggregation_matches_published_mechanism_counts() -> None:
    rows: list[dict[str, object]] = []
    for arm, expected in REFERENCE_SUMMARY.items():
        for index in range(int(expected["n"])):
            rows.append(
                {
                    "arm": arm,
                    "pair_pass": index < int(expected["pair_pass"]),
                    "integration_success": index < int(expected["integration_success"]),
                    "initial_serialized": index < int(expected["initial_serialized"]),
                    "serialized": index < int(expected["initial_serialized"]),
                    "scope_promotions_succeeded": (
                        int(expected["promotions"]) if index == 0 else 0
                    ),
                    "scope_promotions_rejected": 0,
                    "scope_undeclared_blocks": (
                        int(expected["undeclared_blocks"]) if index == 0 else 0
                    ),
                    "planner_failure": False,
                    "scope_enforcement_failure": False,
                    "agent_execution_failure": False,
                    "harness_failure": False,
                    "logical_total_cost": 0.1,
                }
            )

    summary = aggregate_results(rows)
    comparison = compare_reference(summary)

    assert comparison["matches_published_mechanism_counts"] is True
    assert comparison["differences"] == []


def test_paper_info_cli_is_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert experiment_main(["paper6", "info"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["study"]["study_id"] == "claim-plane-paper-6pair"
    assert (
        payload["published_mechanism_counts"]["claim-plane-dynamic"]["promotions"] == 7
    )


def test_paper_prepare_cli_validates_local_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _fake_dataset(tmp_path)
    cooperbench = dataset.parent

    assert (
        experiment_main(
            [
                "paper6",
                "prepare",
                "--cooperbench",
                str(cooperbench),
                "--artifacts",
                str(tmp_path / "artifacts"),
                "--repo-cache",
                str(tmp_path / "repos"),
                "--workspace",
                str(tmp_path / "worktrees"),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["compatible_tasks"] == 5
    assert payload["benchmark"]["frozen_pair_count"] == 6
    assert payload["benchmark"]["frozen_task_count"] == 5
    assert len(payload["benchmark"]["frozen_dataset_sha256"]) == 64


def test_frozen_dataset_digest_changes_only_with_frozen_inputs(tmp_path: Path) -> None:
    dataset = _fake_dataset(tmp_path)
    first = frozen_dataset_digest(dataset)

    unrelated = dataset / "unrelated_task"
    unrelated.mkdir()
    (unrelated / "note.txt").write_text("ignored", encoding="utf-8")
    assert frozen_dataset_digest(dataset) == first

    target = dataset / "pallets_jinja_task" / "task1465" / "feature1" / "feature.md"
    target.write_text("changed feature", encoding="utf-8")
    assert frozen_dataset_digest(dataset) != first


def test_benchmark_provenance_is_non_secret_and_checkout_tolerant(
    tmp_path: Path,
) -> None:
    dataset = _fake_dataset(tmp_path)
    payload = benchmark_provenance(dataset.parent)

    assert payload["cooperbench_git_commit"] is None
    assert payload["cooperbench_git_dirty"] is None
    assert payload["frozen_pair_count"] == 6
    assert len(str(payload["frozen_dataset_sha256"])) == 64


def test_paper_runner_resolves_typed_task_info(monkeypatch, tmp_path: Path) -> None:
    from experiments.cooperbench.paper_6pair import runner
    from experiments.cooperbench.paper_6pair.dataset import TaskInfo

    task_dir = tmp_path / "pallets_jinja_task" / "task1465"
    feature_a = task_dir / "feature1"
    feature_b = task_dir / "feature8"
    feature_a.mkdir(parents=True)
    feature_b.mkdir(parents=True)
    task = TaskInfo(
        repo="pallets_jinja_task",
        task_id=1465,
        directory=task_dir,
        clone_url="https://example.invalid/pallets/jinja.git",
        base_commit="abc123",
        features={1: feature_a, 8: feature_b},
    )
    monkeypatch.setattr(runner, "tasks", {(task.repo, task.task_id): task})

    resolved, resolved_a, resolved_b, base = runner._task_inputs(
        {"repo": task.repo, "tid": task.task_id, "a": 1, "b": 8}
    )

    assert resolved is task
    assert resolved.directory == task_dir
    assert resolved.clone_url == "https://example.invalid/pallets/jinja.git"
    assert resolved_a == feature_a
    assert resolved_b == feature_b
    assert base == "abc123"
