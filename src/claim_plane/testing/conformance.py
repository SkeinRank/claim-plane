"""Reference conformance driver and standalone core compatibility check."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from claim_plane.protocol import (
    AdapterErrorCode,
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterStatus,
    ConformanceObservation,
    ConformanceScenario,
    LifecycleEventStore,
    run_adapter_conformance,
)
from claim_plane.testing.reference_adapter import ReferenceAdapter


class ReferenceConformanceDriver:
    """Execute the canonical scenarios against the dependency-free adapter."""

    name = "reference"

    def __init__(self, root: str | Path | None = None) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="claim-plane-reference-")
        self.root = Path(root or self._temporary.name).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.adapter = ReferenceAdapter()
        self._counter = 0

    def manifest(self):  # type annotation is inherited structurally
        return self.adapter.capability_manifest(str(self.root))

    def _case(self, scenario: ConformanceScenario) -> tuple[Path, str]:
        self._counter += 1
        root = self.root / f"{self._counter:02d}-{scenario.value}"
        root.mkdir(parents=True, exist_ok=True)
        return root, f"session-{self._counter}"

    @staticmethod
    def _request(
        operation: AdapterOperation,
        root: Path,
        request_id: str,
        *,
        session_id: str | None = None,
        intent_id: str | None = None,
        intent_version: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AdapterRequest:
        return AdapterRequest.create(
            operation,
            adapter="reference",
            project_root=str(root),
            request_id=request_id,
            session_id=session_id,
            intent_id=intent_id,
            intent_version=intent_version,
            payload=payload,
        )

    def _active(self, scenario: ConformanceScenario):
        root, session = self._case(scenario)
        self.adapter.enroll_project(
            self._request(AdapterOperation.ENROLL_PROJECT, root, "enroll")
        )
        self.adapter.start_session(
            self._request(
                AdapterOperation.START_SESSION, root, "start", session_id=session
            )
        )
        self.adapter.submit_task(
            self._request(
                AdapterOperation.SUBMIT_TASK,
                root,
                "task",
                session_id=session,
                payload={"prompt": "Update the fixture."},
            )
        )
        admitted = self.adapter.propose_intent(
            self._request(
                AdapterOperation.PROPOSE_INTENT,
                root,
                "intent",
                session_id=session,
                intent_version=0,
                payload={"resources": ["README.md"]},
            )
        )
        return root, session, admitted

    def _mutation(
        self,
        root: Path,
        session: str,
        admitted,
        resource: str,
        request_id: str,
        *,
        version: int | None = None,
    ):
        return self.adapter.request_mutation(
            self._request(
                AdapterOperation.REQUEST_MUTATION,
                root,
                request_id,
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=(
                    admitted.intent_version if version is None else version
                ),
                payload={"resource": resource},
            )
        )

    def run(self, scenario: ConformanceScenario) -> ConformanceObservation:
        method = getattr(self, f"_scenario_{scenario.value}")
        return method(scenario)

    def _scenario_declared_mutation_succeeds(self, scenario):
        root, session, admitted = self._active(scenario)
        result = self._mutation(root, session, admitted, "README.md", "declared")
        assert result.status is AdapterStatus.SUCCEEDED
        return ConformanceObservation("Declared authority admitted the mutation.")

    def _scenario_undeclared_mutation_denied(self, scenario):
        root, session, admitted = self._active(scenario)
        result = self._mutation(root, session, admitted, "outside.py", "undeclared")
        assert result.status is AdapterStatus.DENIED
        return ConformanceObservation(
            "Undeclared authority was denied before mutation."
        )

    def _scenario_legitimate_amendment_admitted_atomically(self, scenario):
        root, session, admitted = self._active(scenario)
        amendment = self.adapter.request_amendment(
            self._request(
                AdapterOperation.REQUEST_AMENDMENT,
                root,
                "amend",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
                payload={"resource": "tests/test_fixture.py", "approved": True},
            )
        )
        assert amendment.status is AdapterStatus.SUCCEEDED
        assert amendment.intent_version == admitted.intent_version + 1
        retry = self._mutation(
            root,
            session,
            amendment,
            "tests/test_fixture.py",
            "amended-mutation",
        )
        assert retry.status is AdapterStatus.SUCCEEDED
        return ConformanceObservation(
            "The amendment committed a new intent version before authority was used.",
            {"intent_version": amendment.intent_version},
        )

    def _scenario_rejected_amendment_preserves_old_authority(self, scenario):
        root, session, admitted = self._active(scenario)
        rejected = self.adapter.request_amendment(
            self._request(
                AdapterOperation.REQUEST_AMENDMENT,
                root,
                "reject-amend",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
                payload={"resource": "outside.py", "approved": False},
            )
        )
        assert rejected.status is AdapterStatus.DENIED
        assert rejected.intent_version == admitted.intent_version
        assert self._mutation(
            root, session, admitted, "README.md", "old-authority"
        ).status is AdapterStatus.SUCCEEDED
        assert self._mutation(
            root, session, admitted, "outside.py", "still-denied"
        ).status is AdapterStatus.DENIED
        return ConformanceObservation(
            "Rejected expansion left the previous authority unchanged."
        )

    def _scenario_stale_intent_version_denied(self, scenario):
        root, session, admitted = self._active(scenario)
        try:
            self._mutation(root, session, admitted, "README.md", "stale", version=0)
        except AdapterProtocolError as exc:
            assert exc.code is AdapterErrorCode.STALE_INTENT_VERSION
        else:
            raise AssertionError("stale intent version was accepted")
        return ConformanceObservation("Stale authority was rejected before mutation.")

    def _scenario_expired_lease_denied(self, scenario):
        root, session, admitted = self._active(scenario)
        self.adapter.expire_lease(root, session)
        try:
            self._mutation(root, session, admitted, "README.md", "expired")
        except AdapterProtocolError as exc:
            assert exc.details.get("reason") == "expired_lease"
        else:
            raise AssertionError("expired lease was accepted")
        return ConformanceObservation("Expired authority failed closed.")

    def _scenario_duplicate_event_idempotent(self, scenario):
        root, session = self._case(scenario)
        request = self._request(
            AdapterOperation.START_SESSION, root, "same", session_id=session
        )
        first = self.adapter.start_session(request)
        with LifecycleEventStore.for_project(root) as store:
            before = len(store.list_events(adapter="reference", session_id=session))
        second = self.adapter.start_session(request)
        with LifecycleEventStore.for_project(root) as store:
            after = len(store.list_events(adapter="reference", session_id=session))
        assert not first.replayed and second.replayed and before == after == 1
        return ConformanceObservation(
            "Duplicate delivery replayed one durable transition."
        )

    def _scenario_out_of_order_event_fails_closed(self, scenario):
        root, session = self._case(scenario)
        self.adapter.start_session(
            self._request(
                AdapterOperation.START_SESSION,
                root,
                "start",
                session_id=session,
            )
        )
        database = root / ".claim-plane/lifecycle/events.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE lifecycle_events SET sequence = 9 "
                "WHERE adapter=? AND session_id=?",
                ("reference", session),
            )
        try:
            self.adapter.resume(
                self._request(
                    AdapterOperation.RESUME,
                    root,
                    "resume",
                    session_id=session,
                )
            )
        except AdapterProtocolError as exc:
            assert exc.code is AdapterErrorCode.CORRUPT_STATE
        else:
            raise AssertionError("out-of-order lifecycle stream was accepted")
        return ConformanceObservation("Invalid event order prevented resume.")

    def _scenario_adapter_crash_resumes_safely(self, scenario):
        root, session, admitted = self._active(scenario)
        replacement = ReferenceAdapter()
        resumed = replacement.resume(
            self._request(AdapterOperation.RESUME, root, "resume", session_id=session)
        )
        assert resumed.status is AdapterStatus.SUCCEEDED
        inspected = replacement.inspect(
            self._request(AdapterOperation.INSPECT, root, "inspect", session_id=session)
        )
        assert inspected.intent_id == admitted.intent_id
        assert inspected.intent_version == admitted.intent_version
        return ConformanceObservation(
            "A fresh adapter process recovered only durable authority."
        )

    def _scenario_cancellation_revokes_authority(self, scenario):
        root, session, admitted = self._active(scenario)
        cancelled = self.adapter.cancel(
            self._request(
                AdapterOperation.CANCEL,
                root,
                "cancel",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
            )
        )
        assert cancelled.status is AdapterStatus.CANCELLED
        inspected = self.adapter.inspect(
            self._request(
                AdapterOperation.INSPECT, root, "inspect-cancelled", session_id=session
            )
        )
        assert inspected.intent_id is None
        assert inspected.intent_version is None
        assert inspected.payload["state"] == "cancelled"
        return ConformanceObservation("Cancellation revoked the active authority.")

    def _scenario_completion_detects_uncovered_mutation(self, scenario):
        root, session, admitted = self._active(scenario)
        result = self.adapter.verify_completion(
            self._request(
                AdapterOperation.VERIFY_COMPLETION,
                root,
                "verify",
                session_id=session,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
                payload={"final_resources": ["README.md", "outside.py"]},
            )
        )
        assert result.status is AdapterStatus.DENIED
        assert result.payload["uncovered_resources"] == ["outside.py"]
        return ConformanceObservation(
            "Final verification detected uncovered authority."
        )

    def _scenario_corrupt_state_cannot_produce_verified(self, scenario):
        root, session, admitted = self._active(scenario)
        self.adapter.corrupt_state(root)
        try:
            self.adapter.verify_completion(
                self._request(
                    AdapterOperation.VERIFY_COMPLETION,
                    root,
                    "verify-corrupt",
                    session_id=session,
                    intent_id=admitted.intent_id,
                    intent_version=admitted.intent_version,
                )
            )
        except AdapterProtocolError as exc:
            assert exc.code is AdapterErrorCode.CORRUPT_STATE
        else:
            raise AssertionError("corrupt state produced a completion result")
        return ConformanceObservation(
            "Corrupt durable state could not produce VERIFIED."
        )

    def _scenario_secret_values_absent_from_evidence(self, scenario):
        root, session = self._case(scenario)
        secret = "reference-secret-value-never-export"
        self.adapter.start_session(
            self._request(
                AdapterOperation.START_SESSION,
                root,
                "secret-start",
                session_id=session,
                payload={"prompt": secret, "api_token": secret},
            )
        )
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (root / ".claim-plane").rglob("*")
            if path.is_file() and path.suffix != ".sqlite3"
        )
        with LifecycleEventStore.for_project(root) as store:
            events = store.list_events(adapter="reference", session_id=session)
        serialized = json.dumps([item.to_dict() for item in events], sort_keys=True)
        assert secret not in text and secret not in serialized
        return ConformanceObservation(
            "Raw secret values were absent from durable evidence."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the reference adapter conformance suite."
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_adapter_conformance(ReferenceConformanceDriver())
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for result in report.results:
            print(
                f"[{result.status.value.upper():7}] "
                f"{result.scenario.value}: {result.detail}"
            )
        print(
            "adapter conformance: passed"
            if report.compatible
            else "adapter conformance: failed"
        )
    return 0 if report.compatible else 2


if __name__ == "__main__":
    raise SystemExit(main())
