#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

SUMMARY="${1:-benchmark/golden-suite/release-summary.json}"
if [[ ! -f "${SUMMARY}" ]]; then
  printf 'error: dogfood release summary not found: %s\n' "${SUMMARY}" >&2
  printf 'run the frozen task × seed × arm matrix and aggregate measured results first\n' >&2
  exit 3
fi

PYTHONPATH=src python -m claim_plane dogfood gate "${SUMMARY}"
