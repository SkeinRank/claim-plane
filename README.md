<div align="center">

# Claim Plane

**A deterministic control and evidence layer for coding agents**  
Task-bound authority. Controlled scope. Verifiable delivery.

[![CI](https://github.com/SkeinRank/claim-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/SkeinRank/claim-plane/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/claim-plane?label=pypi)](https://pypi.org/project/claim-plane/)
[![Python](https://img.shields.io/pypi/pyversions/claim-plane?label=python)](https://pypi.org/project/claim-plane/)
[![License](https://img.shields.io/badge/license-Apache--2.0-4c1)](LICENSE)
[![Status](https://img.shields.io/badge/status-technical%20preview-orange)](#start-here)

**Let agents code. Make every change provable.**

</div>

> **Technical Preview — 0.36.6.** APIs, evidence formats, and deployment contracts may change before 1.0.
> Long-running CooperBench runs expose checkpoint-aware live progress and ETA on stderr while keeping final CLI results machine-readable.

Git worktrees isolate agent processes, but they do not prove that two agents are making compatible changes. Agents can still introduce different names for one concept, design incompatible contracts, expand outside their assigned surfaces, or discover a dependency conflict only after both branches have consumed tokens and time.

Claim Plane coordinates those changes before and during implementation:

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

Claim Plane does not replace Git, an IDE, a planner, or a coding agent. It is a model-agnostic integration layer that can sit between a planner and Cursor, Codex, Claude Code, Copilot, OpenHands, or internal agents.

Swarm planning can now be materialized into Claim Plane-owned isolated worktrees:

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

## Frozen OSS pilot

Version `0.36.6` includes a three-task real-repository pilot for the interactive
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

## Research paper

**Claim Plane: Enforceable Change Intents and Dynamic Scope for Parallel Coding Agents**  
Maxim Nikolaev · Software Engineering (`cs.SE`) · 2026

- [arXiv abstract](https://arxiv.org/abs/2607.21909)
- [PDF](https://arxiv.org/pdf/2607.21909)
- [DOI: 10.48550/arXiv.2607.21909](https://doi.org/10.48550/arXiv.2607.21909)
- [Paper metadata and citation](papers/claim-plane-2026/README.md)
- [Published six-pair CooperBench reproduction](experiments/cooperbench/paper_6pair/)

Repository-level software citation metadata is available in [`CITATION.cff`](CITATION.cff), and the paper-specific BibTeX entry is available in [`papers/claim-plane-2026/citation.bib`](papers/claim-plane-2026/citation.bib).

## Start here

Install the isolated CLI, enroll a feature branch, and run one bounded Codex task:

```bash
uv tool install claim-plane
# or: pipx install claim-plane

cd my-project
git switch -c agent/audit-pagination
claim-plane init
claim-plane connect codex
claim-plane doctor
claim-plane codex --policy guarded
# or run one bounded unattended task:
claim-plane run "Add pagination to the audit API and extend its tests" --policy guarded
claim-plane report latest
```

The five-minute walkthrough is in [`docs/QUICKSTART.md`](docs/QUICKSTART.md). Before using
the preview on important repositories, read the explicit
[guarantees and trust boundaries](docs/GUARANTEES.md),
[troubleshooting guide](docs/TROUBLESHOOTING.md), and
[upgrade/uninstall behavior](docs/UPGRADING.md).

Inspect the installed product contract and packaged schemas with:

```bash
claim-plane preview
claim-plane exit-codes
claim-plane schemas list
```

## Current capabilities

- atomic claim and intent admission through SQLite transactions;
- leases, heartbeats, completion, release, and append-only audit events;
- structured `ChangeIntent` operations: read, write, extend, delete, rename, document, and test;
- adaptive committed/contingent scope with just-in-time atomic re-admission before first mutation;
- exact files, globs, bounded line regions, symbols, concepts, contracts, routes, schemas, configs, and documents;
- strict optional Agent Lexicon resolution: requested semantic mode fails closed when unavailable;
- concept-bound contracts through `subject_concept_id`;
- deterministic outcomes for independent work, compatible overlap, contract dependencies, constrained parallelism, serialization, replanning, and rejection;
- safe broad-scope admission: known glob/file overlap is serialized rather than optimistically admitted;
- versioned intent amendments with optimistic version checks;
- an atomically enforced acyclic dependency graph with producer-first topological order;
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
- adaptive concurrency plans that combine the dependency DAG with region, overlap, contract, schema, and worker-budget constraints to produce deterministic execution waves or a fail-closed `replan_required` result;
- shared swarm admission that derives one deterministic ChangeIntent per work item, admits concurrent authority against the whole session, and promotes serialization constraints into effective dependencies;
- a dynamic dependency scheduler that releases only admitted, prerequisite-complete work within current worker capacity and distinguishes runnable, active, retryable, terminal, and dependency-blocked items;
- a deterministic merge queue that snapshots successful worker worktrees, integrates results on a Claim Plane-owned branch in effective-dependency order, blocks downstream workers until prerequisites are integrated, captures real Git conflicts, and leaves the user target branch untouched;
- two-level swarm verification that checks each integrated work item against its admitted scope, reruns work-item and root acceptance on the managed integration head, detects acceptance-induced mutations, and persists a final `SWARM VERIFIED` evidence report;
- crash-safe swarm recovery with worker heartbeat leases, orphan detection, durable pause/resume/cancel controls, and fresh-identity replacement that rechecks authority and never silently inherits predecessor edits;
- one-command swarm operation with bounded parallel dispatch, compact status, normalized logs, deterministic integration, final verification, and an offline three-worker demo;
- CLI, stdio MCP, JSON Schemas, examples, and a deterministic protocol benchmark.

The base package has no runtime dependencies. Agent Lexicon remains an optional semantic layer.

The brokered boundary is Linux-first. On macOS, the broker and verification pipeline work normally, while non-bypassable repository isolation should run in a Linux VM/container with Bubblewrap.

## Install

Install the CLI as an isolated tool with `uv`:

```bash
uv tool install claim-plane

# Optional semantic identity and evidence signing
uv tool install "claim-plane[semantic,signing]"
```

`pipx install claim-plane` is also supported when `pipx` is the preferred tool manager.

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

When an admitted producer changes a contract, Claim Plane:

1. records a new intent version;
2. marks affected dependent intents as `stale`;
3. creates structured coordination notices;
4. exposes those notices in the worker context pack;
5. propagates staleness transitively to downstream consumers whose producer outputs are no longer trustworthy;
6. requires amendment and re-admission before any stale worker continues.

```bash
claim-plane --db .claim-plane/plane.db notices rate-limit-metrics
claim-plane --db .claim-plane/plane.db ack-notice 1
```

This is advisory coordination with an enforceable stale state, not a distributed source-code lock.

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

## Dependency graph

Every explicit or inferred premise is stored as a directed dependency. Claim Plane rejects a proposed admission or amendment when it would create a cycle. The graph can be inspected in producer-first order:

```bash
claim-plane --db .claim-plane/plane.db graph
```

The graph response includes nodes, typed edges, producer states, cycle evidence, and a topological order. A producer amendment invalidates only directly affected resource premises on the first hop; once a consumer becomes stale, its outputs are treated as untrusted and invalidation propagates transitively.

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
- Built-in source extraction is Python-first.
- Line-region admission is supported; stable AST-node ownership across edits is future work.
- Documentation semantic checking is surface-oriented, not a full code-to-doc factual verifier.
- `SQLitePlaneStore` is a single-host backend. The OS lock is derived from Git's canonical common directory, so separate local databases cannot choose independent lock namespaces. Multi-host deployments still require one network-authoritative registry such as PostgreSQL plus distributed leases and fencing.
- The verified pipeline includes non-ignored untracked files, but ignored build/cache artifacts are intentionally excluded.
- Result commits are created as immutable Git objects; publishing a branch or PR remains an explicit caller action unless a namespaced `result_ref` is configured.
- Observation guarantees cover only tool/MCP accesses emitted to the trace; bypassed reads remain unobserved.
- The default `tree` sandbox detects repository mutations but does not isolate network or the host filesystem; strict OS isolation requires an available supported backend.
- HMAC evidence provides shared-secret authenticity, not public-key identity or hardware attestation.
- The router is deterministic and heuristic, not learned.
- Claim Plane has not yet demonstrated lower total cost to clean merge on large real repositories. The repository includes the frozen Planner v1 policy, an executable reproduction of the published six-pair CooperBench mechanism check, the frozen-plan 30-pair × 3-seed runner, and deterministic publication aggregation. Confirmatory conclusions remain unpublished until the full study is completed and the resulting analysis is reviewed.

The comparative evaluation requirements are documented in [docs/BENCHMARK.md](docs/BENCHMARK.md), and the study infrastructure is described in [experiments/cooperbench/README.md](experiments/cooperbench/README.md).

## License

Apache-2.0.
