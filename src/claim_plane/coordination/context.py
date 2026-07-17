"""Bounded, machine-generated context packs for worker agents."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from claim_plane.core.models import ChangeIntent, ResourceKind


def build_context_pack(
    intent: ChangeIntent,
    *,
    admission: Mapping[str, Any] | None = None,
    active_intents: Iterable[ChangeIntent] = (),
    dependency_records: Iterable[Mapping[str, Any]] = (),
    notices: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    active_by_id = {item.intent_id: item for item in active_intents}
    dependency_records = tuple(dependency_records)
    records_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for record in dependency_records:
        records_by_id.setdefault(str(record.get("depends_on_intent_id")), []).append(
            record
        )

    contracts = []
    terminology: dict[str, dict[str, Any]] = {}
    permissions = []
    for operation in intent.operations:
        resource = operation.resource
        permissions.append(
            {
                "access": operation.access.value,
                "kind": resource.kind.value,
                "identifier": resource.identifier,
                "region": resource.region,
                "concept_id": resource.concept_id,
                "subject_concept_id": resource.subject_concept_id,
                "required": operation.required,
                "commitment": operation.commitment.value,
                "write_enabled": operation.committed or not operation.mutating,
            }
        )
        if resource.kind is ResourceKind.CONTRACT:
            contracts.append(
                {
                    "identifier": resource.identifier,
                    "signature": resource.signature,
                    "access": operation.access.value,
                    "concept_id": resource.concept_id,
                    "subject_concept_id": resource.subject_concept_id,
                }
            )
        if resource.concept_id:
            terminology[resource.concept_id] = {
                "concept_id": resource.concept_id,
                "canonical": resource.metadata.get("canonical") or resource.identifier,
                "surface": resource.identifier,
                "deprecated": bool(resource.metadata.get("deprecated", False)),
            }

    dependency_ids = list(dict.fromkeys([*intent.dependencies, *records_by_id]))
    dependencies = []
    for dependency_id in dependency_ids:
        dependency = active_by_id.get(dependency_id)
        records = records_by_id.get(dependency_id, [])
        status = (
            "missing"
            if any(record.get("status") == "missing" for record in records)
            else "stale"
            if any(
                record.get("status") == "stale"
                or record.get("producer_state") == "stale"
                for record in records
            )
            else "active"
        )
        dependencies.append(
            {
                "intent_id": dependency_id,
                "owner": dependency.owner if dependency else None,
                "available": dependency is not None and status == "active",
                "status": status,
                "kinds": sorted(
                    {str(record.get("dependency_kind")) for record in records}
                ),
                "resource_keys": sorted(
                    {
                        str(record.get("resource_key") or "")
                        for record in records
                        if record.get("resource_key")
                    }
                ),
                "contracts": [
                    op.resource.to_dict()
                    for op in dependency.operations
                    if op.resource.kind is ResourceKind.CONTRACT
                ]
                if dependency
                else [],
            }
        )

    admission = dict(admission or {})
    return {
        "protocol": "claim-plane.context-pack.v1",
        "intent_id": intent.intent_id,
        "task_id": intent.task_id,
        "owner": intent.owner,
        "base_revision": intent.base_revision,
        "permissions": permissions,
        "contracts": contracts,
        "terminology": sorted(
            terminology.values(), key=lambda item: item["concept_id"]
        ),
        "preserves": list(intent.preserves),
        "acceptance": list(intent.acceptance),
        "dependencies": dependencies,
        "coordination": {
            "admission_kind": admission.get("kind"),
            "constraints": admission.get("constraints", []),
            "notifications": admission.get("notifications", []),
            "pending_notices": [dict(item) for item in notices],
        },
        "worker_rules": [
            "Do not mutate undeclared resources.",
            "Do not leave declared bounded regions.",
            "Use canonical terminology and concept-bound contracts.",
            "Request an intent amendment before expanding the write surface or changing a contract.",
            "Stop when the intent becomes stale; refresh premises and re-admit before continuing.",
        ],
    }
