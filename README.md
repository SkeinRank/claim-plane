# Claim Plane

**Semantic concurrency control and continuous integration for parallel coding agents.**

> **Research Preview — 0.24.0.** APIs, evidence formats, and deployment contracts may change before 1.0.
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

## Research paper

**Claim Plane: Enforceable Change Intents and Dynamic Scope for Parallel Coding Agents**  
Maxim Nikolaev · Software Engineering (`cs.SE`) · 2026

- [arXiv abstract](https://arxiv.org/abs/2607.21909)
- [PDF](https://arxiv.org/pdf/2607.21909)
- [DOI: 10.48550/arXiv.2607.21909](https://doi.org/10.48550/arXiv.2607.21909)
- [Paper metadata and citation](papers/claim-plane-2026/README.md)
- [Published six-pair CooperBench reproduction](experiments/cooperbench/paper_6pair/)

Repository-level software citation metadata is available in [`CITATION.cff`](CITATION.cff), and the paper-specific BibTeX entry is available in [`papers/claim-plane-2026/citation.bib`](papers/claim-plane-2026/citation.bib).

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
- project-local Codex enrollment with a stable lifecycle dispatcher, idempotent hook installation, session-bound task bootstrap, pinned Git bases, atomic ChangeIntent admission, pre-mutation authorization, ticketed scope amendment, and verified completion for autonomous Codex work;
- repository-bound swarm sessions with exact Git bases, planner-proposed work items, deterministic DAG validation, graph fingerprints, dependency layers, and optimistic graph-version replacement;
- versioned swarm budget policies with hard worker, graph-size, launch, token, cost, wall-time, retry, and concurrency ceilings that the planner cannot widen silently;
- adaptive concurrency plans that combine the dependency DAG with region, overlap, contract, schema, and worker-budget constraints to produce deterministic execution waves or a fail-closed `replan_required` result;
- shared swarm admission that derives one deterministic ChangeIntent per work item, admits concurrent authority against the whole session, and promotes serialization constraints into effective dependencies;
- a dynamic dependency scheduler that releases only admitted, prerequisite-complete work within current worker capacity and distinguishes runnable, active, retryable, terminal, and dependency-blocked items;
- a deterministic merge queue that snapshots successful worker worktrees, integrates results on a Claim Plane-owned branch in effective-dependency order, blocks downstream workers until prerequisites are integrated, captures real Git conflicts, and leaves the user target branch untouched;
- two-level swarm verification that checks each integrated work item against its admitted scope, reruns work-item and root acceptance on the managed integration head, detects acceptance-induced mutations, and persists a final `SWARM VERIFIED` evidence report;
- crash-safe swarm recovery with worker heartbeat leases, orphan detection, durable pause/resume/cancel controls, and fresh-identity replacement that rechecks authority and never silently inherits predecessor edits;
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

For development from a checkout:

```bash
pip install -e ".[dev,signing]"

# Optional local Agent Lexicon checkout
pip install -e ../agent-lexicon
```

Run the complete checks and example:

```bash
./scripts/check.sh
./scripts/demo.sh
```

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
claim-plane doctor codex

codex
```

`claim-plane init` creates local control-plane state and adds `.claim-plane/` to the repository's Git exclude file. `claim-plane connect codex` installs Claim Plane-owned handlers in `.codex/hooks.json` while preserving unrelated project hooks. Re-running either command is safe and does not duplicate the enrollment.

The connector registers one stable dispatcher for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and `SessionEnd`. Codex discovers these project-local hooks automatically for trusted projects. On the first Codex session, open `/hooks` to review and trust the command hooks; Codex records trust against the hook definition.

For the first task in a session, Claim Plane pins the current Git commit, creates a private session-bound task identity, and injects model-visible coordination context through `UserPromptSubmit`. Codex can inspect the repository read-only, propose the expected committed and contingent scope, preserve requirements, and acceptance checks, then submit that proposal through the local CLI. Claim Plane supplies the intent ID, owner, task ID, and immutable base commit itself before performing normal atomic admission.

The raw user prompt is not stored in connector state. The task record keeps a SHA-256 digest and prompt length for correlation, while the admitted intent stores the explicit goal and execution contract that Codex proposed. Repeated admission of identical content is idempotent, and a changed Git `HEAD` before admission is rejected as a stale bootstrap.

Once admitted, normal Codex prompt and tool lifecycle events renew the active intent lease automatically. The model does not need to issue a separate heartbeat command during an active session.

For intercepted `PreToolUse` calls, Claim Plane classifies repository effects before execution. Read-only calls continue normally. A mutation already covered by committed scope continues through Codex's normal sandbox and approval flow. A mutation covered by one contingent declaration is atomically promoted and re-admitted before the tool call continues. Undeclared mutations, stale-base mutations, unknown mutating tool surfaces, and shell commands whose repository effects cannot be proven are denied with model-visible guidance. Claim Plane stores the decision, affected paths, and counters without persisting raw tool arguments.

When a denial identifies concrete additional file authority, the guard issues a short-lived, session-bound scope-amendment ticket containing only the exact denied mutation set. Codex supplies a rationale through `claim-plane codex-intent amend`; it cannot choose different paths, change the task identity, change the pinned base, or remove existing preserve and acceptance requirements. Claim Plane derives the amended ChangeIntent, checks ticket integrity and intent freshness, performs normal atomic re-admission, and activates the amended intent only when admission succeeds. A rejected amendment leaves the previously active intent unchanged. Multiple contingent resources that cannot be promoted individually in one tool call can therefore be committed together through one inspectable amendment.

The connector also reserves a narrow shell control channel for `claim-plane codex-intent admit`, `status`, `amend`, `verify`, and `abandon`. The command must target the current Codex session and current repository; initial admission uses `--proposal-json` rather than a shell pipe or repository temporary file. Other Claim Plane commands and opaque shell effects remain subject to the normal fail-closed classification. Connector control state under `.claim-plane/**`, `.git/**`, and `.codex/**` cannot be granted through a session intent or amendment.

When Codex tries to finish an active task, the `Stop` lifecycle event runs verified completion. Claim Plane collects tracked and untracked repository changes, checks the actual work against the admitted scope, preserve and contract policies, executes the declared acceptance commands with worktree-integrity checks, and completes the intent only when the resulting evidence is clean. A failed first completion blocks the stop once and returns concrete findings so Codex can repair the work. If the continuation still fails, the session is allowed to stop as explicitly `UNVERIFIED` rather than entering an unbounded model loop.

A clean completion is persisted on the session and surfaced as `VERIFIED` with changed-file counts, mutation-authority counters, scope expansions, acceptance outcome, and verification findings. The same result is available explicitly through `claim-plane codex-intent verify --session-id <id> --repo .` and `claim-plane codex-intent status`.

The connector hardens long-running local use as well. Pre-existing user changes are fingerprinted at task bootstrap: unchanged pre-existing paths are excluded from task attribution, while Codex is denied mutation authority over those paths so existing work is not silently mixed into an autonomous task. Only one active Codex session may hold mutation authority in a physical worktree; independent concurrent sessions should use separate Git worktrees, where normal Claim Plane coordination still applies. A resumed session renews a live intent automatically and can re-admit an expired intent only when the pinned commit and branch are unchanged. Changed repository state, released or stale authority, corrupted session state, missing enrollment state, branch switches, and connector hook drift all fail closed. Re-running `claim-plane connect codex` repairs connector-owned hook definitions while preserving unrelated hooks.
If an unfinished session is intentionally discarded, `claim-plane codex-intent abandon --session-id <id> --repo .` releases its intent authority so another Codex session can use the same worktree immediately.

```text
committed mutation  -> authorize -> Codex sandbox/approval -> execute
contingent mutation -> promote + re-admit -> authorize -> execute
undeclared mutation -> deny -> exact ticket -> reason -> re-admit -> retry
unprovable mutation -> deny before tool execution
Stop -> collect evidence -> acceptance -> VERIFIED or bounded repair continuation
```

`claim-plane doctor codex` also checks that the installed Codex version provides the hook surface required by the guard. Hook interception is an integration boundary, not a substitute for the brokered reference-monitor boundary: runtime hook coverage and timeout behavior remain properties of Codex itself. Claim Plane therefore keeps broker capabilities, repository identity, admission, and verification as the authoritative core primitives.

The session-bound proposal protocol is documented by [`schemas/codex-intent-proposal.schema.json`](schemas/codex-intent-proposal.schema.json), the amendment ticket by [`schemas/codex-scope-amendment.schema.json`](schemas/codex-scope-amendment.schema.json), and verified completion by [`schemas/codex-completion.schema.json`](schemas/codex-completion.schema.json). The lifecycle bridge does not depend on MCP; MCP remains an optional interaction surface for status, explanation, and evidence.

To remove the integration without disturbing other Codex hooks:

```bash
claim-plane disconnect codex
```

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
