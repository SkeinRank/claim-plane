# Architecture

Claim Plane separates semantic interpretation, deterministic coordination, execution, and verification. Model behavior may change, while admission, dependency, and integration decisions must remain inspectable and repeatable.

## Components

### Plane facade

`claim_plane.core.plane.Plane` is the public facade. It owns the registry, semantic resolver, admission engine, Git collector, verifier, routing policy, and neutral integration runner.

### Semantic identity

Agent Lexicon is optional. When semantic mode is requested, Claim Plane loads the published lexicon before opening the plane and fails closed if it is unavailable. Semantic resolution canonicalizes concept operations and scans changed text for deprecated or alias surfaces.

### Registry

SQLite is the reference local registry. Atomic admission uses a write transaction so two processes cannot both admit incompatible work from the same state. The registry persists:

- legacy fine-grained grants;
- intent versions and states;
- admission decisions;
- typed dependency edges;
- structured coordination notices;
- verification reports;
- append-only events and audit records.

Coding-agent adapters emit one normalized lifecycle stream per session. The store assigns monotonic sequence numbers, links every event to the previous durable event, suppresses duplicate request events, validates state transitions, and exposes the same projection to report, replay, recovery, and evidence export. Runtime transcripts remain outside this stream; only redacted decision summaries and canonical digests are retained.

### Admission engine

The admission engine compares an incoming `ChangeIntent` against active intents. It reasons about exact resources, broad scopes, line regions, concepts, concept-bound contracts, base revisions, destructive operations, and explicit dependencies.

Important safety rules:

- unknown overlapping writes fail closed;
- broad scope overlap is not treated as independent;
- a shared contract is relevant only when it governs the overlapping concept;
- same-file parallel work requires either disjoint declared regions or a Same-file Admission v2 proof over graph-backed semantic mutation roots;
- missing explicit dependencies reject admission;
- proposed explicit and inferred premise edges must leave the graph acyclic.

Adaptive scope separates likely writes from possible writes. Only committed mutations
reserve ownership. Contingent mutations are coordinated as read premises until a worker
actually needs them, at which point Claim Plane promotes the exact path and re-runs the
same deterministic admission transaction. Pattern-based contingent scope is narrowed to
the requested concrete path instead of turning into a broad write lease.

For swarm planning, Same-file Admission v2 handles the conservative case where two committed file
writes name the same path but line regions are unavailable. If policy is `region_safe`, the planner
may use the session's pinned Python Dependency Graph v2 snapshot plus Conflict Taxonomy v2 to prove
that declared symbol mutations are independent or explicitly commutative. Ordered semantic
dependencies become scheduler serialization edges in the required direction. Missing semantic roots,
unresolved evidence, direct conflicts, and explicit serialize/deny policy remain fail-closed.

For graph-backed mutations on disjoint files, the planner also evaluates semantic relationships that
Git path overlap cannot reveal. A contract, type, or public-API producer may therefore create an
ordered edge to a consumer in another file. Independent cross-file roots remain parallel; conflicting
or unresolved semantic evidence remains conservative. This closes the gap between textual separation
and semantic dependency without treating the dependency graph itself as mutation authority.

Semantic Amendment Protocol v2 applies repository semantics to scope growth before the canonical
amendment transaction commits. It permits only monotonic candidates, isolates newly committed
mutation authority, projects that authority onto the semantic graph, propagates downstream impact,
and enforces hard breadth limits. The same transaction compares the new surface with active intents.
Independent or explicitly commutative relationships may proceed; conflicts and unknown relationships
fail closed. Ordered overlap is returned as an ordering requirement rather than approved. Runtime
premise fencing revokes an already active writer when its premise becomes stale. Runtime recovery
then requires stale-causing producers to complete, re-admits the unchanged authority surface on a
new pinned base, and requires an explicit resume before a new broker can acquire a fresh fencing
token.

### Dependency graph

Edges are stored as `consumer -> producer`. Claim Plane validates the complete candidate graph inside the same transaction as admission or amendment. The external graph representation also exposes a producer-first topological order.

Invalidation is deliberately asymmetric:

