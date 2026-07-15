# Releasing Claim Plane

Claim Plane uses the distribution name `claim-plane`, the import package
`claim_plane`, the CLI command `claim-plane`, and Git tags in the form `vX.Y.Z`.

## Prepare a release

1. Update the version in `pyproject.toml`, `src/claim_plane/__init__.py`,
   `src/claim_plane/mcp/server.py`, and the source fallback in
   `src/claim_plane/integration/runner.py`.
2. Move relevant entries from `Unreleased` into a versioned section in
   `CHANGELOG.md`.
3. Run:

   ```bash
   ./scripts/check.sh
   python -m pip install --upgrade build twine
   python -m build
   python -m twine check dist/*
   ```

4. Commit the release, create tag `vX.Y.Z`, push it, and publish a GitHub Release from
   that tag.

## PyPI Trusted Publishing

The workflow `.github/workflows/publish.yml` publishes only after a GitHub Release is
published. Configure the PyPI Trusted Publisher with:

- owner: `SkeinRank`;
- repository: `claim-plane`;
- workflow: `publish.yml`;
- environment: `pypi`.

The workflow verifies that the GitHub tag exactly matches the package version before it
builds and uploads artifacts. The `pypi` GitHub Environment can require manual approval.
No long-lived PyPI API token is required.
