from __future__ import annotations

import io

from experiments.cooperbench.common.progress import (
    ProgressUnit,
    ResearchProgress,
    format_duration,
    progress_bar,
)


def test_progress_bar_and_duration_are_stable() -> None:
    assert progress_bar(0, 24) == "[░░░░░░░░░░░░░░░░░░░░]"
    assert progress_bar(12, 24) == "[██████████░░░░░░░░░░]"
    assert progress_bar(24, 24) == "[████████████████████]"
    assert format_duration(3661) == "01:01:01"


def test_progress_reports_zero_percent_completion_and_eta() -> None:
    stream = io.StringIO()
    units = (
        ProgressUnit("pair-a/parallel", "pair-a · parallel", "parallel"),
        ProgressUnit("pair-a/always-serial", "pair-a · always-serial", "always-serial"),
    )
    progress = ResearchProgress("paper reproduction", units, stream=stream)

    progress.start()
    progress.phase(1, 2, "execute")
    progress.start_unit("pair-a/parallel")
    progress.complete_unit(
        "pair-a/parallel", duration_seconds=10.0, result="PASS", cost=0.125
    )
    progress.start_unit("pair-a/always-serial")
    progress.complete_unit(
        "pair-a/always-serial", duration_seconds=20.0, result="FAIL", cost=0.250
    )
    progress.finish(detail="done")

    output = stream.getvalue()
    assert "[ 0/2]" in output
    assert "0.0%" in output
    assert "ETA calculating" in output
    assert "[ 1/2]" in output
    assert "50.0%" in output
    assert "result PASS" in output
    assert "cost $0.1250" in output
    assert "[ 2/2]" in output
    assert "100.0%" in output
    assert "[complete] 2/2" in output


def test_progress_resume_uses_historical_arm_durations_for_eta() -> None:
    stream = io.StringIO()
    units = (
        ProgressUnit("p1/parallel", "p1 · parallel", "parallel"),
        ProgressUnit("p1/serial", "p1 · serial", "serial"),
        ProgressUnit("p2/parallel", "p2 · parallel", "parallel"),
        ProgressUnit("p2/serial", "p2 · serial", "serial"),
    )
    progress = ResearchProgress(
        "resume",
        units,
        completed_units=("p1/parallel", "p1/serial"),
        historical_durations={"p1/parallel": 10.0, "p1/serial": 30.0},
        stream=stream,
    )

    progress.start()
    progress.start_unit("p2/parallel")

    output = stream.getvalue()
    assert "resume 2/4" in output
    assert "50.0%" in output
    assert "ETA ~00:00:40" in output
