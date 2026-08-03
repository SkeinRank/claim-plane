# Protocol

## Agent Adapter Protocol

`claim-plane.agent-adapter.v1` is the runtime-neutral boundary between Claim Plane and a coding-agent runtime. The protocol keeps runtime-specific hook or API payloads outside Claim Plane Core while carrying the identities required for deterministic authority decisions.

Every request contains:

- a stable `request_id` used for idempotency;
- an `operation` from the public adapter lifecycle;
- the adapter name and enrolled project root;
- optional session, run, intent, and intent-version identities;
- an explicit positive timeout;
- a runtime-owned payload that cannot grant authority by itself.

The lifecycle covers project enrollment and diagnostics, session start and stop, task submission, intent proposal, mutation requests, scope amendments, result observation, completion verification, cancellation, inspection, and crash resume. Responses return a portable status, the current intent binding when one exists, a replay marker, adapter result data, and structured failure information.

Adapters must treat `request_id` as an idempotency key. Reusing the same identifier with identical input returns the stored result without repeating the state transition. Reusing it with different input fails closed with `idempotency_conflict`. Adapter caches persist only a request fingerprint and the adapter response; raw request payloads, including prompts and tool arguments, are not stored by the generic boundary.

Authority-bearing calls may include the expected intent ID and content version. A mismatch is rejected before the runtime-specific mutation or amendment path is entered. Cancellation must release active authority. Resume may renew or re-admit previously known authority only through the existing deterministic lifecycle; an unknown resumed session starts without mutation authority.

The public Python surface is `claim_plane.protocol.AgentAdapter`, `AdapterRequest`, and `AdapterResponse`. `CodexAdapter` is the first implementation. Additional runtimes implement the same interface without adding runtime-specific types to the protocol module. The request and response documents are described by [`schemas/agent-adapter-request.schema.json`](../schemas/agent-adapter-request.schema.json) and [`schemas/agent-adapter-response.schema.json`](../schemas/agent-adapter-response.schema.json).

Claim Plane Core remains authoritative. An adapter translates lifecycle and observations; it never grants mutation authority directly.

## Normalized lifecycle events

`claim-plane.lifecycle-event.v1` is the runtime-neutral, append-only chronology for an agent session. Native runtime callbacks are reduced to a stable vocabulary:

```text
SessionStarted
TaskSubmitted
IntentProposed
AdmissionRequested
AdmissionGranted / AdmissionDenied
MutationRequested
MutationAllowed / MutationDenied
MutationObserved
ScopeExpansionRequested
ScopeExpansionGranted / ScopeExpansionDenied
VerificationStarted
VerificationCompleted
AgentStopped
SessionEnded
```

Each event carries a stable event identity, adapter and session identity, optional run and intent binding, a monotonically increasing session-local sequence, a causal link to the previous durable event, a UTC timestamp, a normalized payload, and a canonical SHA-256 digest. The JSON envelope is described by [`schemas/lifecycle-event.schema.json`](../schemas/lifecycle-event.schema.json).

The event store appends one request batch atomically. A repeated adapter request resolves to the already stored events without advancing the sequence. Reusing the same event identity with different normalized data fails closed. Gaps, mixed sessions, broken causal links, invalid transitions, and digest mismatches make the stream invalid; an invalid stream can never project to a verified report or be exported as evidence.

Lifecycle payloads are summaries rather than runtime transcripts. Prompts, credentials, authorization values, raw commands, tool input, and hook output are excluded. Safe fields include request digests, operation counts, decision status, affected paths when known, intent identity and version, and verification outcome.

`LifecycleEventStore`, `build_lifecycle_report`, and `render_lifecycle_replay` are shared by all adapters. Reports and recovery reconstruct state from the same validated stream, while replay renders chronology without repeating provider calls. Durable streams may be exported as canonical NDJSON for later evidence sealing.

## ChangeIntent

A `ChangeIntent` declares planned work before implementation.

Required identity fields:

