"""Two-level swarm verification and durable evidence."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    create_swarm_session,
    get_swarm_session,
    get_swarm_verification,
    integrate_next_swarm_result,
    plan_swarm_concurrency,
    plan_swarm_merge_queue,
    provision_swarm_worktrees,
    run_codex_work_item,
    verify_swarm_session,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _item(
    work_id: str,
    path: str,
    *,
    depends_on: tuple[str, ...] = (),
    acceptance: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {path}.",
        "depends_on": list(depends_on),
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": path},
            }
        ],
        "acceptance": list(acceptance),
    }


def _session(
    repo: Path,
    items: list[dict[str, object]],
    *,
    root_acceptance: tuple[str, ...] = (),
    session_id: str = "swm-verify",
) -> None:
    create_swarm_session(
        repo,
        session_id=session_id,
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {
                "title": "Verify swarm",
                "goal": "Integrate and verify.",
                "acceptance": list(root_acceptance),
            },
            "integration_target": {"branch": "main"},
            "work_graph": {
                "protocol": "claim-plane.swarm-work-graph.v1",
                "work_items": items,
            },
            "budget_policy": {
                "protocol": "claim-plane.swarm-budget-policy.v1",
                "workers": {
                    "max_active": 2,
                    "max_work_items": 8,
                    "max_total_launches": 8,
                },
                "resources": {"max_wall_time_seconds": 30},
            },
        },
    )
    plan_swarm_concurrency(repo, session_id)
    provision_swarm_worktrees(repo, session_id)
    plan_swarm_merge_queue(repo, session_id)


def _fake_codex(tmp_path: Path, *, rogue: bool = False, noop: bool = False) -> Path:
    script = tmp_path / ("fake-codex-rogue" if rogue else "fake-codex")
    change_a = (
        "pass"
        if noop
        else '(root / "src" / "a.py").write_text("a = 2\\n", encoding="utf-8")'
    )
    change_b = (
        "pass"
        if noop
        else '(root / "src" / "b.py").write_text("b = 2\\n", encoding="utf-8")'
    )
    rogue_line = (
        "(root / 'rogue.txt').write_text('rogue\\n', encoding='utf-8')" if rogue else ""
    )
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
prompt = sys.argv[-1]
root = pathlib.Path.cwd()
if "Work item: a " in prompt:
    {change_a}
    {rogue_line}
elif "Work item: b " in prompt:
    {change_b}
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({{"type": "thread.started", "thread_id": "thread-test"}}), flush=True)
print(
    json.dumps(
        {{
            "type": "turn.completed",
            "usage": {{"input_tokens": 5, "output_tokens": 3}},
        }}
    ),
    flush=True,
)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run_and_merge(
    repo: Path,
    codex: Path,
    work_ids: tuple[str, ...] = ("a", "b"),
    session_id: str = "swm-verify",
) -> None:
    for work_id in work_ids:
        run_codex_work_item(repo, session_id, work_id, codex_binary=str(codex))
        integrate_next_swarm_result(repo, session_id)


def test_swarm_verification_produces_durable_two_level_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(
        repo,
        [
            _item(
                "a",
                "src/a.py",
                acceptance=(
                    "python -c \"assert open('src/a.py').read() == 'a = 2\\n'\"",
                ),
            ),
            _item("b", "src/b.py", depends_on=("a",)),
        ],
        root_acceptance=(
            "python -c \"assert open('src/a.py').read() == 'a = 2\\n' "
            "and open('src/b.py').read() == 'b = 2\\n'\"",
        ),
    )
    _run_and_merge(repo, _fake_codex(tmp_path))

    result = verify_swarm_session(repo, "swm-verify")
    stored = get_swarm_verification(repo, "swm-verify")

    assert result["summary"]["status"] == "verified"
    assert result["summary"]["work_verified"] == 2
    assert result["verification_fingerprint"] == stored["verification_fingerprint"]
    assert {item["work_id"] for item in result["verification"]["work_evidence"]} == {
        "a",
        "b",
    }
    assert get_swarm_session(repo, "swm-verify").state.value == "completed"


def test_swarm_integration_fails_closed_on_undeclared_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py")])
    codex = _fake_codex(tmp_path, rogue=True)

    run = run_codex_work_item(repo, "swm-verify", "a", codex_binary=str(codex))
    assert run.state.value == "succeeded"
    result = integrate_next_swarm_result(repo, "swm-verify")

    assert result["integrated"] is False
    assert result["summary"]["status"] == "conflict"
    evidence = result["entry"]["integration_evidence"]
    assert evidence["disposition"] == "reject"
    assert "undeclared_path" in evidence["reasons"]
    assert any(
        item["reason"] == "undeclared_path" and item["path"] == "rogue.txt"
        for item in evidence["authority_violations"]
    )
    integration = Path(result["merge_queue"]["integration_worktree_path"])
    assert not (integration / "rogue.txt").exists()


def test_acceptance_mutation_fails_closed_and_is_cleaned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(
        repo,
        [_item("a", "src/a.py")],
        root_acceptance=("python -c \"open('src/mutated.py','w').write('x')\"",),
    )
    _run_and_merge(repo, _fake_codex(tmp_path), ("a",))

    result = verify_swarm_session(repo, "swm-verify")
    integration = Path(result["verification"]["metadata"]["integration_worktree"])

    assert result["summary"]["status"] == "failed"
    assert result["summary"]["snapshot_integrity_ok"] is False
    assert "src/mutated.py" in result["verification"]["acceptance_mutation_paths"]
    assert not (integration / "src" / "mutated.py").exists()


def test_work_acceptance_mutation_is_attributed_and_cleaned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(
        repo,
        [
            _item(
                "a",
                "src/a.py",
                acceptance=(
                    "python -c \"open('src/work-mutated.py','w').write('x')\"",
                ),
            )
        ],
    )
    _run_and_merge(repo, _fake_codex(tmp_path), ("a",))

    result = verify_swarm_session(repo, "swm-verify")
    integration = Path(result["verification"]["metadata"]["integration_worktree"])
    work = result["verification"]["work_evidence"][0]

    assert result["summary"]["status"] == "failed"
    assert work["verified"] is False
    assert any(item["code"] == "snapshot_mutation" for item in work["findings"])
    assert not (integration / "src" / "work-mutated.py").exists()


def test_successful_noop_does_not_reuse_a_prior_integration_commit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py")])
    _run_and_merge(repo, _fake_codex(tmp_path, noop=True), ("a",))

    result = verify_swarm_session(repo, "swm-verify", run_acceptance=False)
    work = result["verification"]["work_evidence"][0]

    assert work["changed_paths"] == []
    assert any(item["code"] == "missing_declared_change" for item in work["findings"])
    assert result["summary"]["status"] == "failed"


def test_verification_requires_completed_merge_queue(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py")])
    with pytest.raises(ValueError, match="completed merge queue"):
        verify_swarm_session(repo, "swm-verify", run_acceptance=False)


def test_database_migrates_to_verification_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py")])
    database = repo / ".claim-plane" / "swarm.db"
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE swarm_verifications")
    connection.execute("PRAGMA user_version=7")
    connection.commit()
    connection.close()

    get_swarm_session(repo, "swm-verify")

    connection = sqlite3.connect(database)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    connection.close()
    assert version == 9
    assert "swarm_verifications" in tables
