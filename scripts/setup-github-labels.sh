#!/usr/bin/env bash
set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
  echo "error: GitHub CLI (gh) is required" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "error: GitHub CLI is not authenticated; run 'gh auth login' first" >&2
  exit 1
fi

repo="${CLAIM_PLANE_GITHUB_REPO:-}"
if [[ -z "$repo" ]]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi

ensure_label() {
  local name="$1"
  local color="$2"
  local description="$3"
  gh label create "$name" \
    --repo "$repo" \
    --color "$color" \
    --description "$description" \
    --force >/dev/null
  printf 'label: %-24s #%s\n' "$name" "$color"
}

echo "Synchronizing Claim Plane labels in $repo"

ensure_label "type: bug" "D73A4A" "Something is broken or behaves incorrectly"
ensure_label "type: feature" "A2EEEF" "New functionality or product capability"
ensure_label "type: docs" "0075CA" "Documentation improvements or corrections"
ensure_label "type: research" "7057FF" "Research, experiments, evaluation, and methodology"
ensure_label "type: integration" "0E8A16" "Integrations with agents, providers, CI, MCP, or external systems"
ensure_label "type: performance" "F9D0C4" "Latency, throughput, scalability, or resource-efficiency work"

ensure_label "area: core" "1D76DB" "Deterministic core and admission logic"
ensure_label "area: broker" "1D76DB" "Broker, registry, leases, coordination, and fencing"
ensure_label "area: verification" "1D76DB" "Verification, evidence, drift, contracts, and acceptance checks"
ensure_label "area: cli" "1D76DB" "Command-line interface and developer-facing commands"
ensure_label "area: cooperbench" "1D76DB" "CooperBench research harness, studies, and reproducibility"
ensure_label "area: tooling" "1D76DB" "Developer tooling, CI, packaging, release, and repository infrastructure"

ensure_label "status: needs-triage" "FBCA04" "Needs maintainer review and classification"
ensure_label "status: blocked" "B60205" "Blocked by another issue, dependency, or external condition"

ensure_label "help wanted" "008672" "Maintainers welcome community help on this issue"
ensure_label "good first issue" "7057FF" "Good starting point for a first contribution"
ensure_label "duplicate" "CFD3D7" "This issue or pull request already exists"
ensure_label "invalid" "E4E669" "Not actionable or does not describe a valid issue"
ensure_label "wontfix" "FFFFFF" "This will not be worked on"

echo "Done. Verify with: gh label list --repo '$repo' --limit 100 --sort name"
