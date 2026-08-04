#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

BUILD=0
if [[ "${1:-}" == "--build" ]]; then
  BUILD=1
elif [[ $# -gt 0 ]]; then
  printf 'usage: %s [--build]\n' "$0" >&2
  exit 1
fi

python - <<'PY'
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path

root = Path.cwd()
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
expected = {
    "src/claim_plane/__init__.py": f'__version__ = "{version}"',
    "src/claim_plane/mcp/server.py": f'SERVER_VERSION = "{version}"',
    "src/claim_plane/integration/runner.py": f'return "{version}+source"',
    "CITATION.cff": f"version: {version}",
    "README.md": f"Technical Preview — {version}",
    "CHANGELOG.md": f"## [{version}]",
}
for relative, marker in expected.items():
    text = (root / relative).read_text(encoding="utf-8")
    if marker not in text:
        raise SystemExit(f"{relative} is missing version marker {marker!r}")

required = (
    "docs/QUICKSTART.md",
    "docs/CLI_REFERENCE.md",
    "docs/GUARANTEES.md",
    "docs/TROUBLESHOOTING.md",
    "docs/UPGRADING.md",
    "examples/technical-preview/TASK.md",
    "scripts/technical-preview-demo.sh",
    ".github/ISSUE_TEMPLATE/technical-preview.yml",
)
for relative in required:
    if not (root / relative).is_file():
        raise SystemExit(f"missing technical-preview artifact: {relative}")
PY

PYTHONPATH=src python -m claim_plane --version >/dev/null
PYTHONPATH=src python -m claim_plane preview --repo . --json >/dev/null
PYTHONPATH=src python -m claim_plane exit-codes --json >/dev/null
PYTHONPATH=src python -m claim_plane schemas list --json >/dev/null
PYTHONPATH=src python - <<'PY'
import hashlib
from pathlib import Path

from claim_plane.resources import list_schemas

root = Path.cwd()
packaged = {item["name"]: item["sha256"] for item in list_schemas()}
source = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (root / "schemas").glob("*.json")
}
if packaged != source:
    raise SystemExit("packaged schemas differ from repository schemas")
PY

./scripts/technical-preview-demo.sh >/dev/null

if [[ "${BUILD}" -eq 1 ]]; then
  DIST="$(mktemp -d "${TMPDIR:-/tmp}/claim-plane-dist.XXXXXX")"
  python -m pip wheel . --no-deps --no-build-isolation --wheel-dir "${DIST}" >/dev/null
  python - "${DIST}" <<'PY'
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

wheels = list(Path(sys.argv[1]).glob("claim_plane-*.whl"))
if len(wheels) != 1:
    raise SystemExit(f"expected one wheel, found {len(wheels)}")
with zipfile.ZipFile(wheels[0]) as archive:
    names = set(archive.namelist())
    if "claim_plane/py.typed" not in names:
        raise SystemExit("wheel is missing py.typed")
    if not any(name.startswith("claim_plane/resources/schemas/") for name in names):
        raise SystemExit("wheel is missing packaged schemas")
PY
fi

printf 'technical-preview validation: passed\n'
