# Contributing to Claim Plane

Claim Plane is an early Technical Preview. Contributions should preserve its central
property: deterministic checks fail closed at the governed boundary and return structured
guidance instead of silently guessing.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,signing]"
pre-commit install
./scripts/check.sh
```

The pre-commit hooks run Ruff lint fixes and formatting across the repository before
each commit, so unrelated stale Python files cannot bypass the local gate. They use the
Ruff executable from the active development environment and verify the exact pinned
version before running, so commits do not create a separate pre-commit environment or
require package-index access after development dependencies are installed. CI installs
the same pinned development dependencies and runs the same repository-wide gate.

The canonical repository-wide validation command is:

```bash
./scripts/check.sh
```

It runs the same quality gate used by GitHub Actions: linting, formatting, typing, shell
syntax checks, research-environment validation, bytecode compilation, tests, and the
protocol suite. If Ruff modifies a file during pre-commit, stage the change and commit
again.

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

## Issues and discussions

Use GitHub Discussions for questions, exploratory ideas, architecture discussion, research
results, and proposals that are not yet actionable. Open an issue once there is concrete
work to track. The issue chooser provides structured forms for bugs, features, research
tasks, and reproducibility problems.

Issue Forms apply type and triage labels automatically. The issue-intake workflow maps the
required Area field to one `area:*` label from the repository taxonomy. Maintainers can
synchronize the full label set with:

```bash
./scripts/setup-github-labels.sh
```

Large protocol, storage, sandbox, or evidence changes should state the invariant being
added, the failure mode being prevented, and how the change will be tested before they
move from discussion into implementation.
