#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-${TMPDIR:-/tmp}/claim-plane-swarm-demo}"

cd "${ROOT}"
PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  python -m claim_plane.cli swarm demo --directory "${TARGET}"
