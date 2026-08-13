# CooperBench research studies

This directory contains reproducible research infrastructure for Claim Plane evaluations on CooperBench. It is intentionally separate from `src/claim_plane`: installing the runtime library does not install model clients, planner prompts, benchmark datasets, or study runners.

The experiment code follows three rules:

1. Study inputs that affect execution are explicit and fingerprinted.
2. Run artifacts use a stable directory layout with atomic checkpoints for resume.
3. Secrets are never written into study declarations or run manifests.

The shared foundation is model-free. Planner v1 is preserved as a separate research module. The published six-pair study and the published 30-pair × 3-seed confirmatory study are executable from this directory without Jupyter.

## Study declaration

A study JSON file freezes the Claim Plane version, planner policy identity, model identities, coder seeds, execution arms, and exact feature-pair order. The schema is `schemas/study.schema.json`.

Validate a declaration and print its deterministic fingerprint:

```bash
python -m experiments.cooperbench validate path/to/study.json
```

Create the artifact tree for one declared coder seed and shard:

```bash
python -m experiments.cooperbench init path/to/study.json \
  --seed 101 \
  --shard-index 1 \
  --shard-count 3
```

The default root is `.claim-plane/experiments`, which is ignored by Git.

## Planner v1

The planner used by the published CooperBench mechanism check is preserved as
`planner_v1`. It is research-only code and is not imported by the Claim Plane runtime.
Its model identity, prompts, retry budgets, source-localization rules, deterministic
uncertainty candidate generation, and final calibration constants are frozen under the
`planner-v1` policy identity.

Print the model and policy fingerprint without making a network call:

```bash
python -m experiments.cooperbench planner policy
```

Inspect the exact current-source context shown to the planner:

```bash
python -m experiments.cooperbench planner context \
  --tree /path/to/worktree \
  --feature-dir /path/to/CooperBench/dataset/repo_task/task123/feature1
```

Run the primary planner and the final uncertainty calibration:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench planner run \
  --tree /path/to/worktree \
  --feature-dir /path/to/feature1 \
  --seed 101 \
  --output plan.json
```

The planner follows the oracle-localized context condition disclosed in the paper:
gold feature data identifies relevant current-source regions, while the model receives
the current repository contents rather than the gold implementation. The calibration
step can only select bounded candidates produced by deterministic repository analysis;
it cannot invent additional paths or ranges.

## Published six-pair study

`paper_6pair/` contains the CLI-oriented reproduction of the mechanism check reported
in Section 8 of the Claim Plane preprint. The pair order, conflict labels, models,
seeds, execution limits, four arms, planner policy, and published mechanism counts
are checked into the repository. The coding-agent executor is extracted from the
V8.5 research harness; Jupyter is not required.

Validate that a local CooperBench checkout contains the exact frozen inputs without
making any model call:

```bash
python -m experiments.cooperbench paper6 prepare \
  --cooperbench /path/to/CooperBench
```

Inspect the frozen study and the mechanism counts reported in the paper:

```bash
python -m experiments.cooperbench paper6 info
```

Run the complete study:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench reproduce-paper \
  --cooperbench /path/to/CooperBench
```

The command performs CooperBench gold-feature sanity checks before paid model calls,
then executes the six frozen pairs under `parallel`, `claim-plane-static`,
`claim-plane-dynamic`, and `always-serial`. Static and Dynamic Claim Plane reuse the
exact same persisted Planner v1 outputs, including across resumed processes.

Long-running executions report live progress to stderr while the final JSON result stays
on stdout. The display starts at the durable checkpoint, shows the current pair and arm,
elapsed time, percentage complete, and an ETA once enough execution timing is available.
ETA estimates learn separate arm durations as the run progresses. Completed execution
artifacts also retain `wall_time_seconds`, so a resumed run can reuse prior timing data.
Set `CLAIM_PLANE_PROGRESS=0` to suppress progress output in automation.

Outputs are written under the canonical artifact tree. In addition to per-unit results
and full agent traces, the completed run contains `results.json`, `summary.json`,
`summary.csv`, provider accounting, and `reference_comparison.json`. Live model APIs
may change behavior over time even with frozen seeds, so the published counts are a
regression reference rather than a promise of byte-identical future generations.

The original study used oracle-localized initial context: CooperBench gold patches
identify relevant current-source regions, while neither the gold implementation nor
gold replacement text is shown to the planner or coder. API calls are physically
sequential; the parallel arm represents logical topology in which both workers start
from the same immutable base.

