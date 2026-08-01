"""SQLite persistence for durable swarm planning and budget state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from claim_plane.swarm.admission import SharedAdmissionPlan, SharedAdmissionStatus
from claim_plane.swarm.budget import SwarmBudgetPolicy
from claim_plane.swarm.concurrency import ConcurrencyPlan
from claim_plane.swarm.models import SwarmSession, SwarmSessionState, WorkGraph
from claim_plane.swarm.runs import CodexRunRecord, CodexRunState
from claim_plane.swarm.scheduler import compute_scheduler_snapshot
from claim_plane.swarm.worktrees import ManagedWorktree

_SCHEMA_VERSION = 6


class SwarmSessionStore:
    """Single-host durable store with optimistic graph and budget updates."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        schema_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if schema_version > _SCHEMA_VERSION:
            raise ValueError(
                f"swarm database schema {schema_version} is newer than this client"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_sessions (
                session_id TEXT PRIMARY KEY,
                repository_identity TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                state TEXT NOT NULL,
                graph_version INTEGER NOT NULL,
                graph_fingerprint TEXT NOT NULL,
                budget_version INTEGER NOT NULL DEFAULT 1,
                budget_fingerprint TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(swarm_sessions)"
            ).fetchall()
        }
        if "budget_version" not in columns:
            self._connection.execute(
                "ALTER TABLE swarm_sessions "
                "ADD COLUMN budget_version INTEGER NOT NULL DEFAULT 1"
            )
        if "budget_fingerprint" not in columns:
            self._connection.execute(
                "ALTER TABLE swarm_sessions "
                "ADD COLUMN budget_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_sessions_updated "
            "ON swarm_sessions(updated_at DESC, session_id ASC)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_concurrency_plans (
                session_id TEXT PRIMARY KEY,
                plan_version INTEGER NOT NULL,
                graph_version INTEGER NOT NULL,
                graph_fingerprint TEXT NOT NULL,
                budget_version INTEGER NOT NULL,
                budget_fingerprint TEXT NOT NULL,
                plan_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES swarm_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_worktrees (
                session_id TEXT NOT NULL,
                work_id TEXT NOT NULL,
                repository_identity TEXT NOT NULL,
                graph_version INTEGER NOT NULL,
                graph_fingerprint TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                branch TEXT NOT NULL UNIQUE,
                worktree_path TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(session_id, work_id),
                FOREIGN KEY(session_id) REFERENCES swarm_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_worktrees_session "
            "ON swarm_worktrees(session_id, work_id)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_codex_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                work_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                token_limit INTEGER,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES swarm_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_swarm_codex_runs_attempt "
            "ON swarm_codex_runs(session_id, work_id, attempt)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_codex_runs_state "
            "ON swarm_codex_runs(session_id, state, work_id)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_shared_admissions (
                session_id TEXT PRIMARY KEY,
                admission_version INTEGER NOT NULL,
                graph_version INTEGER NOT NULL,
                graph_fingerprint TEXT NOT NULL,
                budget_version INTEGER NOT NULL,
                budget_fingerprint TEXT NOT NULL,
                concurrency_plan_fingerprint TEXT NOT NULL,
                admission_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES swarm_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        if schema_version < _SCHEMA_VERSION:
            self._migrate_payloads()
            self._connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._connection.commit()

    def _migrate_payloads(self) -> None:
        rows = self._connection.execute(
            "SELECT session_id, payload_json FROM swarm_sessions"
        ).fetchall()
        for row in rows:
            session_id = str(row["session_id"])
            payload: Any = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError(f"stored swarm session {session_id!r} is invalid")
            session = SwarmSession.from_dict(payload)
            self._connection.execute(
                """
                UPDATE swarm_sessions
                   SET budget_version = ?, budget_fingerprint = ?, payload_json = ?
                 WHERE session_id = ?
                """,
                (
                    session.budget_version,
                    session.budget_fingerprint,
                    self._payload(session),
                    session_id,
                ),
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SwarmSessionStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _payload(session: SwarmSession) -> str:
        return json.dumps(
            session.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def create(self, session: SwarmSession) -> tuple[SwarmSession, bool]:
        payload = self._payload(session)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO swarm_sessions (
                        session_id, repository_identity, base_commit, state,
                        graph_version, graph_fingerprint,
                        budget_version, budget_fingerprint, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.repository_identity,
                        session.base_commit,
                        session.state.value,
                        session.graph_version,
                        session.graph_fingerprint,
                        session.budget_version,
                        session.budget_fingerprint,
                        payload,
                        session.created_at,
                        session.updated_at,
                    ),
                )
            return session, True
        except sqlite3.IntegrityError:
            existing = self.get(session.session_id)
            if existing is None:
                raise
            same_request = (
                existing.repository_identity == session.repository_identity
                and existing.base_commit == session.base_commit
                and existing.base_branch == session.base_branch
                and existing.root_task == session.root_task
                and existing.integration_target == session.integration_target
                and existing.work_graph == session.work_graph
                and existing.budget_policy == session.budget_policy
                and existing.metadata == session.metadata
            )
            if same_request:
                return existing, False
            raise ValueError(
                f"swarm session {session.session_id!r} already exists"
            ) from None

    def get(self, session_id: str) -> SwarmSession | None:
        row = self._connection.execute(
            "SELECT payload_json FROM swarm_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(f"stored swarm session {session_id!r} is invalid")
        return SwarmSession.from_dict(payload)

    def require(self, session_id: str) -> SwarmSession:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"unknown swarm session {session_id!r}")
        return session

    def list(self) -> list[SwarmSession]:
        rows = self._connection.execute(
            "SELECT payload_json FROM swarm_sessions "
            "ORDER BY updated_at DESC, session_id ASC"
        ).fetchall()
        sessions: list[SwarmSession] = []
        for row in rows:
            payload: Any = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("stored swarm session is invalid")
            sessions.append(SwarmSession.from_dict(payload))
        return sessions

    def replace_graph(
        self,
        session_id: str,
        graph: WorkGraph,
        *,
        expected_version: int,
        updated_at: str,
    ) -> SwarmSession:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT payload_json, graph_version FROM swarm_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown swarm session {session_id!r}")
            current_version = int(row["graph_version"])
            if current_version != expected_version:
                raise ValueError(
                    f"stale work graph: expected version {expected_version}, "
                    f"current version is {current_version}"
                )
            current = self._session_from_row(row, session_id)
            if current.graph_fingerprint == graph.fingerprint():
                self._connection.commit()
                return current
            updated = current.with_graph(graph, updated_at=updated_at)
            cursor = self._connection.execute(
                """
                UPDATE swarm_sessions
                   SET state = ?, graph_version = ?, graph_fingerprint = ?,
                       payload_json = ?, updated_at = ?
                 WHERE session_id = ? AND graph_version = ?
                """,
                (
                    updated.state.value,
                    updated.graph_version,
                    updated.graph_fingerprint,
                    self._payload(updated),
                    updated.updated_at,
                    session_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "work graph changed concurrently; retry with fresh status"
                )
            self._connection.execute(
                "DELETE FROM swarm_concurrency_plans WHERE session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                "DELETE FROM swarm_shared_admissions WHERE session_id = ?",
                (session_id,),
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def replace_budget_policy(
        self,
        session_id: str,
        policy: SwarmBudgetPolicy,
        *,
        expected_version: int,
        updated_at: str,
    ) -> SwarmSession:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT payload_json, budget_version FROM swarm_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown swarm session {session_id!r}")
            current_version = int(row["budget_version"])
            if current_version != expected_version:
                raise ValueError(
                    f"stale budget policy: expected version {expected_version}, "
                    f"current version is {current_version}"
                )
            current = self._session_from_row(row, session_id)
            if current.budget_fingerprint == policy.fingerprint():
                self._connection.commit()
                return current
            updated = current.with_budget_policy(policy, updated_at=updated_at)
            cursor = self._connection.execute(
                """
                UPDATE swarm_sessions
                   SET state = ?, budget_version = ?, budget_fingerprint = ?,
                       payload_json = ?, updated_at = ?
                 WHERE session_id = ? AND budget_version = ?
                """,
                (
                    updated.state.value,
                    updated.budget_version,
                    updated.budget_fingerprint,
                    self._payload(updated),
                    updated.updated_at,
                    session_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "budget policy changed concurrently; retry with fresh status"
                )
            self._connection.execute(
                "DELETE FROM swarm_concurrency_plans WHERE session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                "DELETE FROM swarm_shared_admissions WHERE session_id = ?",
                (session_id,),
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _plan_payload(plan: ConcurrencyPlan) -> str:
        return json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def get_concurrency_plan(
        self, session_id: str
    ) -> tuple[ConcurrencyPlan, int] | None:
        row = self._connection.execute(
            "SELECT plan_version, payload_json FROM swarm_concurrency_plans "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(
                f"stored concurrency plan for {session_id!r} is invalid"
            )
        return ConcurrencyPlan.from_dict(payload), int(row["plan_version"])

    def save_concurrency_plan(
        self,
        session_id: str,
        plan: ConcurrencyPlan,
        *,
        expected_graph_version: int,
        expected_budget_version: int,
        created_at: str,
    ) -> tuple[ConcurrencyPlan, int, bool]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            session_row = self._connection.execute(
                "SELECT graph_version, graph_fingerprint, budget_version, "
                "budget_fingerprint FROM swarm_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise KeyError(f"unknown swarm session {session_id!r}")
            current_graph_version = int(session_row["graph_version"])
            current_budget_version = int(session_row["budget_version"])
            if current_graph_version != expected_graph_version:
                raise ValueError(
                    "work graph changed while concurrency was being planned; retry"
                )
            if current_budget_version != expected_budget_version:
                raise ValueError(
                    "budget policy changed while concurrency was being planned; retry"
                )
            if (
                plan.graph_version != current_graph_version
                or plan.graph_fingerprint != str(session_row["graph_fingerprint"])
                or plan.budget_version != current_budget_version
                or plan.budget_fingerprint != str(session_row["budget_fingerprint"])
            ):
                raise ValueError(
                    "concurrency plan is not bound to the current graph and budget"
                )
            existing = self._connection.execute(
                "SELECT plan_version, plan_fingerprint, payload_json, created_at "
                "FROM swarm_concurrency_plans WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            fingerprint = plan.fingerprint()
            if (
                existing is not None
                and str(existing["plan_fingerprint"]) == fingerprint
            ):
                payload = json.loads(str(existing["payload_json"]))
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"stored concurrency plan for {session_id!r} is invalid"
                    )
                self._connection.commit()
                return (
                    ConcurrencyPlan.from_dict(payload),
                    int(existing["plan_version"]),
                    False,
                )
            plan_version = 1 if existing is None else int(existing["plan_version"]) + 1
            first_created_at = (
                created_at if existing is None else str(existing["created_at"])
            )
            self._connection.execute(
                """
                INSERT INTO swarm_concurrency_plans (
                    session_id, plan_version, graph_version, graph_fingerprint,
                    budget_version, budget_fingerprint, plan_fingerprint,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    plan_version = excluded.plan_version,
                    graph_version = excluded.graph_version,
                    graph_fingerprint = excluded.graph_fingerprint,
                    budget_version = excluded.budget_version,
                    budget_fingerprint = excluded.budget_fingerprint,
                    plan_fingerprint = excluded.plan_fingerprint,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    plan_version,
                    plan.graph_version,
                    plan.graph_fingerprint,
                    plan.budget_version,
                    plan.budget_fingerprint,
                    fingerprint,
                    self._plan_payload(plan),
                    first_created_at,
                    created_at,
                ),
            )
            self._connection.execute(
                "DELETE FROM swarm_shared_admissions WHERE session_id = ?",
                (session_id,),
            )
            self._connection.commit()
            return plan, plan_version, True
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _worktree_payload(record: ManagedWorktree) -> str:
        return json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def list_worktrees(self, session_id: str) -> list[ManagedWorktree]:
        rows = self._connection.execute(
            "SELECT payload_json FROM swarm_worktrees "
            "WHERE session_id = ? ORDER BY work_id ASC",
            (session_id,),
        ).fetchall()
        records: list[ManagedWorktree] = []
        for row in rows:
            payload: Any = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError(
                    f"stored managed worktree for {session_id!r} is invalid"
                )
            records.append(ManagedWorktree.from_dict(payload))
        return records

    def save_worktrees(
        self,
        session_id: str,
        records: list[ManagedWorktree],
        *,
        expected_graph_version: int,
        expected_graph_fingerprint: str,
    ) -> tuple[list[ManagedWorktree], int]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT graph_version, graph_fingerprint FROM swarm_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown swarm session {session_id!r}")
            if int(row["graph_version"]) != expected_graph_version:
                raise ValueError(
                    "work graph changed while worktrees were being provisioned; retry"
                )
            if str(row["graph_fingerprint"]) != expected_graph_fingerprint:
                raise ValueError(
                    "work graph fingerprint changed while worktrees were being "
                    "provisioned; retry"
                )
            plan = self._connection.execute(
                "SELECT graph_version, graph_fingerprint, payload_json "
                "FROM swarm_concurrency_plans WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if plan is None:
                raise ValueError(
                    "swarm session has no concurrency plan; run "
                    "'claim-plane swarm plan' first"
                )
            plan_payload: Any = json.loads(str(plan["payload_json"]))
            if not isinstance(plan_payload, dict):
                raise ValueError("stored concurrency plan is invalid")
            parsed_plan = ConcurrencyPlan.from_dict(plan_payload)
            if parsed_plan.status.value != "ready":
                raise ValueError(
                    "cannot provision worktrees for a replan-required plan"
                )
            if (
                int(plan["graph_version"]) != expected_graph_version
                or str(plan["graph_fingerprint"]) != expected_graph_fingerprint
            ):
                raise ValueError(
                    "concurrency plan is stale for the current work graph; re-plan"
                )
            created = 0
            for record in records:
                if (
                    record.session_id != session_id
                    or record.graph_version != expected_graph_version
                    or record.graph_fingerprint != expected_graph_fingerprint
                ):
                    raise ValueError(
                        "managed worktree record is not bound to the current "
                        "session graph"
                    )
                existing = self._connection.execute(
                    "SELECT payload_json FROM swarm_worktrees "
                    "WHERE session_id = ? AND work_id = ?",
                    (session_id, record.work_id),
                ).fetchone()
                if existing is not None:
                    payload: Any = json.loads(str(existing["payload_json"]))
                    if not isinstance(payload, dict):
                        raise ValueError("stored managed worktree is invalid")
                    current = ManagedWorktree.from_dict(payload)
                    if current != record:
                        raise ValueError(
                            f"managed worktree {record.work_id!r} already exists "
                            "with different ownership metadata"
                        )
                    continue
                self._connection.execute(
                    """
                    INSERT INTO swarm_worktrees (
                        session_id, work_id, repository_identity, graph_version,
                        graph_fingerprint, base_commit, branch, worktree_path,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.session_id,
                        record.work_id,
                        record.repository_identity,
                        record.graph_version,
                        record.graph_fingerprint,
                        record.base_commit,
                        record.branch,
                        record.worktree_path,
                        self._worktree_payload(record),
                        record.created_at,
                        record.updated_at,
                    ),
                )
                created += 1
            self._connection.commit()
            return self.list_worktrees(session_id), created
        except Exception:
            self._connection.rollback()
            raise

    def delete_worktrees(self, session_id: str, work_ids: list[str]) -> int:
        if not work_ids:
            return 0
        placeholders = ",".join("?" for _ in work_ids)
        with self._connection:
            cursor = self._connection.execute(
                f"DELETE FROM swarm_worktrees WHERE session_id = ? "
                f"AND work_id IN ({placeholders})",
                (session_id, *work_ids),
            )
        return int(cursor.rowcount)

    @staticmethod
    def _shared_admission_payload(plan: SharedAdmissionPlan) -> str:
        return json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def get_shared_admission(
        self, session_id: str
    ) -> tuple[SharedAdmissionPlan, int] | None:
        row = self._connection.execute(
            "SELECT admission_version, payload_json "
            "FROM swarm_shared_admissions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        payload: Any = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(f"stored shared admission for {session_id!r} is invalid")
        return SharedAdmissionPlan.from_dict(payload), int(row["admission_version"])

    def save_shared_admission(
        self,
        session_id: str,
        plan: SharedAdmissionPlan,
        *,
        expected_graph_version: int,
        expected_budget_version: int,
        expected_concurrency_plan_fingerprint: str,
        created_at: str,
    ) -> tuple[SharedAdmissionPlan, int, bool]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            session = self.require(session_id)
            if (
                session.graph_version != expected_graph_version
                or session.budget_version != expected_budget_version
                or session.graph_fingerprint != plan.graph_fingerprint
                or session.budget_fingerprint != plan.budget_fingerprint
            ):
                raise ValueError("swarm session changed during shared admission")
            stored_plan = self.get_concurrency_plan(session_id)
            if stored_plan is None:
                raise ValueError("swarm session has no concurrency plan")
            concurrency, _ = stored_plan
            if concurrency.fingerprint() != expected_concurrency_plan_fingerprint:
                raise ValueError("concurrency plan changed during shared admission")
            current = self.get_shared_admission(session_id)
            if current is not None and current[0].fingerprint() == plan.fingerprint():
                self._connection.commit()
                return current[0], current[1], False
            version = 1 if current is None else current[1] + 1
            self._connection.execute(
                """
                INSERT INTO swarm_shared_admissions (
                    session_id, admission_version, graph_version, graph_fingerprint,
                    budget_version, budget_fingerprint,
                    concurrency_plan_fingerprint, admission_fingerprint, status,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    admission_version=excluded.admission_version,
                    graph_version=excluded.graph_version,
                    graph_fingerprint=excluded.graph_fingerprint,
                    budget_version=excluded.budget_version,
                    budget_fingerprint=excluded.budget_fingerprint,
                    concurrency_plan_fingerprint=excluded.concurrency_plan_fingerprint,
                    admission_fingerprint=excluded.admission_fingerprint,
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    version,
                    plan.graph_version,
                    plan.graph_fingerprint,
                    plan.budget_version,
                    plan.budget_fingerprint,
                    plan.concurrency_plan_fingerprint,
                    plan.fingerprint(),
                    plan.status.value,
                    self._shared_admission_payload(plan),
                    created_at,
                    created_at,
                ),
            )
            self._connection.commit()
            return plan, version, True
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _codex_run_payload(record: CodexRunRecord) -> str:
        return json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def list_codex_runs(
        self, session_id: str, *, work_id: str | None = None
    ) -> list[CodexRunRecord]:
        query = "SELECT payload_json FROM swarm_codex_runs WHERE session_id = ?"
        values: tuple[Any, ...] = (session_id,)
        if work_id is not None:
            query += " AND work_id = ?"
            values = (session_id, work_id)
        query += " ORDER BY created_at ASC, attempt ASC"
        rows = self._connection.execute(query, values).fetchall()
        records: list[CodexRunRecord] = []
        for row in rows:
            payload: Any = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("stored Codex run is invalid")
            records.append(CodexRunRecord.from_dict(payload))
        return records

    def get_codex_run(self, run_id: str) -> CodexRunRecord | None:
        row = self._connection.execute(
            "SELECT payload_json FROM swarm_codex_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        payload: Any = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(f"stored Codex run {run_id!r} is invalid")
        return CodexRunRecord.from_dict(payload)

    def require_codex_run(self, run_id: str) -> CodexRunRecord:
        record = self.get_codex_run(run_id)
        if record is None:
            raise KeyError(f"unknown Codex run {run_id!r}")
        return record

    def reserve_codex_run(
        self,
        record: CodexRunRecord,
        *,
        max_active: int,
        max_active_per_work_item: int,
        max_total_launches: int,
        max_attempts_per_work_item: int,
        max_total_tokens: int | None,
        expected_admission_fingerprint: str,
    ) -> CodexRunRecord:
        active_values = tuple(
            state.value
            for state in (
                CodexRunState.RESERVED,
                CodexRunState.RUNNING,
                CodexRunState.CANCELLING,
            )
        )
        placeholders = ",".join("?" for _ in active_values)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            session = self.require(record.session_id)
            if (
                session.graph_version != record.graph_version
                or session.graph_fingerprint != record.graph_fingerprint
                or session.budget_version != record.budget_version
                or session.budget_fingerprint != record.budget_fingerprint
            ):
                raise ValueError(
                    "Codex run is not bound to the current graph and budget"
                )
            if session.state not in {
                SwarmSessionState.PLANNED,
                SwarmSessionState.RUNNING,
            }:
                raise ValueError(
                    f"cannot reserve Codex run while session is {session.state.value}"
                )
            shared = self.get_shared_admission(record.session_id)
            if shared is None:
                raise ValueError(
                    "swarm session has no shared admission; run "
                    "'claim-plane swarm admit' first"
                )
            admission, _ = shared
            if admission.fingerprint() != expected_admission_fingerprint:
                raise ValueError("shared admission changed while reserving worker")
            if admission.status is not SharedAdmissionStatus.READY:
                raise ValueError("shared admission requires replanning")
            existing_records = self.list_codex_runs(record.session_id)
            snapshot = compute_scheduler_snapshot(session, admission, existing_records)
            if record.work_id not in snapshot.dispatchable_work_ids:
                state = next(
                    item.state.value
                    for item in snapshot.work
                    if item.work_id == record.work_id
                )
                runnable = ", ".join(snapshot.dispatchable_work_ids) or "none"
                raise ValueError(
                    f"work item {record.work_id!r} is not dispatchable "
                    f"(state={state}); dispatchable: {runnable}"
                )
            total_launches = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM swarm_codex_runs WHERE session_id = ?",
                    (record.session_id,),
                ).fetchone()[0]
            )
            if total_launches >= max_total_launches:
                raise ValueError("workers.max_total_launches is exhausted")
            work_attempts = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM swarm_codex_runs "
                    "WHERE session_id = ? AND work_id = ?",
                    (record.session_id, record.work_id),
                ).fetchone()[0]
            )
            if work_attempts >= max_attempts_per_work_item:
                raise ValueError(
                    f"restart budget is exhausted for work item {record.work_id!r}"
                )
            active = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM swarm_codex_runs "
                    f"WHERE session_id = ? AND state IN ({placeholders})",
                    (record.session_id, *active_values),
                ).fetchone()[0]
            )
            if active >= max_active:
                raise ValueError("workers.max_active is exhausted")
            active_for_work = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM swarm_codex_runs "
                    f"WHERE session_id = ? AND work_id = ? "
                    f"AND state IN ({placeholders})",
                    (record.session_id, record.work_id, *active_values),
                ).fetchone()[0]
            )
            if active_for_work >= max_active_per_work_item:
                raise ValueError(
                    "workers.max_active_per_work_item is exhausted for "
                    f"{record.work_id!r}"
                )
            if max_total_tokens is not None:
                row = self._connection.execute(
                    f"SELECT COALESCE(SUM(total_tokens), 0), "
                    f"COALESCE(SUM(CASE WHEN state IN ({placeholders}) "
                    f"THEN token_limit ELSE 0 END), 0) "
                    "FROM swarm_codex_runs WHERE session_id = ?",
                    (*active_values, record.session_id),
                ).fetchone()
                consumed = int(row[0] or 0)
                reserved = int(row[1] or 0)
                requested = record.budget.token_limit or 0
                if consumed + reserved + requested > max_total_tokens:
                    raise ValueError("resources.max_total_tokens would be exceeded")
            if session.state is SwarmSessionState.PLANNED:
                running_session = replace(
                    session,
                    state=SwarmSessionState.RUNNING,
                    updated_at=record.created_at,
                )
                cursor = self._connection.execute(
                    "UPDATE swarm_sessions "
                    "SET state = ?, payload_json = ?, updated_at = ? "
                    "WHERE session_id = ? AND state = ?",
                    (
                        SwarmSessionState.RUNNING.value,
                        self._payload(running_session),
                        record.created_at,
                        record.session_id,
                        SwarmSessionState.PLANNED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("swarm session state changed while reserving run")
            self._connection.execute(
                """
                INSERT INTO swarm_codex_runs (
                    run_id, session_id, work_id, attempt, state, token_limit,
                    total_tokens, duration_seconds, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.session_id,
                    record.work_id,
                    record.attempt,
                    record.state.value,
                    record.budget.token_limit,
                    record.usage.total_tokens,
                    record.duration_seconds,
                    self._codex_run_payload(record),
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._connection.commit()
            return record
        except Exception:
            self._connection.rollback()
            raise

    def update_codex_run(self, record: CodexRunRecord) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE swarm_codex_runs
                   SET state = ?, token_limit = ?, total_tokens = ?,
                       duration_seconds = ?, payload_json = ?, updated_at = ?
                 WHERE run_id = ?
                """,
                (
                    record.state.value,
                    record.budget.token_limit,
                    record.usage.total_tokens,
                    record.duration_seconds,
                    self._codex_run_payload(record),
                    record.updated_at,
                    record.run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown Codex run {record.run_id!r}")

    def bind_worktree_runner(
        self,
        session_id: str,
        work_id: str,
        *,
        worker_id: str,
        intent_id: str | None,
        updated_at: str,
    ) -> ManagedWorktree:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT payload_json FROM swarm_worktrees "
                "WHERE session_id = ? AND work_id = ?",
                (session_id, work_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown managed worktree {work_id!r}")
            payload: Any = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("stored managed worktree is invalid")
            current = ManagedWorktree.from_dict(payload)
            updated = replace(
                current,
                worker_id=worker_id,
                intent_id=intent_id,
                updated_at=updated_at,
            )
            self._connection.execute(
                "UPDATE swarm_worktrees SET payload_json = ?, updated_at = ? "
                "WHERE session_id = ? AND work_id = ?",
                (
                    self._worktree_payload(updated),
                    updated_at,
                    session_id,
                    work_id,
                ),
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _session_from_row(row: sqlite3.Row, session_id: str) -> SwarmSession:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(f"stored swarm session {session_id!r} is invalid")
        return SwarmSession.from_dict(payload)
