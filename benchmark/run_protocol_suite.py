from __future__ import annotations

import json
import statistics
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from claim_plane import (
    AccessMode,
    ChangeIntent,
    ChangeManifest,
    IntentOperation,
    ObservedArtifact,
    Plane,
    ResourceKind,
    ResourceRef,
)


def operation(
    access: str,
    kind: str,
    identifier: str,
    signature: str | None = None,
    subject_concept_id: str | None = None,
):
    return IntentOperation(
        AccessMode(access),
        ResourceRef(
            ResourceKind(kind),
            identifier,
            signature=signature,
            subject_concept_id=subject_concept_id,
        ),
    )


def make_intent(intent_id: str, owner: str, operations):
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision="main",
        base_commit="a" * 40,
        operations=tuple(operations),
        preserves=("public API",),
        acceptance=("pytest",),
    )


def main() -> int:
    with TemporaryDirectory() as directory:
        plane = Plane.open(Path(directory) / "plane.db")
        latencies = []
        results = {}

        def admit(label, change_intent):
            started = time.perf_counter_ns()
            decision = plane.admit(change_intent)
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            results[label] = {"allowed": decision.allowed, "kind": decision.kind.value}
            return decision

        admit(
            "independent_a",
            make_intent("a", "agent-a", [operation("write", "file", "src/a.py")]),
        )
        admit(
            "independent_b",
            make_intent("b", "agent-b", [operation("write", "file", "src/b.py")]),
        )
        admit(
            "same_file",
            make_intent("c", "agent-c", [operation("write", "file", "src/a.py")]),
        )

        signature = "allow(request)->Decision"
        admit(
            "contract_owner",
            make_intent(
                "core",
                "agent-core",
                [
                    operation("extend", "concept", "RateLimiter"),
                    operation("write", "contract", "allow", signature, "RateLimiter"),
                    operation("write", "file", "src/rate/core.py"),
                ],
            ),
        )
        admit(
            "contract_consumer",
            make_intent(
                "metrics",
                "agent-metrics",
                [
                    operation("extend", "concept", "RateLimiter"),
                    operation("read", "contract", "allow", signature, "RateLimiter"),
                    operation("write", "file", "src/rate/metrics.py"),
                ],
            ),
        )
        admit(
            "contract_mismatch",
            make_intent(
                "bad",
                "agent-bad",
                [
                    operation("extend", "concept", "RateLimiter"),
                    operation(
                        "write",
                        "contract",
                        "allow",
                        "allow(request, policy)->bool",
                        "RateLimiter",
                    ),
                    operation("write", "file", "src/rate/bad.py"),
                ],
            ),
        )

        report = plane.verify_manifest(
            ChangeManifest(
                intent_id="core",
                owner="agent-core",
                base_revision="main",
                base_commit="a" * 40,
                changed_files=("src/rate/core.py", "src/shared.py"),
                artifacts=(
                    ObservedArtifact(
                        ResourceKind.CONTRACT,
                        "allow",
                        "src/rate/core.py",
                        signature="allow(request, policy)->bool",
                        subject_concept_id="RateLimiter",
                    ),
                ),
            )
        )
        results["drift_report"] = {
            "clean": report.clean,
            "finding_codes": sorted(
                {finding.code.value for finding in report.findings}
            ),
        }

        dag_a = make_intent(
            "dag-a", "agent-dag-a", [operation("write", "file", "src/dag_a.py")]
        )
        dag_b = replace(
            make_intent(
                "dag-b",
                "agent-dag-b",
                [operation("write", "file", "src/dag_b.py")],
            ),
            dependencies=("dag-a",),
        )
        assert plane.admit(dag_a).allowed
        assert plane.admit(dag_b).allowed
        cycle = plane.amend(replace(dag_a, dependencies=("dag-b",)), expected_version=1)
        results["dependency_cycle"] = {
            "allowed": cycle.allowed,
            "kind": cycle.kind.value,
            "graph_acyclic": plane.dependency_graph()["acyclic"],
        }

        preserve_intent = replace(
            make_intent(
                "preserve",
                "agent-preserve",
                [operation("write", "file", "src/preserve.py")],
            ),
            preserves=("contract:run=run(task)",),
        )
        assert plane.admit(preserve_intent).allowed
        preserve_report = plane.verify_manifest(
            ChangeManifest(
                intent_id="preserve",
                owner="agent-preserve",
                base_revision="main",
                base_commit="a" * 40,
                changed_files=("src/preserve.py",),
            )
        )
        results["preserve_missing"] = {
            "clean": preserve_report.clean,
            "finding_codes": sorted(
                {finding.code.value for finding in preserve_report.findings}
            ),
        }
        plane.close()

        expected = {
            "independent_a": True,
            "independent_b": True,
            "same_file": False,
            "contract_owner": True,
            "contract_consumer": True,
            "contract_mismatch": False,
        }
        ok = all(
            results[name]["allowed"] is allowed for name, allowed in expected.items()
        )
        ok = ok and not results["drift_report"]["clean"]
        ok = ok and results["dependency_cycle"] == {
            "allowed": False,
            "kind": "reject",
            "graph_acyclic": True,
        }
        ok = ok and not results["preserve_missing"]["clean"]
        ok = ok and "preserve_violation" in results["preserve_missing"]["finding_codes"]
        payload = {
            "passed": ok,
            "results": results,
            "latency_ms": {
                "count": len(latencies),
                "median": statistics.median(latencies),
                "max": max(latencies),
            },
        }
        print(json.dumps(payload, indent=2))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
