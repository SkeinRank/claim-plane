# Changelog

All notable public changes to Claim Plane will be documented in this file.

The project follows Semantic Versioning while it is in the `0.x` research-preview
phase. Internal prototype iterations before the first public release are intentionally
not part of the public release history.

## [Unreleased]

## [0.1.0] — Initial public release

### Coordination and semantic admission

- Add structured `ChangeIntent` admission for reads, writes, extensions, deletes,
  renames, documentation, tests, bounded line regions, concepts, contracts, routes,
  schemas, configuration, and documents.
- Add atomic leases, heartbeats, amendments, completion, release, append-only audit
  events, acyclic dependency validation, producer-first ordering, and transitive stale
  propagation.
- Add optional fail-closed Agent Lexicon integration for canonical concept identity and
  semantic-overlap detection.
- Add bounded worker context packs, deterministic preserve policies, Git hunk checks,
  contract verification, and targeted repair plans.

### Governed execution

- Add a capability-based repository broker with exact read, write, append, delete,
  rename, document, test, and allowlisted-command permissions.
- Add clean-root enforcement, durable prepare/commit journaling, rollback and startup
  recovery, live intent revalidation, broker attestations, and trusted observation
  sessions.
- Add one canonical OS writer lock per Git worktree, registry writer leases, monotonic
  fencing tokens, compare-and-swap Git-tree transitions, and rejection of out-of-band
  worktree mutations.
- Preserve POSIX file modes across broker mutations and recovery.
- Add Linux Bubblewrap support for a proxy-only worker boundary with no repository
  mount; macOS remains supported for broker and verification workflows with a documented
  isolation limitation.

### Verified integration and evidence

- Freeze every worker into an immutable Git tree and synthetic commit before verification.
- Generate one exact binary patch per worker, verify those bytes, and compose the same
  persisted patches in dependency order inside a neutral integration worktree.
- Add worker and integrated acceptance guards, sandbox policies, observed-access checks,
  result commits, reproducible result patches, SHA-256 evidence, and optional HMAC or
  Ed25519 attestation.
- Add complete storage contracts with `SQLitePlaneStore` as the single-host backend and
  `Plane.from_store(...)` as the backend injection boundary.
- Add CLI, stdio MCP, JSON Schemas, examples, CI, protocol tests, and an A/B/C benchmark
  harness for future comparative evaluation.

### Status

- Publish as a Research Preview. Claim Plane is not yet presented as a production-grade
  security boundary or as empirically superior to worktree-only coordination.
