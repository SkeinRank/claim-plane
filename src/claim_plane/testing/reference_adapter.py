"""Dependency-free reference implementation of the public adapter contract.

The reference adapter exists for core and third-party adapter tests.  It models the
portable authority lifecycle without invoking an external coding-agent runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from claim_plane.protocol.adapter import (
    AGENT_ADAPTER_PROTOCOL_VERSION,
    AdapterErrorCode,
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterResponse,
    AdapterStatus,
)
from claim_plane.protocol.capabilities import (
    AdapterCapabilityManifest,
    CapabilityLevel,
    EnforcementLevel,
    GuaranteeDeclaration,
    GuaranteeProvider,
    RuntimeIdentity,
)
from claim_plane.protocol.lifecycle import LifecycleEventStore, record_adapter_lifecycle

REFERENCE_ADAPTER_NAME = "reference"
REFERENCE_ADAPTER_VERSION = "1"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ReferenceAdapter:
    """Small persistent adapter used to exercise Claim Plane Core in isolation."""

    name = REFERENCE_ADAPTER_NAME
    protocol_version = AGENT_ADAPTER_PROTOCOL_VERSION

    def capability_manifest(
        self, project_root: str = "."
    ) -> AdapterCapabilityManifest:
        del project_root
        capabilities = {
            "pre_write_blocking": CapabilityLevel.COMPLETE,
            "shell_mutation_visibility": CapabilityLevel.COMPLETE,
            "direct_filesystem_visibility": CapabilityLevel.COMPLETE,
            "streamed_events": CapabilityLevel.COMPLETE,
            "subagent_visibility": CapabilityLevel.COMPLETE,
            "resume_support": CapabilityLevel.COMPLETE,
            "completion_verification": CapabilityLevel.COMPLETE,
            "worktree_control": CapabilityLevel.MANAGED,
        }
        guarantees = {
            "undeclared_tool_write": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.ADAPTER,
                ("reference authority check",),
                required_capability="pre_write_blocking",
            ),
            "bypassed_host_write": GuaranteeDeclaration(
                EnforcementLevel.POST_VERIFIED,
                GuaranteeProvider.CLAIM_PLANE,
                ("reference final resource comparison",),
                required_capability="completion_verification",
            ),
            "subagent_mutation": GuaranteeDeclaration(
                EnforcementLevel.POST_VERIFIED,
                GuaranteeProvider.CLAIM_PLANE,
                ("reference final resource comparison",),
                required_capability="completion_verification",
            ),
            "completion_verification": GuaranteeDeclaration(
                EnforcementLevel.POST_VERIFIED,
                GuaranteeProvider.CLAIM_PLANE,
                ("reference final resource comparison",),
                required_capability="completion_verification",
            ),
            "corrupted_session_state": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.CLAIM_PLANE,
                ("validated persistent state and lifecycle chain",),
            ),
            "stale_intent_version": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.CLAIM_PLANE,
                ("expected intent binding check",),
            ),
            "cancellation_revokes_authority": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.CLAIM_PLANE,
                ("atomic reference authority revocation",),
            ),
        }
        return AdapterCapabilityManifest(
            adapter=self.name,
            adapter_version=REFERENCE_ADAPTER_VERSION,
            adapter_protocol_version=self.protocol_version,
            runtime=RuntimeIdentity("reference", "1", True),
            capabilities=capabilities,
            guarantees=guarantees,
            metadata={"purpose": "adapter conformance and core testing"},
        )

    @staticmethod
    def _root(request: AdapterRequest) -> Path:
        if request.adapter != REFERENCE_ADAPTER_NAME:
            raise AdapterProtocolError(
                AdapterErrorCode.ADAPTER_MISMATCH,
                f"request targets {request.adapter!r}, expected 'reference'",
            )
        root = Path(request.project_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _state_path(root: Path) -> Path:
        return root / ".claim-plane/reference/state.json"

    @staticmethod
    def _cache_path(root: Path, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return root / ".claim-plane/reference/requests" / f"{digest}.json"

    def _read_state(self, root: Path) -> dict[str, Any]:
        path = self._state_path(root)
        if not path.exists():
            return {
                "protocol": "claim-plane.reference-adapter-state.v1",
                "sessions": {},
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                f"reference adapter state is corrupt: {exc}",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("sessions"), dict
        ):
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                "reference adapter state has an invalid shape",
            )
        return payload

    def _validate_lifecycle(self, root: Path, request: AdapterRequest) -> None:
        if request.session_id is None:
            return
        database = root / ".claim-plane/lifecycle/events.sqlite3"
        if not database.exists():
            return
        try:
            with LifecycleEventStore.for_project(root) as store:
                events = store.list_events(
                    adapter=self.name, session_id=request.session_id
                )
                if events and not store.report(
                    adapter=self.name, session_id=request.session_id
                ).valid:
                    raise AdapterProtocolError(
                        AdapterErrorCode.CORRUPT_STATE,
                        "reference lifecycle stream is invalid",
                    )
        except AdapterProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                f"reference lifecycle stream is unavailable: {exc}",
            ) from exc

    def _cached(self, root: Path, request: AdapterRequest) -> AdapterResponse | None:
        path = self._cache_path(root, request.request_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["fingerprint"] != request.fingerprint():
                raise AdapterProtocolError(
                    AdapterErrorCode.IDEMPOTENCY_CONFLICT,
                    "request_id was reused with different input",
                )
            return AdapterResponse.from_dict(payload["response"]).as_replayed()
        except AdapterProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                f"reference request cache is corrupt: {exc}",
            ) from exc

    def _binding(
        self, state: Mapping[str, Any], session_id: str | None
    ) -> tuple[str | None, int | None]:
        if not session_id:
            return None, None
        session = dict(state.get("sessions", {})).get(session_id)
        if not isinstance(session, Mapping):
            return None, None
        intent = session.get("intent")
        if not isinstance(intent, Mapping) or not intent.get("active"):
            return None, None
        return str(intent["id"]), int(intent["version"])

    def _assert_binding(
        self, state: Mapping[str, Any], request: AdapterRequest
    ) -> dict[str, Any]:
        sessions = dict(state.get("sessions", {}))
        session = sessions.get(request.session_id or "")
        if not isinstance(session, dict):
            raise AdapterProtocolError(
                AdapterErrorCode.UNKNOWN_SESSION, "unknown reference session"
            )
        intent = session.get("intent")
        if not isinstance(intent, dict) or not intent.get("active"):
            raise AdapterProtocolError(
                AdapterErrorCode.UNKNOWN_INTENT,
                "reference session has no active intent",
            )
        if _parse_time(str(intent["lease_expires_at"])) <= _now():
            raise AdapterProtocolError(
                AdapterErrorCode.RUNTIME_FAILURE,
                "reference intent lease expired",
                details={"reason": "expired_lease"},
            )
        if request.intent_id is not None and request.intent_id != intent["id"]:
            raise AdapterProtocolError(
                AdapterErrorCode.STALE_INTENT_VERSION,
                "request intent identity is stale",
            )
        if (
            request.intent_version is not None
            and request.intent_version != int(intent["version"])
        ):
            raise AdapterProtocolError(
                AdapterErrorCode.STALE_INTENT_VERSION,
                "request intent version is stale",
            )
        return session

    def _perform(
        self,
        request: AdapterRequest,
        operation: AdapterOperation,
        action: Callable[[dict[str, Any]], tuple[Mapping[str, Any], AdapterStatus]],
        *,
        binding: bool = False,
    ) -> AdapterResponse:
        if request.operation is not operation:
            raise AdapterProtocolError(
                AdapterErrorCode.UNSUPPORTED_OPERATION,
                f"expected {operation.value}, received {request.operation.value}",
            )
        root = self._root(request)
        self._validate_lifecycle(root, request)
        cached = self._cached(root, request)
        if cached is not None:
            record_adapter_lifecycle(
                project_root=root, request=request, response=cached
            )
            return cached
        state = self._read_state(root)
        try:
            if binding:
                self._assert_binding(state, request)
            payload, status = action(state)
        except AdapterProtocolError as exc:
            record_adapter_lifecycle(project_root=root, request=request, error=exc)
            raise
        _atomic_json(self._state_path(root), state)
        intent_id, intent_version = self._binding(state, request.session_id)
        response = AdapterResponse(
            request_id=request.request_id,
            operation=operation,
            adapter=self.name,
            status=status,
            session_id=request.session_id,
            run_id=request.run_id,
            intent_id=intent_id or request.intent_id,
            intent_version=intent_version,
            payload=dict(payload),
        )
        record_adapter_lifecycle(project_root=root, request=request, response=response)
        _atomic_json(
            self._cache_path(root, request.request_id),
            {"fingerprint": request.fingerprint(), "response": response.to_dict()},
        )
        return response

    @staticmethod
    def _session(state: dict[str, Any], request: AdapterRequest) -> dict[str, Any]:
        session_id = request.session_id
        if not session_id:
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST, "session_id is required"
            )
        sessions = state.setdefault("sessions", {})
        session = sessions.get(session_id)
        if not isinstance(session, dict):
            raise AdapterProtocolError(
                AdapterErrorCode.UNKNOWN_SESSION, "unknown reference session"
            )
        return session

    def enroll_project(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.ENROLL_PROJECT,
            lambda state: ({"enrolled": True}, AdapterStatus.SUCCEEDED),
        )

    def unenroll_project(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.UNENROLL_PROJECT,
            lambda state: ({"enrolled": False}, AdapterStatus.SUCCEEDED),
        )

    def doctor(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.DOCTOR,
            lambda state: (
                {
                    "ready": True,
                    "adapter_manifest": self.capability_manifest().to_dict(),
                },
                AdapterStatus.SUCCEEDED,
            ),
        )

    def start_session(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session_id = request.session_id
            if not session_id:
                raise AdapterProtocolError(
                    AdapterErrorCode.INVALID_REQUEST, "session_id is required"
                )
            state.setdefault("sessions", {}).setdefault(
                session_id,
                {"state": "awaiting_task", "intent": None, "observed": []},
            )
            return {"state": "awaiting_task"}, AdapterStatus.SUCCEEDED

        return self._perform(request, AdapterOperation.START_SESSION, action)

    def stop_session(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._session(state, request)
            session["state"] = "ended"
            return {"state": "ended"}, AdapterStatus.SUCCEEDED

        return self._perform(request, AdapterOperation.STOP_SESSION, action)

    def submit_task(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._session(state, request)
            session["state"] = "awaiting_intent"
            prompt = request.payload.get("prompt")
            session["task_digest"] = hashlib.sha256(
                str(prompt).encode("utf-8")
            ).hexdigest()
            return {"state": "awaiting_intent"}, AdapterStatus.SUCCEEDED

        return self._perform(request, AdapterOperation.SUBMIT_TASK, action)

    def propose_intent(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._session(state, request)
            resources = request.payload.get("resources", ["README.md"])
            if not isinstance(resources, list) or not all(
                isinstance(item, str) and item for item in resources
            ):
                raise AdapterProtocolError(
                    AdapterErrorCode.INVALID_REQUEST, "resources must be an array"
                )
            intent = {
                "id": f"reference-intent-{uuid.uuid4().hex}",
                "version": 1,
                "resources": sorted(set(resources)),
                "active": True,
                "lease_expires_at": _iso(_now() + timedelta(minutes=5)),
            }
            session["intent"] = intent
            session["state"] = "active"
            return (
                {"allowed": True, "resources": intent["resources"]},
                AdapterStatus.SUCCEEDED,
            )

        return self._perform(request, AdapterOperation.PROPOSE_INTENT, action)

    def request_mutation(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._assert_binding(state, request)
            resource = request.payload.get("resource")
            allowed = (
                isinstance(resource, str)
                and resource in session["intent"]["resources"]
            )
            return (
                {"allowed": allowed, "resource": resource},
                AdapterStatus.SUCCEEDED if allowed else AdapterStatus.DENIED,
            )

        return self._perform(
            request, AdapterOperation.REQUEST_MUTATION, action, binding=True
        )

    def request_amendment(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._assert_binding(state, request)
            resource = request.payload.get("resource")
            approved = bool(request.payload.get("approved"))
            if not isinstance(resource, str) or not resource:
                raise AdapterProtocolError(
                    AdapterErrorCode.INVALID_REQUEST, "resource is required"
                )
            if not approved:
                return (
                    {"allowed": False, "resource": resource},
                    AdapterStatus.DENIED,
                )
            resources = set(session["intent"]["resources"])
            resources.add(resource)
            session["intent"]["resources"] = sorted(resources)
            session["intent"]["version"] = int(session["intent"]["version"]) + 1
            return (
                {"allowed": True, "resource": resource},
                AdapterStatus.SUCCEEDED,
            )

        return self._perform(
            request, AdapterOperation.REQUEST_AMENDMENT, action, binding=True
        )

    def observe_result(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._assert_binding(state, request)
            resource = request.payload.get("resource")
            if isinstance(resource, str) and resource:
                observed = set(session.setdefault("observed", []))
                observed.add(resource)
                session["observed"] = sorted(observed)
            return {"observed": resource}, AdapterStatus.SUCCEEDED

        return self._perform(
            request, AdapterOperation.OBSERVE_RESULT, action, binding=True
        )

    def verify_completion(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._assert_binding(state, request)
            final_resources = request.payload.get(
                "final_resources", session.get("observed", [])
            )
            if not isinstance(final_resources, list):
                raise AdapterProtocolError(
                    AdapterErrorCode.INVALID_REQUEST,
                    "final_resources must be an array",
                )
            authorized = set(session["intent"]["resources"])
            uncovered = sorted(set(str(item) for item in final_resources) - authorized)
            verified = not uncovered
            session["state"] = "verified" if verified else "verification_failed"
            return (
                {"verified": verified, "uncovered_resources": uncovered},
                AdapterStatus.SUCCEEDED if verified else AdapterStatus.DENIED,
            )

        return self._perform(
            request, AdapterOperation.VERIFY_COMPLETION, action, binding=True
        )

    def inspect(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._session(state, request)
            return {
                "state": session.get("state"),
                "resources": list((session.get("intent") or {}).get("resources", [])),
            }, AdapterStatus.SUCCEEDED

        return self._perform(request, AdapterOperation.INSPECT, action)

    def cancel(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session = self._assert_binding(state, request)
            session["intent"]["active"] = False
            session["state"] = "cancelled"
            return {"state": "cancelled", "released": True}, AdapterStatus.CANCELLED

        return self._perform(request, AdapterOperation.CANCEL, action, binding=True)

    def resume(self, request: AdapterRequest) -> AdapterResponse:
        def action(state: dict[str, Any]) -> tuple[Mapping[str, Any], AdapterStatus]:
            session_id = request.session_id
            if not session_id:
                raise AdapterProtocolError(
                    AdapterErrorCode.INVALID_REQUEST, "session_id is required"
                )
            session = state.setdefault("sessions", {}).setdefault(
                session_id,
                {"state": "awaiting_task", "intent": None, "observed": []},
            )
            return {"state": session["state"], "resumed": True}, AdapterStatus.SUCCEEDED

        return self._perform(request, AdapterOperation.RESUME, action)

    # The following helpers are intentionally testing-only and make failure injection
    # deterministic without exposing such operations through the adapter protocol.
    def expire_lease(self, project_root: str | Path, session_id: str) -> None:
        root = Path(project_root).resolve()
        state = self._read_state(root)
        session = state["sessions"][session_id]
        session["intent"]["lease_expires_at"] = "2000-01-01T00:00:00Z"
        _atomic_json(self._state_path(root), state)

    def corrupt_state(self, project_root: str | Path) -> None:
        path = self._state_path(Path(project_root).resolve())
        path.write_text("{not-json", encoding="utf-8")
