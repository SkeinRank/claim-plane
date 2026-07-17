"""SQLite state boundary with atomic admission, dependencies, and audit events."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import sqlite3
from dataclasses import replace
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from claim_plane.core.models import (
    AccessMode,
    AdmissionDecision,
    AdmissionKind,
    ChangeIntent,
    ObservedAccess,
    Claim,
    ClaimType,
    IntentState,
    ResourceKind,
    ScopeCommitment,
    Verdict,
    VerdictKind,
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.isoformat()


def _future(seconds: int) -> str:
    return _iso(_utc_now() + dt.timedelta(seconds=seconds))


class ClaimRegistry:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        self._conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise
        else:
            self._conn.execute("COMMIT;")

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS grants (
                claim_type TEXT NOT NULL,
                key TEXT NOT NULL,
                canonical_key TEXT,
                identifier TEXT NOT NULL,
                owner TEXT NOT NULL,
                signature TEXT,
                task_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                granted_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL DEFAULT '9999-12-31T00:00:00+00:00',
                version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (claim_type, key)
            );
            CREATE INDEX IF NOT EXISTS idx_grants_canonical ON grants(claim_type, canonical_key);
            CREATE INDEX IF NOT EXISTS idx_grants_lease ON grants(lease_expires_at);

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decided_at TEXT NOT NULL,
                verdict TEXT NOT NULL,
                claim_type TEXT NOT NULL,
                identifier TEXT NOT NULL,
                key TEXT NOT NULL,
                owner TEXT NOT NULL,
                incumbent TEXT,
                signature TEXT,
                guidance TEXT,
                fingerprint TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                base_revision TEXT NOT NULL,
                state TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                admission_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                content_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state);
            CREATE INDEX IF NOT EXISTS idx_intents_owner ON intents(owner);
            CREATE INDEX IF NOT EXISTS idx_intents_lease ON intents(lease_expires_at);

            CREATE TABLE IF NOT EXISTS intent_dependencies (
                intent_id TEXT NOT NULL,
                depends_on_intent_id TEXT NOT NULL,
                dependency_kind TEXT NOT NULL,
                resource_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (intent_id, depends_on_intent_id, dependency_kind, resource_key)
            );
            CREATE INDEX IF NOT EXISTS idx_dependencies_producer
                ON intent_dependencies(depends_on_intent_id, status);

            CREATE TABLE IF NOT EXISTS coordination_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_intent_id TEXT NOT NULL,
                producer_intent_id TEXT NOT NULL,
                notice_type TEXT NOT NULL,
                resource_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                acknowledged_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_notices_recipient
                ON coordination_notices(recipient_intent_id, status);

            CREATE TABLE IF NOT EXISTS coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                intent_id TEXT,
                owner TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS verification_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                verified_at TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                clean INTEGER NOT NULL,
                report_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS observation_sessions (
                session_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL,
                monitor_id TEXT NOT NULL,
                key_id TEXT NOT NULL,
                coverage TEXT NOT NULL,
                required_tools_json TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL DEFAULT 'open',
                complete INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                head_hash TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                sealed_at TEXT,
                attestation_json TEXT,
                broker_instance_id TEXT,
                broker_attestation_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_observation_sessions_intent
                ON observation_sessions(intent_id, state);

            CREATE TABLE IF NOT EXISTS observation_events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                access_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                event_hmac TEXT NOT NULL,
                PRIMARY KEY (session_id, seq),
                FOREIGN KEY (session_id) REFERENCES observation_sessions(session_id)
            );

            CREATE TABLE IF NOT EXISTS broker_instances (
                instance_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL,
                intent_content_version INTEGER NOT NULL,
                intent_fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                monitor_id TEXT NOT NULL,
                key_id TEXT NOT NULL,
                root_path TEXT NOT NULL,
                repo_identity TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                initial_tree_hash TEXT NOT NULL DEFAULT '',
                expected_tree_hash TEXT NOT NULL DEFAULT '',
                writer_lease_seconds INTEGER NOT NULL DEFAULT 300,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                policy_json TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                binary_digest TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                stopped_at TEXT,
                attestation_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_broker_instances_intent
                ON broker_instances(intent_id, state);

            CREATE TABLE IF NOT EXISTS broker_writer_leases (
                root_path TEXT PRIMARY KEY,
                repo_identity TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                instance_id TEXT NOT NULL UNIQUE,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS broker_fencing_counters (
                root_path TEXT PRIMARY KEY,
                last_token INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_broker_writer_leases_expiry
                ON broker_writer_leases(expires_at);

            CREATE TABLE IF NOT EXISTS broker_operations (
                operation_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                mode TEXT NOT NULL,
                path TEXT NOT NULL,
                target_path TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT,
                prepared_at TEXT NOT NULL,
                committed_at TEXT,
                error TEXT,
                event_start_seq INTEGER,
                event_end_seq INTEGER,
                pre_tree_hash TEXT,
                post_tree_hash TEXT,
                prepare_hmac TEXT NOT NULL,
                commit_hmac TEXT,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                UNIQUE(instance_id, request_id),
                FOREIGN KEY (instance_id) REFERENCES broker_instances(instance_id)
            );
            CREATE INDEX IF NOT EXISTS idx_broker_operations_state
                ON broker_operations(instance_id, state);
            """
        )
        self._ensure_column("grants", "canonical_key", "TEXT")
        self._ensure_column("grants", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column(
            "grants",
            "lease_expires_at",
            "TEXT NOT NULL DEFAULT '9999-12-31T00:00:00+00:00'",
        )
        self._ensure_column("grants", "version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("decisions", "details_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("intents", "content_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("observation_sessions", "broker_instance_id", "TEXT")
        self._ensure_column("observation_sessions", "broker_attestation_json", "TEXT")
        self._ensure_column(
            "broker_instances", "initial_tree_hash", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column(
            "broker_instances", "expected_tree_hash", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column(
            "broker_instances", "writer_lease_seconds", "INTEGER NOT NULL DEFAULT 300"
        )
        self._ensure_column(
            "broker_instances", "fencing_token", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(
            "broker_writer_leases", "fencing_token", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(
            "broker_operations", "fencing_token", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column("broker_operations", "pre_tree_hash", "TEXT")
        self._ensure_column("broker_operations", "post_tree_hash", "TEXT")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ---------------------------------------------------------------- claims

    def arbitrate_claim(
        self, claim: Claim, *, canonical_key: str | None = None
    ) -> Verdict:
        with self._immediate():
            self._expire_claims_locked()
            row = self._conn.execute(
                "SELECT * FROM grants WHERE claim_type=? AND key=?",
                (claim.claim_type.value, claim.key),
            ).fetchone()
            semantic_row = None
            if row is None and canonical_key:
                semantic_row = self._conn.execute(
                    "SELECT * FROM grants WHERE claim_type=? AND canonical_key=? ORDER BY granted_at LIMIT 1",
                    (claim.claim_type.value, canonical_key),
                ).fetchone()
            verdict = self._claim_verdict(claim, row=row, semantic_row=semantic_row)
            if verdict.granted:
                if row is not None and row["owner"] == claim.owner:
                    self._conn.execute(
                        "UPDATE grants SET lease_expires_at=?, version=version+1, canonical_key=COALESCE(canonical_key, ?) WHERE claim_type=? AND key=?",
                        (
                            _future(claim.lease_seconds),
                            canonical_key,
                            claim.claim_type.value,
                            claim.key,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO grants
                          (claim_type,key,canonical_key,identifier,owner,signature,task_id,
                           metadata_json,granted_at,lease_expires_at,version)
                        VALUES (?,?,?,?,?,?,?,?,?,?,1)
                        """,
                        (
                            claim.claim_type.value,
                            claim.key,
                            canonical_key,
                            claim.identifier,
                            claim.owner,
                            claim.signature,
                            claim.task_id,
                            json.dumps(
                                dict(claim.metadata), ensure_ascii=False, sort_keys=True
                            ),
                            _iso(_utc_now()),
                            _future(claim.lease_seconds),
                        ),
                    )
            self._record_decision_locked(verdict, {"canonical_key": canonical_key})
            return verdict

    @staticmethod
    def _claim_verdict(
        claim: Claim, *, row: sqlite3.Row | None, semantic_row: sqlite3.Row | None
    ) -> Verdict:
        if row is None and semantic_row is None:
            return Verdict(
                VerdictKind.GRANTED,
                claim,
                guidance=f"'{claim.identifier}' is reserved for {claim.owner}.",
            )
        incumbent = row or semantic_row
        assert incumbent is not None
        if row is not None and incumbent["owner"] == claim.owner:
            return Verdict(
                VerdictKind.GRANTED,
                claim,
                guidance=f"{claim.owner} already owns '{claim.identifier}'; lease renewed.",
            )
        if semantic_row is not None:
            return Verdict(
                VerdictKind.DUPLICATE,
                claim,
                incumbent=incumbent["owner"],
                incumbent_signature=incumbent["signature"],
                guidance=(
                    f"'{claim.identifier}' resolves to the canonical concept already represented by "
                    f"'{incumbent['identifier']}' and owned by {incumbent['owner']}."
                ),
            )
        if claim.claim_type is ClaimType.CONTRACT and claim.signature_key:
            incumbent_signature = incumbent["signature"]
            incumbent_key = (
                " ".join(incumbent_signature.split()) if incumbent_signature else None
            )
            if incumbent_key and incumbent_key != claim.signature_key:
                return Verdict(
                    VerdictKind.CONTRACT_MISMATCH,
                    claim,
                    incumbent=incumbent["owner"],
                    incumbent_signature=incumbent_signature,
                    guidance=f"Align with existing contract `{incumbent_signature}` before coding.",
                )
        return Verdict(
            VerdictKind.CONFLICT,
            claim,
            incumbent=incumbent["owner"],
            incumbent_signature=incumbent["signature"],
            guidance=f"'{claim.identifier}' is already owned by {incumbent['owner']}.",
        )

    def _record_decision_locked(
        self, verdict: Verdict, details: Mapping[str, object]
    ) -> None:
        claim = verdict.claim
        self._conn.execute(
            """
            INSERT INTO decisions
              (decided_at,verdict,claim_type,identifier,key,owner,incumbent,signature,
               guidance,fingerprint,details_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _iso(_utc_now()),
                verdict.kind.value,
                claim.claim_type.value,
                claim.identifier,
                claim.key,
                claim.owner,
                verdict.incumbent,
                claim.signature,
                verdict.guidance,
                claim.fingerprint(),
                json.dumps(dict(details), ensure_ascii=False, sort_keys=True),
            ),
        )

    def incumbent(self, claim_type: ClaimType, key: str) -> sqlite3.Row | None:
        self.expire_stale()
        return self._conn.execute(
            "SELECT * FROM grants WHERE claim_type=? AND key=?", (claim_type.value, key)
        ).fetchone()

    def all_grants(self) -> list[dict]:
        self.expire_stale()
        return [
            self._decode_row(row, ("metadata_json",))
            for row in self._conn.execute("SELECT * FROM grants ORDER BY granted_at")
        ]

    def decision_log(self) -> list[dict]:
        return [
            self._decode_row(row, ("details_json",))
            for row in self._conn.execute("SELECT * FROM decisions ORDER BY id")
        ]

    def release(self, owner: str) -> int:
        with self._immediate():
            return self._conn.execute(
                "DELETE FROM grants WHERE owner=?", (owner,)
            ).rowcount

    def renew_claims(self, owner: str, lease_seconds: int = 900) -> int:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._immediate():
            return self._conn.execute(
                "UPDATE grants SET lease_expires_at=?, version=version+1 WHERE owner=?",
                (_future(lease_seconds), owner),
            ).rowcount

    def _expire_claims_locked(self) -> int:
        return self._conn.execute(
            "DELETE FROM grants WHERE lease_expires_at <= ?", (_iso(_utc_now()),)
        ).rowcount

    # --------------------------------------------------------------- intents

    def _known_intent_ids_locked(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT intent_id FROM intents WHERE state IN (?,?,?)",
            (
                IntentState.ADMITTED.value,
                IntentState.ACTIVE.value,
                IntentState.COMPLETED.value,
            ),
        ).fetchall()
        return {row["intent_id"] for row in rows}

    def _active_intents_locked(
        self, *, exclude: str | None = None
    ) -> list[ChangeIntent]:
        query = "SELECT payload_json FROM intents WHERE state IN (?,?) AND lease_expires_at > ?"
        params: list[object] = [
            IntentState.ADMITTED.value,
            IntentState.ACTIVE.value,
            _iso(_utc_now()),
        ]
        if exclude:
            query += " AND intent_id<>?"
            params.append(exclude)
        query += " ORDER BY created_at"
        return [
            ChangeIntent.from_dict(json.loads(row["payload_json"]))
            for row in self._conn.execute(query, params)
        ]

    def admit_intent(
        self,
        intent: ChangeIntent,
        evaluator: Callable[
            [ChangeIntent, list[ChangeIntent], set[str]], AdmissionDecision
        ],
    ) -> AdmissionDecision:
        with self._immediate():
            self._expire_intents_locked()
            existing = self._conn.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent.intent_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != intent.fingerprint():
                    raise ValueError(
                        f"intent_id {intent.intent_id!r} already exists with different content; use amend"
                    )
                current_state = IntentState(existing["state"])
                if current_state is not IntentState.BLOCKED:
                    return self._decision_from_json(existing["admission_json"])

                # A blocked decision is inherently time-dependent: its blockers may
                # have completed, expired, or been released since the first attempt.
                # Re-evaluate identical retries atomically instead of returning a
                # permanently cached rejection.
                active = self._active_intents_locked(exclude=intent.intent_id)
                decision = evaluator(intent, active, self._known_intent_ids_locked())
                if decision.allowed:
                    cycle = self._dependency_cycle_locked(intent, decision)
                    if cycle:
                        decision = _cycle_rejection(intent, decision, cycle)
                state = (
                    IntentState.ADMITTED if decision.allowed else IntentState.BLOCKED
                )
                now = _iso(_utc_now())
                self._conn.execute(
                    """
                    UPDATE intents
                    SET state=?,admission_json=?,updated_at=?,lease_expires_at=?,version=version+1
                    WHERE intent_id=?
                    """,
                    (
                        state.value,
                        json.dumps(
                            decision.to_dict(), ensure_ascii=False, sort_keys=True
                        ),
                        now,
                        _future(intent.lease_seconds),
                        intent.intent_id,
                    ),
                )
                if decision.allowed:
                    self._replace_dependency_edges_locked(intent, decision)
                self._event_locked(
                    "intent_readmitted" if decision.allowed else "intent_reblocked",
                    intent.intent_id,
                    intent.owner,
                    decision.to_dict(),
                )
                return decision
            active = self._active_intents_locked()
            decision = evaluator(intent, active, self._known_intent_ids_locked())
            if decision.allowed:
                cycle = self._dependency_cycle_locked(intent, decision)
                if cycle:
                    decision = _cycle_rejection(intent, decision, cycle)
            state = IntentState.ADMITTED if decision.allowed else IntentState.BLOCKED
            now = _iso(_utc_now())
            self._conn.execute(
                """
                INSERT INTO intents
                  (intent_id,task_id,owner,base_revision,state,fingerprint,payload_json,
                   admission_json,created_at,updated_at,lease_expires_at,version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    intent.intent_id,
                    intent.task_id,
                    intent.owner,
                    intent.base_revision,
                    state.value,
                    intent.fingerprint(),
                    json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True),
                    json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    _future(intent.lease_seconds),
                ),
            )
            if decision.allowed:
                self._replace_dependency_edges_locked(intent, decision)
            self._event_locked(
                "intent_admitted" if decision.allowed else "intent_blocked",
                intent.intent_id,
                intent.owner,
                decision.to_dict(),
            )
            return decision

    def promote_contingent_operations(
        self,
        intent_id: str,
        *,
        path: str,
        modes: Iterable[AccessMode],
        evaluator: Callable[
            [ChangeIntent, list[ChangeIntent], set[str]], AdmissionDecision
        ],
        expected_version: int | None = None,
        broker_instance_id: str | None = None,
        broker_key: bytes | None = None,
    ) -> AdmissionDecision:
        """Promote matching contingent path operations after atomic re-admission.

        A rejected promotion leaves the currently admitted intent unchanged. When a
        trusted broker initiates the promotion, its attested intent binding is advanced
        to the new content version in the same transaction so execution can continue
        without reopening a capability gap.
        """

        normalized_modes = {AccessMode(mode) for mode in modes}
        if not normalized_modes:
            raise ValueError("scope promotion requires at least one access mode")
        raw_path = str(path).replace("\\", "/").strip()
        if raw_path.startswith("/") or ".." in raw_path.split("/"):
            raise ValueError("scope promotion path must stay inside the repository")
        while raw_path.startswith("./"):
            raw_path = raw_path[2:]
        path = raw_path.rstrip("/")
        if not path:
            raise ValueError("scope promotion path must not be empty")

        with self._immediate():
            self._expire_intents_locked()
            row = self._conn.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown intent: {intent_id}")
            current_state = IntentState(row["state"])
            if current_state not in {IntentState.ADMITTED, IntentState.ACTIVE}:
                raise ValueError(
                    f"cannot promote contingent scope in state {current_state.value}"
                )
            if expected_version is not None and int(row["version"]) != expected_version:
                raise ValueError(
                    f"stale intent version: expected {expected_version}, current {row['version']}"
                )

            current = ChangeIntent.from_dict(json.loads(row["payload_json"]))
            promoted: list[dict[str, object]] = []
            operations = []
            committed_keys = {
                (
                    operation.access.value,
                    operation.resource.kind.value,
                    operation.resource.identifier,
                    operation.resource.region,
                )
                for operation in current.committed_operations
            }
            for operation in current.operations:
                matches = (
                    operation.contingent
                    and operation.access in normalized_modes
                    and operation.resource.kind
                    in {ResourceKind.FILE, ResourceKind.DOCUMENT}
                    and operation.resource.covers_path(path)
                )
                if not matches:
                    operations.append(operation)
                    continue

                if operation.resource.is_pattern:
                    # Keep the broad possibility contingent and promote only the
                    # concrete path that the worker is about to mutate.
                    operations.append(operation)
                    concrete_resource = replace(
                        operation.resource,
                        identifier=path,
                        metadata={
                            **operation.resource.metadata,
                            "promoted_from_contingent": operation.resource.identifier,
                        },
                    )
                    committed = replace(
                        operation,
                        resource=concrete_resource,
                        commitment=ScopeCommitment.COMMITTED,
                    )
                    key = (
                        committed.access.value,
                        committed.resource.kind.value,
                        committed.resource.identifier,
                        committed.resource.region,
                    )
                    if key not in committed_keys:
                        operations.append(committed)
                        committed_keys.add(key)
                    promoted.append(
                        {
                            "source": operation.to_dict(),
                            "committed": committed.to_dict(),
                        }
                    )
                    continue

                committed = replace(operation, commitment=ScopeCommitment.COMMITTED)
                operations.append(committed)
                promoted.append(
                    {
                        "source": operation.to_dict(),
                        "committed": committed.to_dict(),
                    }
                )

            if not promoted:
                raise ValueError(
                    f"no contingent operation for {path!r} matches modes "
                    + ", ".join(sorted(mode.value for mode in normalized_modes))
                )

            candidate = replace(
                current,
                operations=tuple(operations),
                metadata={**current.metadata, "_scope_expansion": True},
            )
            active = self._active_intents_locked(exclude=intent_id)
            decision = evaluator(
                candidate, active, self._known_intent_ids_locked() | {intent_id}
            )
            if decision.allowed:
                cycle = self._dependency_cycle_locked(candidate, decision)
                if cycle:
                    decision = _cycle_rejection(candidate, decision, cycle)
            if not decision.allowed:
                self._event_locked(
                    "intent_scope_expansion_rejected",
                    intent_id,
                    current.owner,
                    {
                        "path": path,
                        "modes": sorted(mode.value for mode in normalized_modes),
                        "promoted_operations": promoted,
                        "decision": decision.to_dict(),
                    },
                )
                return decision

            now = _iso(_utc_now())
            self._conn.execute(
                """
                UPDATE intents
                SET state=?,fingerprint=?,payload_json=?,admission_json=?,updated_at=?,
                    lease_expires_at=?,version=version+1,content_version=content_version+1
                WHERE intent_id=?
                """,
                (
                    current_state.value,
                    candidate.fingerprint(),
                    json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True),
                    json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True),
                    now,
                    _future(candidate.lease_seconds),
                    intent_id,
                ),
            )
            self._replace_dependency_edges_locked(candidate, decision)

            if broker_instance_id is not None:
                if not broker_key:
                    raise ValueError(
                        "broker_key is required when rebinding a broker after scope promotion"
                    )
                updated = self._conn.execute(
                    "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
                assert updated is not None
                self._refresh_broker_intent_binding_locked(
                    broker_instance_id,
                    intent_id=intent_id,
                    intent_content_version=int(updated["content_version"]),
                    intent_fingerprint=str(updated["fingerprint"]),
                    broker_key=broker_key,
                )

            self._event_locked(
                "intent_scope_expanded",
                intent_id,
                current.owner,
                {
                    "path": path,
                    "modes": sorted(mode.value for mode in normalized_modes),
                    "promoted_operations": promoted,
                    "decision": decision.to_dict(),
                    "broker_instance_id": broker_instance_id,
                },
            )
            return decision

    def amend_intent(
        self,
        intent: ChangeIntent,
        evaluator: Callable[
            [ChangeIntent, list[ChangeIntent], set[str]], AdmissionDecision
        ],
        *,
        expected_version: int | None = None,
    ) -> AdmissionDecision:
        with self._immediate():
            self._expire_intents_locked()
            row = self._conn.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent.intent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown intent: {intent.intent_id}")
            if row["owner"] != intent.owner:
                raise ValueError("intent owner cannot change during amendment")
            if expected_version is not None and int(row["version"]) != expected_version:
                raise ValueError(
                    f"stale intent version: expected {expected_version}, current {row['version']}"
                )
            current_state = IntentState(row["state"])
            if current_state not in {
                IntentState.ADMITTED,
                IntentState.ACTIVE,
                IntentState.STALE,
                IntentState.BLOCKED,
            }:
                raise ValueError(f"cannot amend intent in state {current_state.value}")
            old_intent = ChangeIntent.from_dict(json.loads(row["payload_json"]))
            active = self._active_intents_locked(exclude=intent.intent_id)
            decision = evaluator(
                intent, active, self._known_intent_ids_locked() | {intent.intent_id}
            )
            if decision.allowed:
                cycle = self._dependency_cycle_locked(intent, decision)
                if cycle:
                    decision = _cycle_rejection(intent, decision, cycle)
            if not decision.allowed:
                self._event_locked(
                    "intent_amendment_rejected",
                    intent.intent_id,
                    intent.owner,
                    decision.to_dict(),
                )
                return decision
            changed_keys = sorted(_changed_resource_keys(old_intent, intent))
            now = _iso(_utc_now())
            self._conn.execute(
                """
                UPDATE intents SET task_id=?,base_revision=?,state=?,fingerprint=?,payload_json=?,
                    admission_json=?,updated_at=?,lease_expires_at=?,version=version+1,
                    content_version=content_version+1
                WHERE intent_id=?
                """,
                (
                    intent.task_id,
                    intent.base_revision,
                    IntentState.ADMITTED.value,
                    intent.fingerprint(),
                    json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True),
                    json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True),
                    now,
                    _future(intent.lease_seconds),
                    intent.intent_id,
                ),
            )
            self._replace_dependency_edges_locked(intent, decision)
            stale = (
                self._invalidate_dependents_locked(
                    intent.intent_id, changed_keys, reason="producer_amended"
                )
                if changed_keys
                else []
            )
            self._event_locked(
                "intent_amended",
                intent.intent_id,
                intent.owner,
                {
                    "decision": decision.to_dict(),
                    "changed_resources": changed_keys,
                    "stale_dependents": stale,
                },
            )
            return decision

    def _replace_dependency_edges_locked(
        self, intent: ChangeIntent, decision: AdmissionDecision
    ) -> None:
        now = _iso(_utc_now())
        self._conn.execute(
            "DELETE FROM intent_dependencies WHERE intent_id=?", (intent.intent_id,)
        )
        for dependent, producer, kind, resource_key in self._dependency_candidates(
            intent, decision
        ):
            if kind == "explicit":
                producer_row = self._conn.execute(
                    "SELECT state FROM intents WHERE intent_id=?", (producer,)
                ).fetchone()
                if producer_row is None:
                    status = "missing"
                elif producer_row["state"] == IntentState.STALE.value:
                    status = "stale"
                elif producer_row["state"] in {
                    IntentState.ADMITTED.value,
                    IntentState.ACTIVE.value,
                    IntentState.COMPLETED.value,
                }:
                    status = "active"
                else:
                    status = "missing"
            else:
                status = "active"
            self._upsert_dependency_locked(
                dependent,
                producer,
                kind,
                resource_key,
                now,
                status=status,
            )

    def _dependency_candidates(
        self, intent: ChangeIntent, decision: AdmissionDecision
    ) -> set[tuple[str, str, str, str]]:
        candidates: set[tuple[str, str, str, str]] = {
            (intent.intent_id, dependency, "explicit", "")
            for dependency in intent.dependencies
        }
        for conflict in decision.conflicts:
            if conflict.kind not in {
                AdmissionKind.NOTIFY_ON_CHANGE,
                AdmissionKind.CONTRACT_DEPENDENCY,
            }:
                continue
            incoming = conflict.incoming_operation
            existing = conflict.existing_operation
            if incoming.access is AccessMode.READ and existing.mutating:
                dependent, producer = intent.intent_id, conflict.existing_intent_id
                resource_key = (
                    incoming.resource.subject_key or incoming.resource.semantic_key
                )
            elif existing.access is AccessMode.READ and incoming.mutating:
                dependent, producer = conflict.existing_intent_id, intent.intent_id
                resource_key = (
                    existing.resource.subject_key or existing.resource.semantic_key
                )
            else:
                dependent, producer = intent.intent_id, conflict.existing_intent_id
                resource_key = (
                    incoming.resource.subject_key or incoming.resource.semantic_key
                )
            candidates.add((dependent, producer, "premise", resource_key or ""))
        return candidates

    def _dependency_cycle_locked(
        self, intent: ChangeIntent, decision: AdmissionDecision
    ) -> list[str] | None:
        """Return a cycle after applying the proposed dependency update."""

        edges = {
            (row["intent_id"], row["depends_on_intent_id"])
            for row in self._conn.execute(
                "SELECT intent_id,depends_on_intent_id FROM intent_dependencies WHERE status='active'"
            )
            if row["intent_id"] != intent.intent_id
        }
        edges.update(
            (dependent, producer)
            for dependent, producer, _kind, _key in self._dependency_candidates(
                intent, decision
            )
        )
        return _find_dependency_cycle(edges)

    def _upsert_dependency_locked(
        self,
        dependent: str,
        producer: str,
        kind: str,
        key: str,
        now: str,
        *,
        status: str = "active",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO intent_dependencies
              (intent_id,depends_on_intent_id,dependency_kind,resource_key,status,created_at,updated_at)
            VALUES (?,?,?,?, ?, ?, ?)
            ON CONFLICT(intent_id,depends_on_intent_id,dependency_kind,resource_key)
            DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
            """,
            (dependent, producer, kind, key, status, now, now),
        )

    def invalidate_dependents(
        self, producer_intent_id: str, resource_keys: list[str], *, reason: str
    ) -> list[str]:
        with self._immediate():
            return self._invalidate_dependents_locked(
                producer_intent_id, resource_keys, reason=reason
            )

    def _invalidate_dependents_locked(
        self, producer: str, resource_keys: list[str], *, reason: str
    ) -> list[str]:
        stale: list[str] = []
        seen_producers = {producer}
        queue: deque[tuple[str, tuple[str, ...], int, tuple[str, ...]]] = deque(
            [(producer, tuple(resource_keys), 0, (producer,))]
        )
        now = _iso(_utc_now())

        while queue:
            current_producer, keys, depth, chain = queue.popleft()
            params: list[object] = [current_producer]
            clause = ""
            # The direct hop is scoped to changed premises. Once an intent is
            # stale, all outputs it could have produced are untrusted downstream.
            if depth == 0 and keys:
                placeholders = ",".join("?" for _ in keys)
                clause = f" AND (resource_key='' OR resource_key IN ({placeholders}))"
                params.extend(keys)
            rows = self._conn.execute(
                f"SELECT DISTINCT intent_id,resource_key FROM intent_dependencies WHERE depends_on_intent_id=? AND status='active'{clause}",
                params,
            ).fetchall()
            for row in rows:
                dependent = row["intent_id"]
                current = self._conn.execute(
                    "SELECT owner,state FROM intents WHERE intent_id=?", (dependent,)
                ).fetchone()
                if current is None or current["state"] not in {
                    IntentState.ADMITTED.value,
                    IntentState.ACTIVE.value,
                    IntentState.COMPLETED.value,
                }:
                    continue

                self._conn.execute(
                    "UPDATE intents SET state=?,updated_at=?,version=version+1 WHERE intent_id=?",
                    (IntentState.STALE.value, now, dependent),
                )
                self._conn.execute(
                    "UPDATE intent_dependencies SET status='stale',updated_at=? WHERE intent_id=? AND depends_on_intent_id=?",
                    (now, dependent, current_producer),
                )
                payload = {
                    "reason": reason,
                    "resource_keys": list(keys),
                    "root_producer": producer,
                    "direct_producer": current_producer,
                    "depth": depth + 1,
                    "dependency_chain": [*chain, dependent],
                }
                self._insert_notice_locked(
                    recipient=dependent,
                    producer=current_producer,
                    notice_type="premise_invalidated",
                    resource_key=row["resource_key"] or "",
                    payload=payload,
                    now=now,
                )
                self._event_locked(
                    "intent_stale",
                    dependent,
                    current["owner"],
                    {"producer": current_producer, **payload},
                )
                stale.append(dependent)
                if dependent not in seen_producers:
                    seen_producers.add(dependent)
                    queue.append((dependent, (), depth + 1, (*chain, dependent)))

        return sorted(set(stale))

    def _insert_notice_locked(
        self,
        *,
        recipient: str,
        producer: str,
        notice_type: str,
        resource_key: str,
        payload: Mapping[str, object],
        now: str,
    ) -> None:
        existing = self._conn.execute(
            """
            SELECT id FROM coordination_notices
            WHERE recipient_intent_id=? AND producer_intent_id=? AND notice_type=?
              AND resource_key=? AND status='pending'
            LIMIT 1
            """,
            (recipient, producer, notice_type, resource_key),
        ).fetchone()
        if existing is not None:
            self._conn.execute(
                "UPDATE coordination_notices SET payload_json=? WHERE id=?",
                (json.dumps(dict(payload), sort_keys=True), existing["id"]),
            )
            return
        self._conn.execute(
            """
            INSERT INTO coordination_notices
              (recipient_intent_id,producer_intent_id,notice_type,resource_key,status,payload_json,created_at)
            VALUES (?,?,?,?, 'pending', ?, ?)
            """,
            (
                recipient,
                producer,
                notice_type,
                resource_key,
                json.dumps(dict(payload), sort_keys=True),
                now,
            ),
        )

    def dependencies(self, intent_id: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT d.*, p.state AS producer_state
            FROM intent_dependencies d
            LEFT JOIN intents p ON p.intent_id=d.depends_on_intent_id
            WHERE d.intent_id=?
            ORDER BY d.dependency_kind,d.depends_on_intent_id,d.resource_key
            """,
            (intent_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def dependency_graph(self) -> dict[str, object]:
        rows = [
            dict(row)
            for row in self._conn.execute(
                """
                SELECT d.*, consumer.state AS consumer_state, producer.state AS producer_state
                FROM intent_dependencies d
                LEFT JOIN intents consumer ON consumer.intent_id=d.intent_id
                LEFT JOIN intents producer ON producer.intent_id=d.depends_on_intent_id
                ORDER BY d.intent_id,d.depends_on_intent_id,d.dependency_kind,d.resource_key
                """
            )
        ]
        intents = [
            dict(row)
            for row in self._conn.execute(
                "SELECT intent_id,state,owner,version FROM intents ORDER BY created_at"
            )
        ]
        nodes = {row["intent_id"] for row in intents}
        edges = {
            (row["intent_id"], row["depends_on_intent_id"])
            for row in rows
            if row["status"] == "active"
        }
        cycle = _find_dependency_cycle(edges)
        order = _topological_dependency_order(nodes, edges) if cycle is None else []
        return {
            "protocol": "claim-plane.dependency-graph.v1",
            "acyclic": cycle is None,
            "cycle": cycle or [],
            "topological_order": order,
            "nodes": intents,
            "edges": rows,
        }

    def notices(self, intent_id: str, *, pending_only: bool = True) -> list[dict]:
        query = "SELECT * FROM coordination_notices WHERE recipient_intent_id=?"
        params: list[object] = [intent_id]
        if pending_only:
            query += " AND status='pending'"
        query += " ORDER BY id"
        return [
            self._decode_row(row, ("payload_json",))
            for row in self._conn.execute(query, params)
        ]

    def acknowledge_notice(self, notice_id: int) -> None:
        with self._immediate():
            cursor = self._conn.execute(
                "UPDATE coordination_notices SET status='acknowledged',acknowledged_at=? WHERE id=? AND status='pending'",
                (_iso(_utc_now()), notice_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown or already acknowledged notice: {notice_id}")

    def activate_intent(self, intent_id: str) -> None:
        self._set_intent_state(
            intent_id,
            IntentState.ACTIVE,
            "intent_activated",
            {IntentState.ADMITTED, IntentState.ACTIVE},
        )

    def complete_intent(self, intent_id: str) -> None:
        self._set_intent_state(
            intent_id,
            IntentState.COMPLETED,
            "intent_completed",
            {IntentState.ADMITTED, IntentState.ACTIVE},
        )

    def release_intent(self, intent_id: str) -> None:
        self._set_intent_state(
            intent_id,
            IntentState.RELEASED,
            "intent_released",
            {
                IntentState.BLOCKED,
                IntentState.ADMITTED,
                IntentState.ACTIVE,
                IntentState.EXPIRED,
                IntentState.STALE,
            },
            no_op_from={IntentState.COMPLETED},
        )

    def _set_intent_state(
        self,
        intent_id: str,
        state: IntentState,
        event_type: str,
        allowed_from: set[IntentState],
        *,
        no_op_from: set[IntentState] | None = None,
    ) -> None:
        with self._immediate():
            row = self._conn.execute(
                "SELECT owner,state FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown intent: {intent_id}")
            current = IntentState(row["state"])
            if current in (no_op_from or set()):
                self._event_locked(
                    f"{event_type}_noop",
                    intent_id,
                    row["owner"],
                    {
                        "state": current.value,
                        "requested_state": state.value,
                        "reason": "terminal_state_preserved",
                    },
                )
                return
            if current not in allowed_from:
                raise ValueError(
                    f"cannot move intent {intent_id} from {current.value} to {state.value}"
                )
            self._conn.execute(
                "UPDATE intents SET state=?,updated_at=?,version=version+1 WHERE intent_id=?",
                (state.value, _iso(_utc_now()), intent_id),
            )
            self._event_locked(
                event_type, intent_id, row["owner"], {"state": state.value}
            )

    def heartbeat_intent(self, intent_id: str, lease_seconds: int = 900) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._immediate():
            row = self._conn.execute(
                "SELECT owner,state FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown intent: {intent_id}")
            if IntentState(row["state"]) not in {
                IntentState.ADMITTED,
                IntentState.ACTIVE,
            }:
                raise ValueError(
                    f"cannot heartbeat intent {intent_id} in state {row['state']}"
                )
            self._conn.execute(
                "UPDATE intents SET lease_expires_at=?,updated_at=?,version=version+1 WHERE intent_id=?",
                (_future(lease_seconds), _iso(_utc_now()), intent_id),
            )
            self._event_locked(
                "intent_heartbeat",
                intent_id,
                row["owner"],
                {"lease_seconds": lease_seconds},
            )

    def get_intent(self, intent_id: str) -> ChangeIntent | None:
        self.expire_stale()
        row = self._conn.execute(
            "SELECT payload_json FROM intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        return ChangeIntent.from_dict(json.loads(row["payload_json"])) if row else None

    def get_intent_record(self, intent_id: str) -> dict | None:
        self.expire_stale()
        row = self._conn.execute(
            "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        data = self._decode_row(row, ("payload_json", "admission_json"))
        data["dependencies"] = self.dependencies(intent_id)
        data["notices"] = self.notices(intent_id)
        return data

    def list_intents(self, *, active_only: bool = False) -> list[dict]:
        self.expire_stale()
        if active_only:
            rows = self._conn.execute(
                "SELECT * FROM intents WHERE state IN (?,?) ORDER BY created_at",
                (IntentState.ADMITTED.value, IntentState.ACTIVE.value),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM intents ORDER BY created_at"
            ).fetchall()
        return [
            self._decode_row(row, ("payload_json", "admission_json")) for row in rows
        ]

    def active_intents(self) -> list[ChangeIntent]:
        return [
            ChangeIntent.from_dict(record["payload_json"])
            for record in self.list_intents(active_only=True)
        ]

    def coordination_events(self) -> list[dict]:
        return [
            self._decode_row(row, ("payload_json",))
            for row in self._conn.execute(
                "SELECT * FROM coordination_events ORDER BY id"
            )
        ]

    def _event_locked(
        self,
        event_type: str,
        intent_id: str | None,
        owner: str | None,
        payload: Mapping[str, object],
    ) -> None:
        self._conn.execute(
            "INSERT INTO coordination_events (occurred_at,event_type,intent_id,owner,payload_json) VALUES (?,?,?,?,?)",
            (
                _iso(_utc_now()),
                event_type,
                intent_id,
                owner,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
            ),
        )

    def _expire_intents_locked(self) -> int:
        return self._conn.execute(
            """
            UPDATE intents SET state=?,updated_at=?,version=version+1
            WHERE state IN (?,?) AND lease_expires_at <= ?
            """,
            (
                IntentState.EXPIRED.value,
                _iso(_utc_now()),
                IntentState.ADMITTED.value,
                IntentState.ACTIVE.value,
                _iso(_utc_now()),
            ),
        ).rowcount

    def expire_stale(self) -> dict[str, int]:
        with self._immediate():
            return {
                "claims": self._expire_claims_locked(),
                "intents": self._expire_intents_locked(),
            }

    # ------------------------------------------------ trusted observations

    def start_observation_session(
        self,
        session_id: str,
        intent_id: str,
        *,
        monitor_id: str,
        key_id: str = "default",
        coverage: str = "tool_proxy",
        required_tools: Iterable[str] = (),
    ) -> dict:
        if not session_id.strip() or not monitor_id.strip():
            raise ValueError("session_id and monitor_id must not be empty")
        if coverage not in {"tool_proxy", "brokered_proxy", "os_monitor", "declared"}:
            raise ValueError("unsupported observation coverage")
        tools = tuple(
            dict.fromkeys(item.strip() for item in required_tools if item.strip())
        )
        now = _iso(_utc_now())
        with self._immediate():
            intent = self._conn.execute(
                "SELECT intent_id FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise KeyError(f"unknown intent: {intent_id}")
            self._conn.execute(
                """
                INSERT INTO observation_sessions
                  (session_id,intent_id,monitor_id,key_id,coverage,required_tools_json,
                   state,complete,event_count,head_hash,started_at)
                VALUES (?,?,?,?,?,?,'open',0,0,'',?)
                """,
                (
                    session_id,
                    intent_id,
                    monitor_id,
                    key_id,
                    coverage,
                    json.dumps(tools, ensure_ascii=False),
                    now,
                ),
            )
            self._event_locked(
                "observation_session_started",
                intent_id,
                None,
                {
                    "session_id": session_id,
                    "monitor_id": monitor_id,
                    "coverage": coverage,
                },
            )
        return self.observation_session(session_id)

    def _append_observation_event_locked(
        self,
        session_id: str,
        access: ObservedAccess,
        *,
        key: bytes,
    ) -> dict:
        row = self._conn.execute(
            "SELECT * FROM observation_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown observation session: {session_id}")
        if row["state"] != "open":
            raise ValueError(f"observation session {session_id} is {row['state']}")
        seq = int(row["event_count"]) + 1
        occurred_at = access.timestamp or _iso(_utc_now())
        prev_hash = str(row["head_hash"] or "")
        payload = {
            "session_id": session_id,
            "seq": seq,
            "occurred_at": occurred_at,
            "access": access.to_dict(),
            "prev_hash": prev_hash,
        }
        event_hash = _sha256_json(payload)
        event_hmac = hmac.new(
            key, event_hash.encode("ascii"), hashlib.sha256
        ).hexdigest()
        self._conn.execute(
            """
            INSERT INTO observation_events
              (session_id,seq,occurred_at,access_json,prev_hash,event_hash,event_hmac)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                session_id,
                seq,
                occurred_at,
                json.dumps(access.to_dict(), ensure_ascii=False, sort_keys=True),
                prev_hash,
                event_hash,
                event_hmac,
            ),
        )
        self._conn.execute(
            "UPDATE observation_sessions SET event_count=?,head_hash=? WHERE session_id=?",
            (seq, event_hash, session_id),
        )
        return {
            "session_id": session_id,
            "seq": seq,
            "event_hash": event_hash,
            "event_hmac": event_hmac,
            "access": access.to_dict(),
        }

    def record_observation_event(
        self,
        session_id: str,
        access: ObservedAccess,
        *,
        key: bytes,
    ) -> dict:
        if not key:
            raise ValueError("observation signing key must not be empty")
        with self._immediate():
            session = self._conn.execute(
                "SELECT coverage FROM observation_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(f"unknown observation session: {session_id}")
            if session["coverage"] == "brokered_proxy":
                raise ValueError(
                    "brokered_proxy events must be committed by a registered broker instance"
                )
            return self._append_observation_event_locked(session_id, access, key=key)

    def seal_observation_session(
        self,
        session_id: str,
        *,
        key: bytes,
        complete: bool = True,
    ) -> dict:
        if not key:
            raise ValueError("observation signing key must not be empty")
        with self._immediate():
            row = self._conn.execute(
                "SELECT * FROM observation_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown observation session: {session_id}")
            if row["state"] != "open":
                raise ValueError(f"observation session {session_id} is {row['state']}")
            sealed_at = _iso(_utc_now())
            summary = {
                "protocol": "claim-plane.observation-session.v1",
                "session_id": session_id,
                "intent_id": row["intent_id"],
                "monitor_id": row["monitor_id"],
                "key_id": row["key_id"],
                "coverage": row["coverage"],
                "required_tools": json.loads(row["required_tools_json"]),
                "event_count": int(row["event_count"]),
                "head_hash": row["head_hash"],
                "complete": bool(complete),
                "started_at": row["started_at"],
                "sealed_at": sealed_at,
            }
            signature = hmac.new(
                key, _canonical_json(summary), hashlib.sha256
            ).hexdigest()
            attestation = {
                **summary,
                "algorithm": "hmac-sha256",
                "signature": signature,
            }
            self._conn.execute(
                """
                UPDATE observation_sessions
                SET state='sealed',complete=?,sealed_at=?,attestation_json=?
                WHERE session_id=?
                """,
                (
                    1 if complete else 0,
                    sealed_at,
                    json.dumps(attestation, ensure_ascii=False, sort_keys=True),
                    session_id,
                ),
            )
            self._event_locked(
                "observation_session_sealed",
                row["intent_id"],
                None,
                {
                    "session_id": session_id,
                    "complete": bool(complete),
                    "head_hash": row["head_hash"],
                },
            )
        return self.observation_session(session_id)

    def observation_session(self, session_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM observation_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown observation session: {session_id}")
        data = dict(row)
        data["required_tools"] = json.loads(data.pop("required_tools_json"))
        raw = data.pop("attestation_json")
        data["attestation"] = json.loads(raw) if raw else None
        broker_raw = data.pop("broker_attestation_json", None)
        data["broker_attestation"] = json.loads(broker_raw) if broker_raw else None
        data["complete"] = bool(data["complete"])
        return data

    def verify_observation_session(self, session_id: str, *, key: bytes) -> dict:
        session = self.observation_session(session_id)
        rows = self._conn.execute(
            "SELECT * FROM observation_events WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
        prev_hash = ""
        accesses: list[ObservedAccess] = []
        errors: list[str] = []
        for expected_seq, row in enumerate(rows, 1):
            access_payload = json.loads(row["access_json"])
            access = ObservedAccess.from_dict(access_payload)
            payload = {
                "session_id": session_id,
                "seq": expected_seq,
                "occurred_at": row["occurred_at"],
                "access": access.to_dict(),
                "prev_hash": prev_hash,
            }
            event_hash = _sha256_json(payload)
            expected_hmac = hmac.new(
                key, event_hash.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if int(row["seq"]) != expected_seq:
                errors.append(f"sequence mismatch at {expected_seq}")
            if row["prev_hash"] != prev_hash:
                errors.append(f"previous hash mismatch at {expected_seq}")
            if row["event_hash"] != event_hash:
                errors.append(f"event hash mismatch at {expected_seq}")
            if not hmac.compare_digest(str(row["event_hmac"]), expected_hmac):
                errors.append(f"event signature mismatch at {expected_seq}")
            prev_hash = event_hash
            accesses.append(access)
        if len(rows) != int(session["event_count"]):
            errors.append("event count mismatch")
        if prev_hash != session["head_hash"]:
            errors.append("head hash mismatch")
        required_tools = set(session.get("required_tools") or ())
        observed_tools = {item.tool for item in accesses if item.tool}
        missing_tools = sorted(required_tools - observed_tools)
        if missing_tools:
            errors.append(
                "required monitor tools missing from trace: " + ", ".join(missing_tools)
            )
        attestation = session.get("attestation")
        if session["state"] != "sealed" or not isinstance(attestation, dict):
            errors.append("session is not sealed")
        else:
            unsigned = {
                key_: value
                for key_, value in attestation.items()
                if key_ not in {"algorithm", "signature"}
            }
            expected = hmac.new(
                key, _canonical_json(unsigned), hashlib.sha256
            ).hexdigest()
            if attestation.get("algorithm") != "hmac-sha256":
                errors.append("unsupported session signature algorithm")
            if not hmac.compare_digest(
                str(attestation.get("signature") or ""), expected
            ):
                errors.append("session attestation signature mismatch")
        return {
            "valid": not errors,
            "errors": errors,
            "session": session,
            "accesses": [item.to_dict() for item in accesses],
            "digest": _sha256_json(
                {
                    "session": session,
                    "events": [access.to_dict() for access in accesses],
                }
            ),
        }

    # --------------------------------------------------------- broker runtime

    def _broker_instance_payload(self, row: Mapping[str, object]) -> dict:
        return {
            "protocol": "claim-plane.broker-instance.v2",
            "instance_id": str(row["instance_id"]),
            "intent_id": str(row["intent_id"]),
            "intent_content_version": int(str(row["intent_content_version"])),
            "intent_fingerprint": str(row["intent_fingerprint"]),
            "session_id": str(row["session_id"]),
            "monitor_id": str(row["monitor_id"]),
            "key_id": str(row["key_id"]),
            "root_path": str(row["root_path"]),
            "repo_identity": str(row["repo_identity"]),
            "base_commit": str(row["base_commit"]),
            "initial_tree_hash": str(row["initial_tree_hash"]),
            "writer_lease_seconds": int(str(row["writer_lease_seconds"])),
            "fencing_token": int(str(row["fencing_token"])),
            "policy_digest": str(row["policy_digest"]),
            "binary_digest": str(row["binary_digest"]),
            "started_at": str(row["started_at"]),
        }

    def _expire_broker_writer_leases_locked(self) -> None:
        now = _iso(_utc_now())
        expired = self._conn.execute(
            "SELECT root_path,instance_id FROM broker_writer_leases WHERE expires_at<=?",
            (now,),
        ).fetchall()
        for lease in expired:
            self._conn.execute(
                "DELETE FROM broker_writer_leases WHERE root_path=? AND instance_id=?",
                (lease["root_path"], lease["instance_id"]),
            )
            self._conn.execute(
                """
                UPDATE broker_instances
                SET state='expired',stopped_at=COALESCE(stopped_at,?),last_seen_at=?
                WHERE instance_id=? AND state='active'
                """,
                (now, now, lease["instance_id"]),
            )

    def _refresh_broker_intent_binding_locked(
        self,
        instance_id: str,
        *,
        intent_id: str,
        intent_content_version: int,
        intent_fingerprint: str,
        broker_key: bytes,
    ) -> None:
        instance = self._verify_broker_attestation_locked(
            instance_id, broker_key=broker_key
        )
        if instance["state"] != "active":
            raise ValueError(f"broker instance {instance_id} is {instance['state']}")
        if instance["intent_id"] != intent_id:
            raise ValueError("broker instance belongs to another intent")

        fields = dict(instance)
        fields["intent_content_version"] = intent_content_version
        fields["intent_fingerprint"] = intent_fingerprint
        payload = self._broker_instance_payload(fields)
        attestation = {
            **payload,
            "algorithm": "hmac-sha256",
            "signature": hmac.new(
                broker_key, _canonical_json(payload), hashlib.sha256
            ).hexdigest(),
        }
        encoded = json.dumps(attestation, ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            """
            UPDATE broker_instances
            SET intent_content_version=?,intent_fingerprint=?,attestation_json=?,last_seen_at=?
            WHERE instance_id=?
            """,
            (
                intent_content_version,
                intent_fingerprint,
                encoded,
                _iso(_utc_now()),
                instance_id,
            ),
        )
        self._conn.execute(
            """
            UPDATE observation_sessions
            SET broker_attestation_json=?
            WHERE broker_instance_id=?
            """,
            (encoded, instance_id),
        )

    def register_broker_instance(
        self,
        *,
        instance_id: str,
        intent_id: str,
        session_id: str,
        monitor_id: str,
        key_id: str,
        root_path: str,
        repo_identity: str,
        base_commit: str,
        initial_tree_hash: str,
        writer_lease_seconds: int,
        policy: Mapping[str, object],
        binary_digest: str,
        broker_key: bytes,
        required_tools: Iterable[str] = (),
    ) -> dict:
        if not broker_key:
            raise ValueError("broker signing key must not be empty")
        if not initial_tree_hash:
            raise ValueError("broker initial tree hash must not be empty")
        if writer_lease_seconds <= 0:
            raise ValueError("broker writer lease must be positive")
        now = _iso(_utc_now())
        lease_expires = _future(writer_lease_seconds)
        tools = tuple(
            dict.fromkeys(item.strip() for item in required_tools if item.strip())
        )
        with self._immediate():
            self._expire_intents_locked()
            self._expire_broker_writer_leases_locked()
            intent_row = self._conn.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if intent_row is None:
                raise KeyError(f"unknown intent: {intent_id}")
            if intent_row["state"] not in {
                IntentState.ADMITTED.value,
                IntentState.ACTIVE.value,
            }:
                raise ValueError(
                    f"broker requires admitted or active intent; {intent_id} is {intent_row['state']}"
                )
            intent = ChangeIntent.from_dict(json.loads(intent_row["payload_json"]))
            if intent.base_commit != base_commit:
                raise ValueError(
                    f"broker base commit {base_commit} does not match intent base {intent.base_commit}"
                )
            incumbent = self._conn.execute(
                "SELECT * FROM broker_writer_leases WHERE root_path=?", (root_path,)
            ).fetchone()
            if incumbent is not None and incumbent["instance_id"] != instance_id:
                raise ValueError(
                    "governed worktree already has an active broker writer: "
                    f"{incumbent['instance_id']}"
                )
            counter = self._conn.execute(
                "SELECT last_token FROM broker_fencing_counters WHERE root_path=?",
                (root_path,),
            ).fetchone()
            fencing_token = (int(counter["last_token"]) if counter else 0) + 1
            self._conn.execute(
                """
                INSERT INTO broker_fencing_counters(root_path,last_token) VALUES (?,?)
                ON CONFLICT(root_path) DO UPDATE SET last_token=excluded.last_token
                """,
                (root_path, fencing_token),
            )
            session = self._conn.execute(
                "SELECT * FROM observation_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                self._conn.execute(
                    """
                    INSERT INTO observation_sessions
                      (session_id,intent_id,monitor_id,key_id,coverage,required_tools_json,
                       state,complete,event_count,head_hash,started_at)
                    VALUES (?,?,?,?,?,?,'open',0,0,'',?)
                    """,
                    (
                        session_id,
                        intent_id,
                        monitor_id,
                        key_id,
                        "brokered_proxy",
                        json.dumps(tools, ensure_ascii=False),
                        now,
                    ),
                )
            else:
                if session["intent_id"] != intent_id:
                    raise ValueError("broker session belongs to another intent")
                if session["coverage"] != "brokered_proxy":
                    raise ValueError("broker instance requires brokered_proxy coverage")
                if session["state"] != "open":
                    raise ValueError(
                        f"observation session {session_id} is {session['state']}"
                    )
                bound = session["broker_instance_id"]
                if bound and bound != instance_id:
                    raise ValueError(
                        "observation session is already bound to another broker"
                    )
            policy_json = json.dumps(dict(policy), ensure_ascii=False, sort_keys=True)
            policy_digest = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
            fields: dict[str, object] = {
                "instance_id": instance_id,
                "intent_id": intent_id,
                "intent_content_version": int(intent_row["content_version"]),
                "intent_fingerprint": str(intent_row["fingerprint"]),
                "session_id": session_id,
                "monitor_id": monitor_id,
                "key_id": key_id,
                "root_path": root_path,
                "repo_identity": repo_identity,
                "base_commit": base_commit,
                "initial_tree_hash": initial_tree_hash,
                "expected_tree_hash": initial_tree_hash,
                "writer_lease_seconds": writer_lease_seconds,
                "fencing_token": fencing_token,
                "policy_digest": policy_digest,
                "binary_digest": binary_digest,
                "started_at": now,
            }
            payload = self._broker_instance_payload(fields)
            attestation = {
                **payload,
                "algorithm": "hmac-sha256",
                "signature": hmac.new(
                    broker_key, _canonical_json(payload), hashlib.sha256
                ).hexdigest(),
            }
            self._conn.execute(
                """
                INSERT INTO broker_instances
                  (instance_id,intent_id,intent_content_version,intent_fingerprint,
                   session_id,monitor_id,key_id,root_path,repo_identity,base_commit,
                   initial_tree_hash,expected_tree_hash,writer_lease_seconds,fencing_token,
                   policy_json,policy_digest,binary_digest,state,started_at,last_seen_at,
                   attestation_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)
                """,
                (
                    instance_id,
                    intent_id,
                    int(intent_row["content_version"]),
                    intent_row["fingerprint"],
                    session_id,
                    monitor_id,
                    key_id,
                    root_path,
                    repo_identity,
                    base_commit,
                    initial_tree_hash,
                    initial_tree_hash,
                    writer_lease_seconds,
                    fencing_token,
                    policy_json,
                    policy_digest,
                    binary_digest,
                    now,
                    now,
                    json.dumps(attestation, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO broker_writer_leases
                  (root_path,repo_identity,intent_id,instance_id,acquired_at,renewed_at,expires_at,fencing_token)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    root_path,
                    repo_identity,
                    intent_id,
                    instance_id,
                    now,
                    now,
                    lease_expires,
                    fencing_token,
                ),
            )
            self._conn.execute(
                """
                UPDATE observation_sessions
                SET broker_instance_id=?,broker_attestation_json=?
                WHERE session_id=?
                """,
                (
                    instance_id,
                    json.dumps(attestation, ensure_ascii=False, sort_keys=True),
                    session_id,
                ),
            )
            self._event_locked(
                "broker_instance_registered",
                intent_id,
                intent_row["owner"],
                {
                    "instance_id": instance_id,
                    "session_id": session_id,
                    "base_commit": base_commit,
                    "initial_tree_hash": initial_tree_hash,
                    "policy_digest": policy_digest,
                    "writer_lease_expires_at": lease_expires,
                    "fencing_token": fencing_token,
                },
            )
        return self.broker_instance(instance_id)

    def broker_instance(self, instance_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM broker_instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown broker instance: {instance_id}")
        data = dict(row)
        data["policy"] = json.loads(data.pop("policy_json"))
        data["attestation"] = json.loads(data.pop("attestation_json"))
        return data

    def _verify_broker_attestation_locked(
        self, instance_id: str, *, broker_key: bytes
    ) -> sqlite3.Row:
        if not broker_key:
            raise ValueError("broker signing key must not be empty")
        instance = self._conn.execute(
            "SELECT * FROM broker_instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
        if instance is None:
            raise KeyError(f"unknown broker instance: {instance_id}")
        attestation = json.loads(instance["attestation_json"])
        payload = self._broker_instance_payload(instance)
        expected = hmac.new(
            broker_key, _canonical_json(payload), hashlib.sha256
        ).hexdigest()
        if attestation.get("algorithm") != "hmac-sha256" or not hmac.compare_digest(
            str(attestation.get("signature") or ""), expected
        ):
            raise ValueError("broker instance attestation is invalid")
        return instance

    def _validate_broker_instance_locked(
        self,
        instance_id: str,
        *,
        broker_key: bytes,
        require_active: bool = True,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not broker_key:
            raise ValueError("broker signing key must not be empty")
        self._expire_intents_locked()
        instance = self._verify_broker_attestation_locked(
            instance_id, broker_key=broker_key
        )
        if require_active and instance["state"] != "active":
            raise ValueError(f"broker instance {instance_id} is {instance['state']}")
        if require_active:
            self._expire_broker_writer_leases_locked()
            lease = self._conn.execute(
                "SELECT * FROM broker_writer_leases WHERE root_path=?",
                (instance["root_path"],),
            ).fetchone()
            if lease is None or lease["instance_id"] != instance_id:
                raise ValueError(
                    "broker writer lease is missing or owned by another instance"
                )
            if lease["repo_identity"] != instance["repo_identity"]:
                raise ValueError("broker writer lease repository identity mismatch")
            if int(lease["fencing_token"]) != int(instance["fencing_token"]):
                raise ValueError("broker writer lease fencing token mismatch")
        intent = self._conn.execute(
            "SELECT * FROM intents WHERE intent_id=?", (instance["intent_id"],)
        ).fetchone()
        if intent is None:
            raise KeyError(f"unknown intent: {instance['intent_id']}")
        allowed_states = {
            IntentState.ADMITTED.value,
            IntentState.ACTIVE.value,
        }
        if not require_active:
            allowed_states.add(IntentState.COMPLETED.value)
        if intent["state"] not in allowed_states:
            raise ValueError(
                f"broker capability revoked: intent {intent['intent_id']} is {intent['state']}"
            )
        if int(intent["content_version"]) != int(instance["intent_content_version"]):
            raise ValueError("broker capability revoked by intent amendment")
        if intent["fingerprint"] != instance["intent_fingerprint"]:
            raise ValueError("broker capability fingerprint no longer matches intent")
        payload_intent = ChangeIntent.from_dict(json.loads(intent["payload_json"]))
        if payload_intent.base_commit != instance["base_commit"]:
            raise ValueError("broker capability base commit no longer matches intent")
        session = self._conn.execute(
            "SELECT * FROM observation_sessions WHERE session_id=?",
            (instance["session_id"],),
        ).fetchone()
        if session is None or session["broker_instance_id"] != instance_id:
            raise ValueError("broker observation session binding is invalid")
        if require_active and session["state"] != "open":
            raise ValueError(f"broker observation session is {session['state']}")
        return instance, intent

    def validate_broker_instance(
        self,
        instance_id: str,
        *,
        broker_key: bytes,
        current_tree_hash: str | None = None,
    ) -> dict:
        with self._immediate():
            instance, intent = self._validate_broker_instance_locked(
                instance_id, broker_key=broker_key, require_active=True
            )
            expected_tree = str(instance["expected_tree_hash"] or "")
            if current_tree_hash is not None and current_tree_hash != expected_tree:
                raise ValueError(
                    "broker worktree diverged from the committed operation chain: "
                    f"expected {expected_tree}, observed {current_tree_hash}"
                )
            now = _iso(_utc_now())
            expires = _future(int(instance["writer_lease_seconds"]))
            self._conn.execute(
                "UPDATE broker_instances SET last_seen_at=? WHERE instance_id=?",
                (now, instance_id),
            )
            self._conn.execute(
                """
                UPDATE broker_writer_leases
                SET renewed_at=?,expires_at=?
                WHERE root_path=? AND instance_id=?
                """,
                (now, expires, instance["root_path"], instance_id),
            )
            return {
                "instance": self.broker_instance(instance_id),
                "intent": ChangeIntent.from_dict(json.loads(intent["payload_json"])),
                "intent_record": dict(intent),
            }

    def stop_broker_instance(self, instance_id: str, *, broker_key: bytes) -> dict:
        with self._immediate():
            instance = self._verify_broker_attestation_locked(
                instance_id, broker_key=broker_key
            )
            if instance["state"] == "active":
                now = _iso(_utc_now())
                owner_row = self._conn.execute(
                    "SELECT owner FROM intents WHERE intent_id=?",
                    (instance["intent_id"],),
                ).fetchone()
                self._conn.execute(
                    "UPDATE broker_instances SET state='stopped',stopped_at=?,last_seen_at=? WHERE instance_id=?",
                    (now, now, instance_id),
                )
                self._conn.execute(
                    "DELETE FROM broker_writer_leases WHERE root_path=? AND instance_id=?",
                    (instance["root_path"], instance_id),
                )
                self._event_locked(
                    "broker_instance_stopped",
                    instance["intent_id"],
                    owner_row["owner"] if owner_row else None,
                    {"instance_id": instance_id},
                )
        return self.broker_instance(instance_id)

    def verify_broker_instance(self, instance_id: str, *, broker_key: bytes) -> dict:
        errors: list[str] = []
        try:
            with self._immediate():
                instance, _ = self._validate_broker_instance_locked(
                    instance_id, broker_key=broker_key, require_active=False
                )
                if instance["state"] not in {"active", "stopped"}:
                    errors.append(f"broker instance state is {instance['state']}")
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
        instance_data = None
        try:
            instance_data = self.broker_instance(instance_id)
        except KeyError:
            pass
        return {"valid": not errors, "errors": errors, "instance": instance_data}

    def broker_operation_for_request(
        self, instance_id: str, request_id: str
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM broker_operations WHERE instance_id=? AND request_id=?",
            (instance_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return self._decode_row(row, ("payload_json", "response_json"))

    def prepare_broker_operation(
        self,
        *,
        operation_id: str,
        instance_id: str,
        request_id: str,
        operation: str,
        mode: AccessMode,
        path: str,
        target_path: str | None,
        payload: Mapping[str, object],
        broker_key: bytes,
        fencing_token: int,
        pre_tree_hash: str | None = None,
    ) -> dict:
        with self._immediate():
            instance, _ = self._validate_broker_instance_locked(
                instance_id, broker_key=broker_key, require_active=True
            )
            if fencing_token <= 0 or int(instance["fencing_token"]) != fencing_token:
                raise ValueError(
                    f"stale broker fencing token: expected {instance['fencing_token']}, got {fencing_token}"
                )
            if (
                pre_tree_hash is not None
                and str(instance["expected_tree_hash"]) != pre_tree_hash
            ):
                raise ValueError(
                    "broker operation pre-tree does not match the committed chain: "
                    f"expected {instance['expected_tree_hash']}, got {pre_tree_hash}"
                )
            existing = self._conn.execute(
                "SELECT * FROM broker_operations WHERE instance_id=? AND request_id=?",
                (instance_id, request_id),
            ).fetchone()
            if existing is not None:
                return self._decode_row(existing, ("payload_json", "response_json"))
            prepared_at = _iso(_utc_now())
            signed = {
                "protocol": "claim-plane.broker-operation.v2",
                "operation_id": operation_id,
                "instance_id": instance_id,
                "request_id": request_id,
                "operation": operation,
                "mode": mode.value,
                "path": path,
                "target_path": target_path,
                "payload": dict(payload),
                "fencing_token": fencing_token,
                "pre_tree_hash": pre_tree_hash,
                "prepared_at": prepared_at,
            }
            prepare_hmac = hmac.new(
                broker_key, _canonical_json(signed), hashlib.sha256
            ).hexdigest()
            self._conn.execute(
                """
                INSERT INTO broker_operations
                  (operation_id,instance_id,request_id,operation,mode,path,target_path,
                   state,payload_json,prepared_at,pre_tree_hash,prepare_hmac,fencing_token)
                VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?)
                """,
                (
                    operation_id,
                    instance_id,
                    request_id,
                    operation,
                    mode.value,
                    path,
                    target_path,
                    json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                    prepared_at,
                    pre_tree_hash,
                    prepare_hmac,
                    fencing_token,
                ),
            )
            return self.broker_operation_for_request(instance_id, request_id) or {}

    def commit_broker_operation(
        self,
        operation_id: str,
        *,
        accesses: Iterable[ObservedAccess],
        response: Mapping[str, object],
        observation_key: bytes,
        broker_key: bytes,
        fencing_token: int,
        post_tree_hash: str | None = None,
    ) -> dict:
        if not observation_key:
            raise ValueError("observation signing key must not be empty")
        with self._immediate():
            row = self._conn.execute(
                "SELECT * FROM broker_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown broker operation: {operation_id}")
            if row["state"] == "committed":
                return self._decode_row(row, ("payload_json", "response_json"))
            if row["state"] != "pending":
                raise ValueError(f"broker operation {operation_id} is {row['state']}")
            instance, _ = self._validate_broker_instance_locked(
                row["instance_id"], broker_key=broker_key, require_active=True
            )
            if fencing_token <= 0 or int(instance["fencing_token"]) != fencing_token:
                raise ValueError(
                    f"stale broker fencing token: expected {instance['fencing_token']}, got {fencing_token}"
                )
            if int(row["fencing_token"]) != fencing_token:
                raise ValueError(
                    "broker operation fencing token does not match its instance"
                )
            pre_tree_hash = row["pre_tree_hash"]
            if pre_tree_hash is not None and str(instance["expected_tree_hash"]) != str(
                pre_tree_hash
            ):
                raise ValueError(
                    "broker operation lost the tree compare-and-swap race: "
                    f"expected {instance['expected_tree_hash']}, prepared from {pre_tree_hash}"
                )
            if (pre_tree_hash is None) != (post_tree_hash is None):
                raise ValueError(
                    "broker tree transition requires both pre and post hashes"
                )
            start_seq: int | None = None
            end_seq: int | None = None
            for access in accesses:
                metadata = {
                    **dict(access.metadata),
                    "broker_protocol": "claim-plane.broker.v2",
                    "broker_instance_id": instance["instance_id"],
                    "broker_operation_id": operation_id,
                    "broker_policy_digest": instance["policy_digest"],
                    "intent_content_version": int(instance["intent_content_version"]),
                    "fencing_token": fencing_token,
                    "request_id": row["request_id"],
                }
                bound = ObservedAccess(
                    mode=access.mode,
                    resource=access.resource,
                    tool=access.tool,
                    timestamp=access.timestamp,
                    metadata=metadata,
                )
                event = self._append_observation_event_locked(
                    instance["session_id"], bound, key=observation_key
                )
                seq = int(event["seq"])
                start_seq = seq if start_seq is None else start_seq
                end_seq = seq
            committed_at = _iso(_utc_now())
            response_payload = dict(response)
            commit_payload = {
                "protocol": "claim-plane.broker-operation.v2",
                "operation_id": operation_id,
                "instance_id": row["instance_id"],
                "request_id": row["request_id"],
                "state": "committed",
                "response": response_payload,
                "fencing_token": fencing_token,
                "pre_tree_hash": pre_tree_hash,
                "post_tree_hash": post_tree_hash,
                "event_start_seq": start_seq,
                "event_end_seq": end_seq,
                "committed_at": committed_at,
            }
            commit_hmac = hmac.new(
                broker_key, _canonical_json(commit_payload), hashlib.sha256
            ).hexdigest()
            self._conn.execute(
                """
                UPDATE broker_operations SET state='committed',response_json=?,
                    committed_at=?,event_start_seq=?,event_end_seq=?,post_tree_hash=?,commit_hmac=?
                WHERE operation_id=?
                """,
                (
                    json.dumps(response_payload, ensure_ascii=False, sort_keys=True),
                    committed_at,
                    start_seq,
                    end_seq,
                    post_tree_hash,
                    commit_hmac,
                    operation_id,
                ),
            )
            if post_tree_hash is not None:
                updated = self._conn.execute(
                    """
                    UPDATE broker_instances
                    SET expected_tree_hash=?,last_seen_at=?
                    WHERE instance_id=? AND expected_tree_hash=? AND state='active'
                    """,
                    (post_tree_hash, committed_at, row["instance_id"], pre_tree_hash),
                )
                if updated.rowcount != 1:
                    raise ValueError("broker tree transition compare-and-swap failed")
            return (
                self.broker_operation_for_request(row["instance_id"], row["request_id"])
                or {}
            )

    def fail_broker_operation(
        self,
        operation_id: str,
        *,
        state: str,
        error: str,
        broker_key: bytes,
    ) -> dict:
        if state not in {"failed", "rolled_back"}:
            raise ValueError(
                "broker operation failure state must be failed or rolled_back"
            )
        with self._immediate():
            row = self._conn.execute(
                "SELECT * FROM broker_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown broker operation: {operation_id}")
            self._verify_broker_attestation_locked(
                row["instance_id"], broker_key=broker_key
            )
            if row["state"] == "committed":
                raise ValueError("committed broker operation cannot be failed")
            self._conn.execute(
                "UPDATE broker_operations SET state=?,error=? WHERE operation_id=?",
                (state, error, operation_id),
            )
            return (
                self.broker_operation_for_request(row["instance_id"], row["request_id"])
                or {}
            )

    def pending_broker_operations(self, instance_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM broker_operations WHERE instance_id=? AND state='pending' ORDER BY prepared_at",
            (instance_id,),
        ).fetchall()
        return [
            self._decode_row(row, ("payload_json", "response_json")) for row in rows
        ]

    def verify_broker_session(self, session_id: str, *, broker_key: bytes) -> dict:
        session = self.observation_session(session_id)
        instance_id = session.get("broker_instance_id")
        if not instance_id:
            return {
                "valid": False,
                "errors": ["brokered session is not bound to a broker instance"],
                "session": session,
                "instance": None,
                "operations": [],
            }
        verified = self.verify_broker_instance(str(instance_id), broker_key=broker_key)
        errors = list(verified["errors"])
        instance = verified.get("instance")
        if instance and instance.get("session_id") != session_id:
            errors.append("broker instance is bound to another observation session")
        if session.get("coverage") != "brokered_proxy":
            errors.append(
                "broker instance session does not use brokered_proxy coverage"
            )
        operation_rows = self._conn.execute(
            "SELECT * FROM broker_operations WHERE instance_id=? ORDER BY prepared_at",
            (instance_id,),
        ).fetchall()
        operations = [
            self._decode_row(row, ("payload_json", "response_json"))
            for row in operation_rows
        ]
        event_rows = self._conn.execute(
            "SELECT * FROM observation_events WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
        events_by_operation: dict[str, list[int]] = defaultdict(list)
        for event in event_rows:
            access = json.loads(event["access_json"])
            metadata = dict(access.get("metadata") or {})
            operation_id = str(metadata.get("broker_operation_id") or "")
            if not operation_id:
                errors.append(f"broker event {event['seq']} has no operation binding")
                continue
            if metadata.get("broker_protocol") != "claim-plane.broker.v2":
                errors.append(
                    f"broker event {event['seq']} uses an unsupported protocol"
                )
            if metadata.get("broker_instance_id") != instance_id:
                errors.append(
                    f"broker event {event['seq']} belongs to another instance"
                )
            if instance and metadata.get("broker_policy_digest") != instance.get(
                "policy_digest"
            ):
                errors.append(f"broker event {event['seq']} policy digest mismatch")
            if instance and int(metadata.get("fencing_token") or 0) != int(
                instance.get("fencing_token") or 0
            ):
                errors.append(f"broker event {event['seq']} fencing token mismatch")
            events_by_operation[operation_id].append(int(event["seq"]))
        known_operations = {str(item["operation_id"]) for item in operations}
        for operation_id in events_by_operation:
            if operation_id not in known_operations:
                errors.append(
                    f"broker event references unknown operation {operation_id}"
                )
        for operation in operations:
            prepare_payload = {
                "protocol": "claim-plane.broker-operation.v2",
                "operation_id": operation["operation_id"],
                "instance_id": operation["instance_id"],
                "request_id": operation["request_id"],
                "operation": operation["operation"],
                "mode": operation["mode"],
                "path": operation["path"],
                "target_path": operation["target_path"],
                "payload": operation["payload_json"],
                "fencing_token": int(operation.get("fencing_token") or 0),
                "pre_tree_hash": operation.get("pre_tree_hash"),
                "prepared_at": operation["prepared_at"],
            }
            expected_prepare = hmac.new(
                broker_key, _canonical_json(prepare_payload), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(
                str(operation.get("prepare_hmac") or ""), expected_prepare
            ):
                errors.append(
                    f"broker operation {operation['operation_id']} prepare signature mismatch"
                )
            seqs = events_by_operation.get(str(operation["operation_id"]), [])
            state = str(operation["state"])
            if instance and int(operation.get("fencing_token") or 0) != int(
                instance.get("fencing_token") or 0
            ):
                errors.append(
                    f"broker operation {operation['operation_id']} fencing token mismatch"
                )
            if state == "pending":
                errors.append(
                    f"broker operation {operation['operation_id']} is still pending"
                )
            if state == "committed":
                if seqs:
                    if operation.get("event_start_seq") != min(seqs) or operation.get(
                        "event_end_seq"
                    ) != max(seqs):
                        errors.append(
                            f"broker operation {operation['operation_id']} event range mismatch"
                        )
                elif (
                    operation.get("event_start_seq") is not None
                    or operation.get("event_end_seq") is not None
                ):
                    errors.append(
                        f"broker operation {operation['operation_id']} has an empty event range mismatch"
                    )
                commit_payload = {
                    "protocol": "claim-plane.broker-operation.v2",
                    "operation_id": operation["operation_id"],
                    "instance_id": operation["instance_id"],
                    "request_id": operation["request_id"],
                    "state": "committed",
                    "response": operation.get("response_json") or {},
                    "fencing_token": int(operation.get("fencing_token") or 0),
                    "pre_tree_hash": operation.get("pre_tree_hash"),
                    "post_tree_hash": operation.get("post_tree_hash"),
                    "event_start_seq": operation.get("event_start_seq"),
                    "event_end_seq": operation.get("event_end_seq"),
                    "committed_at": operation.get("committed_at"),
                }
                expected_commit = hmac.new(
                    broker_key, _canonical_json(commit_payload), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(
                    str(operation.get("commit_hmac") or ""), expected_commit
                ):
                    errors.append(
                        f"broker operation {operation['operation_id']} commit signature mismatch"
                    )
            elif seqs:
                errors.append(
                    f"non-committed broker operation {operation['operation_id']} has observation events"
                )
        if instance:
            expected_tree = str(instance.get("initial_tree_hash") or "")
            for operation in operations:
                pre_tree = operation.get("pre_tree_hash")
                post_tree = operation.get("post_tree_hash")
                state = str(operation.get("state") or "")
                if pre_tree is None and post_tree is None:
                    continue
                if state != "committed":
                    if post_tree is not None:
                        errors.append(
                            f"non-committed broker operation {operation['operation_id']} carries a post-tree"
                        )
                    continue
                if not pre_tree or not post_tree:
                    errors.append(
                        f"broker operation {operation['operation_id']} has an incomplete tree transition"
                    )
                    continue
                if str(pre_tree) != expected_tree:
                    errors.append(
                        f"broker operation {operation['operation_id']} tree chain starts from {pre_tree}, expected {expected_tree}"
                    )
                expected_tree = str(post_tree)
            if expected_tree != str(instance.get("expected_tree_hash") or ""):
                errors.append(
                    "broker instance expected tree does not match the committed operation chain"
                )
        return {
            "valid": not errors,
            "errors": errors,
            "session": session,
            "instance": instance,
            "operations": operations,
            "digest": _sha256_json(
                {
                    "session": session,
                    "instance": instance or {},
                    "operations": operations,
                }
            ),
        }

    # ---------------------------------------------------------- verification

    def record_verification(self, intent_id: str, report: Mapping[str, object]) -> None:
        with self._immediate():
            self._conn.execute(
                "INSERT INTO verification_reports (verified_at,intent_id,clean,report_json) VALUES (?,?,?,?)",
                (
                    _iso(_utc_now()),
                    intent_id,
                    1 if bool(report.get("clean")) else 0,
                    json.dumps(dict(report), ensure_ascii=False, sort_keys=True),
                ),
            )
            row = self._conn.execute(
                "SELECT owner FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            self._event_locked(
                "verification_completed",
                intent_id,
                row["owner"] if row else None,
                dict(report),
            )

    def verification_reports(self, intent_id: str | None = None) -> list[dict]:
        if intent_id:
            rows = self._conn.execute(
                "SELECT * FROM verification_reports WHERE intent_id=? ORDER BY id",
                (intent_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM verification_reports ORDER BY id"
            ).fetchall()
        return [self._decode_row(row, ("report_json",)) for row in rows]

    @staticmethod
    def _decision_from_json(raw: str) -> AdmissionDecision:
        from claim_plane.core.serialization import admission_decision_from_dict

        return admission_decision_from_dict(json.loads(raw))

    @staticmethod
    def _decode_row(row: sqlite3.Row, json_columns: tuple[str, ...]) -> dict:
        data = dict(row)
        for column in json_columns:
            raw = data.get(column)
            if isinstance(raw, str):
                data[column] = json.loads(raw)
        return data

    def export_audit(self, path: str | Path) -> None:
        payload = {
            "claim_decisions": self.decision_log(),
            "coordination_events": self.coordination_events(),
            "verification_reports": self.verification_reports(),
            "dependencies": [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM intent_dependencies ORDER BY intent_id"
                )
            ],
            "notices": [
                self._decode_row(row, ("payload_json",))
                for row in self._conn.execute(
                    "SELECT * FROM coordination_notices ORDER BY id"
                )
            ],
            "observation_sessions": [
                self.observation_session(row["session_id"])
                for row in self._conn.execute(
                    "SELECT session_id FROM observation_sessions ORDER BY started_at"
                )
            ],
            "observation_events": [
                self._decode_row(row, ("access_json",))
                for row in self._conn.execute(
                    "SELECT * FROM observation_events ORDER BY session_id, seq"
                )
            ],
            "broker_instances": [
                self.broker_instance(row["instance_id"])
                for row in self._conn.execute(
                    "SELECT instance_id FROM broker_instances ORDER BY started_at"
                )
            ],
            "broker_operations": [
                self._decode_row(row, ("payload_json", "response_json"))
                for row in self._conn.execute(
                    "SELECT * FROM broker_operations ORDER BY prepared_at"
                )
            ],
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def close(self) -> None:
        self._conn.close()


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_json(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _operation_resource_key(operation) -> str:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return f"{resource.kind.value}:{resource.key}:{resource.region or ''}"
    if resource.kind is ResourceKind.CONTRACT:
        return f"contract:{resource.subject_key or ''}:{resource.semantic_key}:{resource.signature or ''}"
    return f"{resource.kind.value}:{resource.semantic_key}:{resource.signature or ''}"


def _changed_resource_keys(old: ChangeIntent, new: ChangeIntent) -> set[str]:
    old_keys = {_operation_resource_key(op) for op in old.operations}
    new_keys = {_operation_resource_key(op) for op in new.operations}
    changed = old_keys ^ new_keys
    # Dependents store semantic/resource premise keys, so also return compact keys.
    compact: set[str] = set()
    for operation in (*old.operations, *new.operations):
        marker = _operation_resource_key(operation)
        if marker in changed:
            compact.add(
                operation.resource.subject_key or operation.resource.semantic_key
            )
    return {key for key in compact if key}


def _cycle_rejection(
    intent: ChangeIntent, decision: AdmissionDecision, cycle: list[str]
) -> AdmissionDecision:
    return AdmissionDecision(
        kind=AdmissionKind.REJECT,
        intent=intent,
        allowed=False,
        conflicts=decision.conflicts,
        constraints=tuple(
            dict.fromkeys(
                [*decision.constraints, "Dependency graph must remain acyclic."]
            )
        ),
        notifications=decision.notifications,
        guidance="Dependency cycle rejected: " + " -> ".join(cycle),
    )


def _find_dependency_cycle(
    edges: Iterable[tuple[str, str]],
) -> list[str] | None:
    adjacency: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for dependent, producer in edges:
        adjacency[dependent].add(producer)
        nodes.update((dependent, producer))

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in visited:
            return None
        if node in visiting:
            index = stack.index(node)
            return [*stack[index:], node]
        visiting.add(node)
        stack.append(node)
        for neighbor in sorted(adjacency.get(node, ())):
            cycle = visit(neighbor)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in sorted(nodes):
        cycle = visit(node)
        if cycle:
            return cycle
    return None


def _topological_dependency_order(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> list[str]:
    """Return producers before dependents for ``dependent -> producer`` edges."""

    node_set = set(nodes)
    consumers_by_producer: dict[str, set[str]] = defaultdict(set)
    indegree = {node: 0 for node in node_set}
    for dependent, producer in edges:
        node_set.update((dependent, producer))
        indegree.setdefault(dependent, 0)
        indegree.setdefault(producer, 0)
        if dependent not in consumers_by_producer[producer]:
            consumers_by_producer[producer].add(dependent)
            indegree[dependent] += 1

    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for consumer in sorted(consumers_by_producer.get(node, ())):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
    return order if len(order) == len(indegree) else []
