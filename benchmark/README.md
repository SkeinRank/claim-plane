# Benchmarks

## Deterministic protocol suite

`run_protocol_suite.py` is a zero-model conformance and latency smoke test. It checks core admission and verification behavior; it does not prove lower token cost or higher development throughput.

```bash
PYTHONPATH=src python benchmark/run_protocol_suite.py
```

## Adapter-driven A/B/C harness

`run_abc_harness.py` compares external workflows without coupling Claim Plane to one agent vendor:

- **A** — worktrees only;
- **B** — planner plus natural-language coordination;
- **C** — planner plus Claim Plane.

Each adapter command must print one final JSON object containing outcome and cost metrics. Start from the checked-in example:

```bash
cp benchmark/abc-spec.example.json benchmark/abc-spec.local.json
python benchmark/run_abc_harness.py benchmark/abc-spec.local.json \
  --out .claim-plane/benchmarks/abc-result.json
```

The harness aggregates clean-result rate, success rate, elapsed time, tokens, reported cost, CI failures, repair attempts, and human intervention. It does not provide agent implementations or claim a benchmark result by itself.

The experiment design and validity requirements are documented in `docs/BENCHMARK.md`.
