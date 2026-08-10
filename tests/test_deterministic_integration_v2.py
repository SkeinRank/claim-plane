from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    create_swarm_session,
    integrate_next_swarm_result,
    plan_swarm_concurrency,
    plan_swarm_merge_queue,
    provision_swarm_worktrees,
    run_codex_work_item,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "app.py").write_text(
        dedent(
            """
            def first(value: str) -> str:
                return value.strip()




            def second(value: str) -> str:
                return value.upper()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _symbol_item(work_id: str, qualified: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {qualified}.",
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": "app.py"},
            },
            {
                "access": "write",
                "resource": {
                    "kind": "symbol",
                    "identifier": qualified,
                    "metadata": {
                        "path": "app.py",
                        "language": "python",
                        "qualified_identifier": qualified,
                    },
                },
            },
        ],
    }


def _session(repo: Path, items: list[dict[str, object]], session_id: str) -> None:
    create_swarm_session(
        repo,
        session_id=session_id,
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Integration", "goal": "Integrate workers."},
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
                "concurrency": {
                    "same_file": "region_safe",
                    "unknown_overlap": "serialize",
                    "shared_contract": "serialize",
                    "schema_change": "serialize",
                },
            },
        },
    )
    plan_swarm_concurrency(repo, session_id)
    provision_swarm_worktrees(repo, session_id)
    plan_swarm_merge_queue(repo, session_id)


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex-integration-v2"
    script.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
prompt = sys.argv[-1]
root = pathlib.Path.cwd()
path = root / "app.py"
text = path.read_text(encoding="utf-8")
if "Work item: first " in prompt:
    text = text.replace("return value.strip()", "return value.strip() + ' first'")
elif "Work item: second " in prompt:
    text = text.replace("return value.upper()", "return value.upper() + ' second'")
elif "Work item: wrong " in prompt:
    text = text.replace("return value.upper()", "return value.lower()")
path.write_text(text, encoding="utf-8")
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thread-test"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_actual_symbol_scope_is_enforced_before_integration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_symbol_item("wrong", "first")], "swm-int-v2-wrong")
    codex = _fake_codex(tmp_path)

    run = run_codex_work_item(
        repo, "swm-int-v2-wrong", "wrong", codex_binary=str(codex)
    )
    assert run.state.value == "succeeded"
    result = integrate_next_swarm_result(repo, "swm-int-v2-wrong")

    assert result["integrated"] is False
    evidence = result["entry"]["integration_evidence"]
    assert evidence["protocol"] == "claim-plane.deterministic-integration.v2"
    assert evidence["disposition"] == "reject"
    assert "semantic_scope_violation" in evidence["reasons"]
    assert any(
        item.get("semantic_identity") == "symbol:app.py#second"
        for item in evidence["authority_violations"]
    )


def test_same_file_independent_actual_mutations_survive_semantic_recheck(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(
        repo,
        [_symbol_item("first", "first"), _symbol_item("second", "second")],
        "swm-int-v2-parallel",
    )
    codex = _fake_codex(tmp_path)

    assert (
        run_codex_work_item(
            repo, "swm-int-v2-parallel", "first", codex_binary=str(codex)
        ).state.value
        == "succeeded"
    )
    assert (
        run_codex_work_item(
            repo, "swm-int-v2-parallel", "second", codex_binary=str(codex)
        ).state.value
        == "succeeded"
    )

    first = integrate_next_swarm_result(repo, "swm-int-v2-parallel")
    second = integrate_next_swarm_result(repo, "swm-int-v2-parallel")

    assert first["integrated"] is True
    assert second["integrated"] is True
    evidence = second["entry"]["integration_evidence"]
    assert evidence["disposition"] == "apply"
    assert evidence["staged_semantic_roots"] == ["symbol:app.py#second"]
    assert evidence["semantic_checks"][0]["prior_work_id"] == "first"
    assert evidence["semantic_checks"][0]["kind"] == "independent"
    assert evidence["semantic_checks"][0]["allowed"] is True
    integration = Path(second["merge_queue"]["integration_worktree_path"])
    text = (integration / "app.py").read_text(encoding="utf-8")
    assert "return value.strip() + ' first'" in text
    assert "return value.upper() + ' second'" in text
