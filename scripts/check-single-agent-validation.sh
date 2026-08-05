#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_single_agent_validation.py \
  tests/test_dogfood_golden_suite.py \
  tests/test_technical_preview_packaging.py

python -m compileall -q src/claim_plane

echo "single-agent comparative validation: passed"
