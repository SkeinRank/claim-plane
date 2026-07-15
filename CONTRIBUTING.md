# Contributing to Claim Plane

Claim Plane is an early Research Preview. Contributions should preserve its central
property: deterministic checks fail closed at the governed boundary and return structured
guidance instead of silently guessing.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,signing]"
./scripts/check.sh
```

Install the optional semantic integration from PyPI with:

```bash
python -m pip install -e ".[semantic,signing,dev]"
```

## Pull requests

- Add or update regression tests for every behavioral change.
- Keep public protocols backward-aware during the `0.x` phase, but prefer secure defaults.
- Document trust-boundary changes in `docs/TRUSTED_EXECUTION.md` or
  `docs/BROKERED_RUNTIME.md`.
- Do not commit generated evidence, SQLite databases, credentials, private keys, wheels,
  archives, or local benchmark results.
- Run `./scripts/check.sh` before opening a pull request.

## Design discussions

Large protocol, storage, sandbox, or evidence changes should begin with a GitHub issue
that states the invariant being added, the failure mode being prevented, and how the
change will be tested.