## Published confirmatory 30-pair × 3-seed study

`confirmatory_30x3/` converts the V9 notebook protocol into a resumable CLI workflow.
The protocol is deliberately staged so pair selection, planning, and coding variance are
separate artifacts rather than notebook state.

Inspect the fixed dimensions without touching CooperBench or a model provider:

```bash
python -m experiments.cooperbench confirmatory info
```

Gold-validate candidates and freeze exactly 15 conflict plus 15 clean pairs:

```bash
python -m experiments.cooperbench confirmatory prepare \
  --cooperbench /path/to/CooperBench
```

Freeze Planner v1 once. The planner seed for each pair/agent is derived from the V9
`planner-freeze` identity and is independent of coder seed. The command reports live
progress for all 60 feature declarations, including the current pair/feature, elapsed
time, ETA, and cost. Each completed declaration is persisted atomically, so rerunning
the same command resumes from the last durable feature after interruption:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory freeze-plans \
  --cooperbench /path/to/CooperBench
```

Execute one of the nine 10-pair shards:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory run \
  --cooperbench /path/to/CooperBench \
  --seed 101 \
  --shard 1
```

Static and Dynamic Claim Plane consume the same frozen declarations. Seeds `101`, `202`,
and `303` change only coding execution. Each shard has an independent atomic checkpoint,
so process restarts do not regenerate plans or rerun completed arm executions.

Check completion at any time:

```bash
python -m experiments.cooperbench confirmatory status
```

After all nine shards report complete, create the final analysis artifacts:

```bash
python -m experiments.cooperbench confirmatory aggregate
python -m experiments.cooperbench confirmatory verify-analysis
```

`aggregate` refuses partial matrices. It verifies the expected 30 pairs × 3 coder seeds ×
4 arms, shard identity, frozen study fingerprint, and per-shard protocol provenance before
analysis. The default statistical output uses 5,000 deterministic nonparametric bootstrap
samples clustered by CooperBench repository/task identity. It reports arm estimates and
paired deltas without introducing a scientific Python dependency into the runtime package.

Final outputs are written under the study fingerprint in `analysis/` and include:

```text
arm_results.json / arm_results.csv
arm_summary.json / arm_summary.csv
feature_pair_summary.json / feature_pair_summary.csv
task_cluster_summary.json / task_cluster_summary.csv
bootstrap_ci.json / bootstrap_ci.csv
failure_taxonomy.json / failure_taxonomy.csv
mechanism_summary.json / mechanism_summary.csv
cost_summary.json
publication_manifest.json
```

Planner cost is reported once from the frozen plan set rather than multiplied across coder
seeds or Claim Plane arms. `publication_manifest.json` records SHA-256 digests for every
final analysis payload; `verify-analysis` checks those digests offline.

The protocol source artifacts live under
`.claim-plane/experiments/claim-plane-confirmatory-30x3/protocol/`. Once created, the
30-pair study declaration is immutable for that artifact root.

## Physical Parallel Benchmark v2

The frozen confirmatory workload can also be executed with physical concurrency
instrumentation without changing its pair selection, coder seeds, plans, or arm names.
The execution layer distinguishes two independent measurements:

- **inner overlap**: the measured wall-clock intersection of agent A and agent B inside one
  pair when the arm admits concurrent execution;
- **outer concurrency**: a bounded pool of independent pair subprocesses used only to
  reduce experiment turnaround time.

Run six pairs concurrently with a hard six-process ceiling:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory physical-run \
  --cooperbench /path/to/CooperBench \
  --seed 101 \
  --pairs 1-6 \
  --max-parallel-pairs 6
