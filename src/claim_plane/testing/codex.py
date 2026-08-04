"""Codex implementation of the shared adapter conformance scenarios."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from claim_plane.connectors import CodexAdapter
from claim_plane.connectors.codex import init_project
from claim_plane.protocol import (
    AdapterErrorCode,
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterStatus,
    ConformanceObservation,
    ConformanceScenario,
    LifecycleEventStore,
)


class CodexConformanceDriver:
    """Run the canonical suite through the public Codex adapter boundary."""

    name = "codex"

    def __init__(self, root: str | Path | None = None) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="claim-plane-codex-")
        self.root = Path(root or self._temporary.name).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.adapter = CodexAdapter()
        self._counter = 0
        self._manifest_root = self._create_repo("manifest")

    def manifest(self):  # structural protocol typing
        return self.adapter.capability_manifest(str(self._manifest_root))

    def _create_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "claim-plane@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Claim Plane Conformance"],
            cwd=repo,
            check=True,
        )
        (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
        init_project(repo)
        self.adapter.enroll_project(
            self._request(AdapterOperation.ENROLL_PROJECT, repo, "enroll")
        )
        return repo

    def _case(self, scenario: ConformanceScenario) -> tuple[Path, str]:
        self._counter += 1
        repo = self._create_repo(f"{self._counter:02d}-{scenario.value}")
        return repo, f"conformance-{self._counter}"

    @staticmethod
    def _request(
        operation: AdapterOperation,
        repo: Path,
        request_id: str,
        *,
        session_id: str | None = None,
        intent_id: str | None = None,
        intent_version: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AdapterRequest:
        return AdapterRequest.create(
            operation,
            adapter="codex",
            project_root=str(repo),
            request_id=request_id,
            session_id=session_id,
            intent_id=intent_id,
            intent_version=intent_version,
            payload=payload,
        )

    @staticmethod
    def _proposal() -> dict[str, Any]:
        return {
            "protocol": "claim-plane.codex-intent-proposal.v1",
            "goal": "Update the repository fixture",
            "operations": [
                {
                    "access": "write",
                    "kind": "file",
                    "identifier": "README.md",
                    "commitment": "committed",
                }
            ],
            "preserves": [],
            "acceptance": [],
        }

    @staticmethod
    def _patch(path: str, old: str = "a", new: str = "b") -> str:
        return "\n".join(
            (
                "*** Begin Patch",
                f"*** Update File: {path}",
                "@@",
                f"-{old}",
                f"+{new}",
                "*** End Patch",
            )
        )

    def _active(self, scenario: ConformanceScenario):
        repo, session = self._case(scenario)
        self.adapter.start_session(
            self._request(
                AdapterOperation.START_SESSION,
                repo,
                "start",
                session_id=session,
                payload={"source": "startup"},
            )
        )
        self.adapter.submit_task(
            self._request(
                AdapterOperation.SUBMIT_TASK,
                repo,
                "task",
                session_id=session,
                payload={"prompt": "Update the repository fixture."},
            )
        )
        admitted = self.adapter.propose_intent(
            self._request(
                AdapterOperation.PROPOSE_INTENT,
                repo,
                "intent",
                session_id=session,
                intent_version=0,
                payload={"proposal": self._proposal()},
            )
        )
        return repo, session, admitted

    def _mutation(
        self,
        repo: Path,
        session: str,
        admitted,
        path: str,
        request_id: str,
        *,
        version: int | None = None,
    ):
        return self.adapter.request_mutation(
            self._request(
                AdapterOperation.REQUEST_MUTATION,
                repo,
                request_id,
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=(
                    admitted.intent_version if version is None else version
                ),
                payload={
                    "tool_name": "apply_patch",
                    "tool_input": {"command": self._patch(path)},
                },
            )
        )

    def run(self, scenario: ConformanceScenario) -> ConformanceObservation:
        return getattr(self, f"_scenario_{scenario.value}")(scenario)

    def _scenario_declared_mutation_succeeds(self, scenario):
        repo, session, admitted = self._active(scenario)
        response = self._mutation(repo, session, admitted, "README.md", "declared")
        assert response.status is AdapterStatus.SUCCEEDED
        return ConformanceObservation("Codex admitted the declared mutation.")

    def _scenario_undeclared_mutation_denied(self, scenario):
        repo, session, admitted = self._active(scenario)
        response = self._mutation(repo, session, admitted, "outside.py", "undeclared")
        assert response.status is AdapterStatus.DENIED
        return ConformanceObservation("Codex denied an undeclared mutation.")

    def _issue_ticket(self, repo: Path, session: str, admitted, request_id: str):
        denied = self._mutation(repo, session, admitted, "outside.py", request_id)
        assert denied.status is AdapterStatus.DENIED
        status = self.adapter.inspect(
            self._request(
                AdapterOperation.INSPECT,
                repo,
                f"inspect-{request_id}",
                session_id=session,
            )
        )
        return (
            status.payload["scope_amendment"]["pending"]["ticket_id"],
            denied.intent_version,
        )

    def _scenario_legitimate_amendment_admitted_atomically(self, scenario):
        repo, session, admitted = self._active(scenario)
        ticket, current_version = self._issue_ticket(repo, session, admitted, "ticket")
        amended = self.adapter.request_amendment(
            self._request(
                AdapterOperation.REQUEST_AMENDMENT,
                repo,
                "amend",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=current_version,
                payload={
                    "ticket_id": ticket,
                    "reason": (
                        "The supporting file is required by the requested change."
                    ),
                },
            )
        )
        assert amended.status is AdapterStatus.SUCCEEDED
        assert amended.intent_version is not None
        assert amended.intent_version > current_version
        retry = self._mutation(repo, session, amended, "outside.py", "retry")
        assert retry.status is AdapterStatus.SUCCEEDED
        return ConformanceObservation(
            "Codex committed the amendment before retrying the mutation.",
            {"intent_version": amended.intent_version},
        )

    def _scenario_rejected_amendment_preserves_old_authority(self, scenario):
        repo, session, admitted = self._active(scenario)
        _, current_version = self._issue_ticket(repo, session, admitted, "ticket")
        try:
            self.adapter.request_amendment(
                self._request(
                    AdapterOperation.REQUEST_AMENDMENT,
                    repo,
                    "reject",
                    session_id=session,
                    intent_id=admitted.intent_id,
                    intent_version=current_version,
                    payload={
                        "ticket_id": "unknown-ticket",
                        "reason": "This request must be rejected.",
                    },
                )
            )
        except AdapterProtocolError:
            pass
        else:
            raise AssertionError("invalid amendment ticket was accepted")
        status = self.adapter.inspect(
            self._request(
                AdapterOperation.INSPECT, repo, "inspect-old", session_id=session
            )
        )
        assert status.intent_version == current_version
        old = self._mutation(repo, session, status, "README.md", "old-authority")
        assert old.status is AdapterStatus.SUCCEEDED
        return ConformanceObservation("Rejected amendment preserved old authority.")

    def _scenario_stale_intent_version_denied(self, scenario):
        repo, session, admitted = self._active(scenario)
        try:
            self._mutation(repo, session, admitted, "README.md", "stale", version=0)
        except AdapterProtocolError as exc:
            assert exc.code is AdapterErrorCode.STALE_INTENT_VERSION
        else:
            raise AssertionError("stale intent version was accepted")
        return ConformanceObservation(
            "Codex rejected stale authority before hook execution."
        )

    def _scenario_expired_lease_denied(self, scenario):
        repo, session, admitted = self._active(scenario)
        with sqlite3.connect(repo / ".claim-plane/plane.db") as connection:
            connection.execute(
                "UPDATE intents SET lease_expires_at=? WHERE intent_id=?",
                ("2000-01-01T00:00:00+00:00", admitted.intent_id),
            )
        outcome = self.adapter.request_mutation(
            self._request(
                AdapterOperation.REQUEST_MUTATION,
                repo,
                "expired",
                session_id=session,
                intent_id=admitted.intent_id,
                payload={
                    "tool_name": "apply_patch",
                    "tool_input": {"command": self._patch("README.md")},
                },
            )
        )
        assert outcome.status is AdapterStatus.DENIED
        return ConformanceObservation("Codex denied mutation after lease expiry.")

    def _scenario_duplicate_event_idempotent(self, scenario):
        repo, session = self._case(scenario)
        request = self._request(
            AdapterOperation.START_SESSION,
            repo,
            "same-start",
            session_id=session,
            payload={"source": "startup"},
        )
        first = self.adapter.start_session(request)
        with LifecycleEventStore.for_project(repo) as store:
            before = len(store.list_events(adapter="codex", session_id=session))
        second = self.adapter.start_session(request)
        with LifecycleEventStore.for_project(repo) as store:
            after = len(store.list_events(adapter="codex", session_id=session))
        assert not first.replayed and second.replayed and before == after == 1
        return ConformanceObservation(
            "Codex replay did not duplicate lifecycle events."
        )

    def _scenario_out_of_order_event_fails_closed(self, scenario):
        repo, session = self._case(scenario)
        self.adapter.start_session(
            self._request(
                AdapterOperation.START_SESSION,
                repo,
                "start",
                session_id=session,
                payload={"source": "startup"},
            )
        )
        database = repo / ".claim-plane/lifecycle/events.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE lifecycle_events SET sequence=8 "
                "WHERE adapter=? AND session_id=?",
                ("codex", session),
            )
        try:
            self.adapter.resume(
                self._request(
                    AdapterOperation.RESUME,
                    repo,
                    "resume",
                    session_id=session,
                    payload={"source": "resume"},
                )
            )
        except AdapterProtocolError as exc:
            assert exc.code is AdapterErrorCode.CORRUPT_STATE
        else:
            raise AssertionError("out-of-order Codex events were accepted")
        return ConformanceObservation("Codex failed closed on invalid event order.")

    def _scenario_adapter_crash_resumes_safely(self, scenario):
        repo, session, admitted = self._active(scenario)
        replacement = CodexAdapter()
        resumed = replacement.resume(
            self._request(
                AdapterOperation.RESUME,
                repo,
                "resume",
                session_id=session,
                payload={"source": "resume"},
            )
        )
        assert resumed.status is AdapterStatus.SUCCEEDED
        inspected = replacement.inspect(
            self._request(AdapterOperation.INSPECT, repo, "inspect", session_id=session)
        )
        assert inspected.intent_id == admitted.intent_id
        assert inspected.intent_version is not None
        assert inspected.intent_version >= admitted.intent_version
        return ConformanceObservation(
            "A fresh Codex adapter resumed durable authority."
        )

    def _scenario_cancellation_revokes_authority(self, scenario):
        repo, session, admitted = self._active(scenario)
        cancelled = self.adapter.cancel(
            self._request(
                AdapterOperation.CANCEL,
                repo,
                "cancel",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
            )
        )
        assert cancelled.status is AdapterStatus.CANCELLED
        inspected = self.adapter.inspect(
            self._request(
                AdapterOperation.INSPECT,
                repo,
                "inspect-cancelled",
                session_id=session,
            )
        )
        assert cancelled.payload["released"] is True
        assert inspected.payload["state"] == "abandoned"
        return ConformanceObservation("Codex cancellation released mutation authority.")

    def _scenario_completion_detects_uncovered_mutation(self, scenario):
        repo, session, admitted = self._active(scenario)
        (repo / "outside.py").write_text("outside = True\n", encoding="utf-8")
        result = self.adapter.verify_completion(
            self._request(
                AdapterOperation.VERIFY_COMPLETION,
                repo,
                "verify",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
            )
        )
        assert result.status is AdapterStatus.DENIED
        assert result.payload["verified"] is False
        return ConformanceObservation("Codex completion found an uncovered Git change.")

    def _scenario_corrupt_state_cannot_produce_verified(self, scenario):
        repo, session, admitted = self._active(scenario)
        database = repo / ".claim-plane/lifecycle/events.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE lifecycle_events SET digest='broken' "
                "WHERE adapter=? AND session_id=?",
                ("codex", session),
            )
        try:
            self.adapter.verify_completion(
                self._request(
                    AdapterOperation.VERIFY_COMPLETION,
                    repo,
                    "verify-corrupt",
                    session_id=session,
                    intent_id=admitted.intent_id,
                    intent_version=admitted.intent_version,
                )
            )
        except AdapterProtocolError as exc:
            assert exc.code is AdapterErrorCode.CORRUPT_STATE
        else:
            raise AssertionError("corrupt Codex state produced a completion result")
        return ConformanceObservation(
            "Corrupt Codex evidence could not produce VERIFIED."
        )

    def _scenario_secret_values_absent_from_evidence(self, scenario):
        repo, session = self._case(scenario)
        secret = "codex-conformance-secret-never-export"
        self.adapter.start_session(
            self._request(
                AdapterOperation.START_SESSION,
                repo,
                "secret-start",
                session_id=session,
                payload={"source": "startup", "api_token": secret},
            )
        )
        self.adapter.submit_task(
            self._request(
                AdapterOperation.SUBMIT_TASK,
                repo,
                "secret-task",
                session_id=session,
                payload={"prompt": secret},
            )
        )
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (repo / ".claim-plane").rglob("*")
            if path.is_file() and path.suffix != ".sqlite3"
        )
        with LifecycleEventStore.for_project(repo) as store:
            events = store.list_events(adapter="codex", session_id=session)
        serialized = json.dumps([item.to_dict() for item in events], sort_keys=True)
        assert secret not in text and secret not in serialized
        return ConformanceObservation("Codex evidence contained no raw secret values.")
