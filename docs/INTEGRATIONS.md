# Integration patterns

## Project enrollment and diagnostics

The normal single-agent setup is intentionally short:

```bash
cd my-project
claim-plane init
claim-plane connect codex
claim-plane doctor
```

Initialization writes `.claim-plane/config.yaml` with protocol `claim-plane.project-config.v1`. The file contains only project identity, repository identity, default-branch metadata, acceptance commands, and adapter policy settings. Credentials, raw prompts, runtime tokens, and provider secrets are never copied into project configuration.

Acceptance discovery prefers an existing `scripts/check.sh`, then recognized Make, Python, Node, Rust, Go, Maven, or Gradle test entry points. The detected list is a starting point and can be edited before a guarded run. Re-running `claim-plane init` preserves the stable project identity and configured commands.

`claim-plane doctor` combines project, runtime, adapter, policy, and hook diagnostics. Errors make the report not ready; warnings explain limitations such as a dirty worktree, an unavailable optional command, or a project-local sandbox whose out-of-band host writes remain post-verified. JSON output is available through `claim-plane doctor --json`.

`claim-plane reset` removes Claim Plane-owned databases, request caches, lifecycle state, session state, and hook handlers. It preserves repository files, foreign Codex hooks, and the project config unless `--remove-config` is supplied explicitly.

## Controlled single-agent execution

The primary Codex product path is one bounded command:

```bash
claim-plane run "Implement refresh-token rotation" --policy guarded
```

The command performs project and adapter diagnostics, negotiates the adapter protocol, validates the selected policy against the effective capability manifest, captures the initial Git state, and then starts a non-interactive Codex session. The normal Codex hooks continue to own task bootstrap, intent admission, mutation decisions, amendments, and completion. The runner supplies only process control and run identity; it does not grant mutation authority directly.

A wall-time timeout or keyboard interruption terminates the runtime process group and calls the adapter cancellation path. Unfinished intent authority is released before the terminal record is written. On ordinary completion, Claim Plane inspects the bound session and independently invokes verified completion when the runtime did not already seal one. Runtime success without verified scope and acceptance evidence becomes `REVIEW_REQUIRED` or `REJECTED`, never `VERIFIED`.

The result protocol is `claim-plane.controlled-run.v1`. Its durable record binds the run to the initial and final Git states, capability-manifest digest, negotiated adapter/runtime versions, policy compatibility, lifecycle report, completion summary, and cancellation result. Raw task text, raw tool payloads, raw runtime errors, and the Codex final message are not stored in the record; only bounded metadata and cryptographic digests are retained.

Machine-readable use is available through:

```bash
claim-plane run "Implement refresh-token rotation" --json
claim-plane run "Implement refresh-token rotation" --out controlled-run.json
claim-plane run "Implement refresh-token rotation" --timeout 1800
```

## Agent adapter boundary

Coding-agent runtimes integrate through the public `AgentAdapter` contract instead of calling runtime-specific control logic from product code. The adapter receives versioned `AdapterRequest` objects and returns portable `AdapterResponse` objects. The request envelope carries idempotency, timeout, session, run, intent, and expected intent-version data; the payload remains owned by the runtime implementation.

```python
from claim_plane.connectors import CodexAdapter
from claim_plane.protocol import AdapterOperation, AdapterRequest

adapter = CodexAdapter()
response = adapter.enroll_project(
    AdapterRequest.create(
        AdapterOperation.ENROLL_PROJECT,
        adapter="codex",
        project_root=".",
        request_id="enroll-local-codex",
    )
)
```

The same interface covers session lifecycle, task submission, intent admission, mutation requests, scope amendments, completion, cancellation, and resume. A caller that supplies an expected intent version receives a fail-closed `stale_intent_version` error before runtime-specific work begins when the authority has changed. Repeated requests with the same identifier replay the original adapter response; a conflicting reuse of that identifier is rejected.

The CLI uses this boundary for Codex enrollment, lifecycle hooks, intent control, completion verification, and cancellation. Existing connector functions remain importable for compatibility, but new integrations should depend on `AgentAdapter`.

Each adapter exposes a canonical capability and guarantee manifest. Inspect the effective Codex boundary before choosing a policy:

```bash
claim-plane adapters inspect codex --repo .
claim-plane adapters inspect codex --repo . --policy guarded
claim-plane doctor codex --repo . --policy strict
```

The manifest records adapter and runtime versions, capability levels, guarantee levels, and the component responsible for each guarantee. Policy checks fail closed when the selected level requires enforcement that the runtime does not provide. In particular, project-local Codex hooks can hard-block supported intercepted tool writes, while out-of-band host writes remain subject to final Git verification.

Every session-bearing Codex request also emits normalized events through the shared lifecycle store at `.claim-plane/lifecycle/events.sqlite3`. The Codex adapter records only redacted summaries and stable digests. Adapter request replay does not duplicate lifecycle decisions, and resume validates the existing event stream before new state can be appended.