- `intent_id` — stable identifier across amendments;
- `task_id` — originating task;
- `owner` — worker or worker group;
- `base_revision` — human-readable repository revision used for planning;
- `base_commit` — immutable Git commit used for execution and integration; mutable refs require this pin;
- `operations` — read/write/extend/delete/rename/document/test surfaces, each with an optional `commitment` of `committed` or `contingent` (default: `committed`).

Optional coordination fields:

- `dependencies` — intent IDs that must exist;
- `preserves` — worker guidance and structured deterministic policies;
- `acceptance` — commands that may be explicitly executed by verification;
- `metadata` — planner and adapter metadata;
- `lease_seconds` — liveness window.

## ResourceRef

A resource contains:

- `kind` — file, region, symbol, concept, contract, route, schema, config, or document;
- `identifier` — path, name, route, or resource identifier;
- `signature` — contract signature when applicable;
- `subject_concept_id` — concept governed by a contract;
- optional line bounds and metadata.

Contracts that participate in semantic overlap must set `subject_concept_id`. A common but unrelated contract does not authorize concurrent writes to a concept.

## Adaptive operation commitment

`committed` operations participate in current admission and mutation enforcement.
`contingent` mutating operations represent possible future scope and do not reserve write
ownership during initial admission. For coordination safety they are projected as read
premises until promoted, so concurrent writers can still be tracked.

Promotion is monotonic and atomic:

1. select a predeclared contingent path operation;
2. narrow a contingent glob to the concrete requested path when necessary;
3. re-run admission against the current active set;
4. commit the new intent content version only when admission succeeds.

A rejected promotion does not alter the previously admitted intent. A successful promotion
preserves an active intent's lifecycle state. Trusted broker-initiated promotion also
re-attests that broker to the new intent content version in the same transaction.

## Admission

Admission is evaluated atomically against active intents and known dependencies. The result contains:

- `allowed`;
- decision `kind`;
- structured conflicts;
- constraints;
- notifications or required repairs;
- canonicalized intent data.

Allowed decisions enter the admitted lifecycle. Rejected decisions are retained for audit but cannot execute. Re-submitting the same blocked intent re-evaluates it atomically against the current active set, so a worker can wait for a blocker to finish and retry without inventing a new `intent_id`.

## Intent states

```text
blocked ── retry/re-evaluate ──→ admitted
admitted → active → completed
    │         │
    ├─────────┴→ released
    │
    └→ stale → amended/re-admitted
```

Expired leases are excluded from active arbitration. A stale intent represents work whose premise changed after admission. `completed` is the successful terminal state and no longer participates in active arbitration. `released` represents abandoned or explicitly relinquished work. Calling release after completion is a safe no-op so generic cleanup code does not erase successful lifecycle history.

## Amendments

An amendment uses the same `intent_id` and an optional `expected_version`. The registry evaluates the amended intent against the current active set in one transaction. A successful amendment creates a new version. A failed amendment leaves the previous version unchanged.

Producer contract changes may invalidate dependents. Claim Plane rejects cycles before commit, keeps the first invalidation hop resource-scoped, and propagates stale state transitively after an affected consumer becomes an untrusted producer.

## Coordination notices

A notice identifies:

- target intent;
- source intent;
- notice type;
- reason and affected resource keys;
- creation time;
- acknowledgement state;
- root and direct producer;
- invalidation depth and dependency chain.

Notices are exposed through CLI, MCP, and context packs. Acknowledgement records delivery handling but does not automatically make stale work valid again.

## ChangeManifest

A manifest records observed implementation:

- intent identity and owner;
- observed base revision and exact resolved base commit;
- changed files;
- `changed_regions` extracted from Git hunks;
- observed artifacts and typed signatures;
- acceptance results and sandbox backend evidence;
- optional observed read/write accesses from tool or MCP adapters;
- collector metadata.

A manifest is evidence, not a self-asserted success flag.

## Verification

