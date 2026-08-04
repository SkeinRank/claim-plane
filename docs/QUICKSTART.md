# Five-minute Codex quickstart

This guide starts one feature-branch Codex task under Claim Plane authority and
produces a durable evidence report. Use a clean Git worktree with normal branch
protection and human review.

## 1. Install the isolated CLI

```bash
uv tool install claim-plane
# or
pipx install claim-plane

claim-plane --version
claim-plane preview
```

Python 3.10 or newer is required. Upgrade with the same tool used for installation:

```bash
uv tool upgrade claim-plane
# or
pipx upgrade claim-plane
```

## 2. Enroll a repository

```bash
cd my-project
git switch -c agent/pagination
claim-plane init
claim-plane connect codex
claim-plane doctor
```

`init` creates `.claim-plane/config.yaml` and private local state. Credentials are not
copied into Claim Plane. `connect codex` installs only Claim Plane-owned project hooks
and records the detected runtime and sandbox characteristics.

Review the generated acceptance commands before a guarded run:

```bash
cat .claim-plane/config.yaml
claim-plane policy inspect
claim-plane adapters inspect codex --policy guarded
```

## 3. Work interactively or run one bounded task

For normal conversational development, open Codex through Claim Plane:

```bash
claim-plane codex --policy guarded
```

Use the same Codex TUI as usual. When it exits, Claim Plane runs final acceptance,
verifies the admitted scope, and seals evidence.

For one unattended task:

```bash
claim-plane run \
  "Add pagination to the audit API and extend its tests" \
  --policy guarded \
  --timeout 1800
```

Both paths bind work to a versioned `ChangeIntent`. Supported undeclared mutations are
blocked, legitimate scope growth must be re-admitted, and the final Git state is
verified independently from the model's completion message.

Possible terminal results are `VERIFIED`, `REVIEW_REQUIRED`, `REJECTED`, `FAILED`,
`TIMED_OUT`, and `CANCELLED`. Inspect the stable process contract with:

```bash
claim-plane exit-codes
```

## 4. Inspect evidence

```bash
claim-plane report latest
claim-plane replay latest
claim-plane report latest --json --out claim-plane-evidence.json
```

The report contains digests, authority transitions, changed-file and hunk metadata,
policy findings, runtime identity, and verification results. It does not export the raw
task, source diff, credentials, tool payloads, or the final model message.

## 5. Review and commit

```bash
git status --short
git diff --stat
git diff
git add -A
git commit -m "feat: add audit API pagination"
```

A green Claim Plane result proves that the current evidence and final changes satisfied
the declared authority and configured checks. It does not replace architecture,
business-logic, security, or human code review.

## Reset or uninstall

Remove generated local state and Claim Plane-owned hooks while keeping the project config:

```bash
claim-plane reset
```

Remove the enrollment config as well:

```bash
claim-plane reset --remove-config
uv tool uninstall claim-plane
# or: pipx uninstall claim-plane
```

Repository files and unrelated Codex hooks are not removed.
