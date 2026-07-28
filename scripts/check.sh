#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

EXPECTED_RUFF_VERSION="0.15.21"
EXPECTED_RUFF_SPEC="ruff==${EXPECTED_RUFF_VERSION}"

printf 'quality gate: commit %s\n' "$(git rev-parse --short HEAD 2>/dev/null || printf 'unavailable')"
python --version
./scripts/ruff-tool.sh --version
if ! grep -Fq "\"${EXPECTED_RUFF_SPEC}\"" pyproject.toml; then
  printf 'error: pyproject.toml must pin ruff==%s\n' "${EXPECTED_RUFF_VERSION}" >&2
  exit 1
fi
if ! grep -Fq './scripts/ruff-tool.sh' .pre-commit-config.yaml; then
  printf 'error: Ruff pre-commit hooks must use scripts/ruff-tool.sh\n' >&2
  exit 1
fi
if [[ "$(grep -Fc 'language: system' .pre-commit-config.yaml)" -lt 2 ]]; then
  printf 'error: Ruff pre-commit hooks must use language: system\n' >&2
  exit 1
fi

printf '\n[1/7] Ruff lint\n'
./scripts/ruff-tool.sh check src tests benchmark experiments

printf '\n[2/7] Ruff format\n'
./scripts/ruff-tool.sh format --check src tests benchmark experiments

printf '\n[3/7] Mypy\n'
mypy src experiments/cooperbench

printf '\n[4/7] Shell syntax\n'
for script in scripts/*.sh; do
  [[ -f "${script}" ]] || continue
  bash -n "${script}"
done

printf '\n[5/7] Research environment and bytecode\n'
python -m experiments.cooperbench environment > /dev/null
python -m compileall -q src tests benchmark experiments

printf '\n[6/7] Tests\n'
pytest -q

printf '\n[7/7] Protocol suite\n'
PYTHONPATH=src python benchmark/run_protocol_suite.py

printf '\nquality gate: passed\n'
