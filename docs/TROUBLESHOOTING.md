# Troubleshooting

## `claim-plane doctor` reports action required

Run the machine-readable form and inspect each remediation:

```bash
claim-plane doctor --json
claim-plane adapters doctor codex --json
claim-plane adapters inspect codex --policy guarded --json
```

Common causes are a missing Codex executable, unavailable authentication, a stale adapter
pin, an unsupported policy guarantee, a dirty worktree, or an acceptance command that is
not executable.

## The adapter pin is stale

Confirm that the detected runtime change is intentional, then recreate the pin:

```bash
claim-plane adapters pin codex --clear
claim-plane adapters pin codex
claim-plane doctor
```

Do not refresh a pin merely to bypass an unexplained runtime or provider change.

## The project config needs migration

```bash
claim-plane config status
claim-plane config migrate --dry-run
claim-plane config migrate
```

The migration preserves a sibling backup. Unknown config protocols are not guessed or
silently rewritten.

## A run returns `REVIEW_REQUIRED`

The task may have passed runtime and acceptance checks while touching a high-risk path,
using only post-verification guarantees, or requiring a human gate. Inspect:

```bash
claim-plane report latest
claim-plane replay latest
claim-plane policy classify <changed-path>
```

## A run returns `TIMED_OUT` or `CANCELLED`

Claim Plane stops the bounded process and abandons unfinished intent authority. Review the
worktree before starting another run. Increase `--timeout` only after confirming that the
previous process is gone and the remaining changes are understood.

## Evidence replay fails

Replay fails closed when the event order, causal chain, digest, session binding, or durable
run head no longer matches. Preserve the `.claim-plane/runs/<run-id>/` directory and open
a reproducibility issue with version, platform, run ID, and error digest. Do not include
credentials or private source code.

## Reset without deleting repository content

```bash
claim-plane reset
```

This removes Claim Plane-owned local state and hook handlers but preserves the versioned
config, repository content, and foreign Codex hooks. Use `--remove-config` only for a full
enrollment removal.
