"""Minimal stdio MCP server for model-agnostic coding-agent integration."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from claim_plane.core import (
    AccessMode,
    ChangeIntent,
    ChangeManifest,
    Claim,
    ClaimType,
    Plane,
    ResourceKind,
)
from claim_plane.runtime import BrokerClient
from claim_plane.integration import (
    IntegrationRunSpec,
    append_observation,
    verify_evidence_file,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claim-plane"
SERVER_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    db_path: str = ".claim-plane/plane.db"
    semantic: bool = False
    lexicon_path: str | None = None
    exploratory: bool = False


def _object_schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "admit_change_intent",
            "description": "Atomically admit a structured change intent before implementation.",
            "inputSchema": _object_schema(["intent"], {"intent": {"type": "object"}}),
        },
        {
            "name": "amend_change_intent",
            "description": "Atomically amend an intent and invalidate affected dependent workers.",
            "inputSchema": _object_schema(
                ["intent"],
                {
                    "intent": {"type": "object"},
                    "expected_version": {"type": "integer", "minimum": 1},
                },
            ),
        },
        {
            "name": "get_worker_context",
            "description": "Return the bounded task, contracts, terminology, permissions, and coordination constraints.",
            "inputSchema": _object_schema(
                ["intent_id"], {"intent_id": {"type": "string"}}
            ),
        },
        {
            "name": "list_active_intents",
            "description": "List admitted or active change intents.",
            "inputSchema": _object_schema([], {}),
        },
        {
            "name": "heartbeat_intent",
            "description": "Renew an active intent lease.",
            "inputSchema": _object_schema(
                ["intent_id"],
                {
                    "intent_id": {"type": "string"},
                    "lease_seconds": {"type": "integer", "minimum": 1},
                },
            ),
        },
        {
            "name": "complete_intent",
            "description": "Mark an admitted/active intent completed after clean integration.",
            "inputSchema": _object_schema(
                ["intent_id"], {"intent_id": {"type": "string"}}
            ),
        },
        {
            "name": "release_intent",
            "description": "Release an abandoned or integrated intent.",
            "inputSchema": _object_schema(
                ["intent_id"], {"intent_id": {"type": "string"}}
            ),
        },
        {
            "name": "verify_change_manifest",
            "description": "Compare actual changed files and artifacts with an admitted intent.",
            "inputSchema": _object_schema(
                ["manifest"], {"manifest": {"type": "object"}}
            ),
        },
        {
            "name": "plan_targeted_repair",
            "description": "Verify a manifest and return the minimal deterministic repair actions.",
            "inputSchema": _object_schema(
                ["manifest"], {"manifest": {"type": "object"}}
            ),
        },
        {
            "name": "verify_git_worktree",
            "description": "Collect and verify the current Git worktree against an admitted intent.",
            "inputSchema": _object_schema(
                ["intent_id"],
                {
                    "intent_id": {"type": "string"},
                    "repo_path": {"type": "string"},
                    "run_acceptance": {"type": "boolean"},
                    "acceptance_timeout": {"type": "integer", "minimum": 1},
                },
            ),
        },
        {
            "name": "list_coordination_notices",
            "description": "List pending or all premise invalidation notices for one intent.",
            "inputSchema": _object_schema(
                ["intent_id"],
                {"intent_id": {"type": "string"}, "pending_only": {"type": "boolean"}},
            ),
        },
        {
            "name": "acknowledge_coordination_notice",
            "description": "Acknowledge one structured coordination notice.",
            "inputSchema": _object_schema(
                ["notice_id"], {"notice_id": {"type": "integer", "minimum": 1}}
            ),
        },
        {
            "name": "recommend_worker_tier",
            "description": "Recommend economy, standard, or frontier worker tier from declared risk.",
            "inputSchema": _object_schema(
                ["intent_id"], {"intent_id": {"type": "string"}}
            ),
        },
        {
            "name": "get_dependency_graph",
            "description": "Return the acyclic premise graph and producer-first topological order.",
            "inputSchema": _object_schema([], {}),
        },
        {
            "name": "run_integration",
            "description": "Freeze worker snapshots, verify exact patches, create a verified integration commit, and execute bounded repair adapters.",
            "inputSchema": _object_schema(["spec"], {"spec": {"type": "object"}}),
        },
        {
            "name": "record_observed_access",
            "description": "Append a real tool read/write event to a worker observation trace.",
            "inputSchema": _object_schema(
                ["trace_path", "mode", "kind", "identifier"],
                {
                    "trace_path": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": [item.value for item in AccessMode],
                    },
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in ResourceKind],
                    },
                    "identifier": {"type": "string"},
                    "tool": {"type": "string"},
                },
            ),
        },
        {
            "name": "start_observation_session",
            "description": "Start a trusted append-only observation session bound to one admitted intent.",
            "inputSchema": _object_schema(
                ["session_id", "intent_id", "monitor_id"],
                {
                    "session_id": {"type": "string"},
                    "intent_id": {"type": "string"},
                    "monitor_id": {"type": "string"},
                    "key_id": {"type": "string"},
                    "coverage": {
                        "type": "string",
                        "enum": [
                            "brokered_proxy",
                            "tool_proxy",
                            "os_monitor",
                            "declared",
                        ],
                    },
                    "required_tools": {"type": "array", "items": {"type": "string"}},
                },
            ),
        },
        {
            "name": "record_trusted_observed_access",
            "description": "Append a hash-chained access event using a server-held HMAC key.",
            "inputSchema": _object_schema(
                ["session_id", "key_env", "mode", "kind", "identifier"],
                {
                    "session_id": {"type": "string"},
                    "key_env": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": [item.value for item in AccessMode],
                    },
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in ResourceKind],
                    },
                    "identifier": {"type": "string"},
                    "tool": {"type": "string"},
                },
            ),
        },
        {
            "name": "seal_observation_session",
            "description": "Seal and authenticate a trusted observation session.",
            "inputSchema": _object_schema(
                ["session_id", "key_env"],
                {
                    "session_id": {"type": "string"},
                    "key_env": {"type": "string"},
                    "complete": {"type": "boolean"},
                },
            ),
        },
        {
            "name": "verify_observation_session",
            "description": "Verify the full hash chain and session attestation.",
            "inputSchema": _object_schema(
                ["session_id", "key_env"],
                {
                    "session_id": {"type": "string"},
                    "key_env": {"type": "string"},
                },
            ),
        },
        {
            "name": "verify_evidence_bundle",
            "description": "Verify an HMAC-attested evidence bundle using a server environment key.",
            "inputSchema": _object_schema(
                ["evidence_path", "signature_path", "key_env"],
                {
                    "evidence_path": {"type": "string"},
                    "signature_path": {"type": "string"},
                    "key_env": {"type": "string"},
                },
            ),
        },
        {
            "name": "broker_file_access",
            "description": "Execute an intent-enforced repository operation through a running trusted broker.",
            "inputSchema": _object_schema(
                ["socket_path", "token_env", "operation"],
                {
                    "socket_path": {"type": "string"},
                    "token_env": {"type": "string"},
                    "operation": {"type": "string"},
                    "payload": {"type": "object"},
                },
            ),
        },
        {
            "name": "claim_artifact",
            "description": "Legacy fine-grained atomic claim for an identifier or contract.",
            "inputSchema": _object_schema(
                ["identifier", "owner"],
                {
                    "identifier": {"type": "string"},
                    "owner": {"type": "string"},
                    "claim_type": {
                        "type": "string",
                        "enum": [item.value for item in ClaimType],
                    },
                    "signature": {"type": "string"},
                    "task_id": {"type": "string"},
                    "lease_seconds": {"type": "integer", "minimum": 1},
                },
            ),
        },
    ]


class McpServer:
    def __init__(self, config: McpServerConfig, out: TextIO = sys.stdout) -> None:
        self._config = config
        self._out = out

    def _plane(self) -> Plane:
        db_path = Path(self._config.db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        return Plane.open(
            self._config.db_path,
            semantic=self._config.semantic,
            lexicon_path=self._config.lexicon_path,
            governance="exploratory" if self._config.exploratory else "governed",
        )

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        plane = self._plane()
        try:
            if name == "admit_change_intent":
                return plane.admit(
                    ChangeIntent.from_dict(arguments["intent"])
                ).to_dict()
            if name == "amend_change_intent":
                return plane.amend(
                    ChangeIntent.from_dict(arguments["intent"]),
                    expected_version=arguments.get("expected_version"),
                ).to_dict()
            if name == "get_worker_context":
                return plane.context_pack(arguments["intent_id"])
            if name == "list_active_intents":
                return {"intents": plane.intents(active_only=True)}
            if name == "heartbeat_intent":
                plane.heartbeat(
                    arguments["intent_id"], int(arguments.get("lease_seconds", 900))
                )
                return {"renewed": True, "intent_id": arguments["intent_id"]}
            if name == "complete_intent":
                plane.complete(arguments["intent_id"])
                return {"completed": True, "intent_id": arguments["intent_id"]}
            if name == "release_intent":
                plane.release_intent(arguments["intent_id"])
                return {"released": True, "intent_id": arguments["intent_id"]}
            if name == "verify_change_manifest":
                return plane.verify_manifest(
                    ChangeManifest.from_dict(arguments["manifest"])
                ).to_dict()
            if name == "plan_targeted_repair":
                report = plane.verify_manifest(
                    ChangeManifest.from_dict(arguments["manifest"])
                )
                return {
                    "report": report.to_dict(),
                    "repair_plan": plane.repair_plan(report).to_dict(),
                }
            if name == "verify_git_worktree":
                return plane.verify_git(
                    arguments["intent_id"],
                    arguments.get("repo_path", "."),
                    run_acceptance=bool(arguments.get("run_acceptance", False)),
                    acceptance_timeout=int(arguments.get("acceptance_timeout", 300)),
                ).to_dict()
            if name == "list_coordination_notices":
                return {
                    "notices": plane.notices(
                        arguments["intent_id"],
                        pending_only=bool(arguments.get("pending_only", True)),
                    )
                }
            if name == "acknowledge_coordination_notice":
                plane.acknowledge_notice(int(arguments["notice_id"]))
                return {"acknowledged": True, "notice_id": int(arguments["notice_id"])}
            if name == "recommend_worker_tier":
                return plane.recommend_worker(arguments["intent_id"]).to_dict()
            if name == "get_dependency_graph":
                return plane.dependency_graph()
            if name == "run_integration":
                return plane.run_integration(
                    IntegrationRunSpec.from_dict(arguments["spec"])
                ).to_dict()
            if name == "start_observation_session":
                return plane.start_observation_session(
                    arguments["session_id"],
                    arguments["intent_id"],
                    monitor_id=arguments["monitor_id"],
                    key_id=arguments.get("key_id", "default"),
                    coverage=arguments.get("coverage", "tool_proxy"),
                    required_tools=tuple(arguments.get("required_tools") or ()),
                )
            if name == "record_trusted_observed_access":
                key = os.environ.get(arguments["key_env"])
                if not key:
                    raise ValueError(
                        f"environment variable {arguments['key_env']!r} is not set"
                    )
                return plane.record_observed_access(
                    arguments["session_id"],
                    mode=arguments["mode"],
                    kind=arguments["kind"],
                    identifier=arguments["identifier"],
                    tool=arguments.get("tool"),
                    key=key.encode("utf-8"),
                )
            if name == "seal_observation_session":
                key = os.environ.get(arguments["key_env"])
                if not key:
                    raise ValueError(
                        f"environment variable {arguments['key_env']!r} is not set"
                    )
                return plane.seal_observation_session(
                    arguments["session_id"],
                    key=key.encode("utf-8"),
                    complete=bool(arguments.get("complete", True)),
                )
            if name == "verify_observation_session":
                key = os.environ.get(arguments["key_env"])
                if not key:
                    raise ValueError(
                        f"environment variable {arguments['key_env']!r} is not set"
                    )
                return plane.verify_observation_session(
                    arguments["session_id"], key=key.encode("utf-8")
                )
            if name == "record_observed_access":
                return append_observation(
                    arguments["trace_path"],
                    mode=AccessMode(arguments["mode"]),
                    kind=ResourceKind(arguments["kind"]),
                    identifier=arguments["identifier"],
                    tool=arguments.get("tool"),
                ).to_dict()
            if name == "verify_evidence_bundle":
                key = os.environ.get(arguments["key_env"])
                if not key:
                    raise ValueError(
                        f"environment variable {arguments['key_env']!r} is not set"
                    )
                return {
                    "valid": verify_evidence_file(
                        arguments["evidence_path"],
                        arguments["signature_path"],
                        key=key.encode("utf-8"),
                    )
                }
            if name == "broker_file_access":
                token = os.environ.get(arguments["token_env"])
                if not token:
                    raise ValueError(
                        f"environment variable {arguments['token_env']!r} is not set"
                    )
                return BrokerClient(arguments["socket_path"], token).call(
                    arguments["operation"], **dict(arguments.get("payload") or {})
                )
            if name == "claim_artifact":
                return plane.claim(
                    Claim(
                        claim_type=ClaimType(arguments.get("claim_type", "name")),
                        identifier=arguments["identifier"],
                        owner=arguments["owner"],
                        signature=arguments.get("signature"),
                        task_id=arguments.get("task_id"),
                        lease_seconds=int(arguments.get("lease_seconds", 900)),
                    )
                ).to_dict()
            raise ValueError(f"unknown tool: {name}")
        finally:
            plane.close()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": tool_definitions()},
            }
        if method == "tools/call":
            params = request.get("params") or {}
            try:
                result = self._dispatch_tool(
                    params.get("name", ""), params.get("arguments") or {}
                )
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, ensure_ascii=False),
                            }
                        ]
                    },
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def serve(self, stream: TextIO = sys.stdin) -> None:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(request)
            if response is not None:
                self._out.write(json.dumps(response, ensure_ascii=False) + "\n")
                self._out.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="claim-plane-mcp")
    parser.add_argument("--db", default=".claim-plane/plane.db")
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--lexicon")
    parser.add_argument("--exploratory", action="store_true")
    args = parser.parse_args(argv)
    McpServer(
        McpServerConfig(
            db_path=args.db,
            semantic=args.semantic,
            lexicon_path=args.lexicon,
        )
    ).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
