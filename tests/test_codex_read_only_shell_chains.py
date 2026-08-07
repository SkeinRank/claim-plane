from pathlib import Path

import pytest

from claim_plane.connectors.codex_guard import (
    classify_tool_call,
    evaluate_pre_tool_use,
)


def _classify(root: Path, command: str) -> str:
    classification, mutations = classify_tool_call(
        root,
        {
            "tool_name": "exec_command",
            "tool_input": {"command": command},
        },
    )
    assert mutations == ()
    return classification


@pytest.mark.parametrize(
    "command",
    (
        "git diff --check; git diff -- src/app.py tests/test_app.py",
        "git status --short && command -v pytest",
        "pwd; git status --short && claim-plane --version",
        "git diff -- src/app.py tests/test_app.py; printf '%s\\n' '--- status ---'; git status --short",
        "git diff --check | cat",
        "git show --stat --oneline HEAD | head -20",
        "rg -n 'ChangeIntent' src tests | head -40",
        "git log --all --oneline -S 'sub_sub_ctx' -- src/click/shell_completion.py | tail -5",
        "git show HEAD:README.md | sed -n '1,40p'; git status --short",
        "git --no-pager show --stat HEAD | head -20",
        "git -C . status --short | wc -l",
    ),
)
def test_read_only_shell_chains_and_pipelines_are_classified_as_read_only(
    tmp_path: Path, command: str
) -> None:
    assert _classify(tmp_path, command) == "read_only"


@pytest.mark.parametrize(
    "command",
    (
        "git status --short && touch src/extra.py",
        "git status --short; rm -rf src",
        "git diff --check | tee diff.txt",
        "git status --short | xargs touch",
        "git status --short > status.txt",
        "git status --short || true",
        "git status --short & git diff --check",
        "git status --short &&",
        "; git status --short",
        "git status --short;;git diff --check",
        "git status --short\ngit diff --check",
        "git status --short && echo $(pwd)",
        "find . -type f -fprint paths.txt",
        "sed -i.bak 's/a/b/' README.md",
        "git reset --hard HEAD | cat",
    ),
)
def test_shell_compositions_fail_closed_when_any_stage_is_not_provably_read_only(
    tmp_path: Path, command: str
) -> None:
    assert _classify(tmp_path, command) == "opaque_shell"


def test_opaque_shell_denial_names_the_exact_unsupported_pipeline_stage(
    tmp_path: Path,
) -> None:
    evaluation = evaluate_pre_tool_use(
        root=tmp_path,
        payload={
            "tool_name": "exec_command",
            "tool_input": {"command": "git diff --check | tee diff.txt"},
        },
        intent=None,
        intent_is_active=False,
        base_commit_matches=True,
    )

    assert evaluation.allowed is False
    assert evaluation.reason_code == "opaque_shell"
    assert evaluation.diagnostic_code == "unsupported_shell_executable"
    assert evaluation.diagnostic_segment == "tee diff.txt"
    assert evaluation.diagnostic_segment_index == 2
    assert "segment 2 `tee diff.txt`" in evaluation.reason
    assert "executable 'tee'" in evaluation.reason


@pytest.mark.parametrize(
    "command",
    (
        "git diff --check; python -m pytest tests/test_loader.py",
        "git status --short && /opt/pyenv/versions/3.10.14/bin/python -m pytest -q tests/test_other.py",
        "git diff -- src/app.py; pytest -q tests/test_app.py",
    ),
)
def test_bounded_inspection_chains_can_include_targeted_test_feedback(
    tmp_path: Path, command: str
) -> None:
    assert _classify(tmp_path, command) == "test_feedback"


@pytest.mark.parametrize(
    "command",
    (
        "git diff --check; python -c 'open(\"out.txt\", \"w\").write(\"x\")'",
        "pytest -q tests/test_app.py | tee pytest.log",
        "git status --short && pytest",
    ),
)
def test_test_feedback_chains_remain_fail_closed_for_unsafe_or_full_suite_segments(
    tmp_path: Path, command: str
) -> None:
    assert _classify(tmp_path, command) == "opaque_shell"
