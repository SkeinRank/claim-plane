#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q \
  tests/test_test_feedback.py \
  tests/test_codex_enrollment.py \
  tests/test_oss_pilot.py \
  tests/test_controlled_run.py \
  tests/test_console_ux.py