```

Each pair subprocess receives its own repository cache and worktree root. This avoids the
unsafe pattern where concurrent benchmark processes reset or check out the same mutable Git
clone. Parent-observed process intervals are persisted separately from per-arm agent
intervals. Outer throughput is never interpreted as Claim Plane speedup.

`parallel`, statically admitted Claim Plane work, and dynamically admitted Claim Plane work
can now launch their two workers simultaneously. If Dynamic scope expansion rejects one or
both optimistic workers, the harness records the wasted parallel attempt and falls back to
a deterministic serial order. Serialized arms remain sequential by construction.

The physical execution artifacts are written under
`.claim-plane/experiments/physical-parallel-v2/` and use the
`claim-plane.physical-parallel-benchmark.v2` protocol. The historical published results are
left untouched; fresh measurements are required before any wall-clock speedup claim.

## Deterministic ablation study

The frozen confirmatory workload can be replayed under named deterministic admission
configurations without changing the selected pairs, coder seeds, Planner v1 outputs, or
physical timing instrumentation. The default study compares:

- `full_v2`: structural symbols plus the complete semantic dependency graph;
- `file_region_baseline`: file and declared line-region evidence only;
- `symbols_without_dependencies`: structural symbol identities with dependency edges removed;
- `no_contract_propagation`: implementation-level dependencies remain while broad
  import/write/inheritance/type/public-API propagation is removed.

Run six independent pairs concurrently while each pair evaluates all four configurations:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory ablation-run \
  --cooperbench /path/to/CooperBench \
  --seed 101 \
  --pairs 1-6 \
  --max-parallel-pairs 6
```

Artifacts are written under `.claim-plane/experiments/deterministic-ablation-v1/`. Each
profile records the source-bound semantic graph fingerprint, deterministic execution waves,
serialization/order decision, pair outcome, measured inner overlap, and wall-clock delta
against `full_v2`. Outer pair concurrency remains a harness optimization and is never counted
as Claim Plane speedup.

This study isolates initial deterministic admission. Runtime amendment and stale-worker
recovery are evaluated by the deterministic conformance/stress workload rather than being
silently attributed to frozen pairs that do not exercise those events.

## Deterministic v2 confirmatory experiment

The final fresh experiment keeps the published 30-pair set, coder seeds, Planner v1 outputs,
and benchmark revision fixed while comparing four modes:

- `naive_parallel`: uncoordinated physical A/B execution;
- `legacy_static`: the historical conservative static Claim Plane gate;
- `deterministic_v2`: full semantic deterministic admission;
- `always_serial`: the reliability and wall-clock baseline.

Run the complete 90 pair/seed units through a bounded six-process outer pool:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory final-run \
  --cooperbench /path/to/CooperBench \
  --seeds 101,202,303 \
  --pairs 1-30 \
  --max-parallel-pairs 6
```

The outer pool only reduces experiment turnaround. Each pair/seed subprocess executes its
four modes in a deterministic counterbalanced order, and the scientific report computes
paired wall-clock speedup only against the same unit's always-serial run. This keeps harness
fan-out separate from the Claim Plane result.

Offline progress and strict aggregation are available with:

```bash
python -m experiments.cooperbench confirmatory final-status
python -m experiments.cooperbench confirmatory final-aggregate
```

Artifacts live under `.claim-plane/experiments/deterministic-confirmatory-v2/`. Final
aggregation requires all 30 pairs × 3 coder seeds × 4 modes (360 rows), then seals
`analysis/final-report.json` and a SHA-256 manifest. The protocol records pair pass,
integration success, serialization, observed physical concurrency, inner overlap, paired
speedup versus serial, coder cost, and the direct deterministic-v2 delta from the historical
static admission. No result is predeclared by the runner.

## SCIP ablation and Physical Parallel Benchmark v3

The next experiment keeps the same frozen pair set, coder seeds, Planner v1 outputs,
and physical timing instrumentation while isolating the code-intelligence path. Five
paired profiles are available:

- `serial`: always-serial reliability and wall-clock baseline;
- `naive_parallel`: uncoordinated physical A/B execution;
- `builtin_graph`: deterministic builtin semantic graph with candidate blocking disabled;
- `scip_graph_cold`: required fresh SCIP index merged with the builtin graph, with candidate
  blocking disabled;
- `scip_cache_blocking`: the same revision reusing builtin/SCIP caches with
  affected-subgraph candidate blocking enabled.

Run the initial six-pair check:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory scip-v3-run \
  --cooperbench /path/to/CooperBench \
  --seeds 101 \
  --pairs 1-6 \
  --max-parallel-pairs 6
```

A working `scip-python` executable is required for the two SCIP profiles. Those profiles
fail closed if SCIP is unavailable, the checkout does not exactly match the frozen base
revision, or provider evidence is stale; the harness never records a builtin fallback as
a SCIP result. The cold profile clears an isolated per-pair code-intelligence cache and
forces indexing. The warm profile then reuses that exact revision cache.

Each profile records pair pass, integration success, serialization, observed physical
concurrency, measured mean active agents, critical-path time, execution wall time,
control-plane wall time, and end-to-end wall time. Reports include paired execution-only
and end-to-end speedup versus the same pair/seed unit's serial run. SCIP indexing, decode,
graph merge, admission, and cache-hit costs remain separate so indexing overhead cannot be
hidden inside a speedup claim. Outer pair-process concurrency is reported only as harness
throughput.

