# Single-agent golden task suite

This directory contains the public input format for the Codex dogfood study. Claim Plane does not ship measured outcomes or substitute illustrative values for missing executions.

The workflow is intentionally split into immutable inputs and measured outputs:

```text
candidate task catalog
        ↓ freeze
claim-plane.dogfood-suite.v1
        ↓ plan
all task × seed × arm executions
        ↓ external execution and evaluation
claim-plane.dogfood-result.v1 records
        ↓ aggregate
claim-plane.dogfood-summary.v1
        ↓ gate
PASSED / BLOCKED / INCOMPLETE
```

A release-grade suite must contain 20–30 tasks across 5–10 repositories, at least two coder seeds, at least three task classes, at least three risk classes, and full 40-character base commits. Every task carries its source reference, prompt digest, and acceptance commands. The frozen suite digest changes if a repository, task, seed, prompt, or acceptance contract changes.

The three arms are fixed:

```text
bare-codex
claim-plane-observe
claim-plane-guarded
```

Create a private candidate file from `candidate-suite.example.json`, replace all example repository and task values with reviewed real inputs, and freeze it before provider calls:

```bash
claim-plane dogfood freeze candidate.json \
  --release-grade \
  --out golden-suite.json

claim-plane dogfood validate golden-suite.json --release-grade
claim-plane dogfood plan golden-suite.json \
  --release-grade \
  --model <model> \
  --out run-plan.json
```

Execution infrastructure produces one measured evaluation for every plan entry. Bind it to the immutable plan identity before aggregation:

```bash
claim-plane dogfood record \
  run-plan.json <execution-id> evaluation.json \
  --out results/<execution-id>.json
```

The aggregator rejects duplicates, mismatched identities, unexpected executions, and missing cells rather than filling gaps:

```bash
claim-plane dogfood aggregate \
  golden-suite.json \
  run-plan.json \
  results/*.json \
  --release-grade \
  --out release-summary.json
```

The technical-preview gate compares Bare Codex with Claim Plane Guarded. It blocks when guarded mode reduces task success beyond the configured threshold without the required accepted-delivery improvement. Incomplete matrices never pass:

```bash
claim-plane dogfood gate release-summary.json
```

The summary records task success, accepted delivery, undeclared and missed mutations, amendments, false blocks, human repairs, retries, wall time, token/cost fields when available, files and lines changed, public API drift, and dependency drift.
