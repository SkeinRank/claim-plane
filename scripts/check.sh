#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

pytest -q
ruff format --check .
ruff check .
mypy src
PYTHONPATH=src python benchmark/run_protocol_suite.py
