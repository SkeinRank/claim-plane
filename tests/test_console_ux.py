from __future__ import annotations

import io
from pathlib import Path

from claim_plane.console import ConsoleRenderer


def _result() -> dict[str, object]:
    return {
        "started_at": "2026-08-04T02:00:00Z",
        "finished_at": "2026-08-04T02:00:12Z",
        "outcome": "VERIFIED",
        "completion": {
            "protocol": "claim-plane.codex-completion.v1",
            "errors": 0,
            "executed_violations": 0,
            "findings": [],
        },
        "changes": {
            "file_count": 2,
            "total_additions": 2,
            "total_deletions": 2,
            "files": [
                {"path": "src/app.py"},
                {"path": "tests/test_app.py"},
            ],
        },
        "acceptance": {
            "passed": True,
            "commands": ["python -m pytest"],
        },
        "risk": {
            "highest_risk": "medium",
            "final_action": "ALLOW",
        },
    }


def test_console_renderer_produces_compact_human_run_summary(tmp_path: Path) -> None:
    output = io.StringIO()
    errors = io.StringIO()
    renderer = ConsoleRenderer(output, errors, colour=False)

    renderer.header(
        run_id="cpr_example",
        root=tmp_path,
        policy="guarded",
        adapter="codex",
        adapter_version="3",
        protocol_version="1.0",
        runtime_name="codex",
        runtime_version="codex-cli 0.146.0",
        model="gpt-5.6-luna",
    )
    renderer.step("Preflight ready", detail="all checks passed")
    renderer.runtime_payload(
        {"type": "thread.started", "thread_id": "thread_example"},
        elapsed_seconds=0.2,
    )
    renderer.runtime_payload({"type": "turn.started"}, elapsed_seconds=0.3)
    renderer.runtime_payload({"type": "turn.completed"}, elapsed_seconds=4.2)
    renderer.verification_started()
    renderer.finish(
        result=_result(),
        evidence_path=tmp_path / ".claim-plane/runs/cpr_example/run.json",
        root=tmp_path,
        final_message="Updated the implementation and tests.",
    )

    rendered = output.getvalue()
    assert "Claim Plane" in rendered
    assert "gpt-5.6-luna" in rendered
    assert "✓ Preflight ready" in rendered
    assert "● Codex working" in rendered
    assert "✓ Scope verified" in rendered
    assert "✓ Acceptance passed" in rendered
    assert "DELIVERY VERIFIED" in rendered
    assert "2 files changed · +2 -2 · 1 acceptance check · 12s" in rendered
    assert "Agent summary (not verification evidence)" in rendered
    assert "\x1b[" not in rendered
    assert errors.getvalue() == ""


def test_console_renderer_compacts_blocked_hook_diagnostics() -> None:
    output = io.StringIO()
    errors = io.StringIO()
    renderer = ConsoleRenderer(output, errors, colour=False)
    line = (
        "2026-08-04T02:07:04Z ERROR router: "
        "Command blocked by PreToolUse hook: cannot prove safety. "
        "Command: git status && touch file.py\n"
    )

    renderer.runtime_stderr(line)
    renderer.runtime_stderr(line)

    rendered = output.getvalue()
    assert rendered.count("Command blocked by policy") == 1
    assert "git status && touch file.py" in rendered
    assert "2026-08-04" not in rendered
    assert errors.getvalue() == ""


def test_console_renderer_verbose_mode_preserves_raw_runtime_diagnostics() -> None:
    output = io.StringIO()
    errors = io.StringIO()
    renderer = ConsoleRenderer(output, errors, verbose=True, colour=False)
    line = "2026-08-04T02:07:04Z ERROR raw runtime diagnostic\n"

    renderer.runtime_stderr(line)

    assert output.getvalue() == ""
    assert errors.getvalue() == line


