# CooperBench research studies

This directory contains reproducible research infrastructure for Claim Plane evaluations on CooperBench. It is intentionally separate from `src/claim_plane`: installing the runtime library does not install model clients, planner prompts, benchmark datasets, or study runners.

The experiment code follows three rules:

1. Study inputs that affect execution are explicit and fingerprinted.
2. Run artifacts use a stable directory layout with atomic checkpoints for resume.
3. Secrets are never written into study declarations or run manifests.

The shared foundation is model-free. Planner v1 is preserved as a separate research module; coding-agent execution, the published six-pair study, and the larger confirmatory study are layered on top of these primitives.

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

The shared study foundation does not call an LLM, download CooperBench, select pairs, or execute repository tasks. Live model access exists only under `planner_v1`, and the runtime package remains model-agnostic. Coding-agent execution and complete study runners remain study-specific so the published and confirmatory protocols can be reviewed independently.
