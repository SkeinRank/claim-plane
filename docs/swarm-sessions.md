# Swarm sessions, adaptive concurrency, managed worktrees, and Codex execution

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

Opening an older swarm database upgrades it in place to schema version 7. Version-1 sessions receive the conservative default budget and deterministic budget fingerprint; version-2 and version-3 databases retain their stored concurrency plans; schema version 4 retains managed-worktree ownership; schema version 5 retains durable Codex-run records. The schema-6 migration adds source-bound shared-admission records. Schema version 7 adds the deterministic merge queue without rewriting existing sessions, plans, worktrees, admissions, or run records. Existing work graphs, budgets, plans, worktrees, runs, repository bindings, base commits, timestamps, and session identities remain unchanged.

## Current boundary

Version 0.22.0 adds deterministic merge ordering and a Claim Plane-owned integration branch on top of durable planning, budgets, adaptive concurrency, managed Git worktrees, bounded Codex execution, shared admission, and dynamic scheduling. Integration remains distinct from swarm verification and never mutates the configured target branch.

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

Version 0.19.0 consumes this deterministic execution contract to provision isolated Git worktrees. Version 0.20.0 added bounded Codex process execution. Version 0.21.0 converts serialization constraints into effective dependencies. Version 0.22.0 releases dependent work only after prerequisites are integrated into the durable integration branch.

## Shared admission and dependency scheduling

Before launching workers, Claim Plane can materialize the authority topology proposed by the work graph:

```bash
claim-plane swarm admit <session-id>
claim-plane swarm admission <session-id>
claim-plane swarm scheduler <session-id>
```

Shared admission derives one deterministic `ChangeIntent` per work item. Each intent is bound to the swarm session repository, pinned base commit, declared committed operations, preserves, acceptance criteria, and effective dependencies. Explicit DAG dependencies are retained, while `serialize` decisions from the adaptive concurrency plan become additional prerequisites. Work that is ordered by dependency is not treated as concurrent authority; work that may coexist is admitted against the other concurrently admissible intents.

The persisted admission record is bound to the exact graph, budget, and concurrency-plan versions and fingerprints. Repeating admission with unchanged sources is idempotent. Replacing the graph or budget, or changing the concurrency plan, invalidates the record atomically. A blocked intent produces `replan_required` rather than granting partial execution authority.

The dependency scheduler is operational rather than static. It combines the admitted effective dependencies with durable Codex run state, retry ceilings, and available `max_active` capacity. Its snapshot distinguishes:

- `runnable`: all effective prerequisites succeeded and a worker slot is available;
- `queued_capacity`: prerequisites succeeded but current capacity is exhausted;
- `active`: a reserved or running worker exists;
- `retryable`: the latest execution failed but restart budget remains;
- `succeeded`: the bounded process completed successfully;
- `blocked`: an unfinished or terminally failed prerequisite prevents dispatch;
- `failed` or `replan_required`: no valid execution path remains under the current policy.

The Codex runner re-evaluates this snapshot inside the same SQLite transaction that reserves a worker slot and binds that reservation to the shared-admission fingerprint. Two concurrent launch attempts therefore cannot both rely on an obsolete capacity view. For compatibility with upgraded 0.20 sessions, the runner can create a missing admission record automatically, but explicit `swarm admit` is the preferred operator flow.

Without a merge queue, a `succeeded` process is the compatibility release signal. Once `swarm merge-plan` creates the deterministic queue, effective dependencies are released only after their entries are `integrated`. This prevents a dependent worker from starting against the original base while its prerequisite exists only as uncommitted changes in another worktree. Process success and integration are still not evidence that the work item is semantically verified or accepted.

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


## Headless Codex worker runner

Version 0.20.0 binds a non-interactive Codex execution to exactly one managed worktree and one work item:

```bash
claim-plane swarm run-codex <session-id> --work-id implementation
claim-plane swarm runs <session-id>
claim-plane swarm run-status <run-id>
claim-plane swarm cancel-codex <run-id>
```

The runner invokes `codex exec --json --sandbox workspace-write --ask-for-approval never` from the owned worktree. It initializes the worktree-local Claim Plane state and installs the Codex lifecycle connector before execution. The generated worker prompt carries the work-item goal, declared scope proposal, dependencies, preserves, and acceptance criteria, but the model still has to submit and admit a session-bound ChangeIntent before its first mutation.

The reservation transaction enforces:

- the exact graph, budget, concurrency-plan, and shared-admission fingerprints;
- current dynamic scheduler eligibility and successful effective dependencies;
- `max_active`, `max_active_per_work_item`, `max_total_launches`, and restart ceilings;
- a fair token reservation from the remaining session budget;
- a bounded timeout inside the remaining elapsed session execution budget.

The first reservation atomically changes the swarm session from `planned` to `running`. Graph and budget replacement therefore cannot race with execution.

Codex JSONL events, stderr, the final agent message, prompt digest, command, PIDs, thread identifier, observed intent identifier, token usage, duration, exit code, and termination classification are persisted under `.claim-plane/swarm/runs/` and in `swarm.db`. The local evidence namespace is created as private directories and rejects every symlink component before persistence. Token overruns, timeouts, cancellation, spawn failures, and non-zero exits are distinct terminal states.

A `succeeded` run means only that the bounded Codex process exited successfully. It may release execution dependencies in the scheduler, but it does not mean the work item or swarm is `VERIFIED`. Merge ordering, cross-intent verification, and final integration remain later lifecycle stages.

Codex JSONL currently exposes token usage but not authoritative provider cost. The run record therefore preserves the session cost ceiling and explicitly marks cost metering as unavailable instead of fabricating a dollar estimate.

## Deterministic merge queue

Version 0.22.0 materializes successful worker results on a dedicated Claim Plane-owned integration branch:

```bash
claim-plane swarm merge-plan <session-id>
claim-plane swarm merge-queue <session-id>
claim-plane swarm merge-next <session-id>
claim-plane swarm merge-all <session-id>
```

The queue is bound to the current graph, budget, shared admission, repository identity, and pinned session base. Its order follows effective dependencies and the deterministic work-graph topological order. Replanning or changing authority invalidates the queue instead of silently reusing an obsolete integration order.

A ready worker result is snapshotted from its Claim Plane-owned branch into a commit. Codex control files and `.claim-plane` state are excluded. The snapshot is applied to a managed integration worktree and committed in queue order. The configured integration target branch is metadata only at this stage and is never checked out, reset, or advanced.

If actual Git changes conflict despite planner-declared compatibility, the cherry-pick is aborted, the integration worktree is reset to the previous durable head, conflict paths are recorded, and the queue enters `conflict`. This makes the real repository result authoritative over the planner's concurrency estimate.

When a work item has effective dependencies, its clean managed worktree is advanced to the current integration head before Codex starts. The recorded execution base therefore contains integrated prerequisite changes. Dirty dependent worktrees and non-ancestor integration heads fail closed rather than losing local work.

Queue entry states are `pending`, `blocked`, `ready`, `integrating`, `integrated`, and `conflict`. Queue status is `waiting`, `ready`, `integrating`, `conflict`, or `completed`. `integrated` means only that Git composition succeeded on the managed branch; swarm verification and final target publication remain later stages.
