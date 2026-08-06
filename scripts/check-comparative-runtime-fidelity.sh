#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_comparative_runtime_fidelity.py \
  tests/test_validation_runtime_ux.py \
  tests/test_single_agent_validation.py \
  tests/test_codex_read_only_shell_chains.py \
  tests/test_controlled_run.py \
  tests/test_oss_pilot.py
