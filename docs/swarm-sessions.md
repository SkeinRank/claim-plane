# Swarm sessions and work graphs

Claim Plane represents a planned multi-agent change as a repository-bound `SwarmSession` with a versioned, deterministic work graph.

A planner may propose decomposition, candidate scope, and dependencies. Claim Plane assigns the session identity, pins the exact Git base, binds the session to one repository identity, validates the dependency graph, and persists the result through an atomic local SQLite transaction.

This boundary keeps planning probabilistic while making the execution contract stable enough for later budget, scheduling, worktree, runner, admission, and integration layers.

## Session model

A swarm session contains:

- one root task and root acceptance criteria;
- an immutable repository identity and exact base commit;
- the branch observed when the session was created;
- an integration target;
- one versioned work graph;
- deterministic graph topology and fingerprint;
- lifecycle state reserved for the complete swarm runtime.

The planner does not choose worker process identifiers, worktrees, leases, or mutation capabilities. Those belong to later execution stages.

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

Dependency layers are structural planning information only. They are not an execution schedule. Budget policy, overlap analysis, and adaptive concurrency will determine the actual execution waves.

## Create a session

Initialize the repository once:

```bash
claim-plane init
```

Validate a proposed graph:

```bash
claim-plane swarm validate \
  --graph examples/swarm/session-spec.json
```

`swarm validate` expects the work-graph object itself. To create a complete repository-bound session, use the session spec:

```bash
claim-plane swarm create \
  --spec examples/swarm/session-spec.json
```

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
```

Machine-readable output is available through `--json` or `--out` where supported.

## Replace a planned graph

A planner may refine a graph before execution. Replacements use optimistic version checking:

```bash
claim-plane swarm replace-graph <session-id> \
  --graph revised-work-graph.json \
  --expected-version 1
```

The update succeeds only when the stored graph still has the expected version. The base commit, repository identity, root task, session identity, and integration target remain unchanged.

## Current boundary

Version 0.16.0 creates and validates durable planning state. It does not launch workers, allocate budget, create worktrees, or schedule execution. Those capabilities build on the session and graph contracts introduced here.
