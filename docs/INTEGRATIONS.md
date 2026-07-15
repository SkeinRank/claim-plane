# Integration patterns

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
