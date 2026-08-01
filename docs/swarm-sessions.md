# Swarm sessions, adaptive concurrency, and managed worktrees

Claim Plane represents a planned multi-agent change as a repository-bound `SwarmSession` with two independently versioned inputs:

1. a deterministic work graph proposed by the planner;
2. a machine-checkable budget policy owned by the control plane.

A planner may propose decomposition, candidate scope, dependencies, and a requested budget. Claim Plane assigns the session identity, pins the exact Git base, binds the session to one repository identity, validates the graph, normalizes the budget, and persists both through atomic local SQLite transactions.

This boundary keeps planning probabilistic while preventing a model from silently granting itself unlimited workers, retries, tokens, cost, or wall time.

## Session model

A swarm session contains:

- one root task and root acceptance criteria;
- an immutable repository identity and exact base commit;
- the branch observed when the session was created;
- an integration target;
- one versioned work graph;
- one independently versioned budget policy;
- deterministic graph and budget fingerprints;
- lifecycle state reserved for the complete swarm runtime.

The planner does not choose worker process identifiers, branch names, worktree paths, leases, or mutation capabilities. Claim Plane now owns deterministic worktree allocation; worker and intent binding remain later execution stages.

## Work graph

Each work item declares:

- a stable `work_id`;
- a concise title and goal;
- explicit upstream dependencies;
- proposed `IntentOperation` scope;
- preserve requirements;
- acceptance commands;
- optional metadata.

Claim Plane rejects the graph before persistence when it contains duplicate identifiers, missing dependencies, self-dependencies, cycles, repository-escaping paths, or attempts to include `.git`, `.codex`, or `.claim-plane` control state.

Input order does not affect graph identity. Work items are normalized by identifier before the graph fingerprint, topological order, roots, leaves, and dependency layers are calculated.

Dependency layers are structural planning information only. They are not an execution schedule.

## Budget policy

The `claim-plane.swarm-budget-policy.v1` object is a hard ceiling, not a planner recommendation.

The conservative default is equivalent to:

```json
{
  "protocol": "claim-plane.swarm-budget-policy.v1",
  "workers": {
    "max_active": 4,
    "max_active_per_work_item": 1,
    "max_work_items": 32,
    "max_total_launches": 64
  },
  "resources": {
    "max_total_tokens": 500000,
    "max_cost_usd": "25",
    "max_wall_time_seconds": 7200
  },
  "retries": {
    "max_replans": 2,
    "max_repairs_per_work_item": 2,
    "max_agent_restarts": 1
  },
  "concurrency": {
    "same_file": "region_safe",
    "unknown_overlap": "serialize",
    "shared_contract": "serialize",
    "schema_change": "serialize"
  }
}
```

### Worker ceilings

- `max_active` caps simultaneously running workers.
- `max_active_per_work_item` prevents one task from consuming the full swarm unless explicitly permitted.
- `max_work_items` bounds planner decomposition before execution.
- `max_total_launches` bounds first attempts plus all later restarts or replacements.

A work graph must fit both `max_work_items` and the minimum launch requirement. A graph with eight items cannot be attached to a policy allowing only six total launches.

### Resource ceilings

- `max_total_tokens` is the session-wide token ceiling.
- `max_cost_usd` is normalized to an exact decimal string with at most six fractional digits.
- `max_wall_time_seconds` bounds elapsed swarm execution time.

Version 0.17.0 persists and validates these ceilings. Meter collection and stop decisions are added by the runner and adaptive concurrency stages.

### Retry ceilings

The policy separately caps planner replans, repairs per work item, and agent process restarts. These limits prevent a failing task from consuming the entire session budget through repeated autonomous recovery.

### Concurrency policy

`same_file` accepts:

- `region_safe`: parallel work may proceed only when later admission proves disjoint regions;
- `serialize`: same-file work must run sequentially;
- `deny`: the decomposition itself is not permitted.

`unknown_overlap`, `shared_contract`, and `schema_change` accept `serialize` or `deny`. They intentionally do not accept an optimistic `allow` mode.

## Validate policy and graph

```bash
claim-plane swarm validate-budget \
  --policy examples/swarm/budget-policy.json \
  --work-items 2

claim-plane swarm validate \
  --graph examples/swarm/session-spec.json
```

`validate-budget` returns the normalized policy, deterministic fingerprint, and remaining launch capacity after one initial attempt per work item.

## Create a session

Initialize the repository once:

```bash
claim-plane init
```

Create the complete repository-bound session:

```bash
claim-plane swarm create \
  --spec examples/swarm/session-spec.json
```

The session spec may include `budget_policy`. When it is omitted, Claim Plane binds the conservative default policy explicitly.

Claim Plane resolves `HEAD` to an exact commit by default. Another revision may be selected explicitly:

