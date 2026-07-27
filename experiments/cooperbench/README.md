# CooperBench research studies

This directory contains reproducible research infrastructure for Claim Plane evaluations on CooperBench. It is intentionally separate from `src/claim_plane`: installing the runtime library does not install model clients, planner prompts, benchmark datasets, or study runners.

The experiment code follows three rules:

1. Study inputs that affect execution are explicit and fingerprinted.
2. Run artifacts use a stable directory layout with atomic checkpoints for resume.
3. Secrets are never written into study declarations or run manifests.

The shared foundation is model-free. Planner v1 is preserved as a separate research module. The published six-pair study is executable from this directory; the larger confirmatory study remains a separate frozen protocol.

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
          checkpoint.json
          declarations/
          plans/
          results/
          traces/
          logs/
```

`study.json` and `pairs.json` are immutable inputs for a study fingerprint. `manifest.json` records non-secret execution provenance, including the installed Claim Plane version, Python/platform information, repository commit when available, and only the names of explicitly requested environment variables. `checkpoint.json` is replaced atomically so interrupted executions can resume from the last durable unit.

## Scope

The shared study foundation does not call an LLM or download CooperBench. Live model access exists only in research modules, and the runtime package remains model-agnostic. The six-pair runner is intentionally study-specific so its published protocol can be reviewed independently from the future confirmatory study.
