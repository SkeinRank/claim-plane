# Claim Plane technical-preview demo

A tiny dependency-free Python repository for trying project enrollment, policy
classification, adapter conformance, and the one-command Codex workflow.

```bash
./scripts/check.sh
claim-plane init
claim-plane config status
claim-plane policy classify src/audit_api/pagination.py
claim-plane connect codex
claim-plane doctor
claim-plane run "Add cursor pagination and tests" --policy guarded
claim-plane report latest
```

The repository intentionally starts with offset pagination. The sample task asks the
agent to add a cursor-oriented helper while preserving existing behavior.
