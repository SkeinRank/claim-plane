# Technical-preview CLI contract

The single-agent Codex path uses these stable command names during the `0.x` technical
preview:

```text
claim-plane init
claim-plane connect codex
claim-plane doctor
claim-plane codex
claim-plane run
claim-plane report
claim-plane replay
claim-plane policy inspect
claim-plane adapters inspect
claim-plane adapters conformance
claim-plane config status
claim-plane config migrate
claim-plane schemas list
claim-plane schemas export
claim-plane reset
```

Use `claim-plane preview --json` to inspect the installed preview manifest and
`claim-plane exit-codes --json` for the machine-readable process contract.

## Public exit codes

| Code | Name | Meaning |
|---:|---|---|
| `0` | `ok` | The command completed and the requested verification passed. |
| `1` | `error` | Invalid input, configuration, compatibility, or an unexpected execution error. |
| `2` | `action_required` | Valid result requiring review, remediation, or a stronger runtime boundary. |
| `3` | `incomplete` | Required measured input or evidence is missing, or delivery was rejected before a passing claim. |
| `4` | `blocked` | A deterministic policy, verification, or release gate blocked the outcome. |
| `124` | `timed_out` | A bounded operation exceeded its wall-time limit and authority was revoked. |
| `130` | `cancelled` | The user cancelled a bounded operation and unfinished authority was revoked. |

Older advanced and research commands retain their documented command-specific `0`, `1`,
and `2` behavior. Automation for the product path should consume JSON output and the
stable codes above.

## Configuration lifecycle

```bash
claim-plane config status
claim-plane config migrate --dry-run
claim-plane config migrate
```

A supported migration is atomic. Claim Plane writes a sibling backup before replacing
the config and refuses to overwrite a different existing backup. Unknown future
protocols fail closed.

## Public schemas

Every wheel contains the exact public JSON Schemas used by the matching release:

```bash
claim-plane schemas list
claim-plane schemas export ./claim-plane-schemas
```

The exported files include SHA-256 identities in `schemas list` output and can be pinned
alongside CI tooling.

## Interactive Codex TUI

`claim-plane codex` opens the normal interactive Codex interface while Claim Plane
owns the authority and evidence boundary:

```bash
claim-plane codex --policy guarded
```

The user can converse with Codex normally. Project hooks still block undeclared
writes and broker justified scope amendments. At each agent-turn boundary the TUI
shows `AGENT TURN COMPLETED` and explicitly keeps final verification pending. After
the TUI exits, Claim Plane runs the trusted final verifier, seals session lifecycle,
and writes `.claim-plane/runs/<run-id>/run.json`.

An initial prompt and guided scope are optional:

```bash
claim-plane codex "Fix timeout handling and update tests" \
  --scope src/connectors/github.py \
  --model gpt-5.6-luna
```

Available launcher controls are `--repo`, `--policy`, `--model`, `--scope`,
`--lock-scope`, `--timeout`, `--acceptance-timeout`, and `--out`. Sandbox mode,
approval policy, and working directory remain Claim Plane-owned so the interactive
session cannot silently weaken its control boundary. Use `claim-plane run` for
non-interactive automation and machine-only JSON output.

## Controlled-run terminal modes

The normal `claim-plane run` view is designed for human review. It prints a compact
header, one line per meaningful lifecycle transition, a final verification card, and
the durable evidence path. Raw Codex stderr is not streamed by default. Known policy
blocks are rendered as concise, deduplicated notices.

```bash
claim-plane run "Implement the task" --policy guarded
```

Use verbose mode for adapter or runtime debugging:

```bash
claim-plane run "Implement the task" --policy guarded --verbose
```

Normal runs keep scope automatic: the planner proposes the initial ChangeIntent and
Claim Plane enforces it. For reproducible experiments or operator-guided work, provide
one or more initial mutation paths:

```bash
claim-plane run "Fix timeout handling and update tests" \
  --scope src/connectors/github.py \
  --policy guarded
```

A required write outside explicit initial scope is denied first and may then receive
a one-time exact-resource amendment ticket. Use `--lock-scope` to disable amendments
entirely; it requires at least one `--scope` path. Directories are recursive when they
already exist or when written with a trailing slash. An explicit request to add or update test coverage is recorded as a structured completion obligation without retaining the prompt text. A run is rejected when that obligation remains unsatisfied, even when scope and configured acceptance are otherwise clean.

Verbose mode preserves raw Codex runtime diagnostics in addition to the human summary.
`--verbose` and `--json` are mutually exclusive. Redirected output is plain text, and
interactive colour can be disabled with the standard `NO_COLOR` environment variable.

## Runtime premise fences and recovery

Advanced coordination workflows can inspect durable execution fences created when a tracked premise becomes stale:

```bash
claim-plane --db .claim-plane/plane.db runtime-fences
claim-plane --db .claim-plane/plane.db runtime-fences <intent-id>
```

A fence means governed mutation authority has been revoked. Operators may also pause one admitted or active intent explicitly:

```bash
claim-plane --db .claim-plane/plane.db runtime-pause <intent-id> \
  --reason ordered_dependency \
  --resource-key project.contract
```

A stale worker is recovered in two explicit transitions. First provide a refreshed ChangeIntent with the same declared authority surface and a new pinned `base_commit`; the stale-causing producer must already be completed. Then resume the refreshed intent:

```bash
claim-plane --db .claim-plane/plane.db runtime-refresh refreshed-intent.json \
  --expected-version 4
claim-plane --db .claim-plane/plane.db runtime-resume <intent-id>
claim-plane --db .claim-plane/plane.db runtime-recoveries <intent-id>
```

Refresh re-evaluates admission and dependencies but cannot expand operations, acceptance, preserves, or dependencies. Ordinary activation and broker registration remain blocked while a successful refresh is waiting for `runtime-resume`. Any broker started after resume receives a fresh fencing token.
