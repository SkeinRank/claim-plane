#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_validation_runtime_ux.py \
  tests/test_single_agent_validation.py \
  tests/test_oss_pilot.py

python -m compileall -q src/claim_plane

echo "validation runtime UX: passed"
