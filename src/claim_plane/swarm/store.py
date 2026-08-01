"""SQLite persistence for durable swarm planning and budget state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from claim_plane.swarm.budget import SwarmBudgetPolicy
from claim_plane.swarm.concurrency import ConcurrencyPlan
from claim_plane.swarm.models import SwarmSession, WorkGraph

_SCHEMA_VERSION = 3


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
            self._connection.commit()
            return plan, plan_version, True
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _session_from_row(row: sqlite3.Row, session_id: str) -> SwarmSession:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(f"stored swarm session {session_id!r} is invalid")
        return SwarmSession.from_dict(payload)
