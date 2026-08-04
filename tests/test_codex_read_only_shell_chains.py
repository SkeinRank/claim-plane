from pathlib import Path

import pytest

from claim_plane.connectors.codex_guard import classify_tool_call


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
    ),
)
def test_read_only_shell_chains_are_classified_as_read_only(
    tmp_path: Path, command: str
) -> None:
    assert _classify(tmp_path, command) == "read_only"


@pytest.mark.parametrize(
    "command",
    (
        "git status --short && touch src/extra.py",
        "git status --short; rm -rf src",
        "git diff --check | cat",
        "git status --short > status.txt",
        "git status --short || true",
        "git status --short & git diff --check",
        "git status --short &&",
        "; git status --short",
        "git status --short;;git diff --check",
        "git status --short\ngit diff --check",
        "git status --short && echo $(pwd)",
    ),
)
def test_shell_chains_fail_closed_when_any_segment_is_not_provably_read_only(
    tmp_path: Path, command: str
) -> None:
    assert _classify(tmp_path, command) == "opaque_shell"
