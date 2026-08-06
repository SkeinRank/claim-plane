# Upgrading and uninstalling

Install and upgrade Claim Plane with one isolated tool manager. Do not mix `uv tool`,
`pipx`, and an editable checkout for the same executable on `PATH`.

## Upgrade

```bash
uv tool upgrade claim-plane
# or
pipx upgrade claim-plane

claim-plane --version
claim-plane preview
claim-plane config status
claim-plane doctor
```

When a supported project-config migration is available, inspect it before writing:

```bash
claim-plane config migrate --dry-run
claim-plane config migrate
```

The migration writes a sibling backup before atomically replacing the config. Claim Plane
refuses unknown future protocols and will not overwrite a different existing backup.
`claim-plane init` also performs supported migrations during an explicit re-enrollment.

Adapter runtime upgrades may invalidate a project pin. Review the detected change before
recreating it:

```bash
claim-plane adapters doctor codex
claim-plane adapters pin codex --clear
claim-plane adapters pin codex
```

## Pre-collection optional dependency bootstrap from 0.37.9

Version `0.37.9` installs private-test optional dependencies before pytest imports the
repository's initial `conftest.py` files. This matters for projects that cache values
such as `PIL_AVAILABLE` during import: installing Pillow later in `pytest_configure`
would leave those cached values stale and still skip the hidden test.

Candidates preserved as `EVALUATOR_INCOMPLETE` by `0.37.8` do not need another Codex
run. Upgrade Claim Plane and resume the existing execution:

```bash
claim-plane validation status
claim-plane validation resume <execution-id>
```

The runner prints `Resuming preserved candidate` with the task, arm, and execution ID
before starting external acceptance. A verified hidden test is then recorded with
`executed: 1`, `passed: 1`, `skipped: 0`, and `state: VERIFIED`.

## Fail-closed acceptance witnesses from 0.37.8

Version `0.37.8` no longer accepts a zero evaluator exit code by itself. Private Python
tests added by the frozen acceptance input must be collected, executed, and passed under
the external pytest witness. Skipped or missing hidden tests are classified as
`EVALUATOR_INCOMPLETE` and the candidate remains resumable.

Results that reported `PASS` while task-specific tests were skipped are diagnostic only.
Preserve the old bundle, reset that task across all three arms, and repeat it:

```bash
claim-plane validation bundle --out claim-plane-pre-witness-diagnostic.zip
claim-plane validation reset-task <task-id>
claim-plane validation prefetch --next
claim-plane validation run --next
```

The environment identity now includes explicit optional test prerequisites. Affected
environments rebuild once from the existing UV cache; unrelated validation evidence is
left unchanged.

## Benchmark isolation from 0.37.6

Version `0.37.6` moves frozen evaluator programs and hidden acceptance inputs out of
the validation workspace tree, removes legacy source paths from agent-visible
manifests, disables Codex web search and shell networking for comparative cells, and
uses the prepared `python` command instead of an absolute host interpreter.

Results produced after an agent could inspect reference artifacts are diagnostic only.
Reset the affected task and repeat all three arms:

```bash
claim-plane validation reset-task <task-id>
claim-plane validation status
claim-plane validation run --next
```

The first run migrates the existing validation root into the private evaluator vault.
Prepared dependency environments and the UV download cache remain reusable.


## macOS prepared-Python isolation from 0.37.5

Version `0.37.5` fixes a macOS/pyenv edge case where a task environment contained
pytest and project dependencies, but an inherited Python launcher redirected the
preflight process to the parent interpreter's package paths. The environment manifest
protocol is refreshed once; packages are rebuilt from the preserved UV download cache.

Reset only the affected task result and rerun it:

```bash
claim-plane validation reset-task <task-id>
claim-plane validation status
claim-plane validation run --next
```

The run must print the prepared Python path and targeted-test imports before opening
Codex. A separate `validation prefetch` is not required.

## Comparative development environments from 0.37.3

Version `0.37.4` fixes a comparative-runtime mismatch where dependency prefetch
completed successfully but Codex shell tools still resolved `python` from the user's
login profile. Results collected before this fix did not give the agent the intended
targeted-test environment and should remain diagnostic only.

Keep the prepared dependency environment, reset only the affected task, and repeat its
three arms:

```bash
claim-plane validation reset-task <task-id>
claim-plane validation status
claim-plane validation run --next
```

The next run performs a fail-fast environment preflight and prints the exact Python
executable plus available test imports before opening Codex. No second prefetch is
needed when the task environment is already prepared.

## Comparative prefetch from 0.37.2

Version `0.37.3` fixes dependency prefetch for frozen evaluators that reject an empty
feature-patch argument. No validation state reset is required. Remove the incomplete
environment automatically by rerunning the same command; Claim Plane rebuilds it from
the preserved UV cache:

```bash
claim-plane validation prefetch --next
```

## Comparative validation from 0.37.1

Version `0.37.2` changes the comparative execution contract: all three arms receive the
same task-level development environment, and Observe/Guarded delegate acceptance to the
single external evaluator. Results already collected with `0.37.1` remain readable but
should not be mixed with new fidelity-matched cells in a public comparison.

Preserve the diagnostic bundle, reset the affected task across all arms, and prefetch its
dependencies before repeating it:

```bash
claim-plane validation bundle --out claim-plane-0.37.1-diagnostic.zip
claim-plane validation reset-task <task-id>
claim-plane validation prefetch --next
claim-plane validation run --next
```

`reset-task` removes only that task's result records and workspaces. The shared download
cache and any prepared task environment remain available.

## Roll back the package

Install an exact earlier version with the same tool manager, then check config and adapter
compatibility before starting a run:

```bash
uv tool install --force claim-plane==0.36.9
# or
pipx install --force claim-plane==0.36.9

claim-plane config status
claim-plane doctor
```

A newer unknown config protocol is never downgraded automatically.

Comparative validation state is stored outside the enrolled repository by default at
`/private/tmp/claim-plane-single-agent-validation`. Package rollback does not rewrite or
remove that evidence. Use a separate `--root` when comparing results from different Claim
Plane versions.

## Remove project enrollment

Preserve the versioned config while removing private state and Claim Plane-owned hooks:

```bash
claim-plane reset
```

Remove the config as well:

```bash
claim-plane reset --remove-config
```

Both modes preserve repository content and unrelated Codex hooks. Config migration backups
are preserved unless `--remove-config` is explicitly used.

## Uninstall the CLI

```bash
uv tool uninstall claim-plane
# or
pipx uninstall claim-plane
```
