#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PYTHONPATH=src python -m pytest -q \
  tests/test_codex_read_only_shell_chains.py \
  tests/test_codex_enrollment.py \
  -k 'read_only_shell or friction_metrics or raw_tool_input'

python -m compileall -q src/claim_plane/connectors/codex.py \
  src/claim_plane/connectors/codex_guard.py \
  src/claim_plane/controlled_run.py

printf 'inspection friction hardening: passed\n'
