<div align="center">

# Claim Plane

**A deterministic control and evidence layer for coding agents**  
Task-bound authority. Controlled scope. Verifiable delivery.

[![CI](https://github.com/SkeinRank/claim-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/SkeinRank/claim-plane/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/claim-plane?label=pypi)](https://pypi.org/project/claim-plane/)
[![Python](https://img.shields.io/pypi/pyversions/claim-plane?label=python)](https://pypi.org/project/claim-plane/)
[![License](https://img.shields.io/badge/license-Apache--2.0-4c1)](LICENSE)
[![Status](https://img.shields.io/badge/status-technical%20preview-orange)](#quick-start)

**Let agents code. Make every change provable.**

</div>

> **Technical Preview — 0.44.0.** APIs, evidence formats, and deployment contracts may change before 1.0.

## Quick start

**Already use Codex? Run the same conversational TUI through Claim Plane.**

Claim Plane does not replace Codex. It controls authority around the session:
Codex explores and implements normally, while Claim Plane admits the task scope,
requires explicit amendments for necessary scope growth, verifies the final Git diff,
runs the configured acceptance checks after Codex exits, and seals the evidence.

### 1. Install the CLI

```bash
uv tool install claim-plane
# or: pipx install claim-plane
```

Claim Plane uses the Codex CLI already installed and authenticated on your machine.

### 2. Enroll a repository once

Run these commands from a Git feature branch:

```bash
cd my-project
git switch -c agent/my-task

claim-plane init
claim-plane connect codex
claim-plane doctor
```

`doctor` reports the actual adapter, sandbox, policy, and enforcement boundary before
an agent starts changing files.

### 3. Work in Codex as usual

```bash
claim-plane codex --policy guarded
```

Then describe the task inside the normal Codex session:

```text
Fix timeout handling and update the appropriate regression tests.
```

That is the default daily workflow. On later sessions in the same enrolled repository,
`claim-plane codex --policy guarded` is normally the only command you need.

### What happens around the session

```text
You work with the normal Codex TUI
              ↓
Codex proposes a task-bound ChangeIntent before mutation
              ↓
Claim Plane admits the initial authority
              ↓
Necessary scope growth uses a recorded amendment instead of silent expansion
              ↓
Codex exits; Claim Plane verifies the real Git diff and runs acceptance
              ↓
DELIVERY VERIFIED, REJECTED, REVIEW REQUIRED, or another explicit outcome
              ↓
Durable report and replay evidence under .claim-plane/runs/
```

For normal use, omit `--scope` and let the task establish the initial authority.
Supply an explicit starting boundary when reproducibility or review requires it:

```bash
claim-plane codex \
  --scope src/connectors/github.py \
  --policy guarded
```

A genuinely required additional file can pass through the brokered amendment path.
Add `--lock-scope` only when every expansion must be forbidden.

Read the [five-minute walkthrough](docs/QUICKSTART.md), the explicit
[guarantees and trust boundaries](docs/GUARANTEES.md), and the
[troubleshooting guide](docs/TROUBLESHOOTING.md).

## Why Claim Plane exists

A Git worktree isolates a process, but it does not prove that an agent stayed within
the task, that every scope expansion was justified, or that a successful agent exit
matches the final repository state. Claim Plane separates probabilistic planning from
deterministic authority and verification.

It does not replace Git, an IDE, a planner, or a coding agent. Codex remains the
interactive coding experience; Claim Plane is the control and evidence layer around it.

<details>
<summary><strong>Architecture and multi-agent flow</strong></summary>

```text
Task
  ↓
Planner emits ChangeIntent
  ↓
Agent Lexicon resolves canonical concepts
  ↓
Claim Plane performs atomic pre-write admission
  ↓
Workers execute in isolated worktrees
  ↓
Contract changes invalidate affected dependents
  ↓
Integration Verifier checks real Git hunks, contracts, policies, and acceptance
  ↓
Clean integration or targeted repair
```

Swarm planning can be materialized into Claim Plane-owned isolated worktrees:

```bash
claim-plane swarm plan <session-id>
claim-plane swarm admit <session-id>
claim-plane swarm scheduler <session-id>
claim-plane swarm provision-worktrees <session-id>
claim-plane swarm worktrees <session-id>
claim-plane swarm run-codex <session-id> --work-id <work-id>
claim-plane swarm merge-plan <session-id>
claim-plane swarm merge-next <session-id>
claim-plane swarm merge-queue <session-id>
claim-plane swarm runs <session-id>
claim-plane swarm recovery-status <session-id>
claim-plane swarm recover <session-id>
claim-plane swarm replace-codex <session-id> --work-id <work-id> --run-id <run-id>
```

</details>

## Deterministic single-agent evidence

The current technical preview retains deterministic single-agent candidate and verdict binding.
Every new controlled run seals the task, base and result repository states, change
summary, effective policy, adapter contract, acceptance definition, and lifecycle head
into one reproducible decision digest. Evidence reports recompute that binding and
replay verifies equivalence without rerunning the provider.

## Frozen OSS pilot

The repository includes a three-task real-repository pilot for the interactive
Codex workflow. It prepares exact Jinja, Click, and dirty-equals repository states
from a frozen CooperBench revision, runs each arm in an independent directory, and
executes authoritative acceptance in an isolated temporary worktree. The evaluator
uses the task-required `agent_workspace` layout, combines official tests with the
candidate tree through a base-aware merge, and records classified failures with full
logs under `.claim-plane/oss-pilot/acceptance/`.

```bash
claim-plane oss-pilot list
claim-plane oss-pilot prepare jinja-loader-local --arm guarded
claim-plane oss-pilot run jinja-loader-local --arm guarded --model gpt-5.6-luna
claim-plane oss-pilot status jinja-loader-local --arm guarded
```

See [`benchmark/oss-pilot/README.md`](benchmark/oss-pilot/README.md) for all three
tasks, frozen inputs, and the comparative-arm layout.

## Inspection friction hardening

The Codex guard accepts bounded read-only chains and pipelines when every stage can
be proven non-mutating. Typical repository inspection remains concise without
allowing shell redirection or unclassified executables to bypass resource authority.

```bash
git show --stat HEAD | head -20
rg -n "ChangeIntent" src tests | head -40
git diff --check; git status --short
```

`claim-plane codex-intent status` reports allowed compound inspections, pipelines,
unclassified shell denials, and subsequent read-only recoveries. The same summary is
bound into controlled-run evidence for comparative Bare, Observe, and Guarded runs.

## Targeted test feedback and current verdict

During an admitted interactive task, Codex may run bounded targeted tests and common
project-native test commands to repair its work. The project-configured full acceptance
command remains reserved for Claim Plane and runs independently after the agent exits.
Untracked test caches and build outputs are treated as managed artifacts; tracked source,
configuration, snapshots, and golden files remain subject to normal authority checks.

OSS pilot re-verification is sealed to the current candidate digest. `oss-pilot status`,
`report`, and `replay` keep the immutable delivery outcome separate from the latest
acceptance recheck. A rejected delivery remains rejected even when its unchanged
candidate later passes the evaluator; the candidate is labeled
`MATCHES_PASSING_ACCEPTANCE_RECHECK` rather than being promoted to verified delivery.

## Comparative single-agent validation

Version `0.37.9` turns the frozen OSS pilot and dogfood contracts into one
operator workflow for fidelity-matched Bare Codex, Claim Plane Observe, and Claim
Plane Guarded executions. The preview profile freezes 12 feature-level tasks across at
least six repository families and expands them into a 36-cell matrix. The release
profile freezes 20 tasks and two independent execution replicates.

```bash
claim-plane validation init --profile preview --model gpt-5.6-luna
claim-plane validation prefetch --next
claim-plane validation status
claim-plane validation run --next
claim-plane validation report
claim-plane validation bundle --out claim-plane-validation.zip
```

`validation prefetch` executes only the dependency-setup prefix of the frozen task
evaluator, stops before official tests, and stores one task-level virtual environment.
The same environment and download cache are activated for Bare, Observe, and Guarded,
so Codex can run targeted tests against the project dependencies in every arm. Candidate
source is rebound with an editable no-dependency install before each execution.
Claim Plane also injects that environment into Codex shell tools with one-run
`shell_environment_policy` overrides and disables login-shell profile rewriting for the
validation session. Before opening Codex, it removes parent Python launcher redirects, verifies the exact
virtual-environment prefix, imports pytest and top-level test dependencies, and checks
the editable candidate import. Prepared site-packages are pinned into the one-run Codex
`PYTHONPATH`, which keeps macOS framework and pyenv launchers from selecting the parent
interpreter's package set.

Private pytest acceptance installs explicit evaluator-only prerequisites before the
repository's initial `conftest.py` imports, then witnesses exact hidden node execution.
Skipped or stale dependency-gated tests remain `EVALUATOR_INCOMPLETE`; preserved
candidates can be resumed without rerunning Codex.

The comparative runner keeps frozen evaluator programs and hidden acceptance inputs in
a private persistent vault outside the workspace tree. Agent-visible manifests contain
only public task identity, while Codex web search and shell networking are disabled for
the cell. Newly written session and run records are audited after Codex exits; detected
reference-artifact access records the cell as `CONTAMINATED` and skips official
acceptance. The agent-facing acceptance command resolves `python` from the prepared
environment rather than embedding an absolute host interpreter.

Official Python acceptance is also witness-bound. Claim Plane derives the private pytest
node IDs added or modified by the frozen test input and requires every node to be
collected, executed, and passed. A skipped test, an uncollected node, a missing pytest
session, or an optional dependency that cannot be installed produces
`EVALUATOR_INCOMPLETE`, never `PASS`. The compact witness summary is sealed into the
re-verification evidence and exported with the validation bundle.
Candidate workspaces and shared development environments are stored outside the matrix
directory, so a session cannot discover previous-arm candidates by traversing its
ordinary parent directories.

Observe and Guarded defer their internal acceptance to the comparative runner. The
external frozen evaluator therefore runs exactly once per cell, after the agent exits,
and remains the sole authoritative task verdict.

Preview acceptance defaults to five minutes and streams evaluator output. Long silent
phases emit elapsed-time heartbeats. Claim Plane stores the cell phase before Codex and
before acceptance, so one interruption preserves the candidate and prints a resumable
command:

```bash
claim-plane validation status
claim-plane validation resume <execution-id>
```

Timeouts and evaluator-environment failures keep the same candidate resumable and do
not fill the comparative matrix with a false task verdict.

Candidates created by `0.37.0` are detected as `LEGACY_CANDIDATE`. Their measured agent
time can be restored during the one-time recovery:

```bash
claim-plane validation resume <execution-id> --agent-seconds 178
```

Diagnostic cells produced before runtime-fidelity matching can be removed across all
three arms while preserving the prepared dependency cache:

```bash
claim-plane validation reset-task <task-id>
```

`validation run --next` prepares the exact repository state, opens the selected
Codex arm, runs the same isolated official evaluator after the agent exits, and
binds measured scope, friction, timing, token, drift, and acceptance metrics to
the immutable plan cell. Aggregation rejects missing, duplicate, unexpected, or
mismatched cells instead of filling gaps.

See [`benchmark/single-agent-validation/README.md`](benchmark/single-agent-validation/README.md)
for the full workflow and release gate semantics.

## Research papers

### Paper #1 — Enforceable change intents and dynamic scope

**Claim Plane: Enforceable Change Intents and Dynamic Scope for Parallel Coding Agents**  
Maxim Nikolaev · Software Engineering (`cs.SE`) · 2026

- [arXiv abstract](https://arxiv.org/abs/2607.21909)
- [PDF](https://arxiv.org/pdf/2607.21909)
- [DOI: 10.48550/arXiv.2607.21909](https://doi.org/10.48550/arXiv.2607.21909)
- [Paper metadata and citation](papers/claim-plane-2026/README.md)
- [Published six-pair CooperBench reproduction](experiments/cooperbench/paper_6pair/)

### Paper #2 — 30-pair, three-seed confirmatory study

**Claim Plane: Reliability Gains and the Limits of Selective Concurrency for Parallel Coding Agents: A 30-Pair, Three-Seed Confirmatory Study of Deterministic Pre-Write Admission**  
Maxim Nikolaev · Software Engineering (`cs.SE`) · 2026

- [arXiv abstract](https://arxiv.org/abs/2608.00947)
- [PDF](https://arxiv.org/pdf/2608.00947)
- [DOI: 10.48550/arXiv.2608.00947](https://doi.org/10.48550/arXiv.2608.00947)
- [Paper metadata and citation](papers/claim-plane-confirmatory-2026/README.md)
- [Published 30-pair × 3-seed CooperBench reproduction](experiments/cooperbench/confirmatory_30x3/)
- [Public study artifacts](https://huggingface.co/datasets/skeinrank/claim-plane-confirmatory-30x3)

Repository-level software citation metadata is available in [`CITATION.cff`](CITATION.cff). Paper-specific BibTeX entries are available in [`papers/claim-plane-2026/citation.bib`](papers/claim-plane-2026/citation.bib) and [`papers/claim-plane-confirmatory-2026/citation.bib`](papers/claim-plane-confirmatory-2026/citation.bib).

## More ways to run

Open the same interactive TUI with the first task already submitted:

```bash
claim-plane codex "Fix timeout handling and update its regression test" \
  --policy guarded
```

Run a bounded unattended task instead of an interactive session:

```bash
claim-plane run "Add pagination to the audit API and extend its tests" \
  --policy guarded
```

Inspect the latest durable evidence without reopening Codex:

```bash
claim-plane report latest
claim-plane replay latest
```

Inspect the installed product contract and packaged schemas:

```bash
claim-plane preview
claim-plane exit-codes
claim-plane schemas list
```

See [upgrade and uninstall behavior](docs/UPGRADING.md) before changing an existing
installation.

<details>
<summary><strong>Current capabilities</strong></summary>

- atomic claim and intent admission through SQLite transactions;
- leases, heartbeats, completion, release, and append-only audit events;
- structured `ChangeIntent` operations: read, write, extend, delete, rename, document, and test;
- adaptive committed/contingent scope with just-in-time atomic re-admission before first mutation;
- exact files, globs, bounded line regions, symbols, concepts, contracts, routes, schemas, configs, and documents;
- versioned Semantic Resource IR v2 with canonical `file → region → symbol → contract` coordinates and stable symbol/contract identities for downstream dependency analysis;
- Semantic Dependency Graph v2 with deterministic `defines`, `imports`, `calls`, `reads`, `writes`, `inherits`, `types`, `tests`, and `public_api` relationships over repository resources;
- strict optional Agent Lexicon resolution: requested semantic mode fails closed when unavailable;
- concept-bound contracts through `subject_concept_id`;
- deterministic outcomes for independent work, compatible overlap, contract dependencies, constrained parallelism, serialization, replanning, and rejection;
- safe broad-scope admission: known glob/file overlap is serialized rather than optimistically admitted;
- versioned intent amendments with optimistic version checks and Semantic Amendment Protocol v2 for bounded semantic scope growth;
- an atomically enforced runtime intent-dependency graph with producer-first topological order;
- resource-scoped direct invalidation followed by transitive stale propagation, structured notices, and acknowledgement;
- bounded worker context packs instead of replaying planner conversations;
- Git hunk collection with declared-region verification;
- Python-first typed and qualified callable extraction;
- fail-closed structured preserve policies with repository-wide contract inventory;
- opt-in single-worktree acceptance and automatic worker acceptance inside integration runs;
- batch verification that permits proven disjoint same-file hunks and blocks actual overlap;
- semantic checks for deprecated or non-canonical terminology in changed text;
- deterministic targeted repair plans;
- bounded headless Codex worker execution in Claim Plane-owned worktrees, with atomic launch/restart ceilings, shared-admission and dynamic-scheduler gating, token and wall-time slices, JSONL evidence capture, cancellation, and durable terminal classification;
- a verified multi-worktree integration pipeline that freezes each worker into an immutable Git tree, verifies one exact patch, applies those same bytes in dependency order, and invokes bounded external repair adapters;
- governed admission by default: mutable refs such as `main` are rejected before work begins unless the intent carries an exact `base_commit`; explicit `--exploratory` mode preserves unpinned local experiments;
- trusted observation sessions stored inside the control-plane database, with hash-chained events, HMAC-authenticated records, explicit sealing, monitor identity, coverage declarations, and dynamic dependency checks;
- a brokered execution mode in which an external Unix-socket proxy performs intent-authorized file reads/writes, records server-side evidence, and can run workers inside a Linux Bubblewrap boundary with no repository mount;
- exact broker capabilities: full writes cannot delete or rename, `extend` is append-only, deletion requires `delete`, and rename destinations must be declared explicitly;
- a durable write-ahead broker journal that records pending operations before filesystem effects, commits observations atomically afterwards, and rolls back or recovers incomplete mutations;
- live capability validation on every broker request, including intent state, lease, content version, fingerprint, base commit, repository identity, and open session status;
- broker-instance attestation bound to one intent version, repository root, policy digest, binary digest, session, base commit, initial Git tree, and exclusive writer-lease policy, with independently verified operation prepare/commit HMACs;
- one atomic active-writer lease per governed worktree inside the authoritative registry, renewed on every broker request and released or expired fail-closed;
- one OS-level writer lock per physical worktree, preventing two local Claim Plane instances with separate SQLite databases from writing the same directory;
- monotonic fencing tokens bound to broker instances, leases, operations, observations, and evidence, so superseded writers fail closed;
- mode-safe broker writes that preserve executable bits, verify old/new POSIX modes, and restore them on rollback;
- complete claim, intent, observation, broker, and verification store contracts, with `SQLitePlaneStore` as the permanent single-host Community backend and `Plane.from_store(...)` as the injection point for future network backends;
- a broker-derived Git-tree chain in which every mutation is a compare-and-swap transition and the final frozen worker snapshot must match the last committed broker tree exactly;
- clean-root enforcement at broker startup and live rejection of any out-of-band tracked or non-ignored untracked mutation;
- allowlisted build/test execution on immutable repository snapshots, with sandbox policy and root-worktree mutation isolation;
- `brokered` observation policy that rejects generic or worker-authored traces and requires `brokered_proxy` sessions containing only Claim Plane broker events;
- legacy JSON/JSONL traces remain available only for optional or required compatibility modes and are rejected by trusted observation policy;
- configurable worker, integration, and repair sandboxes (`tree`, `bwrap`, `bwrap-minimal`, `sandbox-exec`, or `auto`) with fail-closed strict mode, explicit read/write allowlists, minimal Linux namespaces, and sanitized environments;
- separate file and canonical JSON digests plus optional HMAC-SHA256 or Ed25519 evidence attestation, package-source digest, schema-bundle digest, policy-bundle digest, and runtime provenance;
- read-only-by-default worker and integration acceptance guards that reject tracked or non-ignored untracked mutations;
- SHA-256 evidence binding worker patches, manifests, result trees, result commits, and reproducible result patches;
- transparent economy/standard/frontier worker-tier recommendations;
- a public runtime-neutral Agent Adapter Protocol with stable request/session/run/intent identities, persistent idempotency, stale intent-version rejection, structured failures, explicit cancellation and resume semantics, and Codex as the first complete implementation;
- machine-readable adapter capability and guarantee manifests with explicit enforcement levels, guarantee providers, adapter/runtime version binding, policy compatibility checks, lifecycle evidence projection, and executable conformance coverage;
- an adapter registry with semantic protocol negotiation, project-local adapter/runtime pins, fail-closed migration diagnostics, built-in and entry-point discovery, and negotiated-version evidence binding;
- project-local Codex enrollment with a stable lifecycle dispatcher, idempotent hook installation, session-bound task bootstrap, pinned Git bases, atomic ChangeIntent admission, pre-mutation authorization, ticketed scope amendment, and verified completion for autonomous Codex work;
- one-command controlled Codex execution with preflight negotiation, policy compatibility, bounded process lifetime, run/session evidence binding, safe cancellation, final Git verification, stable terminal outcomes, and secret-safe durable results;
- repository-bound swarm sessions with exact Git bases, planner-proposed work items, deterministic DAG validation, graph fingerprints, dependency layers, and optimistic graph-version replacement;
- versioned swarm budget policies with hard worker, graph-size, launch, token, cost, wall-time, retry, and concurrency ceilings that the planner cannot widen silently;
- adaptive concurrency plans that combine the dependency DAG with region, overlap, contract, schema, semantic cross-file dependencies, and worker-budget constraints to produce deterministic execution waves or a fail-closed `replan_required` result;
- Same-file Admission v2 that can admit same-file work when graph-backed symbol mutations are proven `independent` or explicitly `commutative`, while preserving explicit deny/serialize policy and deterministic ordering for mutation-sensitive producer-consumer dependencies;
- Semantic Amendment Protocol v2 that requires monotonic scope growth, caps new authority and propagated impact, checks additional semantic resources against active intents inside the amendment transaction, and leaves ordered overlap explicit until refresh/resume can establish the required order;
- Runtime Premise Fencing that atomically revokes live broker mutation authority when a tracked premise becomes stale, fails prepared operations, releases the writer lease, and persists source-bound fence evidence;
- deterministic runtime pause/refresh/resume that keeps stale workers fenced, requires stale-causing producers to complete, re-admits the unchanged authority surface on a new pinned base, and requires an explicit resume before a fresh broker receives a higher fencing token;
- shared swarm admission that derives one deterministic ChangeIntent per work item, admits concurrent authority against the whole session, projects redundant broad file writes onto committed semantic authority for conflict analysis, and promotes serialization constraints into effective dependencies;
- a dynamic dependency scheduler that releases only admitted, prerequisite-complete work within current worker capacity and distinguishes runnable, active, retryable, terminal, and dependency-blocked items;
- Deterministic Integration v2 on the managed merge queue: successful worker snapshots are reduced to actual path/region/semantic mutation surfaces, checked against admitted authority before replay, re-mapped to structural owners after replay, re-checked against already integrated semantic changes, and only then committed on the Claim Plane-owned integration branch;
- bounded fail-closed integration rescue: transient integration failures can retry the same immutable snapshot, while textual conflicts or stale ordered dependencies can invalidate only the affected successful run and re-execute that work serially from the current integration head; authority violations, semantic ambiguity, post-apply drift, and exhausted repair budgets remain blocked for explicit replanning or review;
- an offline deterministic concurrency conformance suite with versioned canonical scenarios and machine-readable safety/selectivity metrics, runnable with `claim-plane swarm conformance`;
- two-level swarm verification that checks each integrated work item against its admitted scope, reruns work-item and root acceptance on the managed integration head, detects acceptance-induced mutations, and persists a final `SWARM VERIFIED` evidence report;
- crash-safe swarm recovery with worker heartbeat leases, orphan detection, durable pause/resume/cancel controls, and fresh-identity replacement that rechecks authority and never silently inherits predecessor edits;
- one-command swarm operation with bounded parallel dispatch, compact status, normalized logs, deterministic integration, final verification, and an offline three-worker demo;
- CLI, stdio MCP, JSON Schemas, examples, and a deterministic protocol benchmark.

The base package has no runtime dependencies. Agent Lexicon remains an optional semantic layer.

The brokered boundary is Linux-first. On macOS, the broker and verification pipeline work normally, while non-bypassable repository isolation should run in a Linux VM/container with Bubblewrap.

</details>

## Installation details

The quick-start installation is:

```bash
uv tool install claim-plane
# or: pipx install claim-plane
```

Optional semantic identity and evidence signing extras:

```bash
uv tool install "claim-plane[semantic,signing]"
```

Verify the installation and public CLI contract:

```bash
claim-plane --version
claim-plane preview
claim-plane exit-codes
```

For development from a checkout:

```bash
pip install -e ".[dev,signing]"

# Optional local Agent Lexicon checkout
pip install -e ../agent-lexicon
```

Run the complete checks, the focused interactive authority suite, and the example:

```bash
./scripts/check.sh
./scripts/check-interactive-safety.sh
./scripts/demo.sh
```

## Adapter guarantees

Inspect the effective Codex capabilities before selecting an enforcement policy:

```bash
claim-plane adapters inspect codex --repo .
claim-plane adapters inspect codex --repo . --policy guarded
claim-plane doctor codex --repo . --policy strict
```

The manifest distinguishes `HARD_BLOCKED`, `OBSERVED`, `POST_VERIFIED`, and `UNAVAILABLE` behavior and identifies whether each guarantee comes from Claim Plane, the adapter, the runtime, or their composition. Policy compatibility fails closed when the selected level requires a guarantee that the current runtime boundary cannot provide. The manifest digest and effective adapter/runtime identity are included in normalized session evidence.

Run the shared compatibility suite without invoking a model provider:

```bash
claim-plane adapters conformance codex
claim-plane adapters conformance reference --out conformance.json
```

The same thirteen scenarios are applied to the dependency-free reference adapter and Codex. The report covers declared and undeclared mutations, atomic amendments, stale authority, lease expiry, idempotency, invalid event order, crash resume, cancellation, completion coverage, corrupt state, and secret redaction. Every available guarantee must map to passing scenarios; an uncovered or failed claim makes the report incompatible and returns a non-zero exit code.

## Adapter registry

Discover available adapters, verify protocol compatibility, and pin the selected runtime before controlled work:

```bash
claim-plane adapters list --inspect
claim-plane adapters doctor codex --repo .
claim-plane adapters pin codex --repo .
```

The handshake negotiates the installed Claim Plane protocol against the adapter's semantic version range and reports the adapter, runtime, capabilities, guarantees, source, and project pin. An incompatible range or a pinned adapter/runtime mismatch fails before a Codex session starts. The pin is stored under `.claim-plane/adapters/pins/` and may be removed with `claim-plane adapters pin codex --clear`.

External adapter packages can publish a `claim_plane.adapters` Python entry point. They are discovered without changing Claim Plane Core and use the same manifest, conformance, handshake, and pinning paths as Codex.

## Interactive Codex launcher

Use the normal Codex conversational TUI without giving up Claim Plane authority or
final evidence:

```bash
claim-plane codex --policy guarded
```

An optional initial prompt opens the same TUI with the first task already submitted:

```bash
claim-plane codex "Fix timeout handling and update its regression test" \
  --scope src/connectors/github.py \
  --policy guarded
```

Claim Plane owns the working directory, workspace-write sandbox, approval policy,
model override, initial scope, and final verifier. Codex still owns the interactive
conversation. Each completed turn is recorded without ending the controlled session; a
follow-up prompt continues under the same admitted intent. When the TUI exits, Claim
Plane independently verifies the final Git state, runs configured acceptance, records
its duration, seals the controlled-run record, and prints the same delivery card used
by one-shot execution. Scope remains automatic unless the operator supplies `--scope`;
`--lock-scope` disables amendments. Brokered expansion requires a concrete rationale
that explains why the exact denied resource is necessary for the task. Explicit operator requests to add or update tests are retained as structured completion obligations without storing prompt text. Final verification therefore rejects a delivery when the requested test artifact was not changed, even if the admitted source change is scope-clean and the pre-existing test suite still passes.

## One-command controlled Codex run

After enrollment and diagnostics, one command owns the bounded Codex process, authority lifecycle, and final Git verification:

```bash
claim-plane init
claim-plane connect codex
claim-plane doctor
claim-plane run "Add pagination to the audit API" --policy guarded
```

The runner performs adapter negotiation and policy compatibility checks before execution, launches Codex in workspace-write mode, binds the runtime session to a stable run identity, and preserves the normal `ChangeIntent` admission and amendment path. `Ctrl-C` and wall-time expiry stop the process and revoke unfinished authority. A successful runtime exit is not sufficient for a green result: Claim Plane inspects the active intent, verifies completion against the current Git state, and returns `DELIVERY VERIFIED`, `REJECTED`, `REVIEW REQUIRED`, `CANCELLED`, `TIMED OUT`, or `FAILED`.

The default terminal view is intentionally compact: it shows preflight, Codex lifecycle, final scope and acceptance checks, risk, changed files, duration, and the evidence location without dumping raw runtime logs. The agent's final message is labelled as untrusted context rather than verification evidence. Use `--verbose` when diagnosing the underlying Codex stream, or `--json` for automation.

Scope remains automatic for normal use. For a reproducible initial authority boundary, repeat `--scope` with repository-relative files or directories; a genuinely required additional file must pass through the brokered amendment path. Add `--lock-scope` only when CI or an operator must forbid every expansion.

```bash
claim-plane run "Fix timeout handling and update its regression test" \
  --scope src/connectors/github.py \
  --policy guarded
```

The durable result is written under `.claim-plane/runs/<run-id>/run.json`. It contains task and final-message digests rather than raw text, the starting and resulting Git-state digests, adapter manifest and handshake identity, policy compatibility, lifecycle evidence, verification summary, final file and hunk metadata, configured acceptance commands, and cancellation outcome. Use `--out result.json` for an additional export, `--timeout` for the wall-time ceiling, and `--model` for an explicit Codex model override.

## Evidence report and replay

A completed controlled run can be inspected without reopening Codex or repeating provider calls:

```bash
claim-plane report latest
claim-plane replay latest
claim-plane report <run-id> --json --out evidence-report.json
claim-plane replay <run-id> --json --out evidence-replay.json
```

The report is rebuilt from the durable run record and normalized append-only lifecycle journal. It includes task digests, repository bindings, adapter and runtime identity, effective policy, risk findings, guarantee levels, final changed files, hunk coordinates, acceptance status, usage, elapsed time, blocked attempts, observed mutations, scope amendments, verification, and a canonical evidence digest. Source content, raw prompts, tool payloads, credentials, and the final agent message are not exported.

Replay renders the same causal event stream as a stable decision chronology. It is a reconstruction of stored authority transitions, not a new model execution. A corrupt, out-of-order, or mismatched lifecycle stream cannot be replayed or represented as valid evidence.

## Policy presets and risk classes

Inspect the effective policy before running an agent and classify sensitive paths without starting Codex:

```bash
claim-plane policy inspect --repo .
claim-plane policy inspect --policy strict --repo .
claim-plane policy classify src/auth/session.py migrations/0042_tokens.sql --repo .
```

The public presets have stable semantics:

- `observe` records supported would-deny decisions but lets the runtime continue; final Git verification remains mandatory. Control-plane files, corrupt state, branch drift, and pre-existing user changes are never weakened by shadow mode.
- `guarded` blocks supported undeclared mutations, routes scope growth through atomic re-admission, and marks high- or critical-risk delivery for review.
- `strict` fails closed for unknown, destructive, network, secret, and critical-resource actions. It starts only when the adapter manifest proves the required guarantees.
- `critical` requires a human gate for every delivery and denies critical-resource mutation without stronger authority. It never represents an automatic merge decision.

Repository risk is deterministic and path based. The default is `medium`; built-in rules identify CI workflows, migrations, secret material, review authority, package contracts, and runtime topology. Projects can add rules in `.claim-plane/config.yaml`:

```yaml
risk:
  default: medium
  include_builtin_rules: true
  rules: [{"match": "src/auth/**", "level": "critical", "reason": "authentication boundary"}]
```

When several rules match, the highest risk wins. The run evidence stores the full effective policy, its digest, every changed-path classification, reason codes, and the final policy action. A runtime and acceptance result that would otherwise be `VERIFIED` becomes `REVIEW_REQUIRED` or `REJECTED` when the effective risk policy requires it.

## Dogfood and golden task suite

The single-agent technical preview is evaluated on one frozen task corpus rather than changing examples between runs. The suite binds repository commits, task prompts, source references, acceptance commands, task classes, risk classes, coder seeds, and the fixed three-arm comparison:

```text
Bare Codex
Claim Plane Observe
Claim Plane Guarded
```

Freeze and validate the reviewed corpus before any provider calls, then expand it into a deterministic run matrix:

```bash
claim-plane dogfood freeze candidate.json --release-grade --out golden-suite.json
claim-plane dogfood validate golden-suite.json --release-grade
claim-plane dogfood plan golden-suite.json \
  --release-grade \
  --model <model> \
  --out run-plan.json
```

A release-grade suite requires 20–30 tasks, 5–10 repositories, at least two coder seeds, multiple task and risk classes, full repository commit SHAs, and explicit acceptance commands. Each task/seed/arm cell has a stable execution identity. The same frozen task and acceptance contract are reused across all arms.

Execution and evaluation produce `claim-plane.dogfood-result.v1` documents. Bind each measured evaluator output to its immutable plan cell before aggregation:

```bash
claim-plane dogfood record \
  run-plan.json <execution-id> evaluation.json \
  --out results/<execution-id>.json
```

Aggregation never fabricates missing measurements and fails completeness when a cell is absent, duplicated, unexpected, or bound to the wrong suite or plan:

```bash
claim-plane dogfood aggregate \
  golden-suite.json run-plan.json results/*.json \
  --release-grade \
  --out release-summary.json

claim-plane dogfood gate release-summary.json
```

The summary reports task success, accepted delivery, undeclared and missed mutations, scope amendments, false blocks, human repairs, retries, wall time, token and cost fields when available, changed files and lines, public API drift, and dependency drift. The release gate returns `INCOMPLETE` for a partial matrix and `BLOCKED` when guarded mode materially reduces task success without the configured accepted-delivery improvement. It does not present example values as measured results. See `benchmark/golden-suite/README.md` for the full artifact flow.

## Codex swarm operator

Version 0.31.0 exposes the complete swarm lifecycle through one bounded operator command:

```bash
claim-plane init
claim-plane swarm start --spec swarm-session.json
```

For an existing session:

```bash
claim-plane swarm start <session-id>
claim-plane swarm status <session-id>
claim-plane swarm logs <session-id> --follow
```

`swarm start` does not grant new authority. It materializes the current concurrency plan, shared admission, managed worktrees, and merge queue, then dispatches only scheduler-runnable work within the session budget. Successful workers are integrated in deterministic order and the session reaches `COMPLETED` only after two-level verification produces `SWARM VERIFIED`. Merge conflicts, dirty failed worktrees, stale state, or control-plane errors stop the operator loop and remain explicit recovery work.

Run the fully offline demo without an API key or network access:

```bash
claim-plane swarm demo
```

The demo creates three work items, runs two independent workers concurrently, releases the dependent worker only after integration, and leaves the repository and evidence available for inspection.

## Swarm planning foundation

Claim Plane can persist a planner-proposed swarm decomposition and a hard execution budget before any worker is launched:

```bash
claim-plane init
claim-plane swarm validate-budget --policy examples/swarm/budget-policy.json --work-items 2
claim-plane swarm create --spec examples/swarm/session-spec.json
claim-plane swarm status <session-id>
claim-plane swarm graph <session-id>
claim-plane swarm budget <session-id>
claim-plane swarm plan <session-id>
claim-plane swarm concurrency <session-id>
claim-plane swarm admit <session-id>
claim-plane swarm admission <session-id>
claim-plane swarm scheduler <session-id>
claim-plane swarm merge-plan <session-id>
claim-plane swarm merge-queue <session-id>
```

The session is bound to one repository identity and exact Git commit. Work items carry proposed operations, preserve requirements, acceptance commands, and explicit dependencies. Claim Plane rejects duplicate identifiers, missing dependencies, cycles, repository-escaping paths, and attempts to include control-plane or Git state.

The budget is a separate versioned protocol object. It caps active workers, per-work-item concurrency, work-graph size, total launches, token use, cost, wall time, replans, repairs, and restarts. It also records fail-closed policies for same-file work, unknown overlap, shared contracts, and schema changes. Work graph and budget replacements each require their own expected version, preventing concurrent planner updates from silently overwriting one another. The adaptive concurrency controller consumes the exact graph and budget versions, adds deterministic serialization constraints, packs safe work up to `max_active`, and persists source-bound execution waves. A graph or budget replacement invalidates that plan atomically. Shared admission then derives one source-bound ChangeIntent per work item and checks the authority topology before execution. The dynamic scheduler combines those admitted intents, effective dependencies, durable run state, retry ceilings, and remaining worker capacity to release only currently runnable work. Graph, budget, or concurrency-plan changes invalidate the admission record atomically. Once `merge-plan` exists, dependency release is integration-aware: a successful prerequisite must reach the managed integration branch before a dependent worker can start.

See [Swarm sessions and work graphs](docs/swarm-sessions.md) for the complete format and CLI.

## Codex project enrollment

Claim Plane can register a project-local lifecycle bridge for Codex without replacing the normal `codex` command:

```bash
cd my-project
claim-plane init
claim-plane connect codex
claim-plane doctor

codex
```

`claim-plane init` creates a stable project identity, writes the versioned `.claim-plane/config.yaml`, discovers the default branch and likely acceptance commands, prepares local state, and keeps `.claim-plane/` out of Git status through the repository-local exclude file. Re-running initialization preserves the project identity and user-edited acceptance commands.

`claim-plane connect codex` installs Claim Plane-owned handlers in `.codex/hooks.json` while preserving unrelated project hooks. It records the detected runtime and sandbox characteristics and creates an exact adapter pin when the runtime reports a version. Re-running enrollment is safe and does not duplicate handlers.

The connector registers one stable dispatcher for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and `SessionEnd`. Codex discovers these project-local hooks automatically for trusted projects. On the first Codex session, open `/hooks` to review and trust the command hooks; Codex records trust against the hook definition.

For the first task in a session, Claim Plane pins the current Git commit, creates a private session-bound task identity, and injects model-visible coordination context through `UserPromptSubmit`. Codex can inspect the repository read-only, propose the expected committed and contingent scope, preserve requirements, and advisory acceptance checks, then submit that proposal through the local CLI. Claim Plane supplies the intent ID, owner, task ID, and immutable base commit itself before performing normal atomic admission. Project-configured acceptance remains operator-owned: when configured commands exist, Codex cannot replace or extend the executable final-verification contract, and its proposal is retained only as audit metadata.

The raw user prompt is not stored in connector state. The task record keeps a SHA-256 digest and prompt length for correlation, while the admitted intent stores the explicit goal and execution contract that Codex proposed. Repeated admission of identical content is idempotent, and a changed Git `HEAD` before admission is rejected as a stale bootstrap.

Once admitted, normal Codex prompt and tool lifecycle events renew the active intent lease automatically. The model does not need to issue a separate heartbeat command during an active session.

For intercepted `PreToolUse` calls, Claim Plane classifies repository effects before execution. Read-only calls continue normally. A mutation already covered by committed scope continues through Codex's normal sandbox and approval flow. A mutation covered by one contingent declaration is atomically promoted and re-admitted before the tool call continues. Undeclared mutations, stale-base mutations, unknown mutating tool surfaces, and shell commands whose repository effects cannot be proven are denied with model-visible guidance. Claim Plane stores the decision, affected paths, and counters without persisting raw tool arguments.

When a denial identifies concrete additional file authority, the guard issues a short-lived, session-bound scope-amendment ticket containing only the exact denied mutation set. Codex supplies a rationale through `claim-plane codex-intent amend`; it cannot choose different paths, change the task identity, change the pinned base, or remove existing preserve and acceptance requirements. Claim Plane derives the amended ChangeIntent, checks ticket integrity and intent freshness, performs normal atomic re-admission, and activates the amended intent only when admission succeeds. A rejected amendment leaves the previously active intent unchanged. Multiple contingent resources that cannot be promoted individually in one tool call can therefore be committed together through one inspectable amendment.

The connector also reserves a narrow shell control channel for `claim-plane codex-intent admit`, `status`, `amend`, `verify`, and `abandon`. The command must target the current Codex session and current repository; initial admission uses `--proposal-json` rather than a shell pipe or repository temporary file. Other Claim Plane commands and opaque shell effects remain subject to the normal fail-closed classification. Connector control state under `.claim-plane/**`, `.git/**`, and `.codex/**` cannot be granted through a session intent or amendment.

For a directly launched Codex session, `Stop` remains the bounded verified-completion checkpoint. Under `claim-plane codex`, however, `Stop` is only a conversational turn boundary: the hook reports `AGENT TURN COMPLETED` and `final verification pending`, allowing the user to continue the TUI without a premature green result. After the user exits, the launcher collects tracked and untracked repository changes, checks the actual work against admitted scope and preserve/contract policy, executes declared acceptance with worktree-integrity checks, and seals normalized verification before session end.

A clean launcher-owned completion is persisted and surfaced as `VERIFIED` with changed-file counts, mutation-authority counters, scope expansions, acceptance outcome, and verification findings. Direct sessions retain the bounded repair continuation: a failed first Stop can return findings once, and a still-failing continuation ends explicitly `UNVERIFIED` instead of looping indefinitely. The same gate remains available through `claim-plane codex-intent verify --session-id <id> --repo .` and `claim-plane codex-intent status`.

The connector hardens long-running local use as well. Pre-existing user changes are fingerprinted at task bootstrap: unchanged pre-existing paths are excluded from task attribution, while Codex is denied mutation authority over those paths so existing work is not silently mixed into an autonomous task. Only one active Codex session may hold mutation authority in a physical worktree; independent concurrent sessions should use separate Git worktrees, where normal Claim Plane coordination still applies. A resumed session renews a live intent automatically and can re-admit an expired intent only when the pinned commit and branch are unchanged. Changed repository state, released or stale authority, corrupted session state, missing enrollment state, branch switches, and connector hook drift all fail closed. Re-running `claim-plane connect codex` repairs connector-owned hook definitions while preserving unrelated hooks.
If an unfinished session is intentionally discarded, `claim-plane codex-intent abandon --session-id <id> --repo .` releases its intent authority so another Codex session can use the same worktree immediately.

```text
committed mutation  -> authorize -> Codex sandbox/approval -> execute
contingent mutation -> promote + re-admit -> authorize -> execute
undeclared mutation -> deny -> exact ticket -> reason -> re-admit -> retry
unprovable mutation -> deny before tool execution
direct Codex Stop -> collect evidence -> VERIFIED or bounded repair continuation
claim-plane codex Stop -> turn completed -> exit TUI -> acceptance -> VERIFIED
```

`claim-plane doctor` checks Git and worktree state, project configuration, state-directory permissions, acceptance commands, credential hygiene, Codex runtime and authentication availability, sandbox characteristics, adapter negotiation, and the hook surface required by the guard. `claim-plane doctor codex` remains an equivalent explicit form. Hook interception is an integration boundary, not a substitute for the brokered reference-monitor boundary: runtime hook coverage and timeout behavior remain properties of Codex itself. Claim Plane therefore keeps broker capabilities, repository identity, admission, and verification as the authoritative core primitives.

The session-bound proposal protocol is documented by [`schemas/codex-intent-proposal.schema.json`](schemas/codex-intent-proposal.schema.json), the amendment ticket by [`schemas/codex-scope-amendment.schema.json`](schemas/codex-scope-amendment.schema.json), and verified completion by [`schemas/codex-completion.schema.json`](schemas/codex-completion.schema.json). The lifecycle bridge does not depend on MCP; MCP remains an optional interaction surface for status, explanation, and evidence.

To remove only the Codex bridge without disturbing other hooks:

```bash
claim-plane disconnect codex
```

To clear Claim Plane-owned local state while preserving repository files, unrelated hooks, and `.claim-plane/config.yaml`:

```bash
claim-plane reset
```

Use `claim-plane reset --remove-config` only when the project enrollment itself should also be removed.

If `.codex/config.toml` explicitly sets `[features] hooks = false`, enrollment fails rather than overriding the project's Codex policy. If inline Codex hooks already exist in that file, Claim Plane leaves them untouched and reports that Codex will merge both project-local hook sources.

## ChangeIntent

A contract must name the concept it governs. An unrelated shared contract cannot make a semantic overlap safe.

```json
{
  "intent_id": "rate-limit-metrics",
  "task_id": "issue-142-metrics",
  "owner": "agent-metrics",
  "base_revision": "main",
  "base_commit": "<40-character-git-commit>",
  "dependencies": ["rate-limit-core"],
  "operations": [
    {
      "access": "write",
      "kind": "file",
      "identifier": "src/rate_limit/metrics.py"
    },
    {
      "access": "extend",
      "kind": "concept",
      "identifier": "RequestThrottler"
    },
    {
      "access": "read",
      "kind": "contract",
      "identifier": "allow",
      "signature": "allow(request)->RateLimitDecision",
      "subject_concept_id": "RateLimiter"
    }
  ],
  "preserves": [
    "contract:RateLimiter::allow=allow(request)->RateLimitDecision"
  ],
  "acceptance": [
    "pytest tests/rate_limit/test_metrics.py"
  ]
}
```

Admit it atomically:

```bash
claim-plane \
  --db .claim-plane/plane.db \
  --semantic \
  --lexicon examples/rate-limiter/lexicon.yaml \
  admit examples/rate-limiter/intents/metrics.json
```

Generate a compact context pack:

```bash
claim-plane --db .claim-plane/plane.db context rate-limit-metrics
```

The pack includes only the admitted surfaces, canonical concepts, contracts, dependencies, acceptance criteria, current notices, and worker rules.

## Adaptive scope

Operations may be marked as `committed` or `contingent`:

```json
{
  "access": "write",
  "kind": "file",
  "identifier": "src/click/shell_completion.py",
  "commitment": "contingent"
}
```

Committed operations participate in admission immediately and grant mutation authority.
Contingent operations are planning hints: they do not reserve write ownership during
initial admission. Before the first mutation, the scope must be promoted and re-admitted
atomically. A failed promotion leaves the current intent unchanged.

```bash
claim-plane --db .claim-plane/plane.db \
  promote-scope worker-intent src/click/shell_completion.py --mode write --region lines:20-24
```

A governed broker performs the same promotion automatically when a worker first attempts
to mutate a predeclared contingent path. Broad contingent globs are narrowed to the
concrete path being requested rather than promoted as one broad write reservation.
Contingent surfaces may be inspected read-only before promotion, and those possible read
premises still participate in coordination against active writers.

## Code intelligence providers

Claim Plane exposes a versioned provider boundary between language-specific static analysis and
the language-neutral Semantic Dependency Graph consumed by impact, conflict, and admission
reasoning. The built-in Python provider wraps the existing non-executing AST analysis; callers can
pin a provider explicitly, and future backends can implement the same contract without changing
core concurrency semantics.

```python
from claim_plane import CodeIntelligenceRequest, analyze_code_intelligence

snapshot = analyze_code_intelligence(
    CodeIntelligenceRequest(
        language="python",
        sources={"src/app.py": "def run():\n    return 1\n"},
        revision="abc123",
    )
)

print(snapshot.provider_id)
print(snapshot.graph.fingerprint)
```

Provider manifests declare supported languages and capabilities, including whether repository
analysis is non-executing. Provider selection is deterministic and can be pinned by id when a
repository needs a specific backend. The existing `build_python_dependency_graph` API remains
available unchanged.

SCIP indexes can be prepared independently of provider selection. Claim Plane gives the external
indexer an explicit repository root and an isolated temporary output path, validates the emitted
`index.scip`, and stores it under a revision-aware user cache outside the repository. Cache identity
includes the checked-out Git revision, a content fingerprint for dirty or untracked workspace state,
the indexer identity/version, the analyzed Python environment fingerprint, and project metadata. A
stale explicit revision fails closed rather than labeling the current checkout as an older source
state.

```python
from claim_plane import ScipIndexManager

artifact = ScipIndexManager().index_repository(".")
print(artifact.cache_hit, artifact.revision, artifact.index_path)
```

The default Python command is `scip-python`; it remains an external tool rather than a Python
runtime dependency of Claim Plane. Index generation and cache accounting remain separate from
semantic conversion. A sealed artifact can then be projected into Semantic Resource IR while
preserving raw SCIP occurrences and relationships as revision-bound evidence for later graph
enrichment.

```python
from claim_plane import ScipIndexManager, build_scip_semantic_resource_index

artifact = ScipIndexManager().index_repository(".")
resources = build_scip_semantic_resource_index(artifact)
print(resources.fingerprint, len(resources.resources))
```

The projected index can be converted into an evidence-bearing Semantic Dependency Graph.
SCIP occurrence roles produce document-to-symbol define/import/read/write/reference edges, while
declared symbol relationships remain explicit reference, implementation, type-definition, or
definition relations. Every SCIP-backed edge records the source revision, workspace fingerprint,
artifact digest, provider identity, and available source coordinates.

```python
from claim_plane import build_scip_dependency_graph

graph = build_scip_dependency_graph(resources)
for edge in graph.edges:
    if edge.evidence:
        print(edge.relation.value, edge.evidence[0].provider_id)
```

SCIP does not claim to be a call graph, so ordinary reference evidence is not relabeled as a call.
Language frontends can later combine finer structural ownership with this source-bound evidence.
Unresolved global targets stay explicit with `unresolved` resolution instead of being treated as
independent repository resources.

Before pairwise semantic conflict classification, Claim Plane can build an affected-subgraph
candidate plan. Each mutation root is expanded through the reverse dependency surface once, then
an inverted resource index selects only candidate pairs whose affected subgraphs overlap. Broad
file mutations explicitly include the semantic resources they define without using file ownership
as evidence that otherwise independent symbols conflict.

```python
from claim_plane import (
    SemanticMutationCandidate,
    build_affected_subgraph_candidate_blocking,
)

plan = build_affected_subgraph_candidate_blocking(
    graph,
    (
        SemanticMutationCandidate("worker-a", left_changes),
        SemanticMutationCandidate("worker-b", right_changes),
    ),
)
print(plan.selected_pair_count, plan.pruned_pair_count)
```

Candidate blocking is conservative and does not make an admission decision. Missing graph roots,
unknown change semantics, unresolved dependency boundaries, or bounded traversal retain the
affected candidate against every other candidate. An omitted pair therefore means both sides had
complete unbounded graph evidence and disjoint affected subgraphs.

Swarm planning consumes that proof before semantic conflict classification. Pairs omitted by the
blocker skip the expensive graph classifier; retained pairs continue through the ordinary
independent/commutative/ordered/conflicting decision path. Under `region_safe` same-file policy, a
complete disjoint affected-subgraph proof can admit the semantic mutations directly, while explicit
`serialize` and `deny` policy remains authoritative. Runtime scheduling uses the resulting
graph-derived dependency edges as a continuous runnable frontier, so a dependency-ready work item
can use newly available capacity without waiting for every item in an earlier advisory wave.

For repository-bound swarm planning, a clean checkout at the pinned session revision can enrich the
builtin Python dependency graph with cached SCIP evidence automatically. The builtin graph keeps
canonical local resource identities; SCIP contributes additional evidence-bearing relations and
provider-only external nodes. If the checkout is dirty, the pinned revision differs from `HEAD`, or
the optional SCIP indexer is unavailable, planning falls back to the non-executing builtin graph
read directly from the pinned Git revision.

Builtin semantic graphs are also cached by repository identity and pinned revision outside the
repository. Across revisions, Claim Plane compares source digests, invalidates the changed semantic
component in both dependency directions, rebuilds only the affected Python paths with full
repository resolution context, and retains disconnected components unchanged. New source paths,
old unresolved edges whose resolution may change, or missing old graph roots force a full rebuild.
Every graph-aware concurrency plan records the semantic source revision and refresh/invalidation
evidence; a stored plan whose semantic graph no longer matches the swarm base is fenced before
shared admission rather than silently reused. SCIP edge evidence is likewise rejected when its
revision or workspace fingerprint disagrees with the pinned source state.

Local project symbol identity deliberately excludes SCIP package version because the Python
indexer is bound to the current Git revision. The revision remains provenance metadata, while the
same function or class keeps one stable Claim Plane identity across commits. External package
identity retains scheme, package manager, package name, and descriptors while treating package
version as evidence rather than authority identity. Local SCIP-only symbols are not promoted into
repository authority resources.

## Python structural extraction

Python repositories can be projected into Semantic Resource IR v2 without importing or
executing project code. The standard-library AST extractor records classes, functions,
methods, async definitions, signatures, decorators, lexical owners, and inclusive source
spans. Stable symbol identity is based on repository path plus qualified name, so moving a
definition or evolving its signature does not silently create a new authority coordinate.

```python
from claim_plane import extract_python_structure

index = extract_python_structure(source, path="src/parser.py")
owners = index.owners_for_region(40, 52)
for resource in owners:
    print(resource.identity)
```

`owner_for_line()` returns the most specific enclosing symbol and falls back to the file
resource for module-level code. Decorator lines belong to the decorated definition. Repeated
logical definitions such as `typing.overload` declarations share one stable semantic identity
while retaining distinct source occurrences in the structural index. Syntax errors fail closed
with file/line coordinates, and file-based extraction is confined to an explicit repository
root.

Dependency Graph v2 builds on that structural index and a second non-executing AST pass to
resolve repository-local imports, calls, inheritance, type references, shared state reads/writes,
test relationships, and public API surfaces. Resolved repository targets are marked `internal`;
known package/module targets remain `external`; ambiguous lexical targets remain `unresolved`
instead of being silently treated as safe.

```python
from claim_plane import build_python_dependency_graph

graph = build_python_dependency_graph({
    "src/parser.py": parser_source,
    "tests/test_parser.py": test_source,
})

for dependent in graph.dependents("symbol:src/parser.py#ParseResult", transitive=True):
    print(dependent.identity)
```

Graph fingerprint is deterministic for identical source inputs, and every edge retains its typed
relation, resolution class, and source locations. Repository extraction remains static analysis:
Claim Plane does not import or execute target modules while building the graph.

Semantic impact analysis consumes that immutable graph and a bounded set of known mutations.
Callable signature changes are classified as contract changes against stable symbol identities, while
body-only edits can be supplied from Git hunk ownership even when the AST dependency shape is
unchanged. Contract changes propagate through callers, type users, subclasses, importers, shared
state consumers, and tests; implementation-only changes use a narrower propagation surface.

```python
from claim_plane import analyze_graph_change_impact

impact = analyze_graph_change_impact(
    before_graph,
    after_graph,
    changed_identities={"symbol:src/parser.py#Parser.parse"},
)
for item in impact.impacted:
    print(item.node.identity, item.min_distance, item.contract_sensitive)
```

Every reached resource retains a deterministic shortest dependency path back to the mutation root.
External and unresolved edges touching impacted code are emitted as explicit analysis boundaries, not
as evidence that the change is safe. This layer produces impact evidence only; admission policy and
runtime mutation enforcement remain separate.

Semantic Conflict Taxonomy v2 turns two such mutation surfaces into a deterministic relationship:

| Classification | Meaning |
|---|---|
| `independent` | No direct mutation overlap, order-sensitive dependency path, or shared unresolved boundary is present in the complete graph evidence. Existing dependency edges between implementation-only mutations may remain independent when the producer contract is stable. |
| `commutative` | A coupling exists but an explicit deterministic commutativity proof covers it. |
| `ordered` | One mutation changes a contract, state, structure, or other semantic premise consumed by the other; the decision records the required direction. |
| `conflicting` | The same resource is changed without a commutativity proof, or semantic ordering forms a cycle. |
| `unknown` | Available evidence is incomplete or ambiguous, so independence is not claimed. |

```python
from claim_plane import SemanticChange, SemanticChangeKind, classify_semantic_conflict

left = SemanticChange(
    identity="symbol:src/parser.py#Parser.parse",
    kind=SemanticChangeKind.CONTRACT,
    after_resource=graph.node("symbol:src/parser.py#Parser.parse").resource,
)
right = SemanticChange(
    identity="symbol:src/service.py#parse_request",
    kind=SemanticChangeKind.IMPLEMENTATION,
    after_resource=graph.node("symbol:src/service.py#parse_request").resource,
)

decision = classify_semantic_conflict(graph, (left,), (right,))
print(decision.kind.value, decision.order.value if decision.order else None)
```

The taxonomy never grants mutation authority by itself. In particular, `commutative` requires
explicit deterministic evidence rather than a naming or text-distance heuristic, and `unknown` stays
fail-closed. Concurrent-writer admission uses mutation-sensitive ordering: an existing dependency edge
alone does not serialize two implementation-only edits when the producer-side contract remains stable.
Contract, state, structural, added, and removed mutations remain order-sensitive. Runtime premise and
amendment checks retain strict dependency ordering because they protect already-active execution state.
Same-file admission consumes this evidence in a later layer.

Same-file Admission v2 is that policy layer for the swarm concurrency planner. Under
`same_file = "region_safe"`, an unknown line-region overlap can be upgraded only when both work
items declare semantic mutation roots for the same path and the pinned Dependency Graph v2 snapshot
classifies them as `independent` or explicitly `commutative`. `ordered` becomes a deterministic
serialization edge in the required direction. `conflicting`, `unknown`, missing roots, and missing
graph evidence never unlock parallel execution. Explicit `same_file = "serialize"` or `"deny"`
remains authoritative even when semantic evidence would otherwise permit concurrency.

Repository-bound swarm planning builds the Python graph from the session's exact pinned Git commit,
not from dirty working-tree content, and stores each Same-file Admission v2 decision and graph
fingerprint in the concurrency-plan metadata. This keeps the concurrency proof tied to the same
repository state as the work graph.

Graph-backed semantic ordering is not limited to same-file work. When two mutation surfaces are on
disjoint files and one changes a contract/type/API/state premise consumed by the other, the planner adds
a deterministic producer-before-consumer edge. An existing dependency between implementation-only
mutations does not create temporal ordering by itself. Missing or unresolved cross-file semantic evidence
stays conservative instead of being treated as proof that separate Git paths are independent.

The deterministic concurrency conformance suite exercises these rules without launching agents or
executing repository code:

```bash
claim-plane swarm conformance
claim-plane swarm conformance --json --out conformance.json
```

The versioned report includes `safe_parallel_recall`, `false_parallel_rate`,
`unnecessary_serialization_rate`, `ordered_dependency_accuracy`, and
`amendment_recovery_rate`. These are canonical control-plane checks, not claims about real-world swarm
throughput; physical overlap and wall-clock speedup are measured by the separate benchmark layer.

Semantic Amendment Protocol v2 applies the same semantic evidence to authority growth. A candidate
amendment must preserve the task identity, owner, pinned base, dependencies, acceptance criteria,
preserve requirements, and already committed operations. Only newly committed mutation authority
is analyzed. Claim Plane projects that authority onto graph-backed resources, propagates downstream
impact, applies hard bounds to operations, paths, semantic roots, impact breadth, traversal depth, and
contract changes, and checks the new surface against other active intents in the same registry
transaction as amendment admission.

`independent` and explicitly proven `commutative` relationships may proceed. `conflicting`,
`unknown`, unresolved dependency boundaries, non-monotonic changes, and exceeded bounds fail closed.
An `ordered` relationship is surfaced as `order` rather than silently admitted. Runtime premise
fencing revokes an active governed writer when its premise becomes stale. Runtime recovery then
requires the stale-causing producer to complete, preserves the worker's declared authority surface,
re-admits it on a new pinned base, and requires an explicit resume before a fresh broker can start.
The preflight and recovery evidence remain separately inspectable.

```python
from claim_plane import SemanticAmendmentBounds

result = plane.amend_bounded(
    candidate_intent,
    graph,
    bounds=SemanticAmendmentBounds(
        max_new_operations=4,
        max_new_paths=2,
        max_impact_resources=64,
    ),
    expected_version=current_version,
)
print(result.assessment.disposition.value, result.allowed)
```

The machine-readable assessment format is documented by
[`schemas/semantic-amendment.schema.json`](schemas/semantic-amendment.schema.json).

## Admission semantics

| Outcome | Meaning |
|---|---|
| `independent` | No relevant active overlap. |
| `compatible_overlap` | Parallel work is allowed under a shared concept-bound contract. |
| `contract_dependency` | A consumer may proceed against a producer contract and is tracked as dependent. |
| `parallel_with_constraint` | Parallel work is allowed only inside declared, disjoint regions. |
| `notify_on_change` | A read premise is tracked and can invalidate the dependent. |
| `requires_stub` | A machine-checkable contract is required before workers start. |
| `serialize` | A known write overlap must run sequentially or be split. |
| `replan` | Signatures, base revisions, or the change graph must be reconciled. |
| `reject` | The declaration is invalid, ambiguous, or references a missing dependency. |

Unknown overlapping writes fail closed. A broad scope such as `src/**` conflicts with a concrete write to `src/core.py`. Two writers may share one file only when their declared regions are disjoint and their actual Git hunks remain inside those regions.

## Amendments and live dependency invalidation

A producer can amend an admitted intent with an optimistic version check:

```bash
claim-plane --db .claim-plane/plane.db amend updated-core-intent.json --expected-version 1
```

When an admitted producer changes a tracked premise, Claim Plane:

1. records the producer change;
2. marks affected dependent intents as `stale`;
3. atomically fences any active governed broker for each stale dependent;
4. fails prepared-but-uncommitted broker operations and releases the writer lease;
5. creates structured coordination notices and durable runtime-fence evidence;
6. exposes those notices in the worker context pack;
7. propagates staleness transitively to downstream consumers whose producer outputs are no longer trustworthy;
8. requires fresh authority before any stale worker can mutate again.

```bash
claim-plane --db .claim-plane/plane.db notices rate-limit-metrics
claim-plane --db .claim-plane/plane.db runtime-fences rate-limit-metrics
claim-plane --db .claim-plane/plane.db ack-notice 1
```

The stale state is coupled to an enforceable broker capability fence. The agent process may still
exist and reason, but its governed mutation path fails closed. After the stale-causing producer has
completed and its result has been integrated into a new pinned base, refresh the unchanged worker
authority and resume it explicitly:

```bash
claim-plane --db .claim-plane/plane.db runtime-refresh refreshed-intent.json
claim-plane --db .claim-plane/plane.db runtime-resume rate-limit-metrics
claim-plane --db .claim-plane/plane.db runtime-recoveries rate-limit-metrics
```

`runtime-refresh` cannot add files, symbols, contracts, acceptance obligations, preserve requirements,
or dependencies. Authority growth still goes through amendment admission. A restarted broker is
registered only after resume and receives a fresh monotonic fencing token; the old broker remains
fenced for audit. This is not a distributed source-code lock.

## Brokered execution boundary

Trusted sessions prove that recorded events were not altered. Brokered mode additionally makes the control plane perform the operation itself. Start a broker outside the worker sandbox:

```bash
export CLAIM_PLANE_BROKER_TOKEN="random-worker-token"
export CLAIM_PLANE_OBSERVATION_KEY="ci-observation-key"
export CLAIM_PLANE_BROKER_KEY="separate-broker-attestation-key"

claim-plane --db .claim-plane/plane.db broker-serve worker-intent worker-session \
  --root ../worker-worktree \
  --socket /tmp/claim-plane-worker.sock
```

Tool adapters call the broker instead of reading the repository directly:

```bash
claim-plane broker-call read_file \
  --socket /tmp/claim-plane-worker.sock \
  --path src/config.py

claim-plane broker-call replace_lines \
  --socket /tmp/claim-plane-worker.sock \
  --path src/core.py --start-line 40 --end-line 55 \
  --content "..."

# `extend` is append-only
claim-plane broker-call append_file \
  --socket /tmp/claim-plane-worker.sock \
  --path docs/notes.md --content "New section\n"

# `rename` must declare rename_to/target/to in ChangeIntent metadata
claim-plane broker-call rename_file \
  --socket /tmp/claim-plane-worker.sock \
  --path src/old.py --target-path src/new.py
```

On Linux, a proxy-only worker can be started with no repository mount:

```bash
claim-plane broker-run \
  --socket /tmp/claim-plane-worker.sock -- \
  your-agent-runtime
```

The worker sees the broker socket and a minimal runtime namespace, not the repository or the host home directory. This boundary is only non-bypassable when the agent runtime has no alternate filesystem mount, shell escape, or privileged host channel. See `docs/BROKERED_RUNTIME.md`.

On macOS and in long CloudStorage/pytest paths, Claim Plane automatically maps an overlong Unix-socket path to a deterministic private path under `/tmp`; the server, client, and `broker-run` resolve the same path transparently.

For build and test workflows, Claim Plane exposes only named commands from a JSON allowlist. Commands run against an immutable snapshot rather than the mutable broker root:

```json
{
  "unit-tests": {
    "argv": ["python", "-m", "pytest", "-q"],
    "timeout_seconds": 300
  }
}
```

```bash
claim-plane --db .claim-plane/plane.db broker-serve worker-intent worker-session \
  --root ../worker-worktree --socket /tmp/claim-plane-worker.sock \
  --commands broker-commands.json

claim-plane broker-call run_command \
  --socket /tmp/claim-plane-worker.sock --name unit-tests
```

## Governed admission and immutable base pinning

A branch name is planning metadata, not an execution guarantee. Claim Plane uses governed admission by default, so an unpinned intent is rejected before a worker starts. Pin it first:

```bash
claim-plane pin-intent intent.json --repo . --out intent.pinned.json
```

A pinned intent contains both:

```json
{
  "base_revision": "main",
  "base_commit": "a81f42c..."
}
```

Integration fails closed when the base repository, an intent, or a worker repository does not contain the same exact commit. If `base_revision` is already a full object ID, Claim Plane normalizes it into `base_commit`. For migration-only local experiments, pass the global `--exploratory` flag or open `Plane` with `governance="exploratory"`.

## Trusted observed read/write evidence

Claim Plane can store runtime accesses inside the control-plane database rather than trusting an editable worker-owned trace file. A trusted monitor or MCP proxy starts a session, records accesses with a server-held HMAC key, and seals it after execution:

```bash
export CLAIM_PLANE_OBSERVATION_KEY="secret-from-ci"

claim-plane --db .claim-plane/plane.db observe-start worker-session worker-intent \
  --monitor-id mcp-proxy --key-id ci-observer --coverage tool_proxy

claim-plane --db .claim-plane/plane.db observe-record worker-session \
  --key-env CLAIM_PLANE_OBSERVATION_KEY \
  --mode read --kind file --identifier src/config.py --tool read_file

claim-plane --db .claim-plane/plane.db observe-seal worker-session \
  --key-env CLAIM_PLANE_OBSERVATION_KEY
```

Attach the sealed session to a worker and require trusted evidence:

```json
{
  "workers": [
    {
      "intent_id": "rate-limit-metrics",
      "repo_path": "../worktrees/rate-limit-metrics",
      "observation_session_id": "worker-session"
    }
  ],
  "observation_policy": {
    "mode": "trusted",
    "require_complete": true,
    "allowed_coverages": ["brokered_proxy", "tool_proxy", "os_monitor"]
  },
  "observation_key_env": "CLAIM_PLANE_OBSERVATION_KEY"
}
```

Each event includes a sequence number, previous hash, event hash, and HMAC. Sealing authenticates the complete session summary. Integration rejects missing, incomplete, tampered, incorrectly bound, or worker-owned file traces under `trusted` policy. The guarantee is complete relative to the declared trusted monitor boundary; Claim Plane still cannot observe tools that bypass that monitor unless an OS-level monitor supplies the session.

Legacy `record-access` JSONL traces remain supported in `optional` and `required` modes for migration.

For the strongest tool-mediated mode, attach a session created by `broker-serve` and use:

```json
{
  "observation_policy": {
    "mode": "brokered",
    "require_complete": true,
    "allowed_coverages": ["brokered_proxy"]
  }
}
```

`brokered` mode verifies that every accepted event was produced by the intent-enforcing Claim Plane broker. The deployment is non-bypassable only when the worker has no alternate repository mount or privileged channel.

## Integration verification

Collect a manifest from a Git worktree:

```bash
claim-plane --db .claim-plane/plane.db collect-git rate-limit-core --repo . --out manifest.json
claim-plane --db .claim-plane/plane.db verify-manifest manifest.json
```

Or collect and verify in one step:

```bash
claim-plane --db .claim-plane/plane.db verify-git rate-limit-core --repo .
```

Acceptance commands are never executed implicitly. They run only when explicitly enabled:

```bash
claim-plane \
  --db .claim-plane/plane.db \
  verify-git rate-limit-core \
  --repo . \
  --run-acceptance \
  --acceptance-timeout 300
```

The verifier checks:

- changed files are inside admitted write surfaces;
- real Git hunks stay inside declared line regions;
- required exact writes are present;
- the work is based on the admitted revision;
- observed typed signatures match concept-bound contracts;
- structured preserve policies still hold;
- acceptance commands were run and passed when required;
- deprecated or alias terminology did not enter changed text in semantic mode;
- candidate manifests do not contain overlapping hunks or incompatible contracts.

Generate a focused repair plan:

```bash
claim-plane --db .claim-plane/plane.db repair-manifest manifest.json
```

## Acceptance sandbox and evidence attestation

Repository-tree immutability remains the default. For OS-level isolation, configure a backend:

```json
{
  "worker_sandbox": {
    "backend": "auto",
    "strict": true,
    "allow_network": false
  },
  "integration_sandbox": {
    "backend": "auto",
    "strict": true,
    "allow_network": false
  }
}
```

`auto` uses Bubblewrap on supported Linux hosts or `sandbox-exec` where available. Strict mode fails closed instead of silently falling back. The default `tree` backend proves repository-tree immutability but is not a full operating-system security boundary.

Optional HMAC evidence attestation uses a key supplied only through the environment:

```json
{
  "evidence_signing_key_env": "CLAIM_PLANE_SIGNING_KEY",
  "evidence_key_id": "ci-prod"
}
```

Verify later with:

```bash
claim-plane verify-evidence evidence.json evidence.sig.json \
  --key-env CLAIM_PLANE_SIGNING_KEY
```

## Dependency graphs

Claim Plane keeps two deliberately separate dependency views. **Semantic Dependency Graph v2**
describes repository structure before execution: files, symbols, shared state, calls, imports,
types, inheritance, tests, and public API relationships. It is the evidence surface used by
later semantic impact and conflict analysis. **Runtime intent dependencies** describe admitted
work ordering and stale-premise propagation.

Every explicit or inferred runtime premise is stored as a directed intent dependency. Claim Plane rejects a proposed admission or amendment when it would create a cycle. The runtime graph can be inspected in producer-first order:

```bash
claim-plane --db .claim-plane/plane.db graph
```

The runtime graph response includes intent nodes, typed premise edges, producer states, cycle evidence, and a topological order. A producer amendment invalidates only directly affected resource premises on the first hop; once a consumer becomes stale, its outputs are treated as untrusted and invalidation propagates transitively. The semantic repository graph is an immutable analysis artifact and does not replace those runtime ordering guarantees.

## Verified integration pipeline

Claim Plane verifies several agent worktrees as one immutable integration attempt. It does not collect a manifest and later re-read a mutable worktree. Instead, for every worker it:

1. seeds a temporary Git index from the admitted base commit;
2. captures tracked changes and non-ignored untracked files into an immutable tree;
3. creates one synthetic snapshot commit and one binary patch;
4. collects the manifest from a detached worktree at that exact commit;
5. runs worker acceptance on the detached snapshot;
6. fails closed if acceptance mutates the snapshot;
7. applies the persisted, hash-verified patch bytes in dependency order;
8. runs integrated acceptance and proves the composed tree did not change;
9. creates a verified result commit, result patch, and canonical evidence bundle.

```json
{
  "run_id": "rate-limit-feature",
  "base_repo": ".",
  "base_revision": "main",
  "base_commit": "<git-sha>",
  "workers": [
    {
      "intent_id": "rate-limit-core",
      "repo_path": "../worktrees/rate-limit-core"
    },
    {
      "intent_id": "rate-limit-metrics",
      "repo_path": "../worktrees/rate-limit-metrics",
      "repair_command": "codex exec --full-auto 'Apply the repair plan at {repair_plan}'"
    }
  ],
  "integration_commands": ["pytest -q"],
  "max_attempts": 2,
  "require_clean_worker_acceptance": true,
  "require_clean_integration_commands": true,
  "result_ref": "refs/claim-plane/runs/rate-limit-feature"
}
```

Run it with:

```bash
claim-plane --db .claim-plane/plane.db integrate integration-run.json
```

Each attempt stores `worker.patch`, `manifest.json`, their SHA-256 files, `result.patch`, `evidence.json`, and deterministic reports under `.claim-plane/runs/<run_id>/`. The result includes the verified tree and commit hashes. `result_ref` is optional and, when supplied, must live under `refs/claim-plane/`.

Repair commands receive `CLAIM_PLANE_REPORT`, `CLAIM_PLANE_REPAIR_PLAN`, `CLAIM_PLANE_INTENT_ID`, `CLAIM_PLANE_REPO`, `CLAIM_PLANE_ATTEMPT`, and `CLAIM_PLANE_MERGE_ERROR`. The runner never silently expands an intent; the external worker must repair within the admitted surface or submit an amendment.

## Structured preserve policies

Claim Plane enforces deterministic policies with explicit prefixes:

```text
path-unchanged:src/public_api/**
contract:RateLimiter::allow=allow(request)->RateLimitDecision
```

Unstructured prose remains useful worker guidance, but it is not treated as a machine-enforced guarantee.

## Model routing

Claim Plane does not call a model provider. It returns a transparent risk-based recommendation:

```bash
claim-plane --db .claim-plane/plane.db route rate-limit-core
```

A cheaper worker is only a cost optimization. The same integration gate applies to every tier, and failed work should escalate to the configured fallback tier.

## MCP

```bash
claim-plane-mcp \
  --db .claim-plane/plane.db \
  --semantic \
  --lexicon lexicon/lexicon.yaml
```

Primary tools include:

- `admit_change_intent`
- `amend_change_intent`
- `promote_contingent_scope`
- `get_worker_context`
- `list_active_intents`
- `list_coordination_notices`
- `acknowledge_coordination_notice`
- `heartbeat_intent`
- `verify_change_manifest`
- `verify_git_worktree`
- `plan_targeted_repair`
- `recommend_worker_tier`
- `get_dependency_graph`
- `run_integration`
- `record_observed_access`
- `verify_evidence_bundle`

The MCP process is only a transport adapter. Protocol decisions remain deterministic library code.

## Agent Lexicon boundary

Agent Lexicon answers:

> Which canonical project concept does this name or text surface refer to?

Claim Plane answers:

> Who intends to read or mutate that concept, on which revision, under which contract, and may the work proceed concurrently?

The Integration Verifier answers:

> Did the resulting code and documentation respect those declarations, and what is the smallest repair when they did not?

## Project layout

```text
src/claim_plane/
  connectors/     project-local coding-agent enrollment and lifecycle adapters
  coordination/   sound pre-write admission and bounded context packs
  core/           protocol models, storage boundary, registry, semantic bridge, plane facade
  integration/    immutable snapshots, verification, evidence, integration, repair
  routing/        transparent risk-based model-tier recommendation
  mcp/            stdio MCP adapter
  git/            legacy hook adapter
examples/          runnable overlapping-task scenario
schemas/           intents, manifests, integration runs, and observation traces
docs/              architecture, protocol, execution, storage, integration, benchmark, releasing
benchmark/         deterministic protocol suite and adapter-driven A/B/C harness
papers/            publication index, citation metadata, and paper-to-reproduction links
experiments/       reproducible research studies and model-specific evaluation code
  cooperbench/      frozen Planner v1, paper reproduction, confirmatory runner, analysis, and Linux research image
```

## Reproducible research environment

The CooperBench studies can run directly on a host Python environment or inside the
pinned Linux research image under `experiments/cooperbench/docker/`. The container fixes
the Python base image, `uv` version, locale, timezone, and Git identity used by the
research runner while leaving `claim-plane` itself free of runtime container dependencies.

```bash
./scripts/cooperbench-docker.sh build
./scripts/cooperbench-docker.sh environment
./scripts/cooperbench-docker.sh prepare /path/to/CooperBench
OPENROUTER_API_KEY=... ./scripts/cooperbench-docker.sh reproduce /path/to/CooperBench
```

The larger frozen-plan study is also CLI-native. It first gold-validates and freezes the
exact 30-pair set, then freezes Planner v1 once, then runs nine resumable 10-pair shards
covering coder seeds 101, 202, and 303:

```bash
python -m experiments.cooperbench confirmatory prepare --cooperbench /path/to/CooperBench
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory freeze-plans \
  --cooperbench /path/to/CooperBench
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory run \
  --cooperbench /path/to/CooperBench --seed 101 --shard 1
python -m experiments.cooperbench confirmatory status
python -m experiments.cooperbench confirmatory aggregate
python -m experiments.cooperbench confirmatory verify-analysis
```

A physical-parallel execution layer can reuse the same frozen 30-pair workload and
Planner v1 declarations while changing only execution instrumentation. Independent pair
processes are isolated and scheduled through a bounded worker pool, and admitted workers
inside a pair record actual wall-clock overlap rather than inferring parallelism from
logical latency:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory physical-run \
  --cooperbench /path/to/CooperBench \
  --seed 101 \
  --pairs 1-6 \
  --max-parallel-pairs 6
```

The outer worker pool is an experiment-throughput optimization and is reported separately
from inner pair overlap. It therefore cannot be counted as Claim Plane speedup. Each outer
pair receives an isolated repository cache and worktree root so concurrent runs do not
share mutable Git state.

A deterministic ablation runner reuses those same frozen inputs and physical timing hooks
while varying only the admission evidence. The default profiles compare full semantic
concurrency against file/region-only admission, symbol identities without dependency edges,
and semantic dependencies with broad contract propagation removed:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory ablation-run \
  --cooperbench /path/to/CooperBench \
  --seed 101 \
  --pairs 1-6 \
  --max-parallel-pairs 6
```

The ablation output keeps source-bound graph fingerprints, deterministic execution waves,
pair outcomes, measured inner overlap, and wall-clock deltas against `full_v2`. Published
confirmatory artifacts are not rewritten. Runtime amendment and stale-worker recovery remain
separate conformance/stress mechanisms unless a workload actually exercises them.

The final deterministic v2 confirmatory runner then compares the complete semantic admission
layer directly against naive parallelism, the historical conservative static gate, and the
always-serial baseline on the same frozen 30-pair × 3-seed matrix. Pair/seed units run through
a bounded outer pool, while Claim Plane speedup is computed only from paired inner wall-clock
measurements against the serial baseline:

```bash
OPENROUTER_API_KEY=... python -m experiments.cooperbench confirmatory final-run \
  --cooperbench /path/to/CooperBench \
  --seeds 101,202,303 \
  --pairs 1-30 \
  --max-parallel-pairs 6

python -m experiments.cooperbench confirmatory final-status
python -m experiments.cooperbench confirmatory final-aggregate
```

The runner deterministically counterbalances mode order across pair/seed units and writes
fresh evidence under `.claim-plane/experiments/deterministic-confirmatory-v2/`. The aggregate
requires all 90 pair/seed units and all four modes before sealing the final report. No
performance result is bundled with the runtime release; speedup and reliability claims must
come from completed experiment artifacts.

Aggregation is intentionally strict: it requires all nine completed shards and the full
360-row pair/seed/arm matrix before writing final analysis artifacts. The output includes
arm, feature-pair, and repository-task cluster summaries, task-cluster bootstrap confidence
intervals, failure and coordination-mechanism summaries, cost accounting, canonical JSON/CSV
results, and a SHA-256 publication manifest.

The CooperBench checkout is mounted read-only in the research image. Repository caches,
worktrees, checkpoints, results, and analysis artifacts are persisted under
`.claim-plane/docker-research/`. Protocol artifacts record the exact pair set, benchmark
provenance, Planner v1 policy identity, frozen plan fingerprints, and shard identities
without persisting API keys.

## Current limits

Claim Plane remains an alpha coordination kernel.

- It consumes structured intents; it does not yet generate the task graph.
- Built-in source extraction is Python-first; other languages currently remain at file/declared-resource granularity unless an external adapter supplies structured semantic resources.
- Python semantic analysis now provides stable symbol coordinates, dependency edges, downstream impact propagation, conflict classification, same-file admission evidence, bounded semantic amendment preflight, runtime pause/refresh/resume, and deterministic integration-time authority rechecks over the actual diff.
- Documentation semantic checking is surface-oriented, not a full code-to-doc factual verifier.
- `SQLitePlaneStore` is a single-host backend. The OS lock is derived from Git's canonical common directory, so separate local databases cannot choose independent lock namespaces. Multi-host deployments still require one network-authoritative registry such as PostgreSQL plus distributed leases and fencing.
- The verified pipeline includes non-ignored untracked files, but ignored build/cache artifacts are intentionally excluded.
- Result commits are created as immutable Git objects; publishing a branch or PR remains an explicit caller action unless a namespaced `result_ref` is configured.
- Observation guarantees cover only tool/MCP accesses emitted to the trace; bypassed reads remain unobserved.
- The default `tree` sandbox detects repository mutations but does not isolate network or the host filesystem; strict OS isolation requires an available supported backend.
- HMAC evidence provides shared-secret authenticity, not public-key identity or hardware attestation.
- The router is deterministic and heuristic, not learned.
- Claim Plane has not yet demonstrated lower total cost to clean merge on large real repositories. The published 30-pair × 3-seed confirmatory study found strong reliability gains for conservative static admission, but also showed that static admission largely collapsed toward serialization and that dynamic admission exposed region undercoverage and insufficient amendment handling. Provider calls in that published study were physically sequential, so those published results do not establish wall-clock parallel speedup. The deterministic v2 confirmatory runner can now execute the frozen 30 × 3 matrix with measured inner overlap and strict paired comparisons, but no new speedup result is claimed until those fresh artifacts are completed and aggregated.

The comparative evaluation requirements are documented in [docs/BENCHMARK.md](docs/BENCHMARK.md), and the study infrastructure is described in [experiments/cooperbench/README.md](experiments/cooperbench/README.md).

## License

Apache-2.0.
