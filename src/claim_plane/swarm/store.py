"""SQLite persistence for durable swarm-session planning state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from claim_plane.swarm.models import SwarmSession, WorkGraph


class SwarmSessionStore:
    """Single-host durable store with optimistic graph-version updates."""

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
        if schema_version > 1:
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
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_sessions_updated "
            "ON swarm_sessions(updated_at DESC, session_id ASC)"
        )
        if schema_version == 0:
            self._connection.execute("PRAGMA user_version=1")
        self._connection.commit()

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
                        graph_version, graph_fingerprint, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.repository_identity,
                        session.base_commit,
                        session.state.value,
                        session.graph_version,
                        session.graph_fingerprint,
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
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError(f"stored swarm session {session_id!r} is invalid")
            current = SwarmSession.from_dict(payload)
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
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise
