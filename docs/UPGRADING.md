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
