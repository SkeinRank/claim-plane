#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_benchmark_isolation.py \
  tests/test_single_agent_validation.py \
  tests/test_validation_runtime_ux.py \
  tests/test_comparative_runtime_fidelity.py
