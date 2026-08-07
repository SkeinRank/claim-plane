# Releasing Claim Plane

Claim Plane uses the distribution name `claim-plane`, the import package
`claim_plane`, the CLI command `claim-plane`, and Git tags in the form `vX.Y.Z`.

## Prepare a release

1. Update the version in `pyproject.toml`, `src/claim_plane/__init__.py`,
   `src/claim_plane/mcp/server.py`, the source fallback in
   `src/claim_plane/integration/runner.py`, `CITATION.cff`, the README preview
   banner, and `CHANGELOG.md`.
2. Confirm that the public command names, exit codes, schemas, config migration path,
   quickstart, guarantee model, troubleshooting guide, and upgrade instructions match the
   release.
3. Run the complete quality and packaging checks:

   ```bash
   python -m pip install --upgrade "setuptools>=77.0.3" wheel build twine
   ./scripts/check.sh
   ./scripts/check-technical-preview.sh --build
   python -m build --no-isolation
   python -m twine check dist/*
   ```

4. Validate the frozen dogfood result before describing the release as evaluated:

   ```bash
   ./scripts/check-dogfood-release.sh benchmark/golden-suite/release-summary.json
   ```

   Missing or incomplete measurements must remain `INCOMPLETE`; they must not be replaced
   with illustrative values. A technical-preview package may be built for development and
   evaluation, but a public release announcement must state the actual gate status.
5. Install the wheel in a clean environment and complete the five-minute quickstart on
   macOS or Linux. Confirm `claim-plane reset` and `claim-plane reset --remove-config`
   preserve repository files and unrelated Codex hooks.
6. Commit the release, create tag `vX.Y.Z`, push it, and publish a GitHub Release from that
   tag.

## Release checklist

- [ ] package, MCP server, README, and changelog versions agree;
- [ ] `./scripts/check.sh` passes;
- [ ] `./scripts/check-technical-preview.sh --build` passes;
- [ ] wheel and sdist pass `twine check`;
- [ ] wheel contains `py.typed` and all public JSON Schemas;
- [ ] clean install exposes `preview`, `exit-codes`, `config`, and `schemas` commands;
- [ ] `claim-plane init`, `connect codex`, `doctor`, `run`, `report`, and `replay` match the quickstart;
- [ ] config migration dry-run and backup behavior were tested;
- [ ] technical-preview demo passes without a model provider;
- [ ] dogfood gate status and measured limitations are reported honestly;
- [ ] release notes distinguish hard-blocked, observed, and post-verified guarantees;
- [ ] no credentials, private evidence, SQLite state, wheels, or local benchmark output are committed.

## PyPI Trusted Publishing

The workflow `.github/workflows/publish.yml` publishes only after a GitHub Release is
published. Configure the PyPI Trusted Publisher with:

- owner: `SkeinRank`;
- repository: `claim-plane`;
- workflow: `publish.yml`;
- environment: `pypi`.

The workflow verifies that the GitHub tag exactly matches the package version, validates
the technical-preview package contract, builds the distributions, and uploads them. The
`pypi` GitHub Environment can require manual approval. No long-lived PyPI API token is
required.
