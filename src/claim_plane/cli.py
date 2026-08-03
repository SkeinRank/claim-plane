"""Command line interface for Claim Plane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from claim_plane import __version__
from claim_plane.connectors import (
    build_adapter_registry,
    disconnect_codex,
    init_project,
)
from claim_plane.controlled_run import (
    CONTROLLED_POLICY_ENV,
    CONTROLLED_POLICY_MANIFEST_ENV,
    CONTROLLED_RUN_ENV,
    ControlledRunPreflightError,
    run_controlled_task,
)
from claim_plane.policy import POLICY_NAMES, EffectivePolicy, resolve_policy
from claim_plane.protocol import (
    AdapterCapabilityManifest,
    AdapterHandshake,
    AdapterOperation,
    AdapterRequest,
    evaluate_adapter_policy,
    remove_adapter_pin,
    run_adapter_conformance,
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
    admit_swarm_session,
    cancel_codex_run,
    cancel_swarm_session,
    cleanup_swarm_worktrees,
    create_swarm_session,
    create_and_run_swarm_demo,
    get_codex_run,
    get_swarm_admission,
    get_swarm_concurrency_plan,
    get_swarm_scheduler,
    get_swarm_merge_queue,
    get_swarm_operator_snapshot,
    get_swarm_session,
    get_swarm_verification,
    inspect_swarm_recovery,
    inspect_swarm_worktrees,
    list_codex_runs,
    list_swarm_recovery_events,
    list_swarm_operator_logs,
    list_swarm_sessions,
    pause_swarm_session,
    plan_swarm_concurrency,
    plan_swarm_merge_queue,
    integrate_next_swarm_result,
    drain_swarm_merge_queue,
    provision_swarm_worktrees,
    recover_swarm_session,
    replace_codex_worker,
    replace_swarm_budget_policy,
    replace_swarm_work_graph,
    resume_swarm_session,
    run_codex_work_item,
    start_swarm_session,
    validate_budget_policy,
    validate_concurrency_plan,
    validate_work_graph,
    verify_swarm_session,
)

from claim_plane.testing.codex import CodexConformanceDriver
from claim_plane.project import load_project_config, reset_project
from claim_plane.testing.conformance import ReferenceConformanceDriver

DEFAULT_DB = ".claim-plane/plane.db"

_ADAPTER_REGISTRY = build_adapter_registry()
_CODEX_ADAPTER = _ADAPTER_REGISTRY.create("codex")


def _adapter_handshake(
    adapter: str,
    *,
    repo: str,
    require_compatible: bool = False,
) -> AdapterHandshake:
    handshake = _ADAPTER_REGISTRY.handshake(adapter, project_root=repo)
    if require_compatible:
        handshake.require_compatible()
    return handshake


def _print_adapter_handshake(payload: Mapping[str, Any]) -> None:
    negotiated = payload.get("negotiated_protocol_version") or "none"
    runtime = payload.get("runtime") or {}
    print(
        f"Handshake: core {', '.join(payload.get('core_protocol_versions') or [])} "
        f"↔ adapter {payload.get('adapter_protocol_range')} → {negotiated}"
    )
    print(
        f"Runtime: {runtime.get('name', 'unknown')} "
        f"{runtime.get('version') or 'not detected'}"
    )
    pin = payload.get("pin")
    print("Pin: present" if pin else "Pin: none")
    for finding in payload.get("findings") or []:
        print(
            f"  [{str(finding['severity']).upper():7}] "
            f"{finding['message']}"
        )
    print("Compatibility: OK" if payload.get("compatible") else "Compatibility: FAILED")


def _adapter_request_id(
    operation: AdapterOperation,
    *,
    session_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    stable: bool = False,
) -> str:
    if not stable:
        return f"cli-{operation.value}-{secrets.token_hex(12)}"
    canonical = json.dumps(
        {
            "operation": operation.value,
            "session_id": session_id,
            "payload": dict(payload or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"cli-{operation.value}-{digest}"


def _codex_request(
    operation: AdapterOperation,
    *,
    repo: str,
    session_id: str | None = None,
    intent_id: str | None = None,
    intent_version: int | None = None,
    timeout_seconds: float = 30.0,
    payload: Mapping[str, Any] | None = None,
    stable: bool = False,
) -> AdapterRequest:
    return AdapterRequest.create(
        operation,
        adapter="codex",
        project_root=repo,
        request_id=_adapter_request_id(
            operation, session_id=session_id, payload=payload, stable=stable
        ),
        session_id=session_id,
        intent_id=intent_id,
        intent_version=intent_version,
        timeout_seconds=timeout_seconds,
        payload=payload,
    )


def _codex_binding(repo: str, session_id: str) -> tuple[str | None, int | None]:
    response = _CODEX_ADAPTER.inspect(
        _codex_request(
            AdapterOperation.INSPECT,
            repo=repo,
            session_id=session_id,
        )
    )
    return response.intent_id, response.intent_version


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
        action = "Initialized" if result.get("created") else "Verified"
        print(f"{action} Claim Plane for {result['root']}.")
        print(f"Project identity: {result['project_id']}")
        print(f"Default branch: {result['default_branch']}")
        print(f"Config: {result['config']}")
        commands = result.get("acceptance_commands") or []
        if commands:
            print(f"Acceptance: {', '.join(commands)}")
        else:
            print("Acceptance: no command detected; configure it before a guarded run.")
        print("Local state is excluded through the repository Git exclude file.")
    return 0


def cmd_connect_codex(args: argparse.Namespace) -> int:
    handshake = _adapter_handshake("codex", repo=args.repo, require_compatible=True)
    response = _CODEX_ADAPTER.enroll_project(
        _codex_request(AdapterOperation.ENROLL_PROJECT, repo=args.repo)
    )
    result = dict(response.payload)
    result["registry_handshake"] = handshake.to_dict()
    try:
        pin, path = _ADAPTER_REGISTRY.pin("codex", project_root=args.repo)
    except (RuntimeError, ValueError) as exc:
        result["pin"] = None
        result["pin_status"] = "not_available"
        result["pin_detail"] = str(exc)
    else:
        result["pin"] = pin.to_dict()
        result["pin_path"] = str(path)
        result["pin_status"] = "created"
    if args.json:
        _write_json(result)
    else:
        print(f"Connected Codex to Claim Plane for {result['root']}.")
        print(f"Lifecycle hooks: {', '.join(result['events'])}")
        runtime = result.get("runtime_version") or "version not detected"
        print(f"Codex runtime: {runtime}")
        print(f"Sandbox: {result.get('sandbox_detail')}")
        if result.get("pin_status") == "created":
            print(f"Adapter pin: {result['pin_path']}")
        else:
            print(f"Adapter pin: deferred ({result.get('pin_detail')})")
        print(
            "Open /hooks in Codex once to review and trust the project-local "
            "command hooks."
        )
        if result["inline_hooks_present"]:
            print(
                "Note: .codex/config.toml also defines inline hooks; "
                "Codex merges both project-local hook sources."
            )
    return 0


def cmd_disconnect_codex(args: argparse.Namespace) -> int:
    response = _CODEX_ADAPTER.unenroll_project(
        _codex_request(
            AdapterOperation.UNENROLL_PROJECT, repo=args.repo
        )
    )
    result = dict(response.payload)
    if args.json:
        _write_json(result)
    else:
        print(f"Disconnected Codex from Claim Plane for {result['root']}.")
        print(f"Removed {result['removed_handlers']} Claim Plane hook handler(s).")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    disconnect_result: dict[str, Any]
    try:
        disconnect_result = disconnect_codex(args.repo)
    except ValueError as exc:
        disconnect_result = {"connected": False, "warning": str(exc)}
    result = reset_project(args.repo, remove_config=args.remove_config)
    result["codex"] = disconnect_result
    if args.json:
        _write_json(result)
    else:
        print(f"Reset Claim Plane local state for {result['root']}.")
        print(f"Removed entries: {len(result['removed'])}")
        print(
            "Project config removed."
            if args.remove_config
            else "Project config preserved."
        )
        print("Repository files and foreign Codex hooks were left unchanged.")
    return 0


def _effective_policy_for_cli(
    repo: str, explicit: str | None, *, adapter: str = "codex"
) -> EffectivePolicy:
    config = load_project_config(repo)
    adapters = config.get("adapters")
    settings = adapters.get(adapter) if isinstance(adapters, Mapping) else None
    configured = (
        str(settings.get("policy") or "guarded")
        if isinstance(settings, Mapping)
        else "guarded"
    )
    risk = config.get("risk")
    return resolve_policy(
        str(explicit or configured),
        risk=risk if isinstance(risk, Mapping) else None,
        source="command_line" if explicit is not None else "project_config",
        metadata={"adapter": adapter},
    )


def _print_effective_policy(payload: Mapping[str, Any]) -> None:
    preset = payload.get("preset") or {}
    risk = payload.get("risk") or {}
    print(f"Policy: {preset.get('name')} ({payload.get('source')})")
    print(str(preset.get("summary") or ""))
    print(f"Pre-write mode: {preset.get('pre_write_mode')}")
    print(f"Unknown actions: {preset.get('unknown_action')}")
    print(f"Scope expansion: {preset.get('scope_expansion_action')}")
    print(f"Human gate: {'required' if preset.get('human_gate') else 'not required'}")
    print(f"Risk default: {risk.get('default')}")
    for level, action in dict(preset.get("risk_actions") or {}).items():
        print(f"  {level}: {action}")
    print(f"Digest: {payload.get('digest')}")


def cmd_policy_inspect(args: argparse.Namespace) -> int:
    effective = _effective_policy_for_cli(args.repo, args.policy)
    payload = effective.to_dict()
    adapter = _ADAPTER_REGISTRY.create(args.adapter)
    compatibility = evaluate_adapter_policy(
        adapter.capability_manifest(args.repo), effective.name
    )
    payload["adapter"] = args.adapter
    payload["adapter_compatibility"] = compatibility.to_dict()
    if args.json:
        _write_json(payload)
    else:
        _print_effective_policy(payload)
        status = "compatible" if compatibility.compatible else "unavailable"
        print(f"Adapter {args.adapter}: {status}")
        for finding in compatibility.findings:
            print(f"  - {finding.message}")
    return 0 if compatibility.compatible else 2


def cmd_policy_classify(args: argparse.Namespace) -> int:
    effective = _effective_policy_for_cli(args.repo, args.policy)
    payload = effective.classify_many(args.paths)
    payload["effective_policy"] = effective.to_dict()
    if args.json:
        _write_json(payload)
    else:
        print(
            f"Policy {effective.name}: {payload['highest_risk']} risk → "
            f"{payload['final_action']}"
        )
        for finding in payload["findings"]:
            print(
                f"  {finding['path']}: {finding['level']} → "
                f"{finding['action']}"
            )
            print(f"    {finding['explanation']}")
    return 3 if payload["final_action"] == "DENY" else 0


def _manifest_with_policy(
    manifest: AdapterCapabilityManifest, policy: str | None
) -> tuple[dict[str, Any], bool]:
    payload = manifest.to_dict()
    compatible = True
    if policy is not None:
        compatibility = evaluate_adapter_policy(manifest, policy)
        payload["policy_compatibility"] = compatibility.to_dict()
        compatible = compatibility.compatible
    return payload, compatible


def _print_adapter_manifest(payload: Mapping[str, Any]) -> None:
    runtime = payload.get("runtime") or {}
    print(
        f"Adapter: {payload['adapter']} {payload['adapter_version']} "
        f"(protocol {payload['adapter_protocol_version']})"
    )
    runtime_version = runtime.get("version") or "not detected"
    print(f"Runtime: {runtime.get('name', 'unknown')} {runtime_version}")
    print(f"Manifest: {payload['digest']}")
    print("Capabilities:")
    for name, level in sorted(dict(payload.get("capabilities") or {}).items()):
        print(f"  {name}: {level}")
    print("Guarantees:")
    for name, declaration in sorted(
        dict(payload.get("guarantees") or {}).items()
    ):
        print(
            f"  {name}: {declaration['level']} "
            f"({declaration['provided_by']})"
        )
    compatibility = payload.get("policy_compatibility")
    if isinstance(compatibility, Mapping):
        status = "compatible" if compatibility.get("compatible") else "unavailable"
        print(f"Policy {compatibility.get('policy')}: {status}")
        for finding in compatibility.get("findings") or []:
            print(f"  - {finding['message']}")


def cmd_adapters_inspect(args: argparse.Namespace) -> int:
    adapter = _ADAPTER_REGISTRY.create(args.adapter)
    manifest = adapter.capability_manifest(args.repo)
    payload, policy_compatible = _manifest_with_policy(manifest, args.policy)
    handshake = _adapter_handshake(args.adapter, repo=args.repo)
    payload["registry_handshake"] = handshake.to_dict()
    compatible = policy_compatible and handshake.compatible
    if args.json:
        _write_json(payload)
    else:
        _print_adapter_manifest(payload)
        _print_adapter_handshake(payload["registry_handshake"])
    return 0 if compatible else 2


def cmd_adapters_list(args: argparse.Namespace) -> int:
    payload = _ADAPTER_REGISTRY.list_payload(
        project_root=args.repo, inspect=args.inspect
    )
    if args.json:
        _write_json(payload)
    else:
        print(
            "Claim Plane adapter registry "
            f"(protocols {', '.join(payload['core_protocol_versions'])})"
        )
        for adapter in payload["adapters"]:
            pin = "pinned" if adapter.get("pinned") else "unpinned"
            print(
                f"{adapter['name']}: {adapter['source']} · "
                f"{adapter['protocol_range']} · {pin}"
            )
            handshake = adapter.get("handshake")
            if isinstance(handshake, Mapping):
                status = "OK" if handshake.get("compatible") else "FAILED"
                print(
                    f"  negotiated={handshake.get('negotiated_protocol_version')} "
                    "runtime="
                    f"{(handshake.get('runtime') or {}).get('version') or 'not detected'} "
                    f"compatibility={status}"
                )
        for finding in payload.get("discovery_findings") or []:
            print(f"[{str(finding['severity']).upper()}] {finding['message']}")
    return 0


def cmd_adapters_pin(args: argparse.Namespace) -> int:
    if args.clear:
        removed = remove_adapter_pin(args.repo, args.adapter)
        payload = {
            "adapter": args.adapter,
            "removed": removed,
            "path": str(
                Path(args.repo).expanduser().resolve()
                / ".claim-plane"
                / "adapters"
                / "pins"
                / f"{args.adapter}.json"
            ),
        }
        if args.json:
            _write_json(payload)
        else:
            print(
                f"Removed adapter pin for {args.adapter}."
                if removed
                else f"No adapter pin exists for {args.adapter}."
            )
        return 0
    pin, path = _ADAPTER_REGISTRY.pin(args.adapter, project_root=args.repo)
    payload = pin.to_dict()
    payload["path"] = str(path)
    if args.json:
        _write_json(payload)
    else:
        print(
            f"Pinned {pin.adapter} {pin.adapter_version} to protocol "
            f"{pin.protocol_version}."
        )
        print(f"Runtime: {pin.runtime_name} {pin.runtime_version or 'not detected'}")
        print(f"Wrote {path}")
    return 0


def cmd_adapters_doctor(args: argparse.Namespace) -> int:
    if args.adapter != "codex":
        handshake = _adapter_handshake(args.adapter, repo=args.repo)
        payload = handshake.to_dict()
        if args.json:
            _write_json(payload)
        else:
            _print_adapter_handshake(payload)
        return 0 if handshake.compatible else 2
    return cmd_doctor_codex(args)


def _print_conformance_report(payload: Mapping[str, Any]) -> None:
    runtime = payload.get("runtime") or {}
    print(
        f"Adapter conformance: {payload['adapter']} {payload['adapter_version']} "
        f"({runtime.get('name', 'unknown')} {runtime.get('version') or 'not detected'})"
    )
    for result in payload.get("scenarios") or []:
        print(
            f"[{str(result['status']).upper():7}] "
            f"{result['scenario']}: {result['detail']}"
        )
    summary = payload.get("summary") or {}
    print(
        f"Scenarios: {summary.get('passed', 0)} passed, "
        f"{summary.get('failed', 0)} failed, "
        f"{summary.get('skipped', 0)} skipped"
    )
    print(
        "Guarantee claims: verified"
        if summary.get("claims_verified")
        else "Guarantee claims: unverified"
    )
    print("Compatibility: PASS" if payload.get("compatible") else "Compatibility: FAIL")


def cmd_adapters_conformance(args: argparse.Namespace) -> int:
    driver = (
        CodexConformanceDriver(args.workdir)
        if args.adapter == "codex"
        else ReferenceConformanceDriver(args.workdir)
    )
    report = run_adapter_conformance(driver)
    payload = report.to_dict()
    if args.out:
        _write_json(payload, args.out)
    elif args.json:
        _write_json(payload)
    else:
        _print_conformance_report(payload)
    return 0 if report.compatible else 2


def cmd_doctor_codex(args: argparse.Namespace) -> int:
    handshake = _adapter_handshake("codex", repo=args.repo)
    response = _CODEX_ADAPTER.doctor(
        _codex_request(AdapterOperation.DOCTOR, repo=args.repo)
    )
    report = dict(response.payload)
    effective_policy = _effective_policy_for_cli(args.repo, args.policy)
    report["effective_policy"] = effective_policy.to_dict()
    manifest_data = report.get("adapter_manifest")
    policy_compatible = True
    if isinstance(manifest_data, Mapping):
        manifest = AdapterCapabilityManifest.from_dict(manifest_data)
        manifest_payload, policy_compatible = _manifest_with_policy(
            manifest, effective_policy.name
        )
        report["adapter_manifest"] = manifest_payload
        report["policy_compatibility"] = manifest_payload[
            "policy_compatibility"
        ]
    report["registry_handshake"] = handshake.to_dict()
    report["ready"] = (
        bool(report.get("ready")) and policy_compatible and handshake.compatible
    )
    if args.json:
        _write_json(report)
    else:
        print(f"Claim Plane Codex enrollment — {report['root']}")
        for item in report["checks"]:
            status = str(item["status"]).upper()
            detail = str(item.get("detail") or "")
            print(f"[{status:7}] {item['name']}: {detail}")
            missing = item.get("missing_events")
            if missing:
                print(f"          missing events: {', '.join(missing)}")
        if report.get("codex_version"):
            print(f"Codex: {report['codex_version']}")
        if isinstance(report.get("effective_policy"), Mapping):
            _print_effective_policy(report["effective_policy"])
        if isinstance(report.get("adapter_manifest"), Mapping):
            _print_adapter_manifest(report["adapter_manifest"])
        _print_adapter_handshake(report["registry_handshake"])
        print("Status: ready" if report["ready"] else "Status: action required")
    return 0 if report["ready"] else 2


def cmd_run(args: argparse.Namespace) -> int:
    handshake = _adapter_handshake(args.adapter, repo=args.repo)
    adapter = _ADAPTER_REGISTRY.create(args.adapter)
    try:
        result = run_controlled_task(
            args.task,
            root=args.repo,
            adapter=adapter,
            handshake=handshake,
            policy=args.policy,
            timeout_seconds=args.timeout,
            acceptance_timeout=args.acceptance_timeout,
            model=args.model,
            quiet=args.json,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except ControlledRunPreflightError as exc:
        if args.json:
            _write_json(
                {
                    "protocol": "claim-plane.controlled-run-error.v1",
                    "code": "preflight_failed",
                    "message": str(exc),
                }
            )
        else:
            print(f"Controlled run could not start: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _write_json(result.to_dict(), args.out)
    elif args.out:
        _write_json(result.to_dict(), args.out)
    return result.exit_code


def cmd_codex_hook(args: argparse.Namespace) -> int:
    del args
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Codex hook input must be a JSON object")
    controlled_run_id = os.environ.get(CONTROLLED_RUN_ENV)
    if controlled_run_id:
        payload["_claim_plane_run_id"] = controlled_run_id
    controlled_policy = os.environ.get(CONTROLLED_POLICY_ENV)
    if controlled_policy:
        payload["_claim_plane_policy"] = controlled_policy
    controlled_policy_manifest = os.environ.get(CONTROLLED_POLICY_MANIFEST_ENV)
    if controlled_policy_manifest:
        manifest_payload = json.loads(controlled_policy_manifest)
        if not isinstance(manifest_payload, Mapping):
            raise ValueError("controlled policy manifest must be a JSON object")
        effective = EffectivePolicy.from_dict(manifest_payload)
        if controlled_policy and effective.name != controlled_policy:
            raise ValueError("controlled policy name and manifest do not match")
        payload["_claim_plane_policy_manifest"] = effective.to_dict()
    if payload.get("hook_event_name") == "SessionStart":
        repo = str(payload.get("cwd") or ".")
        _adapter_handshake("codex", repo=repo, require_compatible=True)
    return _CODEX_ADAPTER.dispatch_hook(payload, output=sys.stdout)


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
    response = _CODEX_ADAPTER.propose_intent(
        _codex_request(
            AdapterOperation.PROPOSE_INTENT,
            repo=args.repo,
            session_id=args.session_id,
            payload={"proposal": proposal},
        )
    )
    result = dict(response.payload)
    _write_json(result)
    return 0 if result["allowed"] else 2


def cmd_codex_intent_abandon(args: argparse.Namespace) -> int:
    intent_id, intent_version = _codex_binding(args.repo, args.session_id)
    response = _CODEX_ADAPTER.cancel(
        _codex_request(
            AdapterOperation.CANCEL,
            repo=args.repo,
            session_id=args.session_id,
            intent_id=intent_id,
            intent_version=intent_version,
            stable=True,
        )
    )
    result = dict(response.payload)
    _write_json(result)
    return 0


def cmd_codex_intent_amend(args: argparse.Namespace) -> int:
    intent_id, intent_version = _codex_binding(args.repo, args.session_id)
    payload = {"ticket_id": args.ticket, "reason": args.reason}
    response = _CODEX_ADAPTER.request_amendment(
        _codex_request(
            AdapterOperation.REQUEST_AMENDMENT,
            repo=args.repo,
            session_id=args.session_id,
            intent_id=intent_id,
            intent_version=intent_version,
            payload=payload,
            stable=True,
        )
    )
    result = dict(response.payload)
    _write_json(result)
    return 0 if result["allowed"] else 2


def cmd_codex_intent_verify(args: argparse.Namespace) -> int:
    intent_id, intent_version = _codex_binding(args.repo, args.session_id)
    response = _CODEX_ADAPTER.verify_completion(
        _codex_request(
            AdapterOperation.VERIFY_COMPLETION,
            repo=args.repo,
            session_id=args.session_id,
            intent_id=intent_id,
            intent_version=intent_version,
            timeout_seconds=args.acceptance_timeout,
        )
    )
    result = dict(response.payload)
    _write_json(result)
    return 0 if result.get("verified") else 2


def cmd_codex_intent_status(args: argparse.Namespace) -> int:
    response = _CODEX_ADAPTER.inspect(
        _codex_request(
            AdapterOperation.INSPECT, repo=args.repo, session_id=args.session_id
        )
    )
    result = dict(response.payload)
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


def _print_operator_snapshot(payload: Mapping[str, Any]) -> None:
    print(f"Swarm {payload['session_id']}: {str(payload['phase']).upper()}")
    print(f"Task: {payload['root_task']['title']}")
    usage = payload["usage"]
    budget = payload["budget"]
    print(
        "Workers: "
        f"{usage['active_workers']}/{budget['max_active_workers']} active; "
        f"runs={usage['runs']}; tokens={usage['total_tokens']}"
    )
    scheduler = payload.get("scheduler") or {}
    if scheduler:
        dispatchable = ", ".join(scheduler.get("dispatchable_work_ids") or ()) or "none"
        print(f"Dispatchable: {dispatchable}")
    merge = payload.get("merge_queue") or {}
    if merge:
        print(f"Merge queue: {merge.get('status', 'unknown')}")
    verification = payload.get("verification") or {}
    if verification:
        print(f"Verification: {verification.get('status', 'unknown')}")
    print(f"{'WORK':20} {'SCHEDULER':16} {'RUN':18} {'MERGE':12} {'NEXT'}")
    for item in payload["work"]:
        run_state = item["run_state"] or "-"
        merge_state = item["merge_state"] or "-"
        print(
            f"{item['work_id'][:20]:20} "
            f"{item['scheduler_state'][:16]:16} "
            f"{run_state[:18]:18} "
            f"{merge_state[:12]:12} "
            f"{item['next_action']}"
        )


def cmd_swarm_status(args: argparse.Namespace) -> int:
    payload = get_swarm_operator_snapshot(args.repo, args.session_id)
    if args.json or getattr(args, "out", None):
        _write_json(payload, getattr(args, "out", None))
    else:
        _print_operator_snapshot(payload)
    failure_phases = {"failed", "integration_conflict", "replan_required"}
    return 0 if payload["phase"] not in failure_phases else 2


def _operator_event_printer(event: Mapping[str, Any]) -> None:
    stage = str(event.get("stage") or "operator")
    if stage == "dispatch":
        print("Dispatch: " + ", ".join(event.get("work_ids") or ()))
    elif stage == "worker":
        print(
            f"Worker {event.get('work_id')}: {event.get('state')} "
            f"({event.get('tokens', 0)} tokens)"
        )
    elif stage == "worker_error":
        print(f"Worker {event.get('work_id')}: ERROR — {event.get('error')}")
    else:
        summary = event.get("summary") or {}
        status = summary.get("status") or summary.get("state") or "ready"
        print(f"{stage.replace('_', ' ').title()}: {status}")


def cmd_swarm_start(args: argparse.Namespace) -> int:
    session_id = args.session_id
    if args.spec:
        created = create_swarm_session(
            args.repo,
            spec=_read_json(args.spec),
            session_id=session_id,
            base_revision=args.base,
        )
        session_id = str(created["session"]["session_id"])
    if not session_id:
        raise ValueError("session_id is required unless --spec creates one")
    result = start_swarm_session(
        args.repo,
        session_id,
        codex_binary=args.codex_bin,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout,
        token_limit=args.max_tokens,
        acceptance_timeout=args.acceptance_timeout,
        run_acceptance=not args.no_acceptance,
        prepare_only=args.prepare_only,
        reset_failed_worktrees=args.reset_failed_worktrees,
        max_cycles=args.max_cycles,
        on_event=None if args.json or args.out else _operator_event_printer,
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print()
        _print_operator_snapshot(result["snapshot"])
        if result.get("errors"):
            print("Action required:")
            for error in result["errors"]:
                print(f"  {error.get('work_id', '-')}: {error.get('error')}")
    return 0 if result["status"] in {"verified", "prepared"} else 2


def cmd_swarm_logs(args: argparse.Namespace) -> int:
    if args.follow and (args.json or args.out):
        raise ValueError("--follow cannot be combined with --json or --out")
    seen: set[tuple[str, str, str | None, str | None, str]] = set()
    while True:
        events = list_swarm_operator_logs(
            args.repo,
            args.session_id,
            work_id=args.work_id,
            limit=args.limit,
            include_codex_events=not args.no_codex_events,
        )
        fresh = []
        for event in events:
            key = (
                str(event["timestamp"]),
                str(event["event"]),
                event.get("work_id"),
                event.get("run_id"),
                json.dumps(event.get("metadata") or {}, sort_keys=True),
            )
            if key not in seen:
                seen.add(key)
                fresh.append(event)
        if args.json or args.out:
            _write_json(fresh, args.out)
        else:
            for event in fresh:
                target = event.get("work_id") or "-"
                detail = f"  {event['detail']}" if event.get("detail") else ""
                print(f"{event['timestamp']}  {target:20}  {event['event']}{detail}")
        if not args.follow:
            return 0
        snapshot = get_swarm_operator_snapshot(args.repo, args.session_id)
        if snapshot["session_state"] in {"completed", "failed", "cancelled"}:
            return 0
        time.sleep(args.interval)


def cmd_swarm_demo(args: argparse.Namespace) -> int:
    result = create_and_run_swarm_demo(args.directory, keep=True)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(f"Demo repository: {result['repository']}")
        _print_operator_snapshot(result["result"]["snapshot"])
        print("Inspect evidence with:")
        print(f"  claim-plane swarm evidence swm-demo --repo {result['repository']}")
    return 0 if result["result"]["verified"] else 2


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
        print(f"Concurrency plan v{result['plan_version']}: {summary['status']}")
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


def cmd_swarm_admit(args: argparse.Namespace) -> int:
    result = admit_swarm_session(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        action = "Stored" if result["created"] else "Reused"
        print(
            f"{action} shared admission v{result['admission_version']} for "
            f"{result['session_id']}."
        )
        print(
            f"Status: {summary['status']}; admitted={summary['admitted']}; "
            f"blocked={summary['blocked']}"
        )
        for item in result["shared_admission"]["admissions"]:
            label = "ADMITTED" if item["allowed"] else "BLOCKED"
            deps = ", ".join(item["effective_dependencies"]) or "none"
            print(f"  {item['work_id']}: {label} ({item['kind']}); depends_on={deps}")
    return 0 if result["summary"]["status"] == "ready" else 2


def cmd_swarm_admission(args: argparse.Namespace) -> int:
    result = get_swarm_admission(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        print(
            f"Shared admission v{result['admission_version']} for "
            f"{result['session_id']}: {summary['status']}"
        )
        print(
            f"Admitted: {summary['admitted']}; blocked: {summary['blocked']}; "
            f"fingerprint={result['admission_fingerprint'][:20]}"
        )
        for item in result["shared_admission"]["admissions"]:
            print(
                f"  {item['work_id']}: {item['kind']} "
                f"allowed={str(item['allowed']).lower()}"
            )
    return 0 if result["summary"]["status"] == "ready" else 2


def cmd_swarm_scheduler(args: argparse.Namespace) -> int:
    result = get_swarm_scheduler(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        print(f"Scheduler for {result['session_id']}: {summary['status']}")
        print(
            f"Workers: {summary['active_workers']}/"
            f"{summary['max_active_workers']} active; "
            f"slots={summary['available_slots']}"
        )
        runnable = ", ".join(summary["dispatchable_work_ids"]) or "none"
        print(f"Dispatchable: {runnable}")
        for item in result["scheduler"]["work"]:
            print(f"  {item['work_id']}: {item['state']} — {item['detail']}")
    return 0 if result["summary"]["status"] != "replan_required" else 2


def cmd_swarm_merge_plan(args: argparse.Namespace) -> int:
    result = plan_swarm_merge_queue(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        action = "Stored" if result["created"] else "Refreshed"
        print(
            f"{action} deterministic merge queue v{result['queue_version']} "
            f"for {result['session_id']}."
        )
        print(
            f"Status: {summary['status']}; ready="
            f"{', '.join(summary['ready_work_ids']) or 'none'}"
        )
        print(f"Integration head: {summary['integration_head']}")
    return 0 if result["summary"]["status"] != "conflict" else 2


def cmd_swarm_merge_queue(args: argparse.Namespace) -> int:
    result = get_swarm_merge_queue(
        args.repo, args.session_id, refresh=not args.no_refresh
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        print(
            f"Merge queue v{result['queue_version']} for "
            f"{result['session_id']}: {summary['status']}"
        )
        for entry in result["merge_queue"]["entries"]:
            print(f"  {entry['order']}: {entry['work_id']} — {entry['state']}")
            if entry["detail"]:
                print(f"    {entry['detail']}")
        print(f"Integration head: {summary['integration_head']}")
    return 0 if result["summary"]["status"] != "conflict" else 2


def cmd_swarm_merge_next(args: argparse.Namespace) -> int:
    result = integrate_next_swarm_result(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        entry = result["entry"]
        print(
            f"Merge {entry['work_id']}: {entry['state']} "
            f"into the managed integration branch."
        )
        if entry["conflict_paths"]:
            print("Conflicts: " + ", ".join(entry["conflict_paths"]))
        print(f"Integration head: {result['summary']['integration_head']}")
    return 0 if result["integrated"] else 2


def cmd_swarm_merge_all(args: argparse.Namespace) -> int:
    result = drain_swarm_merge_queue(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(
            f"Merge queue for {result['session_id']}: "
            f"{result['summary']['status']}; integrated={len(result['integrated'])}."
        )
        for entry in result["integrated"]:
            print(f"  {entry['work_id']}: {entry['state']}")
        print(f"Integration head: {result['summary']['integration_head']}")
    return 0 if result["summary"]["status"] != "conflict" else 2


def cmd_swarm_verify(args: argparse.Namespace) -> int:
    result = verify_swarm_session(
        args.repo,
        args.session_id,
        run_acceptance=not args.no_acceptance,
        acceptance_timeout=args.acceptance_timeout,
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        label = "SWARM VERIFIED" if summary["verified"] else "SWARM UNVERIFIED"
        print(f"{label}: {result['session_id']}")
        print(
            f"Work: {summary['work_verified']}/{summary['work_items']} verified; "
            f"changed_paths={summary['changed_paths']}"
        )
        print(
            f"Root acceptance={'passed' if summary['root_acceptance_passed'] else 'failed'}; "
            f"snapshot_integrity={str(summary['snapshot_integrity_ok']).lower()}"
        )
        print(
            f"Errors={summary['errors']}; warnings={summary['warnings']}; "
            f"fingerprint={result['verification_fingerprint'][:20]}"
        )
        for item in result["verification"]["work_evidence"]:
            print(
                f"  {item['work_id']}: "
                f"{'VERIFIED' if item['verified'] else 'UNVERIFIED'}; "
                f"paths={len(item['changed_paths'])}"
            )
    return 0 if result["summary"]["verified"] else 2


def cmd_swarm_evidence(args: argparse.Namespace) -> int:
    result = get_swarm_verification(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        summary = result["summary"]
        print(
            f"Swarm evidence v{result['verification_version']} for "
            f"{result['session_id']}: {summary['status']}"
        )
        print(
            f"Work: {summary['work_verified']}/{summary['work_items']}; "
            f"errors={summary['errors']}; warnings={summary['warnings']}"
        )
        print(f"Integration head: {result['verification']['integration_head']}")
        print(f"Fingerprint: {result['verification_fingerprint']}")
    return 0 if result["summary"]["verified"] else 2


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
    result = validate_budget_policy(_read_json(args.policy), work_items=args.work_items)
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
            print(f"  {record['work_id']}: {item['health']}  {record['worktree_path']}")
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
        0 if record.state.value == "succeeded" else (2 if record.state.terminal else 0)
    )


def cmd_swarm_cancel_codex(args: argparse.Namespace) -> int:
    record = cancel_codex_run(args.repo, args.run_id)
    if args.json or args.out:
        _write_json(record.to_dict(), args.out)
    else:
        print(f"Codex run {record.run_id}: {record.state.value}.")
    return 0


def cmd_swarm_recovery_status(args: argparse.Namespace) -> int:
    result = inspect_swarm_recovery(
        args.repo, args.session_id, stale_after_seconds=args.stale_after
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(
            f"Recovery status for {result['session_id']}: "
            f"session={result['session_state']}"
        )
        if not result["active_runs"]:
            print("  no active worker records")
        for item in result["active_runs"]:
            print(
                f"  {item['run_id']}  {item['work_id']:20}  "
                f"{item['health']:9}  {item['detail']}"
            )
    unhealthy = result["summary"]["stale"] + result["summary"]["lost"]
    return 0 if unhealthy == 0 else 2


def cmd_swarm_recover(args: argparse.Namespace) -> int:
    result = recover_swarm_session(
        args.repo,
        args.session_id,
        stale_after_seconds=args.stale_after,
        terminate_stale=args.terminate_stale,
    )
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(
            f"Recovered {result['recovered_count']} run(s) for "
            f"{result['session_id']}; session={result['session_state']}."
        )
        if result["stale_requires_termination"]:
            print(
                "Live processes with expired leases were left untouched: "
                + ", ".join(result["stale_requires_termination"])
            )
            print("Repeat with --terminate-stale to reclaim them explicitly.")
    return 0 if not result["stale_requires_termination"] else 2


def cmd_swarm_pause(args: argparse.Namespace) -> int:
    result = pause_swarm_session(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(f"Swarm session {args.session_id} paused.")
    return 0


def cmd_swarm_resume(args: argparse.Namespace) -> int:
    result = resume_swarm_session(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(f"Swarm session {args.session_id}: {result['session']['state']}.")
    return 0


def cmd_swarm_cancel(args: argparse.Namespace) -> int:
    result = cancel_swarm_session(args.repo, args.session_id)
    if args.json or args.out:
        _write_json(result, args.out)
    else:
        print(
            f"Swarm session {args.session_id} cancelled; "
            f"signalled {len(result['signalled_run_ids'])} active worker(s)."
        )
    return 0


def cmd_swarm_recovery_events(args: argparse.Namespace) -> int:
    events = list_swarm_recovery_events(args.repo, args.session_id)
    payload = [event.to_dict() for event in events]
    if args.json or args.out:
        _write_json(payload, args.out)
    elif not events:
        print("No recovery events.")
    else:
        for event in events:
            target = event.run_id or event.work_id or "-"
            print(f"{event.created_at}  {event.action:24}  {target}")
    return 0


def cmd_swarm_replace_codex(args: argparse.Namespace) -> int:
    record = replace_codex_worker(
        args.repo,
        args.session_id,
        args.work_id,
        replaced_run_id=args.run_id,
        reset_worktree=args.reset_worktree,
        codex_binary=args.codex_bin,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout,
        token_limit=args.max_tokens,
    )
    if args.json or args.out:
        _write_json(record.to_dict(), args.out)
    else:
        print(
            f"Replacement run {record.run_id}: {record.state.value} "
            f"for {record.session_id}/{record.work_id}."
        )
        print(f"Replaced: {record.replacement_of_run_id}")
    return 0 if record.state.value == "succeeded" else 2


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

    reset = sub.add_parser(
        "reset",
        help=(
            "Remove Claim Plane-owned local state without touching repository files."
        ),
    )
    reset.add_argument("--repo", default=".")
    reset.add_argument(
        "--remove-config",
        action="store_true",
        help="Also remove .claim-plane/config.yaml.",
    )
    reset.add_argument("--json", action="store_true")
    reset.set_defaults(func=cmd_reset)

    adapters = sub.add_parser(
        "adapters", help="Inspect coding-agent adapter capabilities and guarantees."
    )
    adapters_sub = adapters.add_subparsers(
        dest="adapters_command", required=True
    )
    adapters_list = adapters_sub.add_parser(
        "list", help="List built-in and discovered external adapters."
    )
    adapters_list.add_argument("--repo", default=".")
    adapters_list.add_argument(
        "--inspect",
        action="store_true",
        help="Run protocol negotiation and pin checks for each adapter.",
    )
    adapters_list.add_argument("--json", action="store_true")
    adapters_list.set_defaults(func=cmd_adapters_list)

    adapters_inspect = adapters_sub.add_parser(
        "inspect", help="Show one machine-readable adapter capability manifest."
    )
    adapters_inspect.add_argument(
        "adapter",
        choices=tuple(item.name for item in _ADAPTER_REGISTRY.registrations()),
    )
    adapters_inspect.add_argument("--repo", default=".")
    adapters_inspect.add_argument(
        "--policy", choices=POLICY_NAMES
    )
    adapters_inspect.add_argument("--json", action="store_true")
    adapters_inspect.set_defaults(func=cmd_adapters_inspect)

    adapters_conformance = adapters_sub.add_parser(
        "conformance",
        help="Run the reusable adapter compatibility suite in isolated fixtures.",
    )
    adapters_conformance.add_argument(
        "adapter", choices=("codex", "reference")
    )
    adapters_conformance.add_argument(
        "--workdir",
        default=None,
        help="Optional directory for isolated conformance fixtures.",
    )
    adapters_conformance.add_argument("--json", action="store_true")
    adapters_conformance.add_argument("--out", default=None)
    adapters_conformance.set_defaults(func=cmd_adapters_conformance)

    adapters_doctor = adapters_sub.add_parser(
        "doctor", help="Negotiate versions and diagnose adapter compatibility."
    )
    adapters_doctor.add_argument(
        "adapter",
        choices=tuple(item.name for item in _ADAPTER_REGISTRY.registrations()),
    )
    adapters_doctor.add_argument("--repo", default=".")
    adapters_doctor.add_argument(
        "--policy", choices=POLICY_NAMES
    )
    adapters_doctor.add_argument("--json", action="store_true")
    adapters_doctor.set_defaults(func=cmd_adapters_doctor)

    adapters_pin = adapters_sub.add_parser(
        "pin", help="Pin an adapter, runtime, and negotiated protocol for this project."
    )
    adapters_pin.add_argument(
        "adapter",
        choices=tuple(item.name for item in _ADAPTER_REGISTRY.registrations()),
    )
    adapters_pin.add_argument("--repo", default=".")
    adapters_pin.add_argument(
        "--clear", action="store_true", help="Remove the project-local adapter pin."
    )
    adapters_pin.add_argument("--json", action="store_true")
    adapters_pin.set_defaults(func=cmd_adapters_pin)

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
        "doctor", help="Inspect project and coding-agent enrollment health."
    )
    doctor.add_argument("--repo", default=".")
    doctor.add_argument(
        "--policy", choices=POLICY_NAMES
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor_codex, connector="codex")
    doctor_sub = doctor.add_subparsers(dest="doctor_connector")
    doctor_codex_parser = doctor_sub.add_parser(
        "codex", help="Inspect the project-local Codex lifecycle bridge."
    )
    doctor_codex_parser.add_argument("--repo", default=".")
    doctor_codex_parser.add_argument(
        "--policy", choices=POLICY_NAMES
    )
    doctor_codex_parser.add_argument("--json", action="store_true")
    doctor_codex_parser.set_defaults(func=cmd_doctor_codex, connector="codex")


    policy_parser = sub.add_parser(
        "policy", help="Inspect policy presets and classify repository risk."
    )
    policy_sub = policy_parser.add_subparsers(
        dest="policy_command", required=True
    )
    policy_inspect = policy_sub.add_parser(
        "inspect", help="Show effective policy semantics and adapter compatibility."
    )
    policy_inspect.add_argument("--repo", default=".")
    policy_inspect.add_argument("--adapter", default="codex")
    policy_inspect.add_argument("--policy", choices=POLICY_NAMES)
    policy_inspect.add_argument("--json", action="store_true")
    policy_inspect.set_defaults(func=cmd_policy_inspect)

    policy_classify = policy_sub.add_parser(
        "classify", help="Classify repository-relative paths under a policy."
    )
    policy_classify.add_argument("paths", nargs="+")
    policy_classify.add_argument("--repo", default=".")
    policy_classify.add_argument("--policy", choices=POLICY_NAMES)
    policy_classify.add_argument("--json", action="store_true")
    policy_classify.set_defaults(func=cmd_policy_classify)


    controlled_run = sub.add_parser(
        "run",
        help="Run one coding task under adapter authority and final Git verification.",
    )
    controlled_run.add_argument("task")
    controlled_run.add_argument("--repo", default=".")
    controlled_run.add_argument(
        "--adapter",
        default="codex",
        choices=tuple(item.name for item in _ADAPTER_REGISTRY.registrations()),
    )
    controlled_run.add_argument(
        "--policy", choices=POLICY_NAMES
    )
    controlled_run.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="Maximum runtime wall time in seconds (default: 3600).",
    )
    controlled_run.add_argument(
        "--acceptance-timeout",
        type=float,
        default=300.0,
        help="Maximum time for final acceptance verification (default: 300).",
    )
    controlled_run.add_argument("--model", default=None)
    controlled_run.add_argument("--json", action="store_true")
    controlled_run.add_argument("--out", default=None)
    controlled_run.set_defaults(func=cmd_run)

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
        "status", help="Show one compact operator view across the swarm lifecycle."
    )
    swarm_status.add_argument("session_id")
    swarm_status.add_argument("--repo", default=".")
    swarm_status.add_argument("--json", action="store_true")
    swarm_status.add_argument("--out")
    swarm_status.set_defaults(func=cmd_swarm_status)

    swarm_start = swarm_sub.add_parser(
        "start",
        help="Prepare, execute, integrate, and verify a bounded Codex swarm.",
    )
    swarm_start.add_argument("session_id", nargs="?")
    swarm_start.add_argument(
        "--spec", help="Create the session from this JSON spec before starting."
    )
    swarm_start.add_argument("--base", default="HEAD")
    swarm_start.add_argument("--codex-bin", default="codex")
    swarm_start.add_argument("--model")
    swarm_start.add_argument("--reasoning-effort")
    swarm_start.add_argument("--timeout", type=int)
    swarm_start.add_argument("--max-tokens", type=int)
    swarm_start.add_argument("--acceptance-timeout", type=int, default=300)
    swarm_start.add_argument("--no-acceptance", action="store_true")
    swarm_start.add_argument("--prepare-only", action="store_true")
    swarm_start.add_argument("--reset-failed-worktrees", action="store_true")
    swarm_start.add_argument("--max-cycles", type=int, default=100)
    swarm_start.add_argument("--repo", default=".")
    swarm_start.add_argument("--json", action="store_true")
    swarm_start.add_argument("--out")
    swarm_start.set_defaults(func=cmd_swarm_start)

    swarm_logs = swarm_sub.add_parser(
        "logs",
        help=(
            "Show one normalized timeline across workers, recovery, merge, "
            "and verification."
        ),
    )
    swarm_logs.add_argument("session_id")
    swarm_logs.add_argument("--work-id")
    swarm_logs.add_argument("--limit", type=int, default=200)
    swarm_logs.add_argument("--no-codex-events", action="store_true")
    swarm_logs.add_argument("--follow", action="store_true")
    swarm_logs.add_argument("--interval", type=float, default=2.0)
    swarm_logs.add_argument("--repo", default=".")
    swarm_logs.add_argument("--json", action="store_true")
    swarm_logs.add_argument("--out")
    swarm_logs.set_defaults(func=cmd_swarm_logs)

    swarm_demo = swarm_sub.add_parser(
        "demo", help="Run an offline deterministic three-worker SWARM VERIFIED demo."
    )
    swarm_demo.add_argument("--directory")
    swarm_demo.add_argument("--json", action="store_true")
    swarm_demo.add_argument("--out")
    swarm_demo.set_defaults(func=cmd_swarm_demo)

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

    swarm_admit = swarm_sub.add_parser(
        "admit",
        help="Compute and persist shared admission for all swarm work items.",
    )
    swarm_admit.add_argument("session_id")
    swarm_admit.add_argument("--repo", default=".")
    swarm_admit.add_argument("--json", action="store_true")
    swarm_admit.add_argument("--out")
    swarm_admit.set_defaults(func=cmd_swarm_admit)

    swarm_admission = swarm_sub.add_parser(
        "admission",
        help="Inspect the persisted shared swarm admission plan.",
    )
    swarm_admission.add_argument("session_id")
    swarm_admission.add_argument("--repo", default=".")
    swarm_admission.add_argument("--json", action="store_true")
    swarm_admission.add_argument("--out")
    swarm_admission.set_defaults(func=cmd_swarm_admission)

    swarm_scheduler = swarm_sub.add_parser(
        "scheduler",
        help="Show dynamically runnable, blocked, active, and completed work.",
    )
    swarm_scheduler.add_argument("session_id")
    swarm_scheduler.add_argument("--repo", default=".")
    swarm_scheduler.add_argument("--json", action="store_true")
    swarm_scheduler.add_argument("--out")
    swarm_scheduler.set_defaults(func=cmd_swarm_scheduler)

    swarm_merge_plan = swarm_sub.add_parser(
        "merge-plan",
        help="Compute or refresh the deterministic integration queue.",
    )
    swarm_merge_plan.add_argument("session_id")
    swarm_merge_plan.add_argument("--repo", default=".")
    swarm_merge_plan.add_argument("--json", action="store_true")
    swarm_merge_plan.add_argument("--out")
    swarm_merge_plan.set_defaults(func=cmd_swarm_merge_plan)

    swarm_merge_queue = swarm_sub.add_parser(
        "merge-queue",
        help="Inspect deterministic integration order and durable entry states.",
    )
    swarm_merge_queue.add_argument("session_id")
    swarm_merge_queue.add_argument("--repo", default=".")
    swarm_merge_queue.add_argument("--no-refresh", action="store_true")
    swarm_merge_queue.add_argument("--json", action="store_true")
    swarm_merge_queue.add_argument("--out")
    swarm_merge_queue.set_defaults(func=cmd_swarm_merge_queue)

    swarm_merge_next = swarm_sub.add_parser(
        "merge-next",
        help="Integrate the next ready worker result into the managed branch.",
    )
    swarm_merge_next.add_argument("session_id")
    swarm_merge_next.add_argument("--repo", default=".")
    swarm_merge_next.add_argument("--json", action="store_true")
    swarm_merge_next.add_argument("--out")
    swarm_merge_next.set_defaults(func=cmd_swarm_merge_next)

    swarm_merge_all = swarm_sub.add_parser(
        "merge-all",
        help="Drain all currently ready merge entries until waiting or conflict.",
    )
    swarm_merge_all.add_argument("session_id")
    swarm_merge_all.add_argument("--repo", default=".")
    swarm_merge_all.add_argument("--json", action="store_true")
    swarm_merge_all.add_argument("--out")
    swarm_merge_all.set_defaults(func=cmd_swarm_merge_all)

    swarm_verify = swarm_sub.add_parser(
        "verify",
        help="Verify integrated worker evidence and root acceptance.",
    )
    swarm_verify.add_argument("session_id")
    swarm_verify.add_argument("--repo", default=".")
    swarm_verify.add_argument("--no-acceptance", action="store_true")
    swarm_verify.add_argument("--acceptance-timeout", type=int, default=300)
    swarm_verify.add_argument("--json", action="store_true")
    swarm_verify.add_argument("--out")
    swarm_verify.set_defaults(func=cmd_swarm_verify)

    swarm_evidence = swarm_sub.add_parser(
        "evidence",
        help="Inspect the latest durable swarm verification report.",
    )
    swarm_evidence.add_argument("session_id")
    swarm_evidence.add_argument("--repo", default=".")
    swarm_evidence.add_argument("--json", action="store_true")
    swarm_evidence.add_argument("--out")
    swarm_evidence.set_defaults(func=cmd_swarm_evidence)

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
    swarm_replace_budget.add_argument("--expected-version", type=int, required=True)
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

    swarm_recovery_status = swarm_sub.add_parser(
        "recovery-status",
        help="Inspect active worker leases, processes, and orphan recovery state.",
    )
    swarm_recovery_status.add_argument("session_id")
    swarm_recovery_status.add_argument("--stale-after", type=int, default=30)
    swarm_recovery_status.add_argument("--repo", default=".")
    swarm_recovery_status.add_argument("--json", action="store_true")
    swarm_recovery_status.add_argument("--out")
    swarm_recovery_status.set_defaults(func=cmd_swarm_recovery_status)

    swarm_recover = swarm_sub.add_parser(
        "recover",
        help="Finalize provably lost workers and reopen interrupted verification.",
    )
    swarm_recover.add_argument("session_id")
    swarm_recover.add_argument("--stale-after", type=int, default=30)
    swarm_recover.add_argument("--terminate-stale", action="store_true")
    swarm_recover.add_argument("--repo", default=".")
    swarm_recover.add_argument("--json", action="store_true")
    swarm_recover.add_argument("--out")
    swarm_recover.set_defaults(func=cmd_swarm_recover)

    swarm_pause = swarm_sub.add_parser(
        "pause", help="Pause dispatch after all active workers have stopped."
    )
    swarm_pause.add_argument("session_id")
    swarm_pause.add_argument("--repo", default=".")
    swarm_pause.add_argument("--json", action="store_true")
    swarm_pause.add_argument("--out")
    swarm_pause.set_defaults(func=cmd_swarm_pause)

    swarm_resume = swarm_sub.add_parser(
        "resume", help="Resume a paused swarm without repeating completed work."
    )
    swarm_resume.add_argument("session_id")
    swarm_resume.add_argument("--repo", default=".")
    swarm_resume.add_argument("--json", action="store_true")
    swarm_resume.add_argument("--out")
    swarm_resume.set_defaults(func=cmd_swarm_resume)

    swarm_cancel = swarm_sub.add_parser(
        "cancel", help="Cancel a swarm session and signal its active workers."
    )
    swarm_cancel.add_argument("session_id")
    swarm_cancel.add_argument("--repo", default=".")
    swarm_cancel.add_argument("--json", action="store_true")
    swarm_cancel.add_argument("--out")
    swarm_cancel.set_defaults(func=cmd_swarm_cancel)

    swarm_recovery_events = swarm_sub.add_parser(
        "recovery-events", help="List durable recovery and replacement events."
    )
    swarm_recovery_events.add_argument("session_id")
    swarm_recovery_events.add_argument("--repo", default=".")
    swarm_recovery_events.add_argument("--json", action="store_true")
    swarm_recovery_events.add_argument("--out")
    swarm_recovery_events.set_defaults(func=cmd_swarm_recovery_events)

    swarm_replace_codex = swarm_sub.add_parser(
        "replace-codex",
        help="Start a fresh worker after rechecking current swarm authority.",
    )
    swarm_replace_codex.add_argument("session_id")
    swarm_replace_codex.add_argument("--work-id", required=True)
    swarm_replace_codex.add_argument("--run-id", required=True)
    swarm_replace_codex.add_argument("--reset-worktree", action="store_true")
    swarm_replace_codex.add_argument("--codex-bin", default="codex")
    swarm_replace_codex.add_argument("--model")
    swarm_replace_codex.add_argument("--reasoning-effort")
    swarm_replace_codex.add_argument("--timeout", type=int)
    swarm_replace_codex.add_argument("--max-tokens", type=int)
    swarm_replace_codex.add_argument("--repo", default=".")
    swarm_replace_codex.add_argument("--json", action="store_true")
    swarm_replace_codex.add_argument("--out")
    swarm_replace_codex.set_defaults(func=cmd_swarm_replace_codex)

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