Verification produces an `IntegrationReport` with deterministic findings and severity. A report is clean only when no error-level finding remains.

Key checks:

- undeclared paths;
- missing required writes;
- changed hunks outside admitted regions;
- stale or missing dependencies;
- missing or incompatible concept-bound contracts;
- structured preserve policy violations, including missing/deleted preserved contracts;
- missing or failed acceptance evidence;
- deprecated or alias semantic surfaces;
- overlapping candidate hunks;
- incompatible contracts across candidate manifests;
- observed writes outside declared surfaces;
- dynamic read-after-write dependencies across concurrent workers.

## IntegrationRunSpec and evidence

An integration run declares:

- a stable run ID, base repository, human-readable base revision, and exact base commit;
- worker intent IDs and worktree paths;
- optional external repair commands;
- worker and integrated acceptance behavior;
- strict snapshot-mutation policies;
- bounded attempts and timeouts;
- evidence directory, cleanup policy, and optional `refs/claim-plane/*` result ref;
- optional worker/integration sandbox policies;
- optional observation trace paths and HMAC evidence signing key reference.

Claim Plane uses immutable and optionally attested evidence:

- `worker.patch` is generated once from `base_commit -> snapshot_commit`;
- `manifest.json` is collected from that snapshot commit;
- both artifacts have SHA-256 digests;
- worker acceptance must leave the snapshot tree unchanged by default;
- neutral integration applies those exact patch bytes in topological order;
- integration commands must leave the composed tree unchanged by default;
- a successful run emits `result_tree`, `result_commit`, `result.patch`, and `evidence.json`;
- separate file and canonical-payload digests bind the spec, base tree, worker artifacts, reports, and result;
- optional HMAC-SHA256 attestation binds the canonical payload to a caller-managed secret key.

A result commit is an immutable Git object. Publishing a branch or pull request is deliberately outside the protocol; callers may configure a namespaced `result_ref` or use the emitted result patch.

## Observed access protocol

Observation traces are JSON or JSONL sequences of `ObservedAccess` records. Each record contains a mode, resource, optional tool name, timestamp, and adapter metadata. Claim Plane only reasons over accesses supplied by an adapter; bypassed tools are outside the guarantee.

Observed writes are fail-closed against admitted write surfaces. Undeclared reads are warnings in single-worker verification. During batch verification, an observed read of a path changed by another worker becomes an error unless the reader declares the producer as a dependency.

## Sandbox and evidence signatures

The default `tree` sandbox is a repository-tree mutation detector, not an operating-system security boundary. `bwrap`, `bwrap-minimal`, `sandbox-exec`, and `auto` request OS-level process isolation. Strict mode fails closed when a backend is unavailable.

Evidence stores both the SHA-256 of the exact pretty-printed file and the SHA-256 of its canonical JSON payload. HMAC-SHA256 signatures use a shared CI secret. Ed25519 signatures use a PEM private key and can be verified independently with the corresponding public key. Both bind the canonical evidence payload.

## Deterministic swarm merge queue

`claim-plane.swarm-merge-queue.v1` binds integration state to one swarm session,
repository identity, exact base commit, work-graph version, budget version, and
shared-admission fingerprint. Eligible worker snapshots are ordered by the
effective dependency graph and applied only to a Claim Plane-owned integration
branch and worktree. The configured user target branch is never mutated by this
protocol.

Each entry moves through `pending`, `blocked`, `ready`, `integrating`,
`integrated`, or `conflict`. Reserving an entry and publishing its durable result
are serialized in the swarm SQLite store. A Git conflict aborts the cherry-pick,
restores the previous integration head, and records the conflicting paths. Once a
queue exists, downstream scheduling requires prerequisites to be `integrated`,
not merely process-successful. Integration remains distinct from semantic
verification and final acceptance.

## Compatibility

The `0.x` protocol is experimental. Newly introduced strict fields default to secure behavior, while `result_ref` remains optional. Unbound `contract:<identifier>=<signature>` preserve policies remain accepted for compatibility, while `contract:<subject>::<identifier>=<signature>` is preferred. Integration runs are additive and optional.


