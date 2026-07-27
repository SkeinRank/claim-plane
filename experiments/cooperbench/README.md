# CooperBench research studies

This directory contains reproducible research infrastructure for Claim Plane evaluations on CooperBench. It is intentionally separate from `src/claim_plane`: installing the runtime library does not install model clients, planner prompts, benchmark datasets, or study runners.

The experiment code follows three rules:

1. Study inputs that affect execution are explicit and fingerprinted.
2. Run artifacts use a stable directory layout with atomic checkpoints for resume.
3. Secrets are never written into study declarations or run manifests.

The shared foundation is model-free. Planner v1, coding-agent execution, the published six-pair study, and the larger confirmatory study are layered on top of these primitives.

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

This layer does not call an LLM, download CooperBench, select pairs, or execute repository tasks. Those behaviors remain study-specific so the runtime package stays model-agnostic and the published and confirmatory protocols can be reviewed independently.
