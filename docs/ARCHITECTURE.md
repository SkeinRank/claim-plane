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

### Admission engine

The admission engine compares an incoming `ChangeIntent` against active intents. It reasons about exact resources, broad scopes, line regions, concepts, concept-bound contracts, base revisions, destructive operations, and explicit dependencies.

Important safety rules:

- unknown overlapping writes fail closed;
- broad scope overlap is not treated as independent;
- a shared contract is relevant only when it governs the overlapping concept;
- same-file parallel work requires disjoint declared regions;
- missing explicit dependencies reject admission;
- proposed explicit and inferred premise edges must leave the graph acyclic.

### Dependency graph

Edges are stored as `consumer -> producer`. Claim Plane validates the complete candidate graph inside the same transaction as admission or amendment. The external graph representation also exposes a producer-first topological order.

Invalidation is deliberately asymmetric:

1. The first hop is filtered by the resource keys changed by the producer.
2. A consumer affected by that premise becomes `stale`.
3. Once stale, all outputs from that consumer are untrusted.
4. Staleness therefore propagates transitively to downstream consumers.

Every notice records the root producer, direct producer, depth, dependency chain, and changed resource keys. A stale dependency is unavailable in worker context even when its previous intent payload remains inspectable.

### Execution boundary

Workers remain in Git branches, worktrees, or external sandboxes. Claim Plane gives each worker a bounded context pack but does not execute a coding model itself.

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
