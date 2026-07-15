# Design rationale

## Why intents instead of locks

A file lock says only that another writer exists. A `ChangeIntent` also carries semantic concepts, contracts, dependencies, allowed regions, invariants, acceptance criteria, and base revision. Claim Plane uses optimistic admission where compatibility is provable and serialization where it is not.

## Why concept-bound contracts

Two agents can name one domain concept differently, while two unrelated concepts can share a method name such as `healthcheck`. Therefore contract equality alone is insufficient. Claim Plane requires a contract to identify the concept it governs before that contract can authorize semantic overlap.

## Why real Git hunks

Declared line regions are only plans. The verifier must compare the plan with observed edits. Claim Plane parses zero-context Git hunks and checks that every changed region remains inside the admitted bounds. Batch verification then reasons about actual hunk overlap, not only common file names.

## Why stale propagation instead of global locking

Long-running agents should not be forced into one global serial queue. When a producer changes a premise, Claim Plane filters the direct invalidation by resource. Once a consumer is stale, its own outputs are no longer trustworthy, so stale state propagates transitively. The planner or worker repairs the affected dependency frontier instead of restarting unrelated work.

## Why acceptance is opt-in

Repository commands may be expensive or unsafe. Reading or verifying a manifest must not execute arbitrary commands. Acceptance runs only through an explicit option, with timeout and captured evidence.

## Why Agent Lexicon remains separate

Agent Lexicon owns durable semantic identity and terminology policy. Claim Plane owns temporary change coordination. Keeping them separate allows Agent Lexicon to remain useful in ordinary IDE and CI workflows while Claim Plane consumes published vocabulary when multi-agent coordination needs it.

## Why no model runtime

Agent products and providers change quickly. Claim Plane coordinates external agents through a small deterministic protocol instead of embedding a proprietary planner, IDE, or model client. This keeps the integration layer model-agnostic and testable.

## Why the graph must remain acyclic

A dependency cycle makes producer-first scheduling and premise refresh ambiguous. Claim Plane validates the complete candidate graph in the same transaction as admission or amendment. A rejected cycle cannot partially mutate the accepted graph.

## Why a neutral integration worktree

Individual worktrees can pass their own checks while still failing when composed. Claim Plane freezes each worker into an immutable tree, verifies one exact patch, and applies those persisted bytes to a detached worktree based on the admitted revision. This exposes textual, contract, and behavioral integration failures without granting any worker direct ownership of the final integration result.

## Why repair remains external

Claim Plane produces deterministic reports and bounded repair plans, but it does not choose a proprietary coding agent. Caller-provided adapters can invoke different models or human workflows while the same admission and verification boundary remains authoritative.

## Why verification freezes worker state

A mutable worktree creates a time-of-check/time-of-use gap: a manifest can be collected, an acceptance command can change files, and a later `git diff` can integrate bytes that were never verified. Claim Plane seeds a temporary index from the admitted base, writes an immutable Git tree and synthetic commit, and produces one binary patch. The manifest and acceptance evidence come from a detached worktree at that commit, while neutral integration applies the persisted patch bytes after verifying their SHA-256 digest.

Acceptance is read-only by default. A command may create ignored caches, but any tracked or non-ignored tree mutation blocks the attempt. The same rule applies after integration commands. This keeps generation/repair separate from verification and makes the resulting commit reproducible from the evidence bundle.

## Why pin both a revision and a commit

A name such as `main` is useful to humans but can resolve differently across clones or over time. Claim Plane preserves the name for explanation and separately pins the exact object ID for enforcement. This prevents a worker from passing acceptance on one history while the integration runner composes its patch onto another.

## Why observed access is supplemental

Planner-declared read sets are useful but incomplete. A tool adapter can emit actual accesses without forcing Claim Plane to become an IDE or operating-system tracer. The guarantee is deliberately scoped: recorded accesses are verified; bypassed tools are not claimed to be observed.

## Why HMAC before public-key attestation

HMAC is dependency-free and sufficient for a CI service and verifier that share a secret. It does not establish public identity. The evidence format keeps signing separate so Sigstore, KMS, or hardware-backed adapters can be added later without changing the verified payload.