1. The first hop is filtered by the resource keys changed by the producer.
2. A consumer affected by that premise becomes `stale`.
3. Any active governed broker for that consumer is fenced in the same transaction: prepared operations are failed and the writer lease is released.
4. Once stale, all outputs from that consumer are untrusted.
5. Staleness therefore propagates transitively to downstream consumers, which receive their own durable fence evidence.

Every notice records the root producer, direct producer, depth, dependency chain, changed resource keys, and runtime-fence identifiers. A stale dependency is unavailable in worker context even when its previous intent payload remains inspectable. Fencing revokes mutation authority. A recovery record separately binds the prior fences, old and new content versions, stale-causing producer versions, and the refreshed base commit. The recovered intent cannot be activated through the ordinary path until the explicit resume transition is recorded.

### Execution boundary

Workers remain in Git branches, worktrees, or external sandboxes. Claim Plane gives each worker a bounded context pack but does not execute a coding model itself.

### Coding-agent connectors

Project-local connectors bind an external coding runtime to Claim Plane without replacing the runtime's normal entry point. The Codex connector installs a stable lifecycle dispatcher in `.codex/hooks.json`, preserves unrelated project hooks, and keeps mutable connector state under `.claim-plane/`. Enrollment is idempotent and respects project-local Codex policy that disables hooks.

Each Codex session has a private local record keyed by a digest of the runtime session ID. The first submitted task pins the current Git commit and receives connector-owned task, intent, and owner identities. The user prompt itself is not persisted in connector state. `UserPromptSubmit` supplies Codex with the bootstrap contract required to perform read-only discovery and submit a structured intent proposal. Claim Plane converts that proposal into the canonical `ChangeIntent`, binds the pinned base, performs atomic admission, activates successful work, and records the resulting committed and contingent scope on the session.

Active Codex lifecycle traffic renews the bound intent lease through Claim Plane. Lease maintenance is connector-owned rather than delegated to model behavior. Resume recovery is authority-bound: an expired intent may be re-admitted as a successor only on the unchanged pinned commit and branch and only if current atomic admission still succeeds. Non-resumable states remain fail-closed.

The Codex connector treats a physical worktree as a single mutation-attribution domain. It permits only one active Codex session to acquire intent authority in that worktree; parallel sessions must use separate Git worktrees. At bootstrap, pre-existing user changes are fingerprinted. The guard refuses to mutate those paths, and unchanged baseline paths are removed from completion attribution. This prevents autonomous work from absorbing unrelated local edits while retaining support for a developer worktree that is not globally clean.
Explicit session abandonment releases unfinished intent authority when the developer chooses not to resume that work.

Connector state is bound to the resolved repository root. Mutation hooks fail closed when enrollment or session state is missing or unreadable, when the branch no longer matches the task bootstrap, or when the project root cannot be established. Enrollment carries a connector revision, and `doctor codex` validates the exact connector-owned lifecycle definitions; reconnecting repairs or upgrades only those handlers and preserves foreign project hooks.

The model may propose goal, scope, preserve requirements, acceptance checks, and explicit dependencies. It cannot choose the authoritative intent ID, task ID, owner, or base revision for the session. Repository-relative file and document declarations are validated before admission, and a changed Git `HEAD` invalidates the bootstrap before any intent is admitted.

For supported `PreToolUse` surfaces, the connector derives concrete mutation requests from the tool input and checks them against the live session intent. Committed file/document authority passes. One matching contingent surface may be atomically promoted through the canonical registry before execution. A missing active intent, stale pinned base, undeclared path, incompatible destructive capability, unknown mutating surface, or mutation whose effects cannot be proven yields a structured denial. No positive hook override is emitted; successful authorization preserves the runtime's own sandbox and approval path.

The connector records authorization counters, decision codes, and affected paths without retaining raw tool arguments. This provides a minimal local audit trail while avoiding duplication of model prompts and command bodies in connector state.

A scope denial with concrete repository effects may create a short-lived amendment ticket. The ticket binds the current intent fingerprint and base commit to the exact mutation set observed by the guard. The model can provide rationale but cannot select a wider resource set. The connector reconstructs a monotonic ChangeIntent amendment, preserves existing protections and acceptance requirements, and uses optimistic version checking plus canonical admission before re-activating the intent. Ticket reuse is limited to an unchanged intent, and line-bounded declarations are not widened from whole-file hook observations.