def test_console_renderer_does_not_claim_scope_pass_without_verification(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    renderer = ConsoleRenderer(output, io.StringIO(), colour=False)
    result = _result()
    result["outcome"] = "FAILED"
    result["completion"] = {
        "protocol": None,
        "errors": 0,
        "executed_violations": 0,
        "findings": [],
    }
    result["acceptance"] = {"passed": False, "commands": ["python -m pytest"]}
    result["error"] = {"code": "controlled_run_failed"}

    renderer.finish(
        result=result,
        evidence_path=tmp_path / ".claim-plane/runs/cpr_failed/run.json",
        root=tmp_path,
        final_message=None,
    )

    rendered = output.getvalue()
    assert "Scope not verified" in rendered
    assert "Scope verified" not in rendered
    assert "Run requires attention" in rendered
    assert "DELIVERY FAILED" in rendered


def test_console_renderer_names_blocked_write_and_initial_scope() -> None:
    output = io.StringIO()
    renderer = ConsoleRenderer(output, io.StringIO(), colour=False)
    line = (
        "2026-08-04T04:23:55Z ERROR router: "
        "Command blocked by PreToolUse hook: Claim Plane blocked write to "
        "tests/test_app.py. Outside initial scope: src/app.py. Mutation write "
        "tests/test_app.py is outside the admitted ChangeIntent. "
        "Command: *** Begin Patch\n"
    )

    renderer.runtime_stderr(line)

    rendered = output.getvalue()
    assert "! Write blocked" in rendered
    assert "tests/test_app.py" in rendered
    assert "Outside initial scope: src/app.py." in rendered
    assert "*** Begin Patch" not in rendered


def test_console_renderer_marks_cancelled_verification_as_skipped(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    renderer = ConsoleRenderer(output, io.StringIO(), colour=False)
    result = _result()
    result["outcome"] = "CANCELLED"
    result["completion"] = {
        "protocol": None,
        "errors": 0,
        "executed_violations": 0,
        "findings": [],
    }
    result["changes"] = {
        "file_count": 0,
        "total_additions": 0,
        "total_deletions": 0,
        "files": [],
    }
    result["acceptance"] = {"passed": False, "commands": ["python -m pytest"]}
    result["cancellation"] = {
        "status": "cancelled",
        "intent_id": "intent-example",
        "intent_version": 0,
    }
    result["error"] = {"code": "cancelled"}

    renderer.finish(
        result=result,
        evidence_path=tmp_path / ".claim-plane/runs/cpr_cancelled/run.json",
        root=tmp_path,
        final_message=None,
    )

    rendered = output.getvalue()
    assert "Scope verification skipped" in rendered
    assert "Acceptance skipped" in rendered
    assert "Authority revoked" in rendered
    assert "Scope not verified" not in rendered
    assert "Acceptance not verified" not in rendered
    assert "Run requires attention" not in rendered
    assert "DELIVERY CANCELLED" in rendered


def test_console_renderer_reports_unsatisfied_task_obligation(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    renderer = ConsoleRenderer(output, io.StringIO(), colour=False)
    result = _result()
    result["outcome"] = "REJECTED"
    result["completion"] = {
        "protocol": "claim-plane.codex-completion.v1",
        "errors": 1,
        "executed_violations": 0,
        "findings": [
            {
                "code": "task_obligation_unsatisfied",
                "severity": "error",
            }
        ],
        "task_obligations": {
            "protocol": "claim-plane.task-obligations.v1",
            "required": [{"id": "test_change"}],
            "satisfied": [],
            "unsatisfied": ["test_change"],
            "all_satisfied": False,
        },
    }
    result["error"] = {"code": "task_obligation_unsatisfied"}

    renderer.finish(
        result=result,
        evidence_path=tmp_path / ".claim-plane/runs/cpr_rejected/run.json",
        root=tmp_path,
        final_message=None,
    )

    rendered = output.getvalue()
    assert "Scope verified" in rendered
    assert "Acceptance passed" in rendered
    assert "Task incomplete" in rendered
    assert "test_change" in rendered
    assert "DELIVERY REJECTED" in rendered
