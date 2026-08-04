# Technical-preview CLI contract

The single-agent Codex path uses these stable command names during the `0.x` technical
preview:

```text
claim-plane init
claim-plane connect codex
claim-plane doctor
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

Verbose mode preserves raw Codex runtime diagnostics in addition to the human summary.
`--verbose` and `--json` are mutually exclusive. Redirected output is plain text, and
interactive colour can be disabled with the standard `NO_COLOR` environment variable.