Connector control commands form a separate narrow class from ordinary shell execution. Only session-local `codex-intent admit`, `status`, `amend`, `verify`, and `abandon` commands are recognized; admission uses inline JSON, amendment coordinates come from the issued ticket, and explicit verification can only target the current session and repository. This keeps coordination control available without granting general shell mutation authority.

Verified completion is a state transition, not a model assertion. A direct Codex session can use `Stop` as the bounded completion checkpoint. Under `claim-plane codex`, `Stop` is only a turn boundary and reports final verification as pending; after the TUI exits, the launcher collects the concrete repository delta, executes declared acceptance on the same worktree with tree-integrity checks, passes the manifest through the canonical integration verifier, and records normalized session end. Only a clean launcher-owned report transitions the active intent to `completed` and the task to `verified`.

Direct sessions may request one repair continuation after failed evidence and then report `UNVERIFIED` without another loop. Interactive launcher sessions keep the TUI open across turns and run acceptance exactly once after exit. Connector-owned `.codex` control files are excluded from task-change accounting; they remain protected mutation surfaces and are never grantable through the session ChangeIntent.

The connector is an integration boundary, not a replacement for Claim Plane authority. Runtime hook coverage and timeout semantics belong to the external coding runtime. Admission, atomic contingent promotion, broker capabilities, repository identity, and verification remain the sources of authorization and proof. The brokered path remains the reference-monitor boundary for deployments that require non-bypassable mutation control. This separation lets the runtime adapter evolve without weakening the core protocol or making MCP participation a prerequisite for enforcement.

### Integration verifier

The verifier compares declared intent with observed work:

- paths and real Git hunks;
- typed, qualified callable contracts;
- concept-bound signatures;
- base revision;
- dependency state;
- structured preserve policies;
- acceptance command results;
- semantic surfaces in changed text;
- cross-manifest hunk and contract collisions.

Preserved contracts are fail-closed. The Git collector builds a repository-wide inventory for the contracts named by a preserve policy, so a deletion cannot be mistaken for an artifact omitted from the changed hunk.


### Deterministic Integration v2

Swarm admission is a prediction over declared authority; integration is the point where Claim Plane can inspect the exact worker result. Each source snapshot is therefore reduced to an actual mutation surface before composition. Path and line-region authority are checked first. Python hunks are additionally mapped to Semantic Resource IR owners so a broad file capability cannot hide an edit to an undeclared sibling symbol.

Git replay is intentionally split from commit creation. The snapshot is applied with no commit, the staged result is re-indexed after any line movement, and the semantic dependency graph is rebuilt over the composed worktree. Actual mutation roots are compared with already integrated work using the conflict taxonomy. Only an allowed staged result becomes a durable integration commit; rejection restores the prior integration head and leaves the target branch untouched.

### Bounded integration rescue

A merge conflict does not grant permission to improvise a resolution. Integration rescue classifies the durable conflict and chooses from four deterministic outcomes. A transient integration error may retry the same immutable source snapshot. A textual conflict, or an ordered dependency whose worker ran on an older integration head, may prepare serial re-execution by resetting only the Claim Plane-owned worker worktree to the current integration head and superseding that specific successful run for scheduling purposes. Authority violations and post-apply semantic drift require explicit review; unresolved semantic conflict requires replanning. Every automatic repair consumes `retries.max_repairs_per_work_item`, and exhaustion stops fail-closed. Historical runs remain in evidence and continue to count toward total launch, token, and wall-time budgets.

### Deterministic concurrency conformance

The offline conformance suite runs canonical fixtures through the deterministic planning, amendment,
and runtime-recovery layers. It records stable scenario identities, expected and observed outcomes,
and aggregate safety/selectivity metrics. The suite is a regression boundary for the controller, not a
performance benchmark: it launches no coding agents and does not measure physical execution overlap.

### Verified integration pipeline

`IntegrationRunner` is model-agnostic and closes the check/use gap. For each attempt it:

