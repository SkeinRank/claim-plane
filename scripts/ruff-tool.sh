#!/usr/bin/env bash
set -euo pipefail

EXPECTED_RUFF_VERSION="0.15.21"

if ! command -v ruff >/dev/null 2>&1; then
  printf 'error: ruff %s is required in the active environment\n' "${EXPECTED_RUFF_VERSION}" >&2
  printf 'hint: install project development dependencies before committing\n' >&2
  exit 1
fi

RUFF_VERSION="$(ruff --version)"
if [[ "${RUFF_VERSION}" != "ruff ${EXPECTED_RUFF_VERSION}" ]]; then
  printf 'error: expected ruff %s, got %s\n' "${EXPECTED_RUFF_VERSION}" "${RUFF_VERSION}" >&2
  exit 1
fi

exec ruff "$@"
