#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_single_agent_determinism.py \
  tests/test_controlled_run.py
