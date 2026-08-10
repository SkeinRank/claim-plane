from __future__ import annotations

import sys
import time

import pytest

from experiments.cooperbench.paper_6pair.runner import _run_agents_physically
from experiments.cooperbench.physical_parallel import (
    ActivityInterval,
    aggregate_worker_overlap,
    interval_overlap,
    parse_pair_indexes,
    run_bounded_pair_processes,
)


def test_interval_overlap_reports_real_overlap() -> None:
    left = ActivityInterval("a", 1_000_000_000, 4_000_000_000)
    right = ActivityInterval("b", 2_000_000_000, 5_000_000_000)

    metrics = interval_overlap(left, right)

    assert metrics.concurrent is True
    assert metrics.overlap_seconds == pytest.approx(2.0)
    assert metrics.union_seconds == pytest.approx(4.0)
    assert metrics.overlap_fraction_of_shorter == pytest.approx(2 / 3)


def test_aggregate_worker_overlap_reports_peak_concurrency() -> None:
    summary = aggregate_worker_overlap(
        (
            ActivityInterval("a", 1, 10),
            ActivityInterval("b", 2, 7),
            ActivityInterval("c", 8, 12),
        )
    )

    assert summary["worker_count"] == 3
    assert summary["peak_active_workers"] == 2
    assert summary["sum_worker_seconds"] > summary["wall_time_seconds"]


def test_parse_pair_indexes_supports_ranges_and_deduplicates() -> None:
    assert parse_pair_indexes("1-3,2,6", pair_count=6) == (1, 2, 3, 6)
    with pytest.raises(ValueError):
        parse_pair_indexes("0,2", pair_count=6)
    with pytest.raises(ValueError):
        parse_pair_indexes("5-3", pair_count=6)


def test_inner_physical_executor_observes_overlap() -> None:
    def worker(value: str) -> str:
        time.sleep(0.08)
        return value

    left, right, timing = _run_agents_physically(
        lambda: worker("a"),
        lambda: worker("b"),
    )

    assert left == "a"
    assert right == "b"
    assert timing["concurrent"] is True
    assert timing["overlap_seconds"] > 0.04
    assert timing["overlap_fraction_of_shorter"] > 0.5


def test_bounded_pair_process_pool_isolates_processes_and_caps_fanout() -> None:
    command = (
        sys.executable,
        "-c",
        "import json,time; time.sleep(0.12); print(json.dumps({'ok': True}))",
    )
    result = run_bounded_pair_processes(
        ((f"pair-{index}", command) for index in range(4)),
        max_parallel_pairs=2,
    )

    assert result["complete"] is True
    assert len(result["workers"]) == 4
    assert result["outer_concurrency"]["peak_active_workers"] == 2
    assert all(worker["payload"] == {"ok": True} for worker in result["workers"])
    assert (
        result["outer_concurrency"]["sum_worker_seconds"]
        > result["batch_wall_time_seconds"]
    )


def test_threadsafe_scope_planes_use_independent_sqlite_connections(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from experiments.cooperbench.paper_6pair.scope import (
        prepare_threadsafe_scope_registry,
    )

    plan_a = {
        "files": [
            {
                "path": "src/app.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 1,
                "line_end": 10,
            }
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "src/other.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 1,
                "line_end": 10,
            }
        ]
    }
    session = prepare_threadsafe_scope_registry(
        plan_a,
        plan_b,
        force_all_committed=False,
        base_commit="a" * 40,
        db_path=tmp_path / "scope.db",
    )
    from claim_plane import Plane

    def read_intent(intent_id: str):
        plane = Plane.open(session["db_path"])
        try:
            return plane.intent(intent_id)
        finally:
            plane.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(read_intent, "A")
        right = pool.submit(read_intent, "B")
        assert left.result() is not None
        assert right.result() is not None


def test_dynamic_physical_pair_uses_thread_local_plane_connections(
    monkeypatch, tmp_path
) -> None:
    from pathlib import Path

    from claim_plane import AccessMode
    from experiments.cooperbench.paper_6pair import runner
    from experiments.cooperbench.paper_6pair.scope import (
        prepare_threadsafe_scope_registry,
    )

    plan_a = {
        "files": [
            {
                "path": "src/app.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 1,
                "line_end": 10,
            }
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "src/other.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 1,
                "line_end": 10,
            }
        ]
    }
    db_path = tmp_path / "scope.db"
    prepare_threadsafe_scope_registry(
        plan_a,
        plan_b,
        force_all_committed=False,
        base_commit="a" * 40,
        db_path=db_path,
    )

    feature_a = tmp_path / "feature-a"
    feature_b = tmp_path / "feature-b"
    feature_a.mkdir()
    feature_b.mkdir()

    def fake_run_agent(_repo, _worktrees, **kwargs):
        guard = kwargs["mutation_guard"]
        if kwargs["feature_dir"] == feature_a:
            guard("src/app.py", AccessMode.WRITE, (2, 3))
            head = "a" * 40
            label = "a"
        else:
            guard("src/other.py", AccessMode.WRITE, (2, 3))
            head = "b" * 40
            label = "b"
        time.sleep(0.06)
        return Path(f"/tmp/{label}"), {
            "head": head,
            "logical_latency": 0.06,
            "feature_pass": True,
        }

    monkeypatch.setattr(runner, "_run_agent", fake_run_agent)
    monkeypatch.setattr(
        runner,
        "_merge_parallel_worktrees",
        lambda _a, _b: {
            "integration_success": True,
            "clean_merge": True,
            "final_tree": Path("/tmp/merged"),
        },
    )

    record = {
        "scope_events": [],
        "dynamic_wasted_coder_cost": 0.0,
        "dynamic_wasted_coder_latency": 0.0,
        "dynamic_wasted_steps": 0,
        "runtime_serialized": False,
        "serialized": False,
        "effective_gate_kind": "parallel",
        "dynamic_serialization_order": None,
        "dynamic_restart_count": 0,
        "physical_timing": None,
        "physical_concurrency_observed": False,
        "physical_overlap_seconds": 0.0,
        "physical_union_seconds": 0.0,
        "physical_overlap_fraction_of_shorter": 0.0,
        "physical_parallel_reason": None,
    }

    tree_a, result_a, tree_b, result_b, final_tree = runner._run_dynamic_physically(
        repo=tmp_path,
        worktrees=[],
        safe_name="case",
        base="a" * 40,
        task_dir=tmp_path,
        feature_a=feature_a,
        feature_b=feature_b,
        seed_a=1,
        seed_b=2,
        run_id="run",
        scope_db_path=db_path,
        plan_a=plan_a,
        plan_b=plan_b,
        record=record,
    )

    assert tree_a == Path("/tmp/a")
    assert tree_b == Path("/tmp/b")
    assert result_a["feature_pass"] is True
    assert result_b["feature_pass"] is True
    assert final_tree == Path("/tmp/merged")
    assert record["physical_concurrency_observed"] is True
    assert record["physical_overlap_seconds"] > 0.03
    assert record["integration_success"] is True
