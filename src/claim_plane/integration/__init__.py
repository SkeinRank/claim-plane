"""Integration collection, execution, verification, and targeted repair."""

from claim_plane.integration.acceptance import AcceptanceRunner
from claim_plane.integration.collector import GitChangeCollector
from claim_plane.integration.observation import (
    ObservationPolicy,
    append_observation,
    load_observation_trace,
)
from claim_plane.integration.repair import build_repair_plan
from claim_plane.integration.sandbox import SandboxPolicy
from claim_plane.integration.signing import (
    sign_payload_ed25519,
    verify_evidence_file,
)
from claim_plane.integration.runner import (
    CommandExecution,
    IntegrationAttempt,
    IntegrationRunResult,
    IntegrationRunner,
    IntegrationRunSpec,
    MergeSimulation,
    WorkerEvidence,
    WorkerTarget,
)
from claim_plane.integration.verifier import IntegrationVerifier

__all__ = [
    "AcceptanceRunner",
    "ObservationPolicy",
    "SandboxPolicy",
    "append_observation",
    "load_observation_trace",
    "verify_evidence_file",
    "sign_payload_ed25519",
    "CommandExecution",
    "GitChangeCollector",
    "IntegrationAttempt",
    "IntegrationRunResult",
    "IntegrationRunner",
    "IntegrationRunSpec",
    "IntegrationVerifier",
    "MergeSimulation",
    "WorkerEvidence",
    "WorkerTarget",
    "build_repair_plan",
]
