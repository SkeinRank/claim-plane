#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${ROOT}/.claim-plane/demo.db"
LEXICON="${ROOT}/examples/rate-limiter/lexicon.yaml"
rm -f "${DB}" "${DB}-wal" "${DB}-shm"

claim-plane --db "${DB}" --semantic --lexicon "${LEXICON}" admit \
  "${ROOT}/examples/rate-limiter/intents/core.json"

claim-plane --db "${DB}" --semantic --lexicon "${LEXICON}" admit \
  "${ROOT}/examples/rate-limiter/intents/metrics.json"

# Expected exit code 2: the signature conflicts with the admitted core contract.
claim-plane --db "${DB}" --semantic --lexicon "${LEXICON}" admit \
  "${ROOT}/examples/rate-limiter/intents/conflicting-contract.json" || true

claim-plane --db "${DB}" --semantic --lexicon "${LEXICON}" context rate-limit-metrics
claim-plane --db "${DB}" --semantic --lexicon "${LEXICON}" verify-manifest \
  "${ROOT}/examples/rate-limiter/manifests/core-clean.json"
claim-plane --db "${DB}" --semantic --lexicon "${LEXICON}" repair-manifest \
  "${ROOT}/examples/rate-limiter/manifests/core-drift.json" || true
