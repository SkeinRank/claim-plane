#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q tests/test_oss_pilot.py
PYTHONPATH=src python -m claim_plane oss-pilot list --json >/dev/null
PYTHONPATH=src python -m claim_plane oss-pilot --help >/dev/null
python -m compileall -q src/claim_plane/oss_pilot.py src/claim_plane/oss_pilot_acceptance.py

echo "OSS pilot contract: passed"
