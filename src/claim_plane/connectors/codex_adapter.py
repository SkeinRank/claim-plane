"""Codex implementation of the runtime-neutral Agent Adapter Protocol."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from claim_plane.connectors import codex as codex_runtime
from claim_plane.connectors.codex_guard import classify_tool_call
from claim_plane.core import Plane
from claim_plane.protocol import (
    AGENT_ADAPTER_PROTOCOL_VERSION,
    AdapterCapabilityManifest,
    AdapterErrorCode,
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterResponse,
    AdapterStatus,
    CapabilityLevel,
    EnforcementLevel,
    GuaranteeDeclaration,
    GuaranteeProvider,
    LifecycleEventStore,
    LifecycleStoreError,
    RuntimeIdentity,
    AdapterRegistry,
    AdapterSource,
    record_adapter_lifecycle,
)

CODEX_ADAPTER_NAME = "codex"
CODEX_ADAPTER_REVISION = 3
_REQUEST_CACHE = Path(".claim-plane/adapters/codex/requests")
_PLANE_DB = Path(".claim-plane/plane.db")
_LIFECYCLE_DB = Path(".claim-plane/lifecycle/events.sqlite3")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _request_cache_path(root: Path, request_id: str) -> Path:
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    return root / _REQUEST_CACHE / f"{key}.json"


def _hook_request_id(payload: Mapping[str, Any]) -> tuple[str, bool]:
    """Return an idempotency key scoped to one concrete hook invocation.

    Codex may reuse a turn-level ``request_id`` or ``event_id`` across several
    lifecycle hooks.  Using that value alone makes UserPromptSubmit, PreToolUse,
    PostToolUse, and Stop collide in the adapter request cache.  Namespace the key
    by the hook event and the full secret-safe payload digest so an exact replay is
    idempotent while distinct tools and lifecycle phases cannot conflict.
    """

    identity: dict[str, str] = {}
    for key in (
        "request_id",
        "requestId",
        "event_id",
        "eventId",
        "hook_event_id",
        "hookEventId",
        "tool_use_id",
        "toolUseId",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            identity[key] = value.strip()
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    payload_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if identity:
        event = str(payload.get("hook_event_name") or "unknown").strip().casefold()
        identity_digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return (
            f"codex-hook-{event}-{identity_digest}-{payload_digest[:24]}",
            True,
        )
    return f"codex-hook-unkeyed-{payload_digest}-{uuid.uuid4().hex}", False


def _map_runtime_error(exc: Exception, request: AdapterRequest) -> AdapterProtocolError:
    message = str(exc)
    lowered = message.casefold()
    details = {
        "operation": request.operation.value,
        "request_id": request.request_id,
        "session_id": request.session_id,
        "intent_id": request.intent_id,
    }
    if isinstance(exc, FileNotFoundError) and request.session_id:
        return AdapterProtocolError(
            AdapterErrorCode.UNKNOWN_SESSION, message, details=details
        )
    if isinstance(exc, KeyError):
        return AdapterProtocolError(
            AdapterErrorCode.UNKNOWN_INTENT, message, details=details
        )
    if "stale" in lowered or "version" in lowered and "intent" in lowered:
        return AdapterProtocolError(
            AdapterErrorCode.STALE_INTENT_VERSION,
            message,
            retryable=True,
            details=details,
        )
    if "corrupt" in lowered or "invalid session" in lowered:
        return AdapterProtocolError(
            AdapterErrorCode.CORRUPT_STATE, message, details=details
        )
    return AdapterProtocolError(
        AdapterErrorCode.RUNTIME_FAILURE,
        message,
        retryable=False,
        details=details,
    )


class CodexAdapter:
    """Translate Codex lifecycle calls into the generic adapter contract."""

    name = CODEX_ADAPTER_NAME
    protocol_version = AGENT_ADAPTER_PROTOCOL_VERSION
    supported_protocol_range = ">=1.0,<2.0"

    def registry_handshake(self, project_root: str = "."):
        """Negotiate the built-in descriptor and enforce an existing project pin."""

        registry = AdapterRegistry()
        registry.register(
            self.name,
            lambda: self,
            protocol_range=self.supported_protocol_range,
            source=AdapterSource.BUILTIN,
        )
        return registry.handshake(self.name, project_root=project_root)

    def capability_manifest(self, project_root: str = ".") -> AdapterCapabilityManifest:
        """Return the effective Codex capability and guarantee declaration."""

        root = codex_runtime.resolve_project_root(project_root)
        report = codex_runtime.doctor_codex(root)
        checks = {str(item.get("name")): item for item in report.checks}

        def status(name: str) -> str:
            return str(checks.get(name, {}).get("status") or "error")

        hooks_complete = all(
            status(name) == "ok"
            for name in (
                "project_initialized",
                "enrollment_state",
                "lifecycle_hooks",
                "connector_hook_definition",
                "project_hook_feature",
            )
        )
        pre_write_complete = (
            hooks_complete and status("pre_mutation_guard_compatibility") == "ok"
        )
        completion_complete = hooks_complete

        capabilities = {
            "pre_write_blocking": (
                CapabilityLevel.COMPLETE
                if pre_write_complete
                else CapabilityLevel.PARTIAL
                if hooks_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "shell_mutation_visibility": (
                CapabilityLevel.PARTIAL
                if hooks_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "direct_filesystem_visibility": (
                CapabilityLevel.PARTIAL
                if hooks_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "streamed_events": (
                CapabilityLevel.COMPLETE
                if hooks_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "subagent_visibility": (
                CapabilityLevel.PARTIAL
                if hooks_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "resume_support": (
                CapabilityLevel.COMPLETE
                if hooks_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "completion_verification": (
                CapabilityLevel.COMPLETE
                if completion_complete
                else CapabilityLevel.UNAVAILABLE
            ),
            "worktree_control": CapabilityLevel.EXTERNAL,
        }

        if pre_write_complete:
            undeclared_tool_write = GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.COMPOSITE,
                (
                    "Codex PreToolUse interception",
                    "Claim Plane intent-version and mutation admission",
                ),
                required_capability="pre_write_blocking",
                detail="Supported tool writes are denied before mutation.",
            )
        elif completion_complete:
            undeclared_tool_write = GuaranteeDeclaration(
                EnforcementLevel.POST_VERIFIED,
                GuaranteeProvider.CLAIM_PLANE,
                ("final Git state and admitted authority comparison",),
                required_capability="completion_verification",
                detail=(
                    "Runtime pre-write coverage is incomplete; "
                    "final verification remains required."
                ),
            )
        else:
            undeclared_tool_write = GuaranteeDeclaration(
                EnforcementLevel.UNAVAILABLE,
                GuaranteeProvider.COMPOSITE,
                (),
                detail=(
                    "Neither complete interception nor final verification is available."
                ),
            )

        post_verified = (
            GuaranteeDeclaration(
                EnforcementLevel.POST_VERIFIED,
                GuaranteeProvider.CLAIM_PLANE,
                ("final Git state and admitted authority comparison",),
                required_capability="completion_verification",
            )
            if completion_complete
            else GuaranteeDeclaration(
                EnforcementLevel.UNAVAILABLE,
                GuaranteeProvider.CLAIM_PLANE,
                (),
            )
        )
        guarantees = {
            "undeclared_tool_write": undeclared_tool_write,
            "bypassed_host_write": post_verified,
            "subagent_mutation": post_verified,
            "completion_verification": post_verified,
            "corrupted_session_state": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.CLAIM_PLANE,
                ("validated append-only lifecycle chain",),
            ),
            "stale_intent_version": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.CLAIM_PLANE,
                ("pre-operation active intent binding check",),
            ),
            "cancellation_revokes_authority": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.CLAIM_PLANE,
                ("atomic intent abandonment and authority release",),
            ),
        }
        return AdapterCapabilityManifest(
            adapter=self.name,
            adapter_version=str(CODEX_ADAPTER_REVISION),
            adapter_protocol_version=self.protocol_version,
            runtime=RuntimeIdentity(
                name="codex",
                version=report.codex_version,
                detected=report.codex_version is not None,
            ),
            capabilities=capabilities,
            guarantees=guarantees,
            metadata={
                "connector_revision": codex_runtime.CODEX_CONNECTOR_REVISION,
                "project_root": str(root),
                "doctor_ready": report.ready,
            },
        )

    def _validate(self, request: AdapterRequest, operation: AdapterOperation) -> Path:
        if request.adapter != self.name:
            raise AdapterProtocolError(
                AdapterErrorCode.ADAPTER_MISMATCH,
                f"request targets adapter {request.adapter!r}, expected {self.name!r}",
            )
        if request.operation is not operation:
            raise AdapterProtocolError(
                AdapterErrorCode.UNSUPPORTED_OPERATION,
                (
                    f"request operation {request.operation.value!r} cannot use "
                    f"{operation.value!r}"
                ),
            )
        try:
            return codex_runtime.resolve_project_root(request.project_root)
        except Exception as exc:  # noqa: BLE001
            raise _map_runtime_error(exc, request) from exc

    def _binding(
        self, root: Path, session_id: str | None
    ) -> tuple[str | None, int | None]:
        if not session_id:
            return None, None
        try:
            status = codex_runtime.codex_intent_status(root, session_id=session_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None, None
        intent_id = status.get("intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            return None, None
        db = root / _PLANE_DB
        if not db.exists():
            return intent_id, None
        plane = Plane.open(db)
        try:
            record = next(
                (
                    item
                    for item in plane.intents()
                    if item.get("intent_id") == intent_id
                ),
                None,
            )
        finally:
            plane.close()
        if record is None:
            return intent_id, None
        return intent_id, int(record["version"])

    def _assert_expected_binding(
        self,
        root: Path,
        request: AdapterRequest,
        *,
        allow_missing_version_zero: bool = False,
    ) -> None:
        if request.intent_id is None and request.intent_version is None:
            return
        actual_id, actual_version = self._binding(root, request.session_id)
        if request.intent_id is not None and request.intent_id != actual_id:
            raise AdapterProtocolError(
                AdapterErrorCode.STALE_INTENT_VERSION,
                "request intent identity does not match the active session authority",
                retryable=True,
                details={
                    "expected_intent_id": request.intent_id,
                    "actual_intent_id": actual_id,
                    "session_id": request.session_id,
                },
            )
        if request.intent_version is None:
            return
        if (
            allow_missing_version_zero
            and actual_version is None
            and request.intent_version == 0
        ):
            return
        if actual_version != request.intent_version:
            raise AdapterProtocolError(
                AdapterErrorCode.STALE_INTENT_VERSION,
                "request intent version is stale",
                retryable=True,
                details={
                    "expected_intent_version": request.intent_version,
                    "actual_intent_version": actual_version,
                    "intent_id": actual_id,
                    "session_id": request.session_id,
                },
            )

    def _cached_response(
        self, root: Path, request: AdapterRequest
    ) -> AdapterResponse | None:
        path = _request_cache_path(root, request.request_id)
        if not path.exists():
            return None
        try:
            record = _read_json_object(path)
            stored_fingerprint = str(record.get("request_fingerprint") or "")
            if stored_fingerprint != request.fingerprint():
                raise AdapterProtocolError(
                    AdapterErrorCode.IDEMPOTENCY_CONFLICT,
                    "request_id was already used with different adapter input",
                    details={"request_id": request.request_id},
                )
            raw_response = record.get("response")
            if not isinstance(raw_response, Mapping):
                raise ValueError("cached adapter response is missing")
            return AdapterResponse.from_dict(raw_response).as_replayed()
        except AdapterProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                f"cached adapter request state is corrupt: {exc}",
                details={"request_id": request.request_id, "path": str(path)},
            ) from exc

    def _store_response(
        self, root: Path, request: AdapterRequest, response: AdapterResponse
    ) -> None:
        _atomic_write_json(
            _request_cache_path(root, request.request_id),
            {
                "protocol": "claim-plane.adapter-request-cache.v1",
                "adapter": self.name,
                "adapter_revision": CODEX_ADAPTER_REVISION,
                "request_fingerprint": request.fingerprint(),
                "response": response.to_dict(),
            },
        )

    @staticmethod
    def _validate_lifecycle_state(root: Path, request: AdapterRequest) -> None:
        if request.session_id is None or not (root / _LIFECYCLE_DB).exists():
            return
        try:
            with LifecycleEventStore.for_project(root) as store:
                events = store.list_events(
                    adapter=request.adapter,
                    session_id=request.session_id,
                )
                if not events:
                    return
                report = store.report(
                    adapter=request.adapter,
                    session_id=request.session_id,
                )
        except Exception as exc:  # noqa: BLE001
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                f"normalized lifecycle state is unavailable: {exc}",
                details={
                    "request_id": request.request_id,
                    "operation": request.operation.value,
                    "session_id": request.session_id,
                },
            ) from exc
        if not report.valid:
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                "normalized lifecycle stream is invalid",
                details={
                    "request_id": request.request_id,
                    "operation": request.operation.value,
                    "session_id": request.session_id,
                    "findings": [item.to_dict() for item in report.findings],
                },
            )

    @staticmethod
    def _record_lifecycle(
        root: Path,
        request: AdapterRequest,
        *,
        response: AdapterResponse | None = None,
        error: AdapterProtocolError | None = None,
    ) -> None:
        try:
            record_adapter_lifecycle(
                project_root=root,
                request=request,
                response=response,
                error=error,
            )
        except LifecycleStoreError as exc:
            raise AdapterProtocolError(
                AdapterErrorCode.CORRUPT_STATE,
                f"normalized lifecycle state is unavailable: {exc}",
                details={
                    "request_id": request.request_id,
                    "operation": request.operation.value,
                    "session_id": request.session_id,
                },
            ) from exc

    def _perform(
        self,
        request: AdapterRequest,
        operation: AdapterOperation,
        action: Callable[[Path], Mapping[str, Any]],
        *,
        check_binding: bool = False,
        allow_missing_version_zero: bool = False,
        status: AdapterStatus | None = None,
    ) -> AdapterResponse:
        root = self._validate(request, operation)
        self._validate_lifecycle_state(root, request)
        cacheable = bool(request.payload.get("_adapter_cacheable", True))
        cached = self._cached_response(root, request) if cacheable else None
        if cached is not None:
            self._record_lifecycle(root, request, response=cached)
            return cached
        try:
            if check_binding:
                self._assert_expected_binding(
                    root,
                    request,
                    allow_missing_version_zero=allow_missing_version_zero,
                )
            payload = dict(action(root))
            if operation in {
                AdapterOperation.DOCTOR,
                AdapterOperation.START_SESSION,
                AdapterOperation.RESUME,
            }:
                manifest = self.capability_manifest(str(root))
                handshake = self.registry_handshake(str(root))
                payload["adapter_manifest"] = (
                    manifest.to_dict()
                    if operation is AdapterOperation.DOCTOR
                    else manifest.evidence_summary()
                )
                payload["adapter_handshake"] = (
                    handshake.to_dict()
                    if operation is AdapterOperation.DOCTOR
                    else handshake.evidence_summary()
                )
        except AdapterProtocolError as error:
            self._record_lifecycle(root, request, error=error)
            raise
        except Exception as exc:  # noqa: BLE001
            mapped_error = _map_runtime_error(exc, request)
            self._record_lifecycle(root, request, error=mapped_error)
            raise mapped_error from exc

        intent_id, intent_version = self._binding(root, request.session_id)
        resolved_status = status or AdapterStatus.SUCCEEDED
        if payload.get("allowed") is False or payload.get("verified") is False:
            resolved_status = AdapterStatus.DENIED
        if int(payload.get("exit_code") or 0) != 0:
            resolved_status = AdapterStatus.DENIED
        response = AdapterResponse(
            request_id=request.request_id,
            operation=request.operation,
            adapter=self.name,
            status=resolved_status,
            session_id=request.session_id,
            run_id=request.run_id,
            intent_id=intent_id or request.intent_id,
            intent_version=intent_version,
            payload=payload,
        )
        self._record_lifecycle(root, request, response=response)
        if cacheable:
            self._store_response(root, request, response)
        return response

    @staticmethod
    def _require_session(request: AdapterRequest) -> str:
        if not request.session_id:
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                f"{request.operation.value} requires session_id",
            )
        return request.session_id

    @staticmethod
    def _hook_payload(
        request: AdapterRequest, event_name: str, *, source: str | None = None
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in request.payload.items()
            if not key.startswith("_adapter_") and key != "lifecycle"
        }
        payload["hook_event_name"] = event_name
        payload["session_id"] = CodexAdapter._require_session(request)
        payload.setdefault("cwd", request.project_root)
        if request.run_id:
            payload.setdefault("_claim_plane_run_id", request.run_id)
        if source is not None:
            payload["source"] = source
        return payload

    @staticmethod
    def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
        output = io.StringIO()
        exit_code = codex_runtime.handle_codex_hook(payload, output=output)
        raw_output = output.getvalue()
        result: dict[str, Any] = {"exit_code": exit_code, "hook_output": raw_output}
        if raw_output.strip():
            try:
                parsed = json.loads(raw_output)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                result["hook_result"] = parsed
                specific = parsed.get("hookSpecificOutput")
                if (
                    isinstance(specific, Mapping)
                    and specific.get("permissionDecision") == "deny"
                ):
                    result["allowed"] = False
                if parsed.get("decision") == "block":
                    result["verified"] = False

        if payload.get("hook_event_name") == "Stop":
            session_id = payload.get("session_id")
            cwd = payload.get("cwd")
            if isinstance(session_id, str) and isinstance(cwd, str):
                try:
                    status = codex_runtime.codex_intent_status(
                        cwd, session_id=session_id
                    )
                except (FileNotFoundError, ValueError, json.JSONDecodeError):
                    status = {}
                completion = status.get("completion")
                if isinstance(completion, Mapping):
                    result.update(dict(completion))
        return result

    def enroll_project(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.ENROLL_PROJECT,
            lambda root: codex_runtime.connect_codex(root),
        )

    def unenroll_project(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.UNENROLL_PROJECT,
            lambda root: codex_runtime.disconnect_codex(root),
        )

    def doctor(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.DOCTOR,
            lambda root: codex_runtime.doctor_codex(root).to_dict(),
        )

    def start_session(self, request: AdapterRequest) -> AdapterResponse:
        self.registry_handshake(request.project_root).require_compatible()
        return self._perform(
            request,
            AdapterOperation.START_SESSION,
            lambda root: self._run_hook(self._hook_payload(request, "SessionStart")),
        )

    def stop_session(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.STOP_SESSION,
            lambda root: self._run_hook(self._hook_payload(request, "SessionEnd")),
            check_binding=True,
        )

    def submit_task(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.SUBMIT_TASK,
            lambda root: self._run_hook(
                self._hook_payload(request, "UserPromptSubmit")
            ),
        )

    def propose_intent(self, request: AdapterRequest) -> AdapterResponse:
        session_id = self._require_session(request)
        proposal = request.payload.get("proposal")
        if not isinstance(proposal, Mapping):
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                "propose_intent requires a proposal object",
            )
        return self._perform(
            request,
            AdapterOperation.PROPOSE_INTENT,
            lambda root: codex_runtime.admit_codex_intent(
                root, session_id=session_id, proposal=dict(proposal)
            ),
            check_binding=True,
            allow_missing_version_zero=True,
        )

    def request_mutation(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.REQUEST_MUTATION,
            lambda root: self._run_hook(self._hook_payload(request, "PreToolUse")),
            check_binding=True,
        )

    def request_amendment(self, request: AdapterRequest) -> AdapterResponse:
        session_id = self._require_session(request)
        ticket_id = request.payload.get("ticket_id")
        reason = request.payload.get("reason")
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                "request_amendment requires ticket_id",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                "request_amendment requires reason",
            )
        return self._perform(
            request,
            AdapterOperation.REQUEST_AMENDMENT,
            lambda root: codex_runtime.amend_codex_scope(
                root,
                session_id=session_id,
                ticket_id=ticket_id.strip(),
                reason=reason.strip(),
            ),
            check_binding=True,
        )

    def observe_result(self, request: AdapterRequest) -> AdapterResponse:
        return self._perform(
            request,
            AdapterOperation.OBSERVE_RESULT,
            lambda root: self._run_hook(self._hook_payload(request, "PostToolUse")),
            check_binding=True,
        )

    def verify_completion(self, request: AdapterRequest) -> AdapterResponse:
        session_id = self._require_session(request)
        timeout = max(1, int(request.timeout_seconds))

        def action(root: Path) -> Mapping[str, Any]:
            if request.payload.get("hook_event_name") == "Stop" or request.payload.get(
                "lifecycle"
            ):
                return self._run_hook(self._hook_payload(request, "Stop"))
            return codex_runtime.verify_codex_completion(
                root,
                session_id=session_id,
                acceptance_timeout=timeout,
            )

        return self._perform(
            request,
            AdapterOperation.VERIFY_COMPLETION,
            action,
            check_binding=True,
        )

    def inspect(self, request: AdapterRequest) -> AdapterResponse:
        session_id = self._require_session(request)
        return self._perform(
            request,
            AdapterOperation.INSPECT,
            lambda root: codex_runtime.codex_intent_status(root, session_id=session_id),
        )

    def cancel(self, request: AdapterRequest) -> AdapterResponse:
        session_id = self._require_session(request)
        return self._perform(
            request,
            AdapterOperation.CANCEL,
            lambda root: codex_runtime.abandon_codex_intent(
                root, session_id=session_id
            ),
            check_binding=True,
            status=AdapterStatus.CANCELLED,
        )

    def resume(self, request: AdapterRequest) -> AdapterResponse:
        self.registry_handshake(request.project_root).require_compatible()
        return self._perform(
            request,
            AdapterOperation.RESUME,
            lambda root: self._run_hook(
                self._hook_payload(request, "SessionStart", source="resume")
            ),
            check_binding=True,
        )

    def dispatch_hook(
        self, payload: Mapping[str, Any], *, output: TextIO | None = None
    ) -> int:
        """Translate one native Codex hook message through the generic protocol."""

        event = payload.get("hook_event_name")
        if not isinstance(event, str) or not event:
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                "Codex hook input requires hook_event_name",
            )
        if (
            event in {"Stop", "SessionEnd"}
            and payload.get("_claim_plane_interactive") is True
        ):
            # A Codex TUI Stop event marks the end of one conversational turn, and
            # SessionEnd is emitted by the runtime before control returns to the
            # launcher.  Defer normalized final verification and session sealing to
            # ``claim-plane codex`` so acceptance runs exactly once, after TUI exit.
            return codex_runtime.handle_codex_hook(dict(payload), output=output)
        root = str(payload.get("cwd") or ".")
        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                "Codex hook session_id must be a string",
            )
        if event in {"PreToolUse", "PostToolUse"}:
            try:
                resolved_root = codex_runtime.resolve_project_root(root)
                classification, _ = classify_tool_call(resolved_root, payload)
            except (FileNotFoundError, ValueError):
                classification = "unknown"
            if classification != "mutation":
                # Repository inspection, connector-control commands, and denied
                # opaque surfaces are enforced and recorded by the runtime bridge,
                # but they are not mutation lifecycle transitions.  Bypassing the
                # normalized mutation adapter path prevents harmless inspection
                # before intent admission from producing invalid state-machine
                # transitions or noisy hook failures in the Codex TUI.
                return codex_runtime.handle_codex_hook(dict(payload), output=output)
            if isinstance(session_id, str) and session_id:
                try:
                    status = codex_runtime.codex_intent_status(
                        root, session_id=session_id
                    )
                except (FileNotFoundError, ValueError, json.JSONDecodeError):
                    status = {}
                if not status.get("intent_id"):
                    return codex_runtime.handle_codex_hook(dict(payload), output=output)
        operation = {
            "SessionStart": (
                AdapterOperation.RESUME
                if str(payload.get("source") or "") == "resume"
                else AdapterOperation.START_SESSION
            ),
            "UserPromptSubmit": AdapterOperation.SUBMIT_TASK,
            "PreToolUse": AdapterOperation.REQUEST_MUTATION,
            "PostToolUse": AdapterOperation.OBSERVE_RESULT,
            "Stop": AdapterOperation.VERIFY_COMPLETION,
            "SessionEnd": AdapterOperation.STOP_SESSION,
        }.get(event)
        if operation is None:
            raise AdapterProtocolError(
                AdapterErrorCode.UNSUPPORTED_OPERATION,
                f"unsupported Codex lifecycle event: {event}",
            )
        request_id, cacheable = _hook_request_id(payload)
        run_id = payload.get("_claim_plane_run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise AdapterProtocolError(
                AdapterErrorCode.INVALID_REQUEST,
                "controlled run identity must be a string",
            )
        request = AdapterRequest.create(
            operation,
            adapter=self.name,
            project_root=root,
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            payload={
                **dict(payload),
                "lifecycle": True,
                "_adapter_cacheable": cacheable,
            },
        )
        method = {
            AdapterOperation.START_SESSION: self.start_session,
            AdapterOperation.RESUME: self.resume,
            AdapterOperation.SUBMIT_TASK: self.submit_task,
            AdapterOperation.REQUEST_MUTATION: self.request_mutation,
            AdapterOperation.OBSERVE_RESULT: self.observe_result,
            AdapterOperation.VERIFY_COMPLETION: self.verify_completion,
            AdapterOperation.STOP_SESSION: self.stop_session,
        }[operation]
        response = method(request)
        hook_output = response.payload.get("hook_output")
        if output is not None and isinstance(hook_output, str):
            output.write(hook_output)
        return int(response.payload.get("exit_code") or 0)
