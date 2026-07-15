"""Public facade for semantic admission and continuous integration verification."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping

from claim_plane.arbiter.base import Arbiter, ExactMatchArbiter
from claim_plane.coordination import AdmissionEngine, build_context_pack
from claim_plane.core.governance import GovernancePolicy
from claim_plane.core.models import (
    AdmissionDecision,
    ChangeIntent,
    ChangeManifest,
    AccessMode,
    ObservedAccess,
    ResourceRef,
    Claim,
    FindingCode,
    IntegrationReport,
    RepairPlan,
    ResourceKind,
    RouteRecommendation,
    Verdict,
    VerdictKind,
)
from claim_plane.core.store import PlaneStore, SQLitePlaneStore, validate_plane_store
from claim_plane.core.semantic import SemanticIdentityResolver
from claim_plane.integration import (
    AcceptanceRunner,
    GitChangeCollector,
    IntegrationRunResult,
    IntegrationRunner,
    IntegrationRunSpec,
    IntegrationVerifier,
    build_repair_plan,
)
from claim_plane.integration.snapshot import (
    capture_worktree_tree,
    changed_worktree_paths,
)
from claim_plane.routing import recommend_worker_tier


class Plane:
    def __init__(
        self,
        registry: PlaneStore,
        arbiter: Arbiter,
        *,
        semantic_resolver: SemanticIdentityResolver,
        governance: GovernancePolicy,
    ) -> None:
        self._registry = validate_plane_store(registry)
        self._arbiter = arbiter
        self._semantic = semantic_resolver
        self._governance = governance
        self._admission = AdmissionEngine()
        self._verifier = IntegrationVerifier()
        self._collector = GitChangeCollector(semantic_resolver)
        self._acceptance = AcceptanceRunner()

    @classmethod
    def from_store(
        cls,
        registry: PlaneStore,
        *,
        semantic: bool = False,
        lexicon_path: str | None = None,
        governance: GovernancePolicy | str | None = None,
    ) -> "Plane":
        """Create a Plane from any backend implementing the complete store contract."""

        registry = validate_plane_store(registry)
        if governance is None:
            governance_policy = GovernancePolicy.governed()
        elif isinstance(governance, str):
            governance_policy = (
                GovernancePolicy.exploratory()
                if governance == "exploratory"
                else GovernancePolicy.governed()
            )
        else:
            governance_policy = governance
        semantic_resolver = SemanticIdentityResolver(
            lexicon_path if semantic or lexicon_path else None,
            required=semantic,
        )
        if semantic or lexicon_path:
            from claim_plane.arbiter.lexicon import LexiconArbiter

            arbiter: Arbiter = LexiconArbiter(registry, lexicon_path=lexicon_path)
        else:
            arbiter = ExactMatchArbiter(registry)
        return cls(
            registry,
            arbiter,
            semantic_resolver=semantic_resolver,
            governance=governance_policy,
        )

    @classmethod
    def open(
        cls,
        db_path: str | Path = ":memory:",
        *,
        semantic: bool = False,
        lexicon_path: str | None = None,
        governance: GovernancePolicy | str | None = None,
    ) -> "Plane":
        registry = SQLitePlaneStore(db_path)
        try:
            return cls.from_store(
                registry,
                semantic=semantic,
                lexicon_path=lexicon_path,
                governance=governance,
            )
        except Exception:
            registry.close()
            raise

    # --------------------------------------------------------------- claims

    def claim(self, claim: Claim) -> Verdict:
        return self._arbiter.arbitrate(claim)

    def claim_many(self, claims: Iterable[Claim]) -> list[Verdict]:
        return [self.claim(claim) for claim in claims]

    def release(self, owner: str) -> int:
        return self._registry.release(owner)

    def renew(self, owner: str, lease_seconds: int = 900) -> int:
        return self._registry.renew_claims(owner, lease_seconds)

    def verify_merge(self, defined: Iterable[Claim]) -> list[Verdict]:
        problems: list[Verdict] = []
        for claim in defined:
            row = self._registry.incumbent(claim.claim_type, claim.key)
            if row is not None and row["owner"] != claim.owner:
                problems.append(
                    Verdict(
                        VerdictKind.CONFLICT,
                        claim,
                        incumbent=row["owner"],
                        incumbent_signature=row["signature"],
                        guidance=f"Merge blocked: '{claim.identifier}' collides with {row['owner']}'s active grant.",
                    )
                )
        return problems

    # ---------------------------------------------------------- change intents

    def _govern_intent(self, intent: ChangeIntent) -> ChangeIntent:
        import re

        if intent.base_commit is None and re.fullmatch(
            r"[0-9a-fA-F]{40,64}", intent.base_revision
        ):
            intent = replace(intent, base_commit=intent.base_revision.lower())
        if self._governance.require_base_commit and intent.base_commit is None:
            raise ValueError(
                "governed admission requires base_commit; resolve base_revision with pin-intent or open Plane in exploratory mode"
            )
        return intent

    def admit(self, intent: ChangeIntent) -> AdmissionDecision:
        enriched = self._semantic.enrich_intent(self._govern_intent(intent))
        return self._registry.admit_intent(enriched, self._admission.evaluate)

    def amend(
        self, intent: ChangeIntent, *, expected_version: int | None = None
    ) -> AdmissionDecision:
        enriched = self._semantic.enrich_intent(self._govern_intent(intent))
        enriched = replace(enriched, metadata={**enriched.metadata, "_amendment": True})
        return self._registry.amend_intent(
            enriched,
            self._admission.evaluate,
            expected_version=expected_version,
        )

    def activate(self, intent_id: str) -> None:
        self._registry.activate_intent(intent_id)

    def heartbeat(self, intent_id: str, lease_seconds: int = 900) -> None:
        self._registry.heartbeat_intent(intent_id, lease_seconds)

    def complete(self, intent_id: str) -> None:
        self._registry.complete_intent(intent_id)

    def release_intent(self, intent_id: str) -> None:
        self._registry.release_intent(intent_id)

    def intent(self, intent_id: str) -> ChangeIntent | None:
        return self._registry.get_intent(intent_id)

    def intents(self, *, active_only: bool = False) -> list[dict]:
        return self._registry.list_intents(active_only=active_only)

    def context_pack(self, intent_id: str) -> dict:
        record = self._registry.get_intent_record(intent_id)
        if record is None:
            raise KeyError(f"unknown intent: {intent_id}")
        intent = ChangeIntent.from_dict(record["payload_json"])
        related = {item.intent_id: item for item in self._registry.active_intents()}
        for dependency in record.get("dependencies", []):
            producer_id = dependency["depends_on_intent_id"]
            producer = self._registry.get_intent(producer_id)
            if producer is not None:
                related[producer_id] = producer
        return build_context_pack(
            intent,
            admission=record["admission_json"],
            active_intents=related.values(),
            dependency_records=record.get("dependencies", ()),
            notices=record.get("notices", ()),
        )

    def recommend_worker(self, intent_id: str) -> RouteRecommendation:
        record = self._registry.get_intent_record(intent_id)
        if record is None:
            raise KeyError(f"unknown intent: {intent_id}")
        intent = ChangeIntent.from_dict(record["payload_json"])
        overlap_count = len(record["admission_json"].get("conflicts", []))
        return recommend_worker_tier(intent, overlap_count=overlap_count)

    def notices(self, intent_id: str, *, pending_only: bool = True) -> list[dict]:
        return self._registry.notices(intent_id, pending_only=pending_only)

    def acknowledge_notice(self, notice_id: int) -> None:
        self._registry.acknowledge_notice(notice_id)

    def dependency_graph(self) -> dict[str, object]:
        return self._registry.dependency_graph()

    # ------------------------------------------------ trusted observation sessions

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
        return self._registry.start_observation_session(
            session_id,
            intent_id,
            monitor_id=monitor_id,
            key_id=key_id,
            coverage=coverage,
            required_tools=required_tools,
        )

    def record_observed_access(
        self,
        session_id: str,
        *,
        mode: AccessMode | str,
        kind: ResourceKind | str,
        identifier: str,
        key: bytes,
        tool: str | None = None,
        timestamp: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        access = ObservedAccess(
            mode=AccessMode(mode),
            resource=ResourceRef(ResourceKind(kind), identifier),
            tool=tool,
            timestamp=timestamp,
            metadata=dict(metadata or {}),
        )
        return self._registry.record_observation_event(session_id, access, key=key)

    def seal_observation_session(
        self, session_id: str, *, key: bytes, complete: bool = True
    ) -> dict:
        return self._registry.seal_observation_session(
            session_id, key=key, complete=complete
        )

    def verify_observation_session(self, session_id: str, *, key: bytes) -> dict:
        return self._registry.verify_observation_session(session_id, key=key)

    def observation_session(self, session_id: str) -> dict:
        return self._registry.observation_session(session_id)

    # --------------------------------------------------------- broker runtime

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
        return self._registry.register_broker_instance(
            instance_id=instance_id,
            intent_id=intent_id,
            session_id=session_id,
            monitor_id=monitor_id,
            key_id=key_id,
            root_path=root_path,
            repo_identity=repo_identity,
            base_commit=base_commit,
            initial_tree_hash=initial_tree_hash,
            writer_lease_seconds=writer_lease_seconds,
            policy=policy,
            binary_digest=binary_digest,
            broker_key=broker_key,
            required_tools=required_tools,
        )

    def broker_instance(self, instance_id: str) -> dict:
        return self._registry.broker_instance(instance_id)

    def validate_broker_instance(
        self,
        instance_id: str,
        *,
        broker_key: bytes,
        current_tree_hash: str | None = None,
    ) -> dict:
        return self._registry.validate_broker_instance(
            instance_id,
            broker_key=broker_key,
            current_tree_hash=current_tree_hash,
        )

    def stop_broker_instance(self, instance_id: str, *, broker_key: bytes) -> dict:
        return self._registry.stop_broker_instance(instance_id, broker_key=broker_key)

    def verify_broker_instance(self, instance_id: str, *, broker_key: bytes) -> dict:
        return self._registry.verify_broker_instance(instance_id, broker_key=broker_key)

    def verify_broker_session(self, session_id: str, *, broker_key: bytes) -> dict:
        return self._registry.verify_broker_session(session_id, broker_key=broker_key)

    def broker_operation_for_request(
        self, instance_id: str, request_id: str
    ) -> dict | None:
        return self._registry.broker_operation_for_request(instance_id, request_id)

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
        return self._registry.prepare_broker_operation(
            operation_id=operation_id,
            instance_id=instance_id,
            request_id=request_id,
            operation=operation,
            mode=mode,
            path=path,
            target_path=target_path,
            payload=payload,
            broker_key=broker_key,
            fencing_token=fencing_token,
            pre_tree_hash=pre_tree_hash,
        )

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
        return self._registry.commit_broker_operation(
            operation_id,
            accesses=accesses,
            response=response,
            observation_key=observation_key,
            broker_key=broker_key,
            fencing_token=fencing_token,
            post_tree_hash=post_tree_hash,
        )

    def fail_broker_operation(
        self, operation_id: str, *, state: str, error: str, broker_key: bytes
    ) -> dict:
        return self._registry.fail_broker_operation(
            operation_id, state=state, error=error, broker_key=broker_key
        )

    def pending_broker_operations(self, instance_id: str) -> list[dict]:
        return self._registry.pending_broker_operations(instance_id)

    # ------------------------------------------------------- integration verify

    def verify_manifest(self, manifest: ChangeManifest) -> IntegrationReport:
        record = self._registry.get_intent_record(manifest.intent_id)
        if record is None:
            raise KeyError(f"unknown intent: {manifest.intent_id}")
        if record["state"] not in {"admitted", "active", "completed"}:
            raise ValueError(
                f"intent {manifest.intent_id} is {record['state']}, not admitted for execution"
            )
        intent = ChangeIntent.from_dict(record["payload_json"])
        report = self._verifier.verify(
            intent,
            manifest,
            active_intents=self._registry.active_intents(),
            dependency_records=record.get("dependencies", ()),
        )
        self._registry.record_verification(intent.intent_id, report.to_dict())
        if any(
            finding.code
            in {FindingCode.CONTRACT_MISMATCH, FindingCode.CONTRACT_MISSING}
            for finding in report.findings
        ):
            keys = sorted(
                {
                    operation.resource.subject_key or operation.resource.semantic_key
                    for operation in intent.operations
                    if operation.resource.kind is ResourceKind.CONTRACT
                }
            )
            self._registry.invalidate_dependents(
                intent.intent_id,
                [key for key in keys if key],
                reason="verification_contract_failure",
            )
        return report

    def collect_git_manifest(
        self, intent_id: str, repo_path: str | Path = "."
    ) -> ChangeManifest:
        record = self._registry.get_intent_record(intent_id)
        if record is None:
            raise KeyError(f"unknown intent: {intent_id}")
        if record["state"] not in {"admitted", "active", "completed"}:
            raise ValueError(
                f"intent {intent_id} is {record['state']}, not admitted for execution"
            )
        return self._collector.collect(
            repo_path, ChangeIntent.from_dict(record["payload_json"])
        )

    def verify_git(
        self,
        intent_id: str,
        repo_path: str | Path = ".",
        *,
        run_acceptance: bool = False,
        acceptance_timeout: int = 300,
    ) -> IntegrationReport:
        manifest = self.collect_git_manifest(intent_id, repo_path)
        if run_acceptance:
            intent = self._registry.get_intent(intent_id)
            assert intent is not None
            tree_before = capture_worktree_tree(repo_path)
            runner = AcceptanceRunner(timeout_seconds=acceptance_timeout)
            results = runner.run(intent.acceptance, repo_path)
            tree_after = capture_worktree_tree(repo_path)
            immutable = tree_before == tree_after
            manifest = replace(
                manifest,
                acceptance_results=results,
                metadata={
                    **manifest.metadata,
                    "acceptance_executed": True,
                    "snapshot_integrity_ok": immutable,
                    "snapshot_tree_before_acceptance": tree_before,
                    "snapshot_tree_after_acceptance": tree_after,
                    "acceptance_mutation_paths": (
                        [] if immutable else list(changed_worktree_paths(repo_path))
                    ),
                },
            )
        return self.verify_manifest(manifest)

    def repair_plan(self, report: IntegrationReport) -> RepairPlan:
        return build_repair_plan(report)

    def verify_batch(
        self, manifests: Iterable[ChangeManifest]
    ) -> dict[str, IntegrationReport]:
        manifests = tuple(manifests)
        intents = []
        dependencies: dict[str, list[dict]] = {}
        for manifest in manifests:
            record = self._registry.get_intent_record(manifest.intent_id)
            if record is None:
                raise KeyError(f"unknown intent: {manifest.intent_id}")
            intents.append(ChangeIntent.from_dict(record["payload_json"]))
            dependencies[manifest.intent_id] = record.get("dependencies", [])
        reports = self._verifier.verify_batch(
            intents, manifests, dependency_records=dependencies
        )
        for intent_id, report in reports.items():
            self._registry.record_verification(intent_id, report.to_dict())
        return reports

    def run_integration(self, spec: IntegrationRunSpec) -> IntegrationRunResult:
        return IntegrationRunner(self).run(spec)

    # ------------------------------------------------------------- introspection

    @property
    def semantic_enabled(self) -> bool:
        return self._semantic.enabled

    def grants(self) -> list[dict]:
        return self._registry.all_grants()

    def audit(self) -> list[dict]:
        return self._registry.decision_log()

    def events(self) -> list[dict]:
        return self._registry.coordination_events()

    def export_audit(self, path: str | Path) -> None:
        self._registry.export_audit(path)

    def close(self) -> None:
        self._registry.close()