## Trusted observation session protocol

A trusted session has states `open -> sealed` and is permanently bound to one intent. Each event contains:

- session ID and sequence number;
- occurred-at timestamp;
- normalized `ObservedAccess`;
- previous event hash;
- event SHA-256;
- HMAC-SHA256 over the event hash.

The seal attests monitor ID, key ID, coverage (`brokered_proxy`, `tool_proxy`, or `os_monitor` for trusted integration), required tools, event count, head hash, completeness, and timestamps. A trusted integration run verifies the complete chain and seal with a server-held key and embeds the session digest in worker evidence.

`declared` coverage can be stored for audit but is rejected by the default trusted policy because it does not assert an enforced monitor boundary.

### Brokered observation

A `brokered_proxy` session is bound to a registered broker instance. Generic observation APIs cannot append brokered events. Each committed event contains `broker_protocol=claim-plane.broker.v2`, broker instance ID, operation ID, policy digest, intent content version, fencing token, and request ID.

Every broker request is represented by a durable operation record. The prepare receipt is written before the filesystem effect; the commit receipt binds the response and exact observation-event range. Integration verifies the broker instance HMAC, prepare/commit HMACs, operation states, event ranges, repository root, policy digest, and exact base commit. Pending operations invalidate the broker session.

The broker enforces exact capabilities rather than a generic mutation flag: `write_file`, `append_file`, `delete_file`, and `rename_file` require distinct intent access modes. `search_text` records each file actually scanned so dynamic dependency analysis retains file-level premises.

## Governed and exploratory modes

Governed mode is the default. Admission requires an exact base commit before a worker starts. Exploratory mode is an explicit compatibility mode for local prototyping; integration still requires an exact pinned commit.

## Swarm verification protocol

`claim-plane.swarm-verification.v1` records final evidence for a completed swarm integration. It binds the report to the repository identity, pinned base, graph and budget versions and fingerprints, shared-admission fingerprint, deterministic merge-queue fingerprint, and integration head. The report contains per-work-item attribution, acceptance outcomes, root integration findings, snapshot-integrity evidence, and the final `verified` or `failed` status. The normative JSON Schema is `schemas/swarm-verification.schema.json`.


## Swarm recovery

`claim-plane.swarm-recovery.v1` records operator-visible recovery, pause, resume, cancellation, and replacement events. Active Codex runs carry a heartbeat and lease expiry. A missing process or expired pre-spawn reservation may be finalized as `lost`; a still-live process with an expired lease remains `stale` until an operator explicitly requests termination. Replacement runs have fresh run and Codex thread identity, reference the predecessor through `replacement_of_run_id`, and must pass current admission, scheduler, retry, launch, budget, dependency, and managed-worktree checks. The normative JSON Schema is `schemas/swarm-recovery.schema.json`.


## Swarm operator snapshot and event protocols

`claim-plane.swarm-operator-snapshot.v1` is a read-only projection over the durable swarm session, budget, scheduler, worker-run, managed-worktree, merge-queue, recovery, and verification records. It does not grant authority or persist an alternative lifecycle. Each work row reports scheduler state, latest run and attempt, token use, merge state, verification state, worktree health, and the next operator action. The normative JSON Schema is `schemas/swarm-operator-snapshot.schema.json`.

`claim-plane.swarm-operator-event.v1` normalizes durable lifecycle records for `swarm logs`. Events may originate from Codex JSONL, worker terminal records, recovery actions, merge entries, or verification. The event preserves its source payload under metadata where useful, but ordering and display do not replace the authoritative source record. The normative JSON Schema is `schemas/swarm-operator-event.schema.json`.

The `swarm start` command is orchestration rather than a new authority protocol. Every dispatch still passes current shared admission, scheduler capacity, retry and resource budgets, managed-worktree ownership, deterministic integration, and final verification.
