# Guarantees and trust boundaries

Claim Plane separates runtime enforcement from evidence integrity. A report must state
what was blocked before mutation, what was observed, what was verified only after
execution, and what was outside adapter visibility.

## What a verified controlled run establishes

For the Git state bound to the run, Claim Plane verifies that:

- the adapter, runtime, protocol, project, policy, session, and intent identities match;
- the lifecycle event chain is ordered, complete for the recorded boundary, and untampered;
- completed mutations are covered by initial or explicitly amended authority;
- stale, cancelled, expired, or corrupt authority does not become an implicit allow;
- the final tracked and relevant untracked changes match the verified Git state;
- configured acceptance commands and deterministic policy checks produced the recorded result.

## What it does not establish

A verified result does not prove that:

- business logic is correct for every input;
- tests are complete;
- the implementation is free of security vulnerabilities;
- project-local hooks form a non-bypassable operating-system sandbox;
- a human review is unnecessary for important code.

## Codex boundary

Project-local Codex hooks can hard-block the supported mutations routed through those
hooks. Direct host writes or activity outside runtime visibility are post-verified by the
final Git comparison. `strict` and `critical` policies refuse to start when their required
guarantees are unavailable.

Use the Linux brokered boundary when non-bypassable repository mutation isolation is
required. On macOS, run that boundary in a Linux VM or container rather than representing
project hooks as operating-system enforcement.

## Evidence hygiene

Normalized lifecycle evidence excludes raw prompts, source content, credentials, tool
payloads, hook output, and final model messages. Digests and structured metadata are
used where the raw value is not required for verification.

## Semantic dependency analysis boundary

Semantic Dependency Graph v2 is static repository evidence, not an execution sandbox and not
an automatic permission grant. The Python frontend parses source without importing or
executing repository modules and classifies targets as `internal`, `external`, or `unresolved`.
Unresolved relationships remain explicit so later admission stages can fail closed instead of
assuming independence.

The graph currently exposes repository structure and dependency queries for downstream impact
and conflict analysis. Semantic impact reports may classify stable-symbol signature changes as
contract-sensitive and trace their downstream consumers, but they are evidence rather than an
automatic allow or deny decision. Body-only changes require an authoritative changed-resource surface
from source ownership or Git-hunk mapping when the dependency graph itself is unchanged. External
and unresolved boundaries remain explicit and cannot be interpreted as proof of independence.

Semantic Conflict Taxonomy v2 may classify two complete mutation surfaces as `independent`,
`commutative`, `ordered`, `conflicting`, or `unknown`. `commutative` requires explicit deterministic
proof evidence; missing graph roots, explicitly unknown changes, shared unresolved boundaries, and
bounded traversal that cannot prove independence remain `unknown`. The taxonomy is evidence for later admission and does not itself grant mutation authority.
Same-file Admission v2 may consume a complete taxonomy decision during swarm planning when the
configured same-file policy is `region_safe`. It can remove an otherwise conservative same-file
serialization constraint only for graph-backed `independent` or explicitly `commutative` mutation
roots. `ordered` produces a deterministic serialization direction; `conflicting`, `unknown`, missing
semantic roots, and missing graph evidence do not unlock parallel execution. Explicit same-file
`serialize` or `deny` policy always wins.

Repository-bound planning derives this semantic evidence from the exact pinned Git revision for the
swarm session. A dirty working tree is not used as the proof source. The resulting decision and graph
fingerprint are persisted with the concurrency plan.

Semantic Amendment Protocol v2 adds a separate fail-closed preflight for new mutation authority.
The preflight is executed inside the same registry transaction as amendment admission, requires
monotonic intent growth, applies explicit operation/path/root/impact/contract bounds, preserves
unresolved dependency boundaries, and evaluates the newly granted semantic surface against active
intents. A rejected preflight does not replace the previously admitted intent. Ordered overlap is
recorded but not granted until a runtime layer can establish the required execution order.

Existing runtime intent ordering, stale-state propagation, Git hunk verification, and brokered
mutation enforcement remain the authoritative execution controls. When a tracked premise is
invalidated, Claim Plane fences any active governed broker in the same registry transaction, fails
its prepared operations, releases its writer lease, and records durable runtime-fence evidence.
Recovery is a distinct deterministic transition: stale-causing producers must be completed, the
refreshed intent must preserve its authority surface and pin a new base commit, admission and
dependencies are re-evaluated, and explicit resume is required before a replacement broker may
start with a fresh fencing token. The control plane records this lifecycle; it does not claim that the
new Git base contains a semantically correct implementation of the producer result.

## Deterministic integration authority

Swarm execution success never authorizes the resulting diff by itself. Before a worker snapshot can become an integration commit, Claim Plane checks its actual changed paths and regions against the admitted work item. When semantic authority is declared for a Python path, changed hunks must map to those admitted structural owners. The staged replay is checked again after composition, and actual semantic overlap with earlier integrated work must be independent, explicitly commutative, or correctly ordered on a refreshed dependency base. Missing structural evidence, semantic drift, unknown overlap, and conflicting actual roots fail closed and restore the prior integration head.

A failed integration also does not authorize automatic conflict resolution. The rescue protocol may only retry an already captured immutable snapshot after a transient integration failure, or supersede one successful run and re-execute that work from the current managed integration head when deterministic evidence identifies textual conflict or a stale ordered dependency. It never synthesizes merge content. Scope violations, staged semantic mismatch, unresolved semantic conflict, and exhausted repair budgets remain blocked. Superseded executions remain durable evidence and continue to count against global resource budgets.

## Deterministic concurrency conformance

`claim-plane swarm conformance` is an offline regression and evidence suite for the deterministic
control plane. It covers canonical safe-parallel, ordered, conflicting, unresolved, amendment, and
runtime-recovery cases and emits versioned metrics. A passing report demonstrates consistency with
those fixtures only. It does not establish workload-level speedup, agent quality, or production safety
for an arbitrary repository; those claims require separate measured experiments and runtime evidence.
