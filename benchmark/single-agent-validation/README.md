# Comparative single-agent validation

Claim Plane `0.37.1` provides one reproducible workflow for comparing the same
coding task under three execution arms:

```text
bare-codex
claim-plane-observe
claim-plane-guarded
```

The workflow freezes inputs before any provider call. Every plan cell binds the
feature prompt, repository base, model, arm, seed label, acceptance contract,
suite digest, and plan digest. Measured results are recorded only after the
candidate is evaluated by the task-local official evaluator.

## Preview workflow

The preview profile selects 12 feature-level tasks across at least six repository
families and creates 36 executions:

```bash
claim-plane validation init \
  --profile preview \
  --model gpt-5.6-luna

claim-plane validation status
```

Run one missing cell at a time:

```bash
claim-plane validation run --next
```

The runner prints the current cell, each phase, live evaluator output, and a heartbeat
when a dependency installation or test process is silent. Preview acceptance is bounded
to five minutes by default. Release validation keeps a twenty-minute default and both
profiles accept `--acceptance-timeout` overrides.

If acceptance is interrupted, the candidate remains in place and the cell is marked
resumable:

```bash
claim-plane validation status
claim-plane validation resume <execution-id>
```

`validation run --next` automatically resumes new interrupted acceptance states. A
candidate produced by the older runner is reported as `LEGACY_CANDIDATE`; restore its
known agent duration with `--agent-seconds` when preserving timing accuracy.

That command performs the complete cell lifecycle:

```text
prepare exact repository state
        ↓
open Bare / Observe / Guarded Codex
        ↓
run isolated official acceptance
        ↓
bind measured result to immutable plan cell
        ↓
show the next missing execution
```

The operator can prepare or collect a specific cell explicitly:

```bash
claim-plane validation prepare <execution-id>
claim-plane validation collect <execution-id>
```

## Reporting

```bash
claim-plane validation report
```

The report includes task success, accepted delivery, undeclared and missed
mutations, admitted amendments, recovered inspection blocks, wall time, known
token and cost fields, change size, public API drift, dependency drift, matrix
completeness, and the release gate decision.

Missing, duplicate, unexpected, and identity-mismatched cells make the matrix
incomplete. Claim Plane does not synthesize measurements or fill gaps.

## Evidence bundle

```bash
claim-plane validation bundle \
  --out claim-plane-single-agent-validation.zip
```

The archive contains:

```text
validation.json
selection.json
suite.json
plan.json
results/
summary.json
summary.md
gate.json
evidence/
bundle.json
```

Each listed file is hashed in `bundle.json`. The archive intentionally contains
control and evidence artifacts rather than full third-party repository copies.

## Release profile

The release profile selects 20 tasks across at least eight repository families
and creates two labeled execution replicates for each arm:

```bash
claim-plane validation init --profile release
```

This is substantially more expensive than the preview profile and should be used
for release evidence rather than routine local development.
