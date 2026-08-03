#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${1:-$(mktemp -d "${TMPDIR:-/tmp}/claim-plane-preview.XXXXXX")}" 
DEMO="${WORKDIR}/claim-plane-preview-demo"

rm -rf "${DEMO}"
mkdir -p "${DEMO}"
cp -R "${ROOT}/examples/technical-preview/." "${DEMO}/"

git -C "${DEMO}" init -q -b main
git -C "${DEMO}" config user.email claim-plane@example.invalid
git -C "${DEMO}" config user.name "Claim Plane Preview"
git -C "${DEMO}" add .
git -C "${DEMO}" commit -qm "demo baseline"

PYTHONPATH="${ROOT}/src" python -m claim_plane preview --repo "${ROOT}" >/dev/null
PYTHONPATH="${ROOT}/src" python -m claim_plane init --repo "${DEMO}" >/dev/null
PYTHONPATH="${ROOT}/src" python -m claim_plane config status --repo "${DEMO}" >/dev/null
PYTHONPATH="${ROOT}/src" python -m claim_plane policy classify \
  src/audit_api/pagination.py --repo "${DEMO}" >/dev/null
PYTHONPATH="${ROOT}/src" python -m claim_plane adapters conformance reference \
  --workdir "${WORKDIR}/conformance" >/dev/null
"${DEMO}/scripts/check.sh" >/dev/null 2>&1

printf 'technical-preview demo: passed\n'
printf 'demo repository: %s\n' "${DEMO}"
printf 'next: cd %q && claim-plane connect codex && claim-plane doctor\n' "${DEMO}"
