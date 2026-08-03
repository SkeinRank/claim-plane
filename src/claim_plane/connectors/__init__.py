"""Project-local connectors for coding-agent runtimes."""

from claim_plane.connectors.codex import (
    CODEX_CONNECTOR_REVISION,
    CODEX_ENROLLMENT_PROTOCOL,
    CODEX_HOOK_COMMAND,
    CODEX_HOOK_EVENTS,
    CODEX_INTENT_ADMISSION_PROTOCOL,
    CODEX_INTENT_PROPOSAL_PROTOCOL,
    CODEX_MIN_GUARD_VERSION,
    CODEX_SESSION_PROTOCOL,
    CodexDoctorReport,
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
from claim_plane.connectors.codex_amendment import (
    CODEX_SCOPE_AMENDMENT_PROTOCOL,
    CODEX_SCOPE_AMENDMENT_TTL_SECONDS,
)
from claim_plane.connectors.codex_guard import CODEX_GUARD_PROTOCOL
from claim_plane.connectors.codex_completion import CODEX_COMPLETION_PROTOCOL
from claim_plane.connectors.codex_adapter import (
    CODEX_ADAPTER_NAME,
    CODEX_ADAPTER_REVISION,
    CodexAdapter,
)

__all__ = [
    "CODEX_ADAPTER_NAME",
    "CODEX_ADAPTER_REVISION",
    "CodexAdapter",
    "CODEX_CONNECTOR_REVISION",
    "CODEX_ENROLLMENT_PROTOCOL",
    "CODEX_HOOK_COMMAND",
    "CODEX_HOOK_EVENTS",
    "CODEX_MIN_GUARD_VERSION",
    "CODEX_GUARD_PROTOCOL",
    "CODEX_COMPLETION_PROTOCOL",
    "CODEX_INTENT_ADMISSION_PROTOCOL",
    "CODEX_INTENT_PROPOSAL_PROTOCOL",
    "CODEX_SESSION_PROTOCOL",
    "CODEX_SCOPE_AMENDMENT_PROTOCOL",
    "CODEX_SCOPE_AMENDMENT_TTL_SECONDS",
    "CodexDoctorReport",
    "admit_codex_intent",
    "abandon_codex_intent",
    "amend_codex_scope",
    "codex_intent_status",
    "verify_codex_completion",
    "connect_codex",
    "disconnect_codex",
    "doctor_codex",
    "handle_codex_hook",
    "init_project",
]