Adapter authors can validate the same behavior contract through `claim_plane.protocol.run_adapter_conformance`. A driver translates the canonical scenarios into runtime-specific requests while the shared runner owns result semantics, report digests, and guarantee-to-scenario coverage. `claim_plane.testing.ReferenceAdapter` provides a dependency-free implementation for core tests, and `claim-plane adapters conformance codex` produces the Codex compatibility report in isolated Git fixtures.

Adapter selection should go through the registry when an integration may load more than one runtime. The registry performs semantic protocol negotiation and verifies a project pin before returning a compatible implementation:

```python
from claim_plane.connectors import build_adapter_registry

registry = build_adapter_registry()
handshake = registry.handshake("codex", project_root=".").require_compatible()
adapter = registry.create(handshake.adapter)
```

Use `claim-plane adapters pin codex` after reviewing the runtime, capability manifest, and conformance report. A later adapter, runtime, protocol, or provider change then fails before `SessionStart` and includes a migration finding. The normalized handshake summary is included in session evidence.

Third-party packages register adapters through the `claim_plane.adapters` entry-point group. The loaded class or factory is validated against `AgentAdapter`, and its declared protocol range is negotiated in exactly the same way as the built-in Codex adapter. Programmatic registration remains available for embedded applications through `AdapterRegistry.register(...)`.

The same APIs read events from Codex or another adapter:

```python
from claim_plane.protocol import LifecycleEventStore

with LifecycleEventStore.for_project(".") as events:
    report = events.report(adapter="codex", session_id="session-id")
    chronology = events.replay(adapter="codex", session_id="session-id")
    events.export_ndjson(
        adapter="codex",
        session_id="session-id",
        destination="events.ndjson",
    )
```

The report refuses to treat an invalid order, broken causal chain, or altered digest as verified. Export is similarly fail-closed so later evidence layers cannot seal a corrupt lifecycle stream.

## Codex session bootstrap

One enrollment applies to ordinary Codex sessions launched inside the Git worktree:

```bash
claim-plane init
claim-plane connect codex
codex
```

`SessionStart` establishes private connector state. On the first `UserPromptSubmit`, Claim Plane pins the current `HEAD`, derives connector-owned task and intent identities, and returns model-visible bootstrap context. Codex performs repository discovery without mutating the worktree, then submits one proposal containing the intended goal, committed scope, plausible contingent scope, preserve requirements, and acceptance checks.

The proposal uses `claim-plane.codex-intent-proposal.v1`. Claim Plane binds identity and base revision rather than accepting those authority-bearing fields from the model, converts the proposal into a canonical `ChangeIntent`, performs governed atomic admission, and activates the intent when admitted. Identical retries are safe. A changed `HEAD` between task bootstrap and admission is rejected.

The connector keeps the raw user prompt out of local session state. The persisted bootstrap contains only a prompt digest and length until an explicit goal is admitted as part of the execution contract.

After admission, `PreToolUse` becomes the connector's pre-mutation authorization point for supported Codex tool surfaces. Read-only calls are unaffected. Committed mutations are authorized without overriding Codex's own sandbox or approval decision. A single matching contingent mutation is promoted through normal atomic re-admission before continuing. Undeclared, stale-base, unclassified shell, and unknown mutating surfaces are denied before the intercepted tool call executes. The connector records only decision metadata and affected repository paths, not raw commands or edit payloads.

An undeclared mutation with provable file effects can open a short-lived scope-amendment ticket. The ticket is bound to the current session, intent fingerprint, pinned base, and exact denied mutation set. Codex provides only a reason through `claim-plane codex-intent amend`; Claim Plane constructs the candidate amendment and re-runs canonical admission. Successful amendments preserve task identity, base revision, preserve requirements, acceptance checks, and existing scope. Rejected amendments do not replace the active intent. A stale ticket cannot be used after another scope change. Whole-file hook calls are never allowed to widen a line-bounded declaration.

The connector's shell control channel is intentionally narrow. `codex-intent admit`, `status`, `amend`, `verify`, and `abandon` may execute only for the current session and current repository. Admission uses inline proposal JSON so the bootstrap does not require a shell pipe or a repository file before authority exists. `.claim-plane/**`, `.git/**`, and `.codex/**` remain connector-protected and cannot be added to the session authority envelope.

`Stop` is the verified-completion checkpoint for an active Codex task. Claim Plane collects the current Git delta, including untracked files, removes connector-owned control files from task-change accounting, executes the intent's acceptance commands, proves that acceptance did not alter the worktree, and runs the normal integration verifier. A clean report completes the intent and records `claim-plane.codex-completion.v1`. A non-clean first attempt returns bounded findings and asks Codex to continue; a still-failing Stop-hook continuation is allowed to end as `UNVERIFIED` so the connector cannot create an unbounded continuation loop.

The completion record includes changed paths, authorized and denied mutation-call counts, admitted scope expansions, acceptance outcome, executed authority violations, and the deterministic verifier report. `claim-plane codex-intent verify` exposes the same gate explicitly, while `codex-intent status` returns the most recent result.

### Codex runtime hardening

