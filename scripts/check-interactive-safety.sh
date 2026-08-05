#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

pytest -q \
  tests/test_negative_authority_safety.py \
  tests/test_interactive_codex_hardening.py \
  tests/test_policy_presets.py

printf 'interactive authority safety: passed\n'
