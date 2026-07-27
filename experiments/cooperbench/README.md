# CooperBench research studies

This directory contains reproducible research infrastructure for Claim Plane evaluations on CooperBench. It is intentionally separate from `src/claim_plane`: installing the runtime library does not install model clients, planner prompts, benchmark datasets, or study runners.

The experiment code follows three rules:

1. Study inputs that affect execution are explicit and fingerprinted.
2. Run artifacts use a stable directory layout with atomic checkpoints for resume.
3. Secrets are never written into study declarations or run manifests.

The shared foundation is model-free. Planner v1 is preserved as a separate research module. The published six-pair study and the frozen-plan 30-pair × 3-seed study are executable from this directory without Jupyter.

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

## Frozen-plan 30-pair × 3-seed study

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
`planner-freeze` identity and is independent of coder seed:

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

## Pinned Linux environment

A Docker image is provided for runs that should not depend on the host Python toolchain.
Its environment lock fixes Python 3.12.11, `uv` 0.11.29, UTC, `C.UTF-8`, and the
benchmark Git identity. Docker remains optional; none of these dependencies are imported
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

The same image can freeze and execute the confirmatory protocol:

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

The shared study foundation does not call an LLM or download CooperBench. Live model access exists only in research modules, and the runtime package remains model-agnostic. The published study and confirmatory study keep protocol-specific selection and execution policy outside `src/claim_plane` while sharing the same deterministic runtime mechanisms.