After the six-pair check, the same protocol can execute the complete frozen matrix:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory scip-v3-run \
  --cooperbench /path/to/CooperBench \
  --seeds 101,202,303 \
  --pairs 1-30 \
  --max-parallel-pairs 6

python -m experiments.cooperbench confirmatory scip-v3-status
python -m experiments.cooperbench confirmatory scip-v3-aggregate
```

Artifacts live under `.claim-plane/experiments/scip-ablation-physical-v3/` and use the
`claim-plane.scip-ablation-physical-benchmark.v3` protocol. Aggregation never predeclares
a target speedup; it seals only observed results.

## Pinned Linux environment

A Docker image is provided for runs that should not depend on the host Python toolchain.
Its environment lock fixes Python 3.12.11, `uv` 0.11.29, Node 20.19.4,
`@sourcegraph/scip-python` 0.6.6, UTC, `C.UTF-8`, and the benchmark Git identity. Docker remains optional; none of these dependencies are imported
by the installable Claim Plane runtime.

Build and inspect the image:

```bash
./scripts/cooperbench-docker.sh build
./scripts/cooperbench-docker.sh environment
```

Validate a CooperBench checkout through the same container without a model call:

```bash
./scripts/cooperbench-docker.sh prepare /absolute/path/to/CooperBench
```

Run or resume the published study:

```bash
OPENROUTER_API_KEY=... \
  ./scripts/cooperbench-docker.sh reproduce /absolute/path/to/CooperBench
```

The same image can reproduce the published confirmatory protocol:

```bash
./scripts/cooperbench-docker.sh confirmatory-prepare /absolute/path/to/CooperBench
OPENROUTER_API_KEY=... \
  ./scripts/cooperbench-docker.sh confirmatory-freeze /absolute/path/to/CooperBench
OPENROUTER_API_KEY=... \
  ./scripts/cooperbench-docker.sh confirmatory-run /absolute/path/to/CooperBench \
  --seed 101 --shard 1
./scripts/cooperbench-docker.sh confirmatory-status
./scripts/cooperbench-docker.sh confirmatory-aggregate
./scripts/cooperbench-docker.sh confirmatory-verify-analysis
```

The checkout is mounted read-only. Persistent artifacts, cloned task repositories, and
worktrees live under `.claim-plane/docker-research/` by default. The API key is passed
only as process environment and is never written by the research manifest helpers.

The historical V8.5 notebook installed `uv` without a version constraint and cloned the
then-current CooperBench default branch at depth one. Because that checkout revision was
not recorded by the notebook, the current runner does not invent a historical commit.
Instead, `paper6 prepare` validates the exact frozen pair inputs, reports the supplied
checkout revision when available, and computes a stable digest over the benchmark files
that define those six pairs.

## Artifact layout

```text
.claim-plane/experiments/
  <study-id>/
    <study-fingerprint>/
      study.json
      pairs.json
      runs/
        <run-id>/
          manifest.json
          benchmark.json
          environment.json
          checkpoint.json
          declarations/
          plans/
          results/
          traces/
          logs/
      analysis/
        arm_results.json
        arm_summary.json
        feature_pair_summary.json
        task_cluster_summary.json
        bootstrap_ci.json
        failure_taxonomy.json
        mechanism_summary.json
        cost_summary.json
        publication_manifest.json
```

`study.json` and `pairs.json` are immutable inputs for a study fingerprint. `manifest.json` records non-secret execution provenance, including the installed Claim Plane version, Python/platform information, repository commit when available, and only the names of explicitly requested environment variables. The published study also writes immutable `benchmark.json` provenance containing the mounted CooperBench revision when Git metadata is available and the frozen dataset digest. `environment.json` records the pinned research lock plus non-secret runtime diagnostics, including the container build commit when available. `checkpoint.json` is replaced atomically so interrupted executions can resume from the last durable unit. A completed confirmatory study adds deterministic JSON/CSV analysis payloads and a hash manifest under `analysis/`.

## Scope

The shared study foundation does not call an LLM or download CooperBench. Live model access exists only in research modules, and the runtime package remains model-agnostic. Both published studies keep protocol-specific selection and execution policy outside `src/claim_plane` while sharing the same deterministic runtime mechanisms.
