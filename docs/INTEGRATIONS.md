# Integration patterns

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

The connector's shell control channel is intentionally narrow. `codex-intent admit`, `status`, `amend`, and `verify` may execute only for the current session and current repository. Admission uses inline proposal JSON so the bootstrap does not require a shell pipe or a repository file before authority exists. `.claim-plane/**`, `.git/**`, and `.codex/**` remain connector-protected and cannot be added to the session authority envelope.

`Stop` is the verified-completion checkpoint for an active Codex task. Claim Plane collects the current Git delta, including untracked files, removes connector-owned control files from task-change accounting, executes the intent's acceptance commands, proves that acceptance did not alter the worktree, and runs the normal integration verifier. A clean report completes the intent and records `claim-plane.codex-completion.v1`. A non-clean first attempt returns bounded findings and asks Codex to continue; a still-failing Stop-hook continuation is allowed to end as `UNVERIFIED` so the connector cannot create an unbounded continuation loop.

The completion record includes changed paths, authorized and denied mutation-call counts, admitted scope expansions, acceptance outcome, executed authority violations, and the deterministic verifier report. `claim-plane codex-intent verify` exposes the same gate explicitly, while `codex-intent status` returns the most recent result.

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
