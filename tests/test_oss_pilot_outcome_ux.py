from __future__ import annotations

from argparse import Namespace

from claim_plane import cli, oss_pilot, validation
from claim_plane.evidence import render_evidence_replay


def _candidate(digest: str = "a" * 64) -> dict[str, str]:
    return {"digest": digest, "base_commit": "b" * 40}


def test_passing_recheck_does_not_relabel_rejected_delivery_as_verified() -> None:
    current = _candidate()
    latest_run = {"outcome": "REJECTED", "verified": False}
    recheck = {"classification": "PASS", "candidate": _candidate()}

    assert (
        oss_pilot._current_candidate_verdict(
            current_candidate=current,
            latest_run=latest_run,
            reverification=recheck,
        )
        == "MATCHES_PASSING_ACCEPTANCE_RECHECK"
    )


def test_failing_recheck_names_acceptance_without_overwriting_delivery() -> None:
    current = _candidate()
    latest_run = {"outcome": "REJECTED", "verified": False}
    recheck = {"classification": "TEST_FAILED", "candidate": _candidate()}

    assert (
        oss_pilot._current_candidate_verdict(
            current_candidate=current,
            latest_run=latest_run,
            reverification=recheck,
        )
        == "MATCHES_FAILING_ACCEPTANCE_RECHECK"
    )


def test_stale_recheck_is_not_applied_to_current_candidate() -> None:
    current = _candidate()
    recheck = {
        "classification": "PASS",
        "candidate": _candidate(digest="c" * 64),
    }

    assert (
        oss_pilot._current_candidate_verdict(
            current_candidate=current,
            latest_run={"outcome": "REJECTED", "verified": False},
            reverification=recheck,
        )
        == "STALE_ACCEPTANCE_RECHECK"
    )


def test_status_cli_prints_delivery_before_acceptance_recheck(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli,
        "oss_pilot_status",
        lambda *args, **kwargs: {
            "task_id": "click-completion-amendment",
            "arm": "guarded",
            "workspace": "/tmp/click",
            "base_commit": "b" * 40,
            "changed": [" M src/click/shell_completion.py"],
            "latest_run": {"run_id": "cpr_example", "outcome": "REJECTED"},
            "delivery_outcome": "REJECTED",
            "current_verdict": "MATCHES_PASSING_ACCEPTANCE_RECHECK",
            "latest_acceptance": {
                "classification": "PASS",
                "candidate_matches_current": True,
                "detail": "official tests passed",
                "log_dir": ".claim-plane/oss-pilot/acceptance/attempt-1",
            },
        },
    )

    assert (
        cli.cmd_oss_pilot_status(
            Namespace(
                task="click-completion-amendment",
                arm="guarded",
                workspace_root="/tmp",
                json=False,
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Delivery: REJECTED" in output
    assert "Candidate state: MATCHES_PASSING_ACCEPTANCE_RECHECK" in output
    assert "Acceptance recheck: PASS · candidate matches current" in output
    assert "Current candidate: VERIFIED_AFTER_RECHECK" not in output


def test_replay_keeps_delivery_outcome_separate_from_recheck() -> None:
    lines = render_evidence_replay(
        {
            "run_id": "cpr_example",
            "outcome": "REJECTED",
            "entries": [],
            "reverification": {
                "created_at": "2026-08-07T04:42:13Z",
                "classification": "PASS",
                "detail": "official tests passed",
                "log_dir": ".claim-plane/oss-pilot/acceptance/attempt-1",
            },
            "current_candidate_verdict": "MATCHES_PASSING_ACCEPTANCE_RECHECK",
            "determinism": {"available": False},
            "replay_digest": "d" * 64,
        }
    )

    assert "  candidate MATCHES_PASSING_ACCEPTANCE_RECHECK" in lines
    assert "  delivery remains REJECTED" in lines


def test_guarded_validation_never_upgrades_rejected_delivery_after_pass_recheck() -> None:
    report = {
        "outcome": "REJECTED",
        "current_candidate_verdict": "MATCHES_PASSING_ACCEPTANCE_RECHECK",
    }

    assert validation._guarded_delivery_accepted(report, task_success=True) is False


def test_guarded_validation_accepts_verified_delivery_with_matching_pass_recheck() -> None:
    report = {
        "outcome": "VERIFIED",
        "current_candidate_verdict": "MATCHES_PASSING_ACCEPTANCE_RECHECK",
    }

    assert validation._guarded_delivery_accepted(report, task_success=True) is True
