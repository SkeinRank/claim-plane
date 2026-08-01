"""Command line interface for Claim Plane."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from claim_plane import __version__
from claim_plane.connectors import (
    admit_codex_intent,
    abandon_codex_intent,
    amend_codex_scope,
    codex_intent_status,
    verify_codex_completion,
    connect_codex,
    disconnect_codex,
    doctor_codex,
    handle_codex_hook,
    init_project,
)
from claim_plane.core import (
    AccessMode,
    ChangeIntent,
    ChangeManifest,
    Claim,
    ClaimType,
    Plane,
    ResourceKind,
)
from claim_plane.core.extract import artifacts_to_claims
from claim_plane.integration import (
    IntegrationRunSpec,
    SandboxPolicy,
    append_observation,
    verify_evidence_file,
)
from claim_plane.runtime import (
    BrokerClient,
    BrokerPolicy,
    BrokerServer,
    build_broker_boundary_command,
)
from claim_plane.swarm import (
    cancel_codex_run,
    cleanup_swarm_worktrees,
    create_swarm_session,
    get_codex_run,
    get_swarm_concurrency_plan,
    get_swarm_session,
    inspect_swarm_worktrees,
    list_codex_runs,
    list_swarm_sessions,
    plan_swarm_concurrency,
    provision_swarm_worktrees,
    replace_swarm_budget_policy,
    replace_swarm_work_graph,
    run_codex_work_item,
    validate_budget_policy,
    validate_concurrency_plan,
    validate_work_graph,
)

DEFAULT_DB = ".claim-plane/plane.db"


def _plane(args: argparse.Namespace) -> Plane:
    db = args.db or DEFAULT_DB
    if db != ":memory:":
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    return Plane.open(
        db,
        semantic=args.semantic,
        lexicon_path=args.lexicon,
        governance="exploratory" if getattr(args, "exploratory", False) else "governed",
    )


def _read_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(payload: Any, out: str | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)


def cmd_init(args: argparse.Namespace) -> int:
    result = init_project(args.repo)
    if args.json:
        _write_json(result)
    else:
        print(f"Initialized Claim Plane for {result['root']}.")
        print("Local state is excluded through the repository Git exclude file.")
    return 0


def cmd_connect_codex(args: argparse.Namespace) -> int:
    result = connect_codex(args.repo)
    if args.json:
        _write_json(result)
    else:
        print(f"Connected Codex to Claim Plane for {result['root']}.")
        print(f"Lifecycle hooks: {', '.join(result['events'])}")
        print(
            "Open /hooks in Codex once to review and trust the project-local command hooks."
        )
        if result["inline_hooks_present"]:
            print(
                "Note: .codex/config.toml also defines inline hooks; "
                "Codex merges both project-local hook sources."
            )
    return 0


def cmd_disconnect_codex(args: argparse.Namespace) -> int:
    result = disconnect_codex(args.repo)
    if args.json:
        _write_json(result)
    else:
        print(f"Disconnected Codex from Claim Plane for {result['root']}.")
        print(f"Removed {result['removed_handlers']} Claim Plane hook handler(s).")
    return 0


def cmd_doctor_codex(args: argparse.Namespace) -> int:
    report = doctor_codex(args.repo)
    if args.json:
        _write_json(report.to_dict())
    else:
        print(f"Claim Plane Codex enrollment — {report.root}")
        for item in report.checks:
            status = str(item["status"]).upper()
            detail = str(item.get("detail") or "")
            print(f"[{status:7}] {item['name']}: {detail}")
            missing = item.get("missing_events")
            if missing:
                print(f"          missing events: {', '.join(missing)}")
        if report.codex_version:
            print(f"Codex: {report.codex_version}")
        print("Status: ready" if report.ready else "Status: action required")
    return 0 if report.ready else 2


def cmd_codex_hook(args: argparse.Namespace) -> int:
    del args
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Codex hook input must be a JSON object")
    return handle_codex_hook(payload, output=sys.stdout)


def _read_stdin_json_object() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("expected a JSON object on stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("stdin must contain a JSON object")
    return payload


def cmd_codex_intent_admit(args: argparse.Namespace) -> int:
    if args.proposal_json:
        proposal = json.loads(args.proposal_json)
        if not isinstance(proposal, dict):
            raise ValueError("--proposal-json must contain a JSON object")
    elif args.proposal:
        proposal = _read_json(args.proposal)
    else:
        proposal = _read_stdin_json_object()
    result = admit_codex_intent(
        args.repo,
        session_id=args.session_id,
        proposal=proposal,
    )
    _write_json(result)
    return 0 if result["allowed"] else 2


def cmd_codex_intent_abandon(args: argparse.Namespace) -> int:
    result = abandon_codex_intent(args.repo, session_id=args.session_id)
    _write_json(result)
    return 0


def cmd_codex_intent_amend(args: argparse.Namespace) -> int:
    result = amend_codex_scope(
        args.repo,
        session_id=args.session_id,
        ticket_id=args.ticket,
        reason=args.reason,
    )
    _write_json(result)
    return 0 if result["allowed"] else 2


def cmd_codex_intent_verify(args: argparse.Namespace) -> int:
    result = verify_codex_completion(
        args.repo,
        session_id=args.session_id,
        acceptance_timeout=args.acceptance_timeout,
    )
    _write_json(result)
    return 0 if result.get("verified") else 2


def cmd_codex_intent_status(args: argparse.Namespace) -> int:
    result = codex_intent_status(args.repo, session_id=args.session_id)
    if args.json:
        _write_json(result)
    else:
        print(f"Codex session: {result['session_id']}")
        print(f"Task: {result.get('task_id') or 'not bootstrapped'}")
        print(f"Intent: {result.get('intent_id') or 'not admitted'}")
        print(f"State: {result.get('state') or 'unknown'}")
        if result.get("base_commit"):
            print(f"Base commit: {result['base_commit']}")
        if result.get("goal"):
            print(f"Goal: {result['goal']}")
        guard = result.get("guard") or {}
        print(
            "Guard: "
            f"{guard.get('authorized_calls', 0)} authorized, "
            f"{guard.get('denied_calls', 0)} denied, "
            f"{guard.get('promotions', 0)} promotions"
        )
        amendment = result.get("scope_amendment") or {}
        print(
            "Scope amendments: "
            f"{amendment.get('admitted', 0)} admitted, "
            f"{amendment.get('denied', 0)} denied"
        )
        completion = result.get("completion") or {}
        if completion:
            label = "VERIFIED" if completion.get("verified") else "UNVERIFIED"
            print(
                "Completion: "
                f"{label}; {completion.get('changed_files', 0)} files changed; "
                f"{completion.get('authorized_mutation_calls', 0)} mutation calls authorized"
            )
        pending = amendment.get("pending") or {}
        if pending.get("ticket_id"):
            paths = [
                item.get("path")
                for item in pending.get("mutations", [])
                if isinstance(item, dict)
            ]
            joined = ", ".join(str(path) for path in paths if path)
            print(f"Pending amendment ticket: {pending['ticket_id']} ({joined})")
    return 0



def cmd_swarm_create(args: argparse.Namespace) -> int:
    result = create_swarm_session(
        args.repo,
        spec=_read_json(args.spec),
        session_id=args.session_id,
        base_revision=args.base,
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        session = result["session"]
        graph = result["graph"]
        budget = result["budget"]
        action = "Created" if result["created"] else "Found existing"
        print(f"{action} swarm session {session['session_id']}.")
        print(f"Base: {session['base_commit']} ({session['base_branch']})")
        print(
            f"Work graph v{session['graph_version']}: "
            f"{graph['work_items']} items, {graph['dependency_edges']} dependencies"
        )
        print(
            f"Budget v{session['budget_version']}: "
            f"max_active={budget['max_active_workers']}, "
            f"launches={budget['max_total_launches']}, "
            f"cost_usd={budget['max_cost_usd']}"
        )
        print("Dependency layers:")
        for index, layer in enumerate(graph["dependency_layers"], start=1):
            print(f"  {index}: {', '.join(layer)}")
    return 0


def cmd_swarm_list(args: argparse.Namespace) -> int:
    sessions = list_swarm_sessions(args.repo)
    payload = [session.to_dict() for session in sessions]
    if args.json:
        _write_json(payload)
    elif not sessions:
        print("No swarm sessions.")
    else:
        for session in sessions:
            print(
                f"{session.session_id}  {session.state.value:10}  "
                f"graph=v{session.graph_version}  "
                f"budget=v{session.budget_version}  "
                f"items={len(session.work_graph.work_items)}  "
                f"base={session.base_commit[:12]}"
            )
    return 0


def cmd_swarm_status(args: argparse.Namespace) -> int:
    session = get_swarm_session(args.repo, args.session_id)
    payload = session.to_dict()
    payload["graph_summary"] = session.work_graph.summary()
    payload["budget_summary"] = session.budget_policy.summary(
        work_items=len(session.work_graph.work_items)
    )
    if args.json:
        _write_json(payload)
    else:
        print(f"Swarm session: {session.session_id}")
        print(f"State: {session.state.value}")
        print(f"Root task: {session.root_task.title}")
        print(f"Goal: {session.root_task.goal}")
        print(f"Base: {session.base_commit} ({session.base_branch})")
        print(f"Integration target: {session.integration_target.branch}")
        print(
            f"Work graph: v{session.graph_version}; "
            f"{len(session.work_graph.work_items)} items; "
            f"fingerprint={session.graph_fingerprint[:20]}"
        )
        print(
            "Topological order: "
            + " -> ".join(session.work_graph.topological_order())
        )
        print(
            f"Budget: v{session.budget_version}; "
            f"max_active={session.budget_policy.workers.max_active}; "
            f"max_launches={session.budget_policy.workers.max_total_launches}; "
            f"fingerprint={session.budget_fingerprint[:20]}"
        )
    return 0


def cmd_swarm_graph(args: argparse.Namespace) -> int:
    session = get_swarm_session(args.repo, args.session_id)
    payload = {
        "session_id": session.session_id,
        "graph_version": session.graph_version,
        "graph_fingerprint": session.graph_fingerprint,
        "work_graph": session.work_graph.to_dict(),
        "summary": session.work_graph.summary(),
    }
    _write_json(payload, args.out)
    return 0


def cmd_swarm_replace_graph(args: argparse.Namespace) -> int:
    session = replace_swarm_work_graph(
        args.repo,
        args.session_id,
        graph_data=_read_json(args.graph),
        expected_version=args.expected_version,
    )
    payload = {
        "session": session.to_dict(),
        "graph": session.work_graph.summary(),
    }
    _write_json(payload, args.out)
    return 0


def cmd_swarm_validate(args: argparse.Namespace) -> int:
    payload = _read_json(args.graph)
    result = {"valid": True, **validate_work_graph(payload)}
    _write_json(result)
    return 0




def cmd_swarm_plan(args: argparse.Namespace) -> int:
    result = plan_swarm_concurrency(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        action = "Stored" if result["created"] else "Reused"
        print(
            f"{action} concurrency plan v{result['plan_version']} for "
            f"{result['session_id']}."
        )
        print(
            f"Status: {summary['status']}; waves={summary['wave_count']}; "
            f"peak={summary['peak_concurrency']}/"
            f"{summary['max_active_workers']}"
        )
        for index, wave in enumerate(summary["waves"], start=1):
            print(f"  wave {index}: {', '.join(wave)}")
        if summary["serialized_pairs"]:
            print(f"Serialized pairs: {summary['serialized_pairs']}")
        if summary["denied_pairs"]:
            print(f"Denied pairs: {summary['denied_pairs']}")
    return 0 if result["summary"]["status"] == "ready" else 2


def cmd_swarm_concurrency(args: argparse.Namespace) -> int:
    result = get_swarm_concurrency_plan(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        print(
            f"Concurrency plan v{result['plan_version']}: "
            f"{summary['status']}"
        )
        print(
            f"Waves: {summary['wave_count']}; "
            f"peak={summary['peak_concurrency']}/"
            f"{summary['max_active_workers']}"
        )
        for index, wave in enumerate(summary["waves"], start=1):
            print(f"  wave {index}: {', '.join(wave)}")
    return 0 if result["summary"]["status"] == "ready" else 2


def cmd_swarm_validate_concurrency(args: argparse.Namespace) -> int:
    graph = _read_json(args.graph)
    policy = _read_json(args.policy) if args.policy else None
    result = validate_concurrency_plan(graph, policy)
    _write_json(result)
    return 0 if result["summary"]["status"] == "ready" else 2


def cmd_swarm_budget(args: argparse.Namespace) -> int:
    session = get_swarm_session(args.repo, args.session_id)
    payload = {
        "session_id": session.session_id,
        "budget_version": session.budget_version,
        "budget_fingerprint": session.budget_fingerprint,
        "budget_policy": session.budget_policy.to_dict(),
        "summary": session.budget_policy.summary(
            work_items=len(session.work_graph.work_items)
        ),
    }
    _write_json(payload, args.out)
    return 0


def cmd_swarm_replace_budget(args: argparse.Namespace) -> int:
    session = replace_swarm_budget_policy(
        args.repo,
        args.session_id,
        policy_data=_read_json(args.policy),
        expected_version=args.expected_version,
    )
    payload = {
        "session": session.to_dict(),
        "budget": session.budget_policy.summary(
            work_items=len(session.work_graph.work_items)
        ),
    }
    _write_json(payload, args.out)
    return 0


def cmd_swarm_validate_budget(args: argparse.Namespace) -> int:
    result = validate_budget_policy(
        _read_json(args.policy), work_items=args.work_items
    )
    _write_json({"valid": True, **result})
    return 0


def cmd_swarm_provision_worktrees(args: argparse.Namespace) -> int:
    result = provision_swarm_worktrees(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(
            f"Provisioned worktrees for {result['session_id']}: "
            f"created={result['created']}, reused={result['reused']}."
        )
        for record in result["records"]:
            print(
                f"  {record['work_id']}: {record['branch']} -> "
                f"{record['worktree_path']}"
            )
    return 0


def cmd_swarm_worktrees(args: argparse.Namespace) -> int:
    result = inspect_swarm_worktrees(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(f"Managed worktrees for {result['session_id']}:")
        if not result["worktrees"]:
            print("  none")
        for item in result["worktrees"]:
            record = item["record"]
            print(
                f"  {record['work_id']}: {item['health']}  "
                f"{record['worktree_path']}"
            )
            if item["detail"]:
                print(f"    {item['detail']}")
        if result["orphans"]:
            print("Unowned worktrees detected inside the managed session directory:")
            for item in result["orphans"]:
                print(f"  {item['worktree_path']}")
    unhealthy = sum(
        count
        for health, count in result["summary"]["health"].items()
        if health not in {"ready", "dirty"}
    )
    return 0 if unhealthy == 0 and not result["orphans"] else 2


def cmd_swarm_cleanup_worktrees(args: argparse.Namespace) -> int:
    result = cleanup_swarm_worktrees(
        args.repo,
        args.session_id,
        work_ids=tuple(args.work_id or ()),
        force=args.force,
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(
            f"Removed {result['removed']} managed worktree(s) from "
            f"{result['session_id']}."
        )
        if result["work_ids"]:
            print("Work items: " + ", ".join(result["work_ids"]))
    return 0


def cmd_swarm_run_codex(args: argparse.Namespace) -> int:
    record = run_codex_work_item(
        args.repo,
        args.session_id,
        args.work_id,
        codex_binary=args.codex_bin,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout,
        token_limit=args.max_tokens,
    )
    payload = record.to_dict()
    if args.json or args.out:
        _write_json(payload, args.out)
    else:
        print(
            f"Codex run {record.run_id}: {record.state.value} "
            f"for {record.session_id}/{record.work_id}."
        )
        print(f"Attempt: {record.attempt}; exit={record.exit_code}")
        print(
            f"Usage: {record.usage.total_tokens} tokens / "
            f"{record.budget.token_limit or 'unbounded'}; "
            f"duration={record.duration_seconds or 0:.2f}s / "
            f"{record.budget.wall_time_limit_seconds}s"
        )
        print(f"Events: {record.events_path}")
        print(f"Stderr: {record.stderr_path}")
        print(f"Final message: {record.final_message_path}")
        if record.termination_reason:
            print(f"Termination: {record.termination_reason}")
        print(
            "Execution succeeded; verification is a separate swarm lifecycle stage."
            if record.state.value == "succeeded"
            else "Execution did not complete successfully."
        )
    return 0 if record.state.value == "succeeded" else 2


def cmd_swarm_runs(args: argparse.Namespace) -> int:
    records = list_codex_runs(args.repo, args.session_id, work_id=args.work_id)
    payload = [record.to_dict() for record in records]
    if args.json or args.out:
        _write_json(payload, args.out)
    elif not records:
        print("No Codex runs.")
    else:
        for record in records:
            print(
                f"{record.run_id}  {record.work_id:20}  "
                f"attempt={record.attempt}  {record.state.value:24}  "
                f"tokens={record.usage.total_tokens}"
            )
    return 0


def cmd_swarm_run_status(args: argparse.Namespace) -> int:
    record = get_codex_run(args.repo, args.run_id)
    if args.json or args.out:
        _write_json(record.to_dict(), args.out)
    else:
        print(f"Codex run: {record.run_id}")
        print(f"Session/work: {record.session_id}/{record.work_id}")
        print(f"State: {record.state.value}")
        print(f"Attempt: {record.attempt}")
        print(f"PID: {record.agent_pid or 'not running'}")
        print(f"Thread: {record.codex_thread_id or 'not observed'}")
        print(f"Intent: {record.intent_id or 'not observed'}")
        print(f"Tokens: {record.usage.total_tokens}")
        print(f"Duration: {record.duration_seconds or 0:.2f}s")
    return (
        0
        if record.state.value == "succeeded"
        else (2 if record.state.terminal else 0)
    )


def cmd_swarm_cancel_codex(args: argparse.Namespace) -> int:
    record = cancel_codex_run(args.repo, args.run_id)
    if args.json or args.out:
        _write_json(record.to_dict(), args.out)
    else:
        print(f"Codex run {record.run_id}: {record.state.value}.")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        verdict = plane.claim(
            Claim(
                ClaimType(args.type),
                args.identifier,
                owner=args.owner,
                signature=args.signature,
                task_id=args.task,
                lease_seconds=args.lease_seconds,
            )
        )
        _write_json(verdict.to_dict())
        return 0 if verdict.granted else 2
    finally:
        plane.close()


def cmd_release(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        count = plane.release(args.owner)
        print(f"Released {count} grant(s) held by {args.owner}.")
        return 0
    finally:
        plane.close()


def cmd_status(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        grants = plane.grants()
        if args.json:
            _write_json(grants)
        elif not grants:
            print("No active grants.")
        else:
            for grant in grants:
                sig = f" sig={grant['signature']}" if grant.get("signature") else ""
                print(
                    f"[{grant['claim_type']}] {grant['identifier']} -> {grant['owner']}"
                    f"{sig} lease={grant['lease_expires_at']}"
                )
        return 0
    finally:
        plane.close()


def cmd_verify_merge(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        text = Path(args.file).read_text(encoding="utf-8")
        defined = artifacts_to_claims(text, owner=args.owner, task_id=args.task)
        problems = plane.verify_merge(defined)
        if not problems:
            print(f"OK: {args.file} — no active claim collision.")
            return 0
        _write_json(
            {"clean": False, "collisions": [item.to_dict() for item in problems]}
        )
        return 2
    finally:
        plane.close()


def cmd_pin_intent(args: argparse.Namespace) -> int:
    payload = _read_json(args.intent)
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{payload['base_revision']}^{{commit}}"],
        cwd=Path(args.repo).resolve(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "could not resolve base_revision")
    payload["base_commit"] = completed.stdout.strip().lower()
    _write_json(payload, args.out)
    return 0


def cmd_record_access(args: argparse.Namespace) -> int:
    item = append_observation(
        args.trace,
        mode=AccessMode(args.mode),
        kind=ResourceKind(args.kind),
        identifier=args.identifier,
        tool=args.tool,
    )
    _write_json(item.to_dict())
    return 0


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"environment variable {name!r} is not set")
    return value


def cmd_broker_serve(args: argparse.Namespace) -> int:
    token = _required_env(args.token_env)
    observation_key = _required_env(args.key_env).encode("utf-8")
    broker_key = _required_env(args.broker_key_env).encode("utf-8")
    commands: dict[str, Any] = {}
    if args.commands:
        commands = _read_json(args.commands)
    policy = BrokerPolicy(
        root=args.root,
        intent_id=args.intent_id,
        session_id=args.session_id,
        socket_path=args.socket,
        token=token,
        observation_key=observation_key,
        broker_key=broker_key,
        db_path=args.db or DEFAULT_DB,
        monitor_id=args.monitor_id,
        key_id=args.key_id,
        instance_id=args.instance_id or f"broker-{secrets.token_hex(12)}",
        required_tools=tuple(args.required_tool or ()),
        max_read_bytes=args.max_read_bytes,
        max_write_bytes=args.max_write_bytes,
        allow_delete=not args.no_delete,
        writer_lease_seconds=args.writer_lease_seconds,
        worktree_lock_dir=args.worktree_lock_dir,
        commands=commands,
        command_sandbox=SandboxPolicy(
            backend=args.command_sandbox_backend,
            strict=args.command_sandbox_strict,
            allow_network=args.command_allow_network,
            repository_writable=False,
        ),
    )
    server = BrokerServer(policy)
    print(json.dumps({"ready": True, **policy.public_dict()}, indent=2), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        plane = _plane(args)
        try:
            session = plane.observation_session(args.session_id)
            if session["state"] == "open":
                plane.seal_observation_session(
                    args.session_id, key=observation_key, complete=True
                )
        except KeyError:
            pass
        finally:
            plane.close()
    return 0


def cmd_broker_call(args: argparse.Namespace) -> int:
    token = _required_env(args.token_env)
    payload: dict[str, Any] = {}
    if args.payload:
        payload = _read_json(args.payload)
    for name in ("path", "target_path", "query", "content", "name"):
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    if args.start_line is not None:
        payload["start_line"] = args.start_line
    if args.end_line is not None:
        payload["end_line"] = args.end_line
    if args.max_results is not None:
        payload["max_results"] = args.max_results
    response = BrokerClient(args.socket, token, timeout=args.timeout).call(
        args.operation, **payload
    )
    _write_json(response, args.out)
    return 0 if response.get("ok") else 2


def cmd_broker_run(args: argparse.Namespace) -> int:
    _required_env(args.token_env)
    command = " ".join(args.command).strip()
    if not command:
        raise ValueError("broker-run requires a command after --")
    argv = build_broker_boundary_command(
        command,
        socket_path=args.socket,
        token_env=args.token_env,
        allow_network=args.allow_network,
        runtime_paths=tuple(args.runtime_path or ()),
    )
    completed = subprocess.run(argv, check=False)
    return completed.returncode


def cmd_verify_evidence(args: argparse.Namespace) -> int:
    key: bytes | None = None
    public_key: bytes | None = None
    if args.key_env:
        value = os.environ.get(args.key_env)
        if not value:
            raise ValueError(f"environment variable {args.key_env!r} is not set")
        key = value.encode("utf-8")
    if args.public_key:
        public_key = Path(args.public_key).read_bytes()
    if key is None and public_key is None:
        raise ValueError("provide --key-env for HMAC or --public-key for Ed25519")
    valid = verify_evidence_file(
        args.evidence,
        args.signature,
        key=key,
        public_key_pem=public_key,
    )
    _write_json(
        {"valid": valid, "evidence": args.evidence, "signature": args.signature}
    )
    return 0 if valid else 2


def _observation_key(args: argparse.Namespace) -> bytes:
    value = os.environ.get(args.key_env)
    if not value:
        raise ValueError(f"environment variable {args.key_env!r} is not set")
    return value.encode("utf-8")


def cmd_observe_start(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        result = plane.start_observation_session(
            args.session_id,
            args.intent_id,
            monitor_id=args.monitor_id,
            key_id=args.key_id,
            coverage=args.coverage,
            required_tools=tuple(args.required_tool or ()),
        )
        _write_json(result)
        return 0
    finally:
        plane.close()


def cmd_observe_record(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        result = plane.record_observed_access(
            args.session_id,
            mode=args.mode,
            kind=args.kind,
            identifier=args.identifier,
            key=_observation_key(args),
            tool=args.tool,
        )
        _write_json(result)
        return 0
    finally:
        plane.close()


def cmd_observe_seal(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        result = plane.seal_observation_session(
            args.session_id, key=_observation_key(args), complete=not args.incomplete
        )
        _write_json(result)
        return 0
    finally:
        plane.close()


def cmd_observe_status(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        if args.key_env:
            result = plane.verify_observation_session(
                args.session_id, key=_observation_key(args)
            )
        else:
            result = plane.observation_session(args.session_id)
        _write_json(result)
        return 0 if not isinstance(result, dict) or result.get("valid", True) else 2
    finally:
        plane.close()


def cmd_admit(args: argparse.Namespace) -> int:
    intent = ChangeIntent.from_dict(_read_json(args.intent))
    plane = _plane(args)
    try:
        decision = plane.admit(intent)
        _write_json(decision.to_dict(), args.out)
        return 0 if decision.allowed else 2
    finally:
        plane.close()


def cmd_amend(args: argparse.Namespace) -> int:
    intent = ChangeIntent.from_dict(_read_json(args.intent))
    plane = _plane(args)
    try:
        decision = plane.amend(intent, expected_version=args.expected_version)
        _write_json(decision.to_dict(), args.out)
        return 0 if decision.allowed else 2
    finally:
        plane.close()


def cmd_promote_scope(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        modes = tuple(AccessMode(item) for item in (args.mode or ["write"]))
        decision = plane.promote_contingent_scope(
            args.intent_id,
            path=args.path,
            modes=modes,
            region=args.region,
            expected_version=args.expected_version,
        )
        _write_json(decision.to_dict(), args.out)
        return 0 if decision.allowed else 2
    finally:
        plane.close()


def cmd_intents(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        records = plane.intents(active_only=args.active)
        _write_json(records)
        return 0
    finally:
        plane.close()


def cmd_intent(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        record = next(
            (item for item in plane.intents() if item["intent_id"] == args.intent_id),
            None,
        )
        if record is None:
            raise KeyError(f"unknown intent: {args.intent_id}")
        _write_json(record)
        return 0
    finally:
        plane.close()


def cmd_activate(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        plane.activate(args.intent_id)
        print(f"Activated {args.intent_id}.")
        return 0
    finally:
        plane.close()


def cmd_heartbeat(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        plane.heartbeat(args.intent_id, args.lease_seconds)
        print(f"Renewed {args.intent_id} for {args.lease_seconds}s.")
        return 0
    finally:
        plane.close()


def cmd_complete(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        plane.complete(args.intent_id)
        print(f"Completed {args.intent_id}.")
        return 0
    finally:
        plane.close()


def cmd_release_intent(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        plane.release_intent(args.intent_id)
        print(f"Released {args.intent_id}.")
        return 0
    finally:
        plane.close()


def cmd_context(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        _write_json(plane.context_pack(args.intent_id), args.out)
        return 0
    finally:
        plane.close()


def cmd_notices(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        _write_json(plane.notices(args.intent_id, pending_only=not args.all))
        return 0
    finally:
        plane.close()


def cmd_ack_notice(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        plane.acknowledge_notice(args.notice_id)
        print(f"Acknowledged notice {args.notice_id}.")
        return 0
    finally:
        plane.close()


def cmd_graph(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        _write_json(plane.dependency_graph(), args.out)
        return 0
    finally:
        plane.close()


def cmd_route(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        _write_json(plane.recommend_worker(args.intent_id).to_dict())
        return 0
    finally:
        plane.close()


def cmd_collect_git(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        manifest = plane.collect_git_manifest(args.intent_id, args.repo)
        _write_json(manifest.to_dict(), args.out)
        return 0
    finally:
        plane.close()


def cmd_verify_git(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        report = plane.verify_git(
            args.intent_id,
            args.repo,
            run_acceptance=args.run_acceptance,
            acceptance_timeout=args.acceptance_timeout,
        )
        _write_json(report.to_dict(), args.out)
        return 0 if report.clean else 2
    finally:
        plane.close()


def cmd_verify_manifest(args: argparse.Namespace) -> int:
    manifest = ChangeManifest.from_dict(_read_json(args.manifest))
    plane = _plane(args)
    try:
        report = plane.verify_manifest(manifest)
        _write_json(report.to_dict(), args.out)
        return 0 if report.clean else 2
    finally:
        plane.close()


def cmd_repair_manifest(args: argparse.Namespace) -> int:
    manifest = ChangeManifest.from_dict(_read_json(args.manifest))
    plane = _plane(args)
    try:
        report = plane.verify_manifest(manifest)
        plan = plane.repair_plan(report)
        _write_json(
            {"report": report.to_dict(), "repair_plan": plan.to_dict()}, args.out
        )
        return 0 if report.clean else 2
    finally:
        plane.close()


def cmd_verify_batch(args: argparse.Namespace) -> int:
    manifests = [ChangeManifest.from_dict(_read_json(path)) for path in args.manifests]
    plane = _plane(args)
    try:
        reports = plane.verify_batch(manifests)
        payload = {intent_id: report.to_dict() for intent_id, report in reports.items()}
        _write_json(payload, args.out)
        return 0 if all(report.clean for report in reports.values()) else 2
    finally:
        plane.close()


def cmd_integrate(args: argparse.Namespace) -> int:
    spec = IntegrationRunSpec.from_dict(_read_json(args.spec))
    plane = _plane(args)
    try:
        result = plane.run_integration(spec)
        _write_json(result.to_dict(), args.out)
        return 0 if result.clean else 2
    finally:
        plane.close()


def cmd_audit(args: argparse.Namespace) -> int:
    plane = _plane(args)
    try:
        if args.out:
            plane.export_audit(args.out)
            print(f"Wrote audit bundle to {args.out}")
        else:
            _write_json({"claims": plane.audit(), "events": plane.events()})
        return 0
    finally:
        plane.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claim-plane",
        description=(
            "Execution control and semantic concurrency for autonomous coding agents."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--db", default=None, help=f"Registry path (default: {DEFAULT_DB})."
    )
    parser.add_argument(
        "--semantic", action="store_true", help="Enable Agent Lexicon resolution."
    )
    parser.add_argument(
        "--lexicon", default=None, help="Path to an Agent Lexicon YAML/JSON file."
    )
    parser.add_argument(
        "--exploratory",
        action="store_true",
        help="Allow unpinned intents for local experiments. Governed admission is the default.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init", help="Initialize local Claim Plane state for a Git project."
    )
    init.add_argument("--repo", default=".")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_init)

    connect = sub.add_parser(
        "connect", help="Enroll a coding-agent runtime in this project."
    )
    connect_sub = connect.add_subparsers(dest="connector", required=True)
    connect_codex_parser = connect_sub.add_parser(
        "codex", help="Install the project-local Codex lifecycle bridge."
    )
    connect_codex_parser.add_argument("--repo", default=".")
    connect_codex_parser.add_argument("--json", action="store_true")
    connect_codex_parser.set_defaults(func=cmd_connect_codex)

    disconnect = sub.add_parser(
        "disconnect", help="Remove a coding-agent runtime enrollment."
    )
    disconnect_sub = disconnect.add_subparsers(dest="connector", required=True)
    disconnect_codex_parser = disconnect_sub.add_parser(
        "codex", help="Remove Claim Plane-owned Codex lifecycle hooks."
    )
    disconnect_codex_parser.add_argument("--repo", default=".")
    disconnect_codex_parser.add_argument("--json", action="store_true")
    disconnect_codex_parser.set_defaults(func=cmd_disconnect_codex)

    doctor = sub.add_parser(
        "doctor", help="Inspect coding-agent runtime enrollment health."
    )
    doctor_sub = doctor.add_subparsers(dest="connector", required=True)
    doctor_codex_parser = doctor_sub.add_parser(
        "codex", help="Inspect the project-local Codex lifecycle bridge."
    )
    doctor_codex_parser.add_argument("--repo", default=".")
    doctor_codex_parser.add_argument("--json", action="store_true")
    doctor_codex_parser.set_defaults(func=cmd_doctor_codex)

    codex_hook = sub.add_parser(
        "codex-hook", help="Internal Codex lifecycle dispatcher."
    )
    codex_hook.set_defaults(func=cmd_codex_hook)

    codex_intent = sub.add_parser(
        "codex-intent",
        help="Manage the ChangeIntent bound to an enrolled Codex session.",
    )
    codex_intent_sub = codex_intent.add_subparsers(
        dest="codex_intent_command", required=True
    )
    codex_intent_admit = codex_intent_sub.add_parser(
        "admit",
        help="Bind and atomically admit a model-proposed ChangeIntent for a session.",
    )
    codex_intent_admit.add_argument("--session-id", required=True)
    codex_intent_admit.add_argument("--repo", default=".")
    proposal_source = codex_intent_admit.add_mutually_exclusive_group()
    proposal_source.add_argument(
        "--proposal",
        help="Read the proposal from a JSON file instead of stdin.",
    )
    proposal_source.add_argument(
        "--proposal-json",
        help="Read the proposal from an inline JSON object; preferred for Codex control calls.",
    )
    codex_intent_admit.set_defaults(func=cmd_codex_intent_admit)

    codex_intent_abandon = codex_intent_sub.add_parser(
        "abandon",
        help="Release unfinished session authority so another Codex session may proceed.",
    )
    codex_intent_abandon.add_argument("--session-id", required=True)
    codex_intent_abandon.add_argument("--repo", default=".")
    codex_intent_abandon.set_defaults(func=cmd_codex_intent_abandon)

    codex_intent_amend = codex_intent_sub.add_parser(
        "amend",
        help="Request the exact scope expansion described by a guard-issued ticket.",
    )
    codex_intent_amend.add_argument("--session-id", required=True)
    codex_intent_amend.add_argument("--ticket", required=True)
    codex_intent_amend.add_argument("--reason", required=True)
    codex_intent_amend.add_argument("--repo", default=".")
    codex_intent_amend.set_defaults(func=cmd_codex_intent_amend)

    codex_intent_verify = codex_intent_sub.add_parser(
        "verify", help="Verify the current Codex worktree and acceptance criteria."
    )
    codex_intent_verify.add_argument("--session-id", required=True)
    codex_intent_verify.add_argument("--repo", default=".")
    codex_intent_verify.add_argument("--acceptance-timeout", type=int, default=300)
    codex_intent_verify.set_defaults(func=cmd_codex_intent_verify)

    codex_intent_status_parser = codex_intent_sub.add_parser(
        "status", help="Show the session-bound task and admitted intent."
    )
    codex_intent_status_parser.add_argument("--session-id", required=True)
    codex_intent_status_parser.add_argument("--repo", default=".")
    codex_intent_status_parser.add_argument("--json", action="store_true")
    codex_intent_status_parser.set_defaults(func=cmd_codex_intent_status)

    swarm = sub.add_parser(
        "swarm", help="Create and inspect repository-bound swarm planning sessions."
    )
    swarm_sub = swarm.add_subparsers(dest="swarm_command", required=True)

    swarm_create = swarm_sub.add_parser(
        "create", help="Create a planned swarm session from a validated work graph."
    )
    swarm_create.add_argument(
        "--spec", required=True, help="Swarm session spec JSON file."
    )
    swarm_create.add_argument("--repo", default=".")
    swarm_create.add_argument("--session-id")
    swarm_create.add_argument("--base", default="HEAD")
    swarm_create.add_argument("--json", action="store_true")
    swarm_create.add_argument("--out")
    swarm_create.set_defaults(func=cmd_swarm_create)

    swarm_list = swarm_sub.add_parser("list", help="List local swarm sessions.")
    swarm_list.add_argument("--repo", default=".")
    swarm_list.add_argument("--json", action="store_true")
    swarm_list.set_defaults(func=cmd_swarm_list)

    swarm_status = swarm_sub.add_parser(
        "status", help="Show one swarm session and its pinned work graph."
    )
    swarm_status.add_argument("session_id")
    swarm_status.add_argument("--repo", default=".")
    swarm_status.add_argument("--json", action="store_true")
    swarm_status.set_defaults(func=cmd_swarm_status)

    swarm_graph = swarm_sub.add_parser(
        "graph", help="Export one versioned swarm work graph and graph summary."
    )
    swarm_graph.add_argument("session_id")
    swarm_graph.add_argument("--repo", default=".")
    swarm_graph.add_argument("--out")
    swarm_graph.set_defaults(func=cmd_swarm_graph)

    swarm_replace = swarm_sub.add_parser(
        "replace-graph",
        help="Replace a planned work graph with optimistic version checking.",
    )
    swarm_replace.add_argument("session_id")
    swarm_replace.add_argument("--graph", required=True)
    swarm_replace.add_argument("--expected-version", type=int, required=True)
    swarm_replace.add_argument("--repo", default=".")
    swarm_replace.add_argument("--out")
    swarm_replace.set_defaults(func=cmd_swarm_replace_graph)

    swarm_validate = swarm_sub.add_parser(
        "validate", help="Validate a work graph and print its deterministic topology."
    )
    swarm_validate.add_argument("--graph", required=True)
    swarm_validate.set_defaults(func=cmd_swarm_validate)


    swarm_plan = swarm_sub.add_parser(
        "plan",
        help="Compute and persist deterministic adaptive execution waves.",
    )
    swarm_plan.add_argument("session_id")
    swarm_plan.add_argument("--repo", default=".")
    swarm_plan.add_argument("--json", action="store_true")
    swarm_plan.add_argument("--out")
    swarm_plan.set_defaults(func=cmd_swarm_plan)

    swarm_concurrency = swarm_sub.add_parser(
        "concurrency",
        help="Inspect the persisted adaptive concurrency plan.",
    )
    swarm_concurrency.add_argument("session_id")
    swarm_concurrency.add_argument("--repo", default=".")
    swarm_concurrency.add_argument("--json", action="store_true")
    swarm_concurrency.add_argument("--out")
    swarm_concurrency.set_defaults(func=cmd_swarm_concurrency)

    swarm_validate_concurrency = swarm_sub.add_parser(
        "validate-concurrency",
        help="Preview adaptive waves for a standalone graph and policy.",
    )
    swarm_validate_concurrency.add_argument("--graph", required=True)
    swarm_validate_concurrency.add_argument("--policy")
    swarm_validate_concurrency.set_defaults(func=cmd_swarm_validate_concurrency)

    swarm_budget = swarm_sub.add_parser(
        "budget", help="Export the versioned budget policy for one swarm session."
    )
    swarm_budget.add_argument("session_id")
    swarm_budget.add_argument("--repo", default=".")
    swarm_budget.add_argument("--out")
    swarm_budget.set_defaults(func=cmd_swarm_budget)

    swarm_replace_budget = swarm_sub.add_parser(
        "replace-budget",
        help="Replace a planned budget policy with optimistic version checking.",
    )
    swarm_replace_budget.add_argument("session_id")
    swarm_replace_budget.add_argument("--policy", required=True)
    swarm_replace_budget.add_argument(
        "--expected-version", type=int, required=True
    )
    swarm_replace_budget.add_argument("--repo", default=".")
    swarm_replace_budget.add_argument("--out")
    swarm_replace_budget.set_defaults(func=cmd_swarm_replace_budget)

    swarm_validate_budget = swarm_sub.add_parser(
        "validate-budget",
        help="Validate and normalize a standalone swarm budget policy.",
    )
    swarm_validate_budget.add_argument("--policy", required=True)
    swarm_validate_budget.add_argument(
        "--work-items",
        type=int,
        help="Optionally verify that a proposed graph size fits the policy.",
    )
    swarm_validate_budget.set_defaults(func=cmd_swarm_validate_budget)

    swarm_provision_worktrees = swarm_sub.add_parser(
        "provision-worktrees",
        help="Provision one Claim Plane-owned Git worktree per work item.",
    )
    swarm_provision_worktrees.add_argument("session_id")
    swarm_provision_worktrees.add_argument("--repo", default=".")
    swarm_provision_worktrees.add_argument("--json", action="store_true")
    swarm_provision_worktrees.add_argument("--out")
    swarm_provision_worktrees.set_defaults(func=cmd_swarm_provision_worktrees)

    swarm_worktrees = swarm_sub.add_parser(
        "worktrees",
        help="Inspect managed worktree ownership, health, and orphan state.",
    )
    swarm_worktrees.add_argument("session_id")
    swarm_worktrees.add_argument("--repo", default=".")
    swarm_worktrees.add_argument("--json", action="store_true")
    swarm_worktrees.add_argument("--out")
    swarm_worktrees.set_defaults(func=cmd_swarm_worktrees)

    swarm_cleanup_worktrees = swarm_sub.add_parser(
        "cleanup-worktrees",
        help="Remove only Claim Plane-owned worktrees and branches.",
    )
    swarm_cleanup_worktrees.add_argument("session_id")
    swarm_cleanup_worktrees.add_argument(
        "--work-id",
        action="append",
        help="Remove a specific work item; repeat for multiple items.",
    )
    swarm_cleanup_worktrees.add_argument("--force", action="store_true")
    swarm_cleanup_worktrees.add_argument("--repo", default=".")
    swarm_cleanup_worktrees.add_argument("--json", action="store_true")
    swarm_cleanup_worktrees.add_argument("--out")
    swarm_cleanup_worktrees.set_defaults(func=cmd_swarm_cleanup_worktrees)

    swarm_run_codex = swarm_sub.add_parser(
        "run-codex",
        help="Run one bounded headless Codex worker in its managed worktree.",
    )
    swarm_run_codex.add_argument("session_id")
    swarm_run_codex.add_argument("--work-id", required=True)
    swarm_run_codex.add_argument("--codex-bin", default="codex")
    swarm_run_codex.add_argument("--model")
    swarm_run_codex.add_argument("--reasoning-effort")
    swarm_run_codex.add_argument("--timeout", type=int)
    swarm_run_codex.add_argument("--max-tokens", type=int)
    swarm_run_codex.add_argument("--repo", default=".")
    swarm_run_codex.add_argument("--json", action="store_true")
    swarm_run_codex.add_argument("--out")
    swarm_run_codex.set_defaults(func=cmd_swarm_run_codex)

    swarm_runs = swarm_sub.add_parser(
        "runs", help="List durable Codex worker-run records for a swarm session."
    )
    swarm_runs.add_argument("session_id")
    swarm_runs.add_argument("--work-id")
    swarm_runs.add_argument("--repo", default=".")
    swarm_runs.add_argument("--json", action="store_true")
    swarm_runs.add_argument("--out")
    swarm_runs.set_defaults(func=cmd_swarm_runs)

    swarm_run_status = swarm_sub.add_parser(
        "run-status", help="Inspect one durable Codex worker-run record."
    )
    swarm_run_status.add_argument("run_id")
    swarm_run_status.add_argument("--repo", default=".")
    swarm_run_status.add_argument("--json", action="store_true")
    swarm_run_status.add_argument("--out")
    swarm_run_status.set_defaults(func=cmd_swarm_run_status)

    swarm_cancel_codex = swarm_sub.add_parser(
        "cancel-codex", help="Request cancellation of one active Codex worker."
    )
    swarm_cancel_codex.add_argument("run_id")
    swarm_cancel_codex.add_argument("--repo", default=".")
    swarm_cancel_codex.add_argument("--json", action="store_true")
    swarm_cancel_codex.add_argument("--out")
    swarm_cancel_codex.set_defaults(func=cmd_swarm_cancel_codex)

    claim = sub.add_parser(
        "claim", help="Request a legacy fine-grained artifact claim."
    )
    claim.add_argument("identifier")
    claim.add_argument(
        "--type", choices=[item.value for item in ClaimType], default="name"
    )
    claim.add_argument("--owner", required=True)
    claim.add_argument("--signature")
    claim.add_argument("--task")
    claim.add_argument("--lease-seconds", type=int, default=900)
    claim.set_defaults(func=cmd_claim)

    release = sub.add_parser("release", help="Release legacy claims for one owner.")
    release.add_argument("owner")
    release.set_defaults(func=cmd_release)

    status = sub.add_parser("status", help="Show active legacy claims.")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    verify_merge = sub.add_parser(
        "verify-merge", help="Legacy source artifact collision check."
    )
    verify_merge.add_argument("file")
    verify_merge.add_argument("--owner", required=True)
    verify_merge.add_argument("--task")
    verify_merge.set_defaults(func=cmd_verify_merge)

    broker_serve = sub.add_parser(
        "broker-serve",
        help="Serve an intent-enforcing Unix-socket repository proxy.",
    )
    broker_serve.add_argument("intent_id")
    broker_serve.add_argument("session_id")
    broker_serve.add_argument("--root", required=True)
    broker_serve.add_argument("--socket", required=True)
    broker_serve.add_argument("--token-env", default="CLAIM_PLANE_BROKER_TOKEN")
    broker_serve.add_argument("--key-env", default="CLAIM_PLANE_OBSERVATION_KEY")
    broker_serve.add_argument("--broker-key-env", default="CLAIM_PLANE_BROKER_KEY")
    broker_serve.add_argument("--monitor-id", default="claim-plane-broker")
    broker_serve.add_argument("--instance-id")
    broker_serve.add_argument("--key-id", default="default")
    broker_serve.add_argument("--required-tool", action="append")
    broker_serve.add_argument("--max-read-bytes", type=int, default=2_000_000)
    broker_serve.add_argument("--max-write-bytes", type=int, default=2_000_000)
    broker_serve.add_argument("--no-delete", action="store_true")
    broker_serve.add_argument(
        "--writer-lease-seconds",
        type=int,
        default=300,
        help="Exclusive governed-worktree writer lease; renewed on every broker request.",
    )
    broker_serve.add_argument(
        "--worktree-lock-dir",
        help="Deprecated compatibility input; governed mode requires the canonical Git lock namespace.",
    )
    broker_serve.add_argument(
        "--commands", help="JSON mapping of allowlisted command names to argv arrays."
    )
    broker_serve.add_argument(
        "--command-sandbox-backend",
        choices=["tree", "auto", "bwrap", "bwrap-minimal", "sandbox-exec", "none"],
        default="tree",
    )
    broker_serve.add_argument("--command-sandbox-strict", action="store_true")
    broker_serve.add_argument("--command-allow-network", action="store_true")
    broker_serve.set_defaults(func=cmd_broker_serve)

    broker_call = sub.add_parser(
        "broker-call",
        help="Call a running broker from a tool adapter or restricted worker.",
    )
    broker_call.add_argument(
        "operation",
        choices=[
            "health",
            "read_file",
            "list_dir",
            "search_text",
            "stat",
            "write_file",
            "append_file",
            "replace_lines",
            "delete_file",
            "rename_file",
            "run_command",
        ],
    )
    broker_call.add_argument("--socket", required=True)
    broker_call.add_argument("--token-env", default="CLAIM_PLANE_BROKER_TOKEN")
    broker_call.add_argument("--payload")
    broker_call.add_argument("--path")
    broker_call.add_argument("--target-path")
    broker_call.add_argument("--query")
    broker_call.add_argument("--content")
    broker_call.add_argument("--name")
    broker_call.add_argument("--start-line", type=int)
    broker_call.add_argument("--end-line", type=int)
    broker_call.add_argument("--max-results", type=int)
    broker_call.add_argument("--timeout", type=float, default=30.0)
    broker_call.add_argument("--out")
    broker_call.set_defaults(func=cmd_broker_call)

    broker_run = sub.add_parser(
        "broker-run",
        help="Run a worker in a Linux Bubblewrap boundary with no repository mount.",
    )
    broker_run.add_argument("--socket", required=True)
    broker_run.add_argument("--token-env", default="CLAIM_PLANE_BROKER_TOKEN")
    broker_run.add_argument("--allow-network", action="store_true")
    broker_run.add_argument("--runtime-path", action="append")
    broker_run.add_argument("command", nargs=argparse.REMAINDER)
    broker_run.set_defaults(func=cmd_broker_run)

    pin_intent = sub.add_parser(
        "pin-intent",
        help="Resolve base_revision and write an intent with an immutable base_commit.",
    )
    pin_intent.add_argument("intent")
    pin_intent.add_argument("--repo", default=".")
    pin_intent.add_argument("--out", required=True)
    pin_intent.set_defaults(func=cmd_pin_intent)

    record_access = sub.add_parser(
        "record-access",
        help="Append one observed tool read/write event to a JSONL trace.",
    )
    record_access.add_argument("trace")
    record_access.add_argument(
        "--mode", choices=[item.value for item in AccessMode], required=True
    )
    record_access.add_argument(
        "--kind", choices=[item.value for item in ResourceKind], required=True
    )
    record_access.add_argument("--identifier", required=True)
    record_access.add_argument("--tool")
    record_access.set_defaults(func=cmd_record_access)

    observe_start = sub.add_parser(
        "observe-start", help="Start an append-only trusted observation session."
    )
    observe_start.add_argument("session_id")
    observe_start.add_argument("intent_id")
    observe_start.add_argument("--monitor-id", required=True)
    observe_start.add_argument("--key-id", default="default")
    observe_start.add_argument(
        "--coverage",
        choices=["brokered_proxy", "tool_proxy", "os_monitor", "declared"],
        default="tool_proxy",
    )
    observe_start.add_argument("--required-tool", action="append")
    observe_start.set_defaults(func=cmd_observe_start)

    observe_record = sub.add_parser(
        "observe-record", help="Append a hash-chained, HMAC-authenticated access event."
    )
    observe_record.add_argument("session_id")
    observe_record.add_argument("--key-env", required=True)
    observe_record.add_argument(
        "--mode", choices=[item.value for item in AccessMode], required=True
    )
    observe_record.add_argument(
        "--kind", choices=[item.value for item in ResourceKind], required=True
    )
    observe_record.add_argument("--identifier", required=True)
    observe_record.add_argument("--tool")
    observe_record.set_defaults(func=cmd_observe_record)

    observe_seal = sub.add_parser(
        "observe-seal", help="Seal and attest a trusted observation session."
    )
    observe_seal.add_argument("session_id")
    observe_seal.add_argument("--key-env", required=True)
    observe_seal.add_argument("--incomplete", action="store_true")
    observe_seal.set_defaults(func=cmd_observe_seal)

    observe_status = sub.add_parser(
        "observe-status",
        help="Show or cryptographically verify an observation session.",
    )
    observe_status.add_argument("session_id")
    observe_status.add_argument("--key-env")
    observe_status.set_defaults(func=cmd_observe_status)

    admit = sub.add_parser(
        "admit", help="Atomically admit a ChangeIntent JSON document."
    )
    admit.add_argument("intent")
    admit.add_argument("--out")
    admit.set_defaults(func=cmd_admit)

    amend = sub.add_parser(
        "amend",
        help="Atomically replace an existing intent and invalidate affected dependents.",
    )
    amend.add_argument("intent")
    amend.add_argument("--expected-version", type=int)
    amend.add_argument("--out")
    amend.set_defaults(func=cmd_amend)

    promote_scope = sub.add_parser(
        "promote-scope",
        help="Promote matching contingent path operations after atomic re-admission.",
    )
    promote_scope.add_argument("intent_id")
    promote_scope.add_argument("path")
    promote_scope.add_argument(
        "--mode",
        action="append",
        choices=[item.value for item in AccessMode],
        help="Access mode to promote (repeatable; default: write).",
    )
    promote_scope.add_argument(
        "--region",
        help="Concrete pre-image line interval to promote, e.g. lines:20-24.",
    )
    promote_scope.add_argument("--expected-version", type=int)
    promote_scope.add_argument("--out")
    promote_scope.set_defaults(func=cmd_promote_scope)

    intents = sub.add_parser("intents", help="List declared intents.")
    intents.add_argument("--active", action="store_true")
    intents.set_defaults(func=cmd_intents)

    intent = sub.add_parser("intent", help="Show one intent record.")
    intent.add_argument("intent_id")
    intent.set_defaults(func=cmd_intent)

    activate = sub.add_parser("activate", help="Mark an admitted intent active.")
    activate.add_argument("intent_id")
    activate.set_defaults(func=cmd_activate)

    heartbeat = sub.add_parser("heartbeat", help="Renew an intent lease.")
    heartbeat.add_argument("intent_id")
    heartbeat.add_argument("--lease-seconds", type=int, default=900)
    heartbeat.set_defaults(func=cmd_heartbeat)

    complete = sub.add_parser("complete", help="Mark an intent completed.")
    complete.add_argument("intent_id")
    complete.set_defaults(func=cmd_complete)

    release_intent = sub.add_parser(
        "release-intent", help="Release an abandoned/merged intent."
    )
    release_intent.add_argument("intent_id")
    release_intent.set_defaults(func=cmd_release_intent)

    context = sub.add_parser("context", help="Build a bounded worker context pack.")
    context.add_argument("intent_id")
    context.add_argument("--out")
    context.set_defaults(func=cmd_context)

    notices = sub.add_parser(
        "notices", help="Show structured premise invalidation notices for an intent."
    )
    notices.add_argument("intent_id")
    notices.add_argument("--all", action="store_true")
    notices.set_defaults(func=cmd_notices)

    ack_notice = sub.add_parser(
        "ack-notice", help="Acknowledge one coordination notice."
    )
    ack_notice.add_argument("notice_id", type=int)
    ack_notice.set_defaults(func=cmd_ack_notice)

    graph = sub.add_parser(
        "graph", help="Show the acyclic premise graph and producer-first order."
    )
    graph.add_argument("--out")
    graph.set_defaults(func=cmd_graph)

    route = sub.add_parser(
        "route", help="Recommend economy/standard/frontier worker tier."
    )
    route.add_argument("intent_id")
    route.set_defaults(func=cmd_route)

    collect_git = sub.add_parser(
        "collect-git", help="Collect actual changes from a Git worktree."
    )
    collect_git.add_argument("intent_id")
    collect_git.add_argument("--repo", default=".")
    collect_git.add_argument("--out")
    collect_git.set_defaults(func=cmd_collect_git)

    verify_git = sub.add_parser(
        "verify-git", help="Verify current worktree against an admitted intent."
    )
    verify_git.add_argument("intent_id")
    verify_git.add_argument("--repo", default=".")
    verify_git.add_argument(
        "--run-acceptance",
        action="store_true",
        help="Execute declared acceptance commands locally before verification.",
    )
    verify_git.add_argument("--acceptance-timeout", type=int, default=300)
    verify_git.add_argument("--out")
    verify_git.set_defaults(func=cmd_verify_git)

    verify_manifest = sub.add_parser(
        "verify-manifest", help="Verify a pre-collected ChangeManifest JSON."
    )
    verify_manifest.add_argument("manifest")
    verify_manifest.add_argument("--out")
    verify_manifest.set_defaults(func=cmd_verify_manifest)

    repair_manifest = sub.add_parser(
        "repair-manifest", help="Verify a manifest and generate a targeted repair plan."
    )
    repair_manifest.add_argument("manifest")
    repair_manifest.add_argument("--out")
    repair_manifest.set_defaults(func=cmd_repair_manifest)

    verify_batch = sub.add_parser(
        "verify-batch", help="Verify several manifests as one integration set."
    )
    verify_batch.add_argument("manifests", nargs="+")
    verify_batch.add_argument("--out")
    verify_batch.set_defaults(func=cmd_verify_batch)

    integrate = sub.add_parser(
        "integrate",
        help="Freeze worker snapshots, verify exact patches, create a verified integration commit, and run bounded repairs.",
    )
    integrate.add_argument("spec", help="IntegrationRunSpec JSON file.")
    integrate.add_argument("--out")
    integrate.set_defaults(func=cmd_integrate)

    verify_evidence = sub.add_parser(
        "verify-evidence", help="Verify an HMAC-signed evidence bundle."
    )
    verify_evidence.add_argument("evidence")
    verify_evidence.add_argument("signature")
    verify_evidence.add_argument("--key-env")
    verify_evidence.add_argument("--public-key")
    verify_evidence.set_defaults(func=cmd_verify_evidence)

    audit = sub.add_parser(
        "audit", help="Export claims, coordination events, and verification reports."
    )
    audit.add_argument("--out")
    audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        FileNotFoundError,
        KeyError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
