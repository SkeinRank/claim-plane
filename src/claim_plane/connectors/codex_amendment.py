"""Deterministic scope-amendment construction for enrolled Codex sessions."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from claim_plane.connectors.codex_guard import MutationRequest
from claim_plane.core import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    ResourceKind,
    ResourceRef,
    ScopeCommitment,
)

CODEX_SCOPE_AMENDMENT_PROTOCOL = "claim-plane.codex-scope-amendment.v1"
CODEX_SCOPE_AMENDMENT_TTL_SECONDS = 300

_WRITE_MODES = frozenset({AccessMode.WRITE, AccessMode.DOCUMENT, AccessMode.TEST})


def mutation_to_dict(mutation: MutationRequest) -> dict[str, Any]:
    """Return the persisted, payload-free description of one requested mutation."""

    return {
        "access": mutation.access.value,
        "path": mutation.path,
        "target_path": mutation.target_path,
    }


def mutation_from_dict(payload: dict[str, Any]) -> MutationRequest:
    """Restore a mutation ticket entry without accepting arbitrary extra fields."""

    allowed = {"access", "path", "target_path"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(
            "unsupported scope-amendment mutation field(s): " + ", ".join(unknown)
        )
    access = AccessMode(str(payload.get("access") or ""))
    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("scope-amendment mutation path must not be empty")
    target = payload.get("target_path")
    if target is not None:
        target = str(target).strip() or None
    if access is AccessMode.RENAME and not target:
        raise ValueError("rename scope amendment requires target_path")
    if access is not AccessMode.RENAME and target is not None:
        raise ValueError("target_path is only valid for rename scope amendments")
    return MutationRequest(
        access=access, path=path, target_path=target, source="amendment"
    )


def _operation_matches_mutation(
    operation: IntentOperation, mutation: MutationRequest
) -> bool:
    if operation.resource.kind not in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return False
    if operation.resource.region is not None:
        return False
    if not operation.resource.covers_path(mutation.path):
        return False
    if mutation.access is AccessMode.WRITE:
        if operation.access not in _WRITE_MODES:
            return False
    elif operation.access is not mutation.access:
        return False
    if mutation.access is AccessMode.RENAME:
        sources = (operation.metadata, operation.resource.metadata)
        target = next(
            (
                str(source[key]).replace("\\", "/").lstrip("./")
                for source in sources
                for key in ("rename_to", "target", "to")
                if source.get(key)
            ),
            None,
        )
        return target == mutation.target_path
    return True


def _exact_contingent_index(
    operations: list[IntentOperation], mutation: MutationRequest
) -> int | None:
    for index, operation in enumerate(operations):
        if not operation.contingent:
            continue
        if operation.resource.is_pattern:
            continue
        if _operation_matches_mutation(operation, mutation):
            return index
    return None


def _committed_matches(
    operations: Iterable[IntentOperation], mutation: MutationRequest
) -> bool:
    return any(
        operation.committed and _operation_matches_mutation(operation, mutation)
        for operation in operations
    )


def _new_operation(mutation: MutationRequest, *, ticket_id: str) -> IntentOperation:
    metadata: dict[str, Any] = {"codex_scope_amendment_ticket": ticket_id}
    if mutation.access is AccessMode.RENAME and mutation.target_path is not None:
        metadata["rename_to"] = mutation.target_path
    return IntentOperation(
        access=mutation.access,
        resource=ResourceRef(kind=ResourceKind.FILE, identifier=mutation.path),
        required=True,
        commitment=ScopeCommitment.COMMITTED,
        metadata=metadata,
    )


def build_scope_amendment(
    current: ChangeIntent,
    mutations: Iterable[MutationRequest],
    *,
    ticket_id: str,
    reason: str,
    amended_at: str,
) -> tuple[ChangeIntent, tuple[dict[str, Any], ...]]:
    """Create a monotonic exact-scope amendment from a broker-issued ticket.

    Existing protections, acceptance requirements, dependencies, identity, and base
    revision are preserved. Exact contingent declarations are committed in place;
    broad contingent declarations remain contingent and receive only the concrete
    file capability required by the denied mutation.
    """

    reason = reason.strip()
    if not reason:
        raise ValueError("scope amendment requires a non-empty reason")
    if len(reason) > 1000:
        raise ValueError("scope amendment reason must be at most 1000 characters")

    requested = tuple(mutations)
    if not requested:
        raise ValueError("scope amendment ticket contains no mutations")

    operations = list(current.operations)
    applied: list[dict[str, Any]] = []
    for mutation in requested:
        if _committed_matches(operations, mutation):
            continue
        index = _exact_contingent_index(operations, mutation)
        if index is not None:
            existing = operations[index]
            operations[index] = replace(
                existing,
                commitment=ScopeCommitment.COMMITTED,
                required=True,
                metadata={
                    **existing.metadata,
                    "codex_scope_amendment_ticket": ticket_id,
                },
            )
        else:
            operations.append(_new_operation(mutation, ticket_id=ticket_id))
        applied.append(mutation_to_dict(mutation))

    if not applied:
        raise ValueError("scope amendment no longer changes the active intent")

    metadata = dict(current.metadata)
    history = metadata.get("scope_amendments")
    if history is None:
        history_items: list[dict[str, Any]] = []
    elif isinstance(history, list) and all(isinstance(item, dict) for item in history):
        history_items = [dict(item) for item in history]
    else:
        raise ValueError("active intent has invalid scope_amendments metadata")
    history_items.append(
        {
            "protocol": CODEX_SCOPE_AMENDMENT_PROTOCOL,
            "ticket_id": ticket_id,
            "reason": reason,
            "amended_at": amended_at,
            "operations": applied,
        }
    )
    metadata["scope_amendments"] = history_items[-100:]

    return (
        replace(current, operations=tuple(operations), metadata=metadata),
        tuple(applied),
    )
