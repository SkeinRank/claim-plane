# Changelog

All notable public changes to Claim Plane will be documented in this file.

The project follows Semantic Versioning while it is in the `0.x` research-preview
phase. Internal prototype iterations before the first public release are intentionally
not part of the public release history.

## [Unreleased]

## [0.2.1] — 2026-07-17

### Fixed

- Make contingent-scope promotion region-aware. A concrete line mutation now promotes
  only the contingent region that covers that mutation instead of all bounded regions
  declared for the same path.
- Preserve broad contingent fallbacks while granting the narrow concrete path/region
  requested at runtime, enabling incremental scope expansion without accidental
  whole-file authority.
- Keep multiple committed line capabilities as an explicit region union in the broker;
  bounded regions no longer collapse into implicit whole-file mutation permission.
- Require whole-file write, append, delete, and rename operations to hold an unbounded
  committed capability, while `replace_lines` may execute inside any one admitted
  bounded interval.

### Changed

- `promote-scope` and the MCP `promote_contingent_scope` tool accept an optional
  concrete `region` such as `lines:20-24`. Broker-driven `replace_lines` promotions
  provide this region automatically.

## [0.2.0] — 2026-07-16

### Added

- Add committed and contingent operation commitments to `ChangeIntent`. Committed
  operations participate in initial admission and grant mutation authority, while
  contingent operations remain non-blocking planning hints until promoted.
- Add atomic contingent-scope promotion with re-admission. Rejected promotions leave
  the currently admitted intent unchanged; successful promotions preserve active
  lifecycle state and create an audited `intent_scope_expanded` event.
- Add broker-driven just-in-time scope promotion. A governed broker may inspect a
  predeclared contingent path and promote it before the first mutation; the broker
  capability is re-attested to the new intent content version in the same transaction.
- Add `promote-scope` CLI and `promote_contingent_scope` MCP entry points, plus
  commitment metadata in worker context packs and the public JSON Schema.

### Changed

- Admission, verification, region enforcement, collision checks, and broker mutation
  authorization now reason only over committed operations. Contingent surfaces never
  silently grant write authority.
- Read-only broker discovery may still inspect contingent path surfaces so workers can
  gather evidence before requesting or triggering promotion.

## [0.1.1] — 2026-07-15

### Added

- Add pre-commit Ruff lint-fix and formatting hooks so staged Python changes are
  normalized before they reach CI.

### Fixed

- Re-evaluate identical blocked intents against the current active set instead of
  permanently returning a cached rejection, enabling the normal wait-and-retry flow.
- Make `release_intent()` safe after successful completion while preserving
  `completed` as the terminal success state and audit history.
- Replace raw `KeyError` failures for malformed intent/resource/operation payloads
  with stable, human-readable required-field validation errors.

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
