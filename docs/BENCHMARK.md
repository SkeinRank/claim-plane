# Benchmark design

Claim Plane should be judged by safe integration throughput, not by the number of agents it can start.

## Compared workflows

### A. Worktree-only

A planner splits tasks, workers run in isolated worktrees, and integration begins after all workers finish.

### B. Planner coordination

The planner also records dependencies and sends natural-language updates, but there is no deterministic admission or intent-to-diff verifier.

### C. Claim Plane

The same planner emits `ChangeIntent` objects. Claim Plane performs semantic admission, bounded context delivery, acyclic dependency admission, transitive invalidation, neutral patch integration, continuous verification, and targeted repair.

The same task decomposition input, models, temperatures, tools, and repository revisions must be used across arms.

## Required task families

- different names for one concept;
- incompatible signatures for one concept;
- producer and consumer against a contract stub;
- code and documentation for one concept;
- unexpected expansion into a shared file;
- disjoint bounded edits in one file;
- schema or configuration changes with dependents;
- producer amendment that invalidates one resource-scoped branch and then propagates transitively;
- dependency-cycle proposals that must be rejected before execution;
- neutral patch composition with an integrated-only failure;
- rename or delete with downstream consumers;
- tasks where serialization is faster than parallelism.

## Primary metrics

- wall-clock time to clean integration;
- total input and output tokens through final repair;
- clean-first-integration rate;
- accepted changes per repository per day;
- human intervention minutes;
- unsafe admission rate;
- false blocking rate.

## Secondary metrics

- failed CI cycles;
- full retries versus targeted repairs;
- duplicate implementation rate;
- contract mismatch rate;
- undeclared paths and out-of-bounds hunks;
- stale premise detection and transitive propagation latency;
- dependency-cycle rejection accuracy;
- neutral merge failure rate;
- planner, verifier, and repair token share;
- cost by model tier and role.

## Honesty requirements

- No arm receives hidden canonical names or human labels unavailable to the others.
- The planner generates intents from the same task and repository context used by baselines.
- Real branches, worktrees, merges, tests, type checks, and repair loops run.
- All planner, coordination, verifier, and repair model calls count toward cost.
- A run ends only at a passing integrated revision or a declared failure budget.
- Results are reported by task family; averages must not hide cases where parallelism hurts.

## Minimum credible pilot

A useful initial pilot is 20–30 tasks across three active repositories, at least three repeats, and at least two worker tiers. The claim to validate is:

> Concept-bound semantic admission, live dependency invalidation, and hunk-aware verification improve total cost or time to clean integration for partially overlapping tasks without reducing correctness.

## Included A/B/C harness

`benchmark/run_abc_harness.py` executes external adapters for all three arms and records wall time, return code, clean status, tokens, cost, and human minutes. The adapters remain external so every arm can use the same model provider and repository setup.

```bash
python benchmark/run_abc_harness.py benchmark/abc-spec.example.json \
  --out benchmark-results.json
```

Each adapter should print a final one-line JSON object, for example:

```json
{"clean": true, "input_tokens": 12000, "output_tokens": 2400, "cost_usd": 0.31, "human_minutes": 0}
```

The harness is measurement infrastructure, not benchmark evidence by itself. Real claims still require controlled tasks, repeated runs, and identical model/tool conditions across arms.
## Reproducible CooperBench study infrastructure

Research code that depends on benchmark datasets or model providers lives under
`experiments/cooperbench/`, outside the installable runtime package. The shared layer is
model-free and defines frozen study inputs, deterministic fingerprints, stable run and
shard identities, non-secret provenance manifests, and atomic checkpoints.

A study declaration can be validated before any paid model call:

```bash
python -m experiments.cooperbench validate path/to/study.json
```

Long executions should initialize their artifact directory before work begins and resume
from the durable checkpoint rather than relying on notebook state. Study-specific planner
code and task execution are added above this foundation and must preserve identical inputs
across compared arms.

### Published six-pair CooperBench mechanism check

The Section 8 mechanism check is executable without Jupyter. Its exact pair order,
conflict labels, models, coder seed, four execution arms, Planner v1 policy, and execution
limits are frozen under `experiments/cooperbench/paper_6pair/`.

Validate a local CooperBench checkout before any paid model call:

```bash
python -m experiments.cooperbench paper6 prepare \
  --cooperbench /path/to/CooperBench
```

Inspect the frozen protocol and published reference counts:

```bash
python -m experiments.cooperbench paper6 info
```

Run or resume the study:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench reproduce-paper \
  --cooperbench /path/to/CooperBench
```

The static and dynamic Claim Plane arms consume the same persisted Planner v1 output for
each feature pair, including after process resume. The runner performs CooperBench gold
sanity checks before live model execution and persists declarations, plans, traces,
per-arm results, aggregate summaries, provider accounting, and a comparison against the
published mechanism counts.

The study declaration records Claim Plane `0.2.1`, the version used by the published run.
The run manifest separately records the installed package version used for a reproduction,
so historical protocol identity is not confused with the current implementation version.
The disclosed oracle-localized context condition is retained: gold patch locations select
current-source regions, but gold implementation text is never provided to the planner or
coder. API calls are physically sequential; the parallel arm is a logical execution
topology in which both workers begin from the same immutable base.

### Pinned Linux runner

For environment-controlled reproduction, build the research image from the repository
root and mount CooperBench read-only:

```bash
./scripts/cooperbench-docker.sh build
./scripts/cooperbench-docker.sh prepare /absolute/path/to/CooperBench
OPENROUTER_API_KEY=... \
  ./scripts/cooperbench-docker.sh reproduce /absolute/path/to/CooperBench
```

The image fixes the research Python and `uv` versions while keeping Docker outside the
Claim Plane runtime dependency graph. Task repositories are still checked out at the exact
base commits declared by CooperBench, and task-local test scripts remain authoritative for
their own dependency setup. Every paper run persists the supplied CooperBench Git revision
when available plus a digest over the frozen benchmark inputs used by the study.