Task bootstrap fingerprints any pre-existing tracked or untracked user changes outside connector-owned control surfaces. Those paths remain user-owned for the lifetime of the task: the guard rejects Codex mutations that touch them, while verified completion removes unchanged baseline paths from task attribution. This allows a developer to keep unrelated local work without letting an autonomous task absorb or overwrite it.

Mutation authority is single-session per physical worktree. A second Codex session can perform read-only discovery, but intent admission is refused while another enrolled Codex session has an active intent in that same worktree. Parallel work should use separate Git worktrees, preserving Claim Plane's normal cross-worktree coordination model and unambiguous completion evidence.
An intentionally discarded unfinished session can release its authority with `claim-plane codex-intent abandon --session-id <id> --repo .`; verified sessions are already complete and cannot be abandoned.

`SessionStart` with Codex resume semantics renews an active intent. If the lease expired, Claim Plane creates a successor intent from the previously admitted execution contract and re-runs canonical admission. Recovery succeeds only when the pinned Git commit and branch are unchanged and current coordination policy still permits the work. Released, stale, missing, or conflicting authority remains blocked and is surfaced through session status.

The hook boundary fails closed on mutation calls when enrollment state is missing, session state is unreadable, the Git root cannot be established, or the active branch changes. Re-running `claim-plane connect codex` upgrades or repairs connector-owned handlers without removing unrelated project hooks.

`claim-plane doctor codex` checks the installed runtime for the minimum hook coverage expected by this integration. Hook interception remains runtime-dependent, so the brokered execution path is still the hard reference-monitor boundary for deployments that require non-bypassable repository mutation control.

MCP remains available for agent interaction, but Codex enrollment, session binding, and mutation authorization do not require a voluntary MCP call from the model.

## Planner and workers

1. A planner emits one `ChangeIntent` per worker.
2. Producer intents are admitted before explicit consumers.
3. Claim Plane canonicalizes concepts and returns admission decisions.
4. Blocked work is split, serialized, amended, or retried unchanged after its blockers leave the active set; workers do not bypass admission.
5. Admitted workers run in isolated Git worktrees.
6. Each worker receives its bounded context pack.
7. Long-running workers heartbeat their leases.
8. Workers poll or receive coordination notices at safe checkpoints.
9. Git changes are continuously collected and verified.
10. The dependency graph is checked for cycles and exposed in producer-first order.
11. Candidate manifests are batch-verified before integration.
12. Worker patches are composed in a neutral detached worktree.
13. Integrated acceptance is executed.
14. Targeted repair is applied only to affected work, within a bounded loop.
15. Successfully integrated intents are completed. Abandoned work is released. A cleanup release after completion is accepted as an idempotent no-op.

## Producer amendment flow

```text
producer amendment
  ↓
atomic re-admission and version increment
  ↓
changed contract keys
  ↓
affected consumers become stale
  ↓
transitive downstream invalidation
  ↓
structured notices with dependency chains
  ↓
consumer amendment and re-admission
```

A notice is not permission to continue. The stale state is the enforceable signal.

## VS Code and Cursor

An extension should remain a thin client:

- show current intent, version, state, and lease;
- display undeclared path and region diagnostics;
- hover canonical terms and concept-bound contracts;
- surface pending notices and stale dependencies;
- offer commands to amend, verify, and acknowledge;
- invoke CLI or MCP instead of duplicating protocol rules in TypeScript.

## CI

A pull request job can collect and verify a worktree:

```bash
claim-plane \
  --db /shared/plane.db \
  verify-git "$CLAIM_PLANE_INTENT_ID" \
  --repo . \
  --run-acceptance
```

For a complete neutral integration attempt, provide an `IntegrationRunSpec` and run:

```bash
claim-plane --db /shared/plane.db integrate integration-run.json
```

The runner applies complete Git patches to a detached worktree and may invoke caller-provided repair adapters between attempts.

SQLite is a local reference implementation. Distributed CI should use a networked registry that preserves the same atomic admission, versioning, stale propagation, and audit semantics.

## MCP flow

Recommended tool sequence:

```text
admit_change_intent
get_worker_context
heartbeat_intent
list_coordination_notices
get_dependency_graph
verify_git_worktree
run_integration
plan_targeted_repair
amend_change_intent (when premises change)
acknowledge_coordination_notice
```

The calling agent must treat blocked or stale states as hard coordination boundaries.

## Storage deployment matrix

| Deployment | State backend | Physical worktree protection |
|---|---|---|
| Local developer / one CI host | `SQLitePlaneStore` | canonical Git worktree lock + fencing token |
| Several processes on one host | Shared or separate SQLite files | Same canonical Git worktree lock + fencing token |
| Several hosts | Network `PlaneStore` such as PostgreSQL | Distributed lease + fencing token + local OS lock |
| Managed/enterprise control plane | HA PostgreSQL-compatible backend | Host daemon ownership, distributed fencing, local lock |

See [STORAGE_BACKENDS.md](STORAGE_BACKENDS.md).