1. captures every worker through a temporary Git index seeded from the admitted base;
2. writes an immutable Git tree and synthetic snapshot commit without touching the worker index;
3. persists one binary patch and its SHA-256 digest;
4. materializes a detached frozen worktree and collects the manifest from that exact snapshot;
5. runs worker acceptance inside the frozen worktree and rejects snapshot mutation;
6. verifies all immutable manifests together;
7. applies the same persisted patch bytes in producer-first topological order;
8. records the composed tree before integration commands;
9. runs integration commands and rejects any resulting tree mutation;
10. creates a verified result commit and reproducible result patch;
11. emits a canonical evidence bundle binding the spec, base tree, worker patches, manifests, reports, result tree, and result commit.

External repair adapters still modify the original worker worktree. The next bounded attempt creates a new immutable snapshot, so repaired bytes cannot bypass verification.

### Repair and routing

Repair planning maps deterministic findings to minimal actions. Routing supplies a transparent risk recommendation; it never weakens verification for cheaper workers.

## Trust boundaries

Deterministic enforcement:

- registry transactions;
- admission and state transitions;
- DAG validation;
- resource-scoped and transitive dependency invalidation;
- path, hunk, contract, acceptance, and policy checks;
- immutable worker snapshots and exact patch composition;
- snapshot and integration-command mutation guards;
- SHA-256 evidence and verified result commits;
- audit persistence.

Probabilistic or external interpretation:

- planner-generated task decomposition;
- initial intent generation;
- semantic proposals not already published in Agent Lexicon;
- external repair commands;
- free-form review of behavior that cannot be expressed as a deterministic policy.

Claim Plane should fail closed at the deterministic boundary and return structured guidance rather than silently guessing.

## Execution integrity

Integration uses one exact base commit. Human-readable refs remain in the protocol for audit and UX, but they are never trusted as immutable execution identities. Every worker repository must contain the pinned object, and all snapshots are frozen directly from it.

Observed-access traces form a second evidence channel beside Git diffs. Git proves what changed; tool traces can prove what a worker actually read or attempted to write. Batch verification uses those reads to discover undeclared producer dependencies.

Acceptance execution has two layers: repository-tree immutability checks are always available, while optional OS backends provide stronger process isolation. Evidence can be HMAC-attested without adding a runtime dependency.


## Trusted execution boundary

Claim Plane distinguishes three observation channels:

1. `optional` — no runtime trace is required;
2. `required` — a legacy JSON/JSONL trace or trusted session must be supplied;
3. `trusted` — only a sealed control-plane observation session is accepted.

A trusted session is bound to one admitted intent. Events are persisted by the control plane with a monotonically increasing sequence, previous-event hash, event hash, and HMAC. Sealing authenticates the session head, monitor identity, coverage class, required tools, event count, and completeness statement. Workers receive no database write primitive and integrations reject an editable file trace in trusted mode.

This establishes integrity and provenance relative to the monitor boundary. It does not make an incomplete tool proxy magically complete: governed deployments must route all permitted worker reads/writes through the declared proxy or provide an OS-level monitor.

Repair adapters use their own sandbox policy and a sanitized environment. Evidence may be HMAC-attested inside one CI trust domain or Ed25519-signed for public-key verification.


## Sound broker operations

Claim Plane separates broker authorization, filesystem mutation, observation, and integration evidence. A broker instance is registered against an exact intent content version and Git base. Every request revalidates that capability. Mutations use a durable prepare record and an external recovery journal before the file changes; only after the trusted observation event is committed is the operation marked complete.

The integration runner accepts brokered evidence only when the observation session is bound to a valid broker instance and every broker event belongs to a signed committed operation. This prevents a generic write permission from authorizing deletion, prevents mutations from preceding observation, revokes released intents, and rejects hand-authored broker-looking sessions.

## Storage and writer authority

`PlaneStore` is the complete storage boundary, composed from explicit claim, intent, observation, broker, and verification protocols. `SQLitePlaneStore` is the default single-host implementation and preserves the historical `ClaimRegistry` API for compatibility. `Plane.from_store(...)` validates a backend before any admission or broker operation runs.

For one host, writer authority is layered:

1. SQLite atomically grants the logical writer lease.
2. An OS file lock under Git's canonical common directory owns the physical worktree even across separate SQLite files and rejects alternate lock namespaces.
3. A monotonic fencing token is attached to every broker operation and evidence
   record, rejecting superseded writers.

A future PostgreSQL backend should provide the network-authoritative lease and
fencing sequence. It does not remove the need for the per-host OS lock.
