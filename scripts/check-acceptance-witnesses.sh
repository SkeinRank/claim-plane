#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_acceptance_witness.py \
  tests/test_oss_pilot.py \
  tests/test_comparative_runtime_fidelity.py \
  tests/test_single_agent_validation.py