```bash
claim-plane swarm create \
  --spec examples/swarm/session-spec.json \
  --base refs/heads/main
```

The local session database is stored under `.claim-plane/` and remains excluded through the repository Git exclude file.

## Inspect planning state

```bash
claim-plane swarm list
claim-plane swarm status <session-id>
claim-plane swarm graph <session-id>
claim-plane swarm budget <session-id>
```

Machine-readable output is available through `--json` or `--out` where supported.

## Replace a planned graph

A planner may refine a graph before execution. Replacements use optimistic graph-version checking:

```bash
claim-plane swarm replace-graph <session-id> \
  --graph revised-work-graph.json \
  --expected-version 1
```

The replacement must still fit the current budget policy.

## Replace a planned budget

A user or trusted orchestrator may tighten or deliberately widen a budget before execution:

```bash
claim-plane swarm replace-budget <session-id> \
  --policy revised-budget-policy.json \
  --expected-version 1
```

The update succeeds only when the stored budget still has the expected version and the current work graph fits the replacement policy. A rejected update leaves the current policy unchanged.

Graph and budget versions are independent. Changing one does not silently overwrite the other.

## Database migration

Opening an older swarm database upgrades it in place to schema version 4. Version-1 sessions receive the conservative default budget and deterministic budget fingerprint; version-2 and version-3 databases retain their stored concurrency plans. The schema-4 migration adds only the managed-worktree ownership table. Existing work graphs, budgets, plans, repository bindings, base commits, timestamps, and session identities remain unchanged.

## Current boundary

Version 0.19.0 creates durable planning, budget, adaptive concurrency, and managed Git worktree state. It does not launch Codex workers or account provider usage. The next swarm stage binds a runner and admitted ChangeIntent to each owned worktree.

## Adaptive concurrency planning

Version 0.18.0 turns the structural dependency layers into a source-bound execution-wave proposal:

```bash
claim-plane swarm plan <session-id>
claim-plane swarm concurrency <session-id>
```

The controller combines the explicit DAG with the active budget policy. It packs independent work up to `workers.max_active`, adds deterministic serialization constraints where parallel execution cannot be proved safe, and persists the result against exact graph and budget versions and fingerprints.

The controller evaluates committed operations only. Contingent scope is excluded until an admitted amendment promotes it, at which point the work graph or authority state must be replanned before execution continues.

The initial controller recognizes four policy reasons:

- `same_file`: line-bounded writes may share a wave only when their declared regions are parseable and disjoint under `region_safe`;
- `unknown_overlap`: missing regions, overlapping globs, and identical semantic resources fail closed to serialization or denial;
- `shared_contract`: a contract and another item touching the same contract or its bound subject cannot run concurrently unless an explicit dependency already orders them;
- `schema_change`: schema-changing work is isolated from other concurrent mutations.

A `serialize` decision adds a deterministic ordering edge while preserving the original DAG. A `deny` decision produces a persisted `replan_required` result with no execution waves.

Replacing either the work graph or budget policy invalidates the stored plan atomically. The next scheduler step must run `claim-plane swarm plan` again before workers can be launched.

Version 0.19.0 consumes this deterministic execution contract to provision isolated Git worktrees. It still does not launch Codex processes or grant mutation authority.


## Managed worktree provisioning

A current `ready` concurrency plan can be materialized into one isolated linked Git worktree per work item:

```bash
claim-plane swarm provision-worktrees <session-id>
claim-plane swarm worktrees <session-id>
```

Claim Plane derives collision-resistant paths and branches from the immutable session and work identifiers. Worktrees are created from the pinned session base, not from the caller's current uncommitted state. The durable ownership record stores the repository identity, graph version and fingerprint, base commit, branch, and absolute path. Repeating provisioning is idempotent when the physical Git state still matches that record.

Provisioning fails closed when the concurrency plan is missing or stale, the graph requires replanning, a user-owned path or branch collides with the deterministic allocation, or an existing managed record belongs to another graph version. Newly created Git state is rolled back if persistence fails.

Health inspection distinguishes ready, dirty, stale-graph, missing, unregistered, branch-mismatch, and base-mismatch states. It also reports Git worktrees located under the managed session directory that have no durable ownership record; these are reported as orphans and are never silently adopted or removed.

Cleanup is ownership-scoped:

```bash
claim-plane swarm cleanup-worktrees <session-id>
claim-plane swarm cleanup-worktrees <session-id> --work-id implementation
claim-plane swarm cleanup-worktrees <session-id> --force
```

Without `--force`, dirty managed worktrees are preserved. Cleanup only removes the exact Claim Plane path and branch recorded for the selected work item. Unregistered directories and unowned worktrees are left untouched.
