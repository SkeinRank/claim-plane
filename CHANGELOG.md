# Changelog

All notable public changes to Claim Plane will be documented in this file.

The project follows Semantic Versioning while it is in the `0.x` research-preview
phase. Internal prototype iterations before the first public release are intentionally
not part of the public release history.

## [Unreleased]

## [0.37.14] — 2026-08-09

### Added

- add a Python structural extractor that parses repository source without importing or executing it and projects classes, functions, methods, async definitions, signatures, decorators, and lexical ownership into Semantic Resource IR v2 symbols;
- add deterministic line-to-owner mapping so later Git-hunk admission can resolve a changed line to the most specific enclosing symbol while preserving file-level fallback;
- add a versioned Python structural index format, packaged JSON Schema, stable source fingerprinting, syntax-error fail-closed behavior, encoding-aware file loading, and repository-root confinement.

## [0.37.13] — 2026-08-09

### Added

- introduce Semantic Resource IR v2 as a versioned, language-neutral authority normal form with explicit file, region, symbol, and contract layers;
- derive deterministic stable identities that keep symbols and contracts anchored across line movement and signature evolution while retaining mutable coordinates as evidence;
- expose intent-level normalized authority projections and a packaged JSON Schema for later dependency, conflict, and concurrency analysis.

## [0.37.12] — 2026-08-07

### Fixed

- preserve per-command acceptance results in durable controlled-run evidence so failed final verification keeps the command, return code, duration, and bounded stdout/stderr diagnostics available to `claim-plane report`;
- allow bounded Ruff lint and formatter checks as guarded development feedback while keeping source-mutating Ruff modes fail-closed;
- restore the technical-preview test baseline to Ruff-format-clean state before interactive dogfood resumes.

## [0.37.11] — 2026-08-07

### Fixed

- separate final delivery outcome from later OSS acceptance rechecks so a rejected delivery can never be presented as `VERIFIED_AFTER_RECHECK`;
- report whether the current candidate matches the evaluator input and label passing, failing, stale, and evaluator-error rechecks without overwriting the original delivery decision;
- allow bounded shell inspection chains to include a targeted test command when every segment is independently read-only or approved test feedback, while keeping pipelines, full acceptance commands, inline Python, and mutating segments fail-closed.

## [0.37.10] — 2026-08-06

### Fixed

- provision `setuptools>=77.0.3` explicitly in CI and publication jobs before release validation;
- build the technical-preview wheel and final distributions from the provisioned release environment, avoiding an implicit second network fetch during the package gate;
- keep setuptools restricted to the build system and release toolchain, without adding it to Claim Plane runtime dependencies.

## [0.37.9] — 2026-08-06

### Fixed

- install explicit optional acceptance dependencies from the external pytest plugin before repository `conftest.py` files are imported, so projects that cache dependency availability during test configuration observe the prepared runtime correctly;
- verify the optional module with a real import after installation and retain fail-closed witness evidence when installation or import still fails;
- identify automatic acceptance retries with an explicit preserved-candidate banner, including task, arm, and execution ID, before the evaluator starts.

## [0.37.8] — 2026-08-06

### Added

- bind private Python acceptance tests to exact pytest node IDs and record collection, execution, pass, failure, and skip evidence in every OSS re-verification result;
- install explicit task-relevant optional test dependencies, including Pillow for `require_pil`, into the shared development environment and the isolated evaluator runtime;
- preserve fail-closed witness evidence in comparative validation bundles and resumable acceptance records.

### Fixed

- classify zero-exit evaluators with skipped, uncollected, unexecuted, or unwitnessed hidden tests as `EVALUATOR_INCOMPLETE` instead of `PASS`;
- prevent incomplete evaluator runs from being counted as completed dogfood measurements.

## [0.37.7] — 2026-08-06

### Fixed

- make the editable candidate install command variadic for static typing so the uv and Python fallback forms can have different argument counts without failing the release quality gate.

## [0.37.6] — 2026-08-06

### Added

- isolate frozen evaluator programs and hidden acceptance inputs in a private persistent vault outside the validation workspace tree;
- disable Codex web search and shell network access for comparative cells so benchmark identities and reference material cannot be recovered through online lookup;
- audit newly written Codex session and Claim Plane run records for hidden benchmark artifact access and record affected cells as `CONTAMINATED` without running official acceptance;
- include contamination evidence in exported validation bundles;
- show the selected matrix arm before repository preparation and stream bounded clone/fetch progress with a three-minute fetch limit.

### Fixed

- remove frozen source checkout paths, evaluator directories, and hidden input locations from agent-visible workspace manifests;
- pass private evaluator assets directly from the outer validation runner instead of teaching the agent where those assets live;
- replace host-specific absolute Python acceptance commands with `python` resolved from the prepared task environment;
- automatically migrate existing validation roots into the private evaluator vault and remove managed in-tree CooperBench source checkouts before Codex opens.

## [0.37.5] — 2026-08-06

### Fixed

- remove inherited `__PYVENV_LAUNCHER__`, `PYTHONEXECUTABLE`, and `PYTHONHOME` from dependency bootstrap, editable binding, preflight, and Codex execution environments so macOS/pyenv parent launchers cannot redirect the prepared interpreter away from its task packages;
- verify `sys.prefix` against the prepared virtual environment and import pytest during preflight instead of relying only on module discovery;
- pin prepared site-packages into the one-run Codex `PYTHONPATH` while keeping the candidate workspace source first;
- keep task-environment integrity stable when the frozen seed package is replaced by an editable arm workspace, preventing unnecessary environment rebuilds between Bare, Observe, and Guarded.

### Added

- record the prepared Python prefix, site-package roots, and pytest availability in the environment manifest;
- add regression coverage for poisoned parent Python launcher variables and exact virtual-environment prefix verification.

## [0.37.4] — 2026-08-06

### Fixed

- pin the prepared task virtual environment inside Codex shell tools with one-run `shell_environment_policy` overrides instead of relying only on the TUI parent process environment;
- disable login-shell profile rewriting for comparative sessions so `python`, `pytest`, and project commands resolve through the task environment in Bare, Observe, and Guarded;
- fail before opening Codex when the prepared Python, `VIRTUAL_ENV`, pytest, top-level test imports, or editable project import cannot be verified.

### Added

- report the exact Python executable and available targeted-test imports before every comparative Codex session;
- add `claim-plane codex --codex-config` for bounded one-run Codex configuration forwarding without modifying user `config.toml`;
- add focused regression coverage for shell-environment injection, command forwarding, and missing dependency detection.

## [0.37.3] — 2026-08-06

### Fixed

- make comparative environment prefetch pass a valid non-empty feature patch to frozen evaluators, including tasks whose feature patch is intentionally absent during dependency bootstrap;
- cover the exact `git apply` regression that previously failed with `No valid patches in input`.

## [0.37.2] — 2026-08-06

### Added

- Add `claim-plane validation prefetch` to prepare one reusable task-level development environment from the dependency prefix of the frozen evaluator without executing official tests.
- Share the same virtual environment and UV download cache across Bare Codex, Claim Plane Observe, and Claim Plane Guarded, while rebinding editable candidate source for each arm.
- Add `claim-plane validation reset-task` to remove all diagnostic cells for one frozen task while preserving its dependency cache for a fidelity-matched repeat.
- Report prepared environment count, cache location, cache hits, and current environment identity in comparative runtime metadata.
- Add `scripts/check-comparative-runtime-fidelity.sh` and focused regression coverage for environment reuse, acceptance delegation, task reset, and real-world inspection commands.

### Fixed

- Defer internal final acceptance for Observe and Guarded validation cells so the frozen external evaluator is the single authoritative acceptance run for every arm.
- Make project dependencies available during the Codex development loop so targeted tests can provide repair feedback before final acceptance.
- Admit bounded quoted `rg` and `grep` patterns, `git remote -v`, `git remote get-url`, read-only Codex web tools, and bytecode-free `py_compile` feedback without broadening arbitrary shell authority.
- Prevent dependency-prefetch boundary detection from confusing `uv pip install pytest` with actual test execution.

### Changed

- Mark comparative results created before the matched-environment contract as diagnostic evidence that should be reset and repeated before publication.

## [0.37.1] — 2026-08-05

### Added

- Show the current comparative matrix cell, runtime phase, elapsed time, evaluator output, and silence heartbeats during long validation operations.
- Add `claim-plane validation resume` to continue official acceptance for a preserved candidate without rerunning Codex.
- Persist execution metadata before the agent starts and before acceptance begins so interrupted cells remain visible and recoverable.
- Detect candidates created by the earlier validation runner and expose them as `LEGACY_CANDIDATE` with an explicit recovery command.
- Add profile-aware acceptance limits: five minutes for preview and twenty minutes for release, with an operator override.
- Add bounded evaluator cleanup and durable `INTERRUPTED` acceptance evidence instead of losing a cell on Ctrl-C.
- Keep timeouts and evaluator-environment failures resumable instead of recording them as completed task failures.
- Add reusable runtime progress primitives and `scripts/check-validation-runtime-ux.sh` for future long-running benchmark and precomputation workflows.

### Fixed

- Prevent a completed Codex candidate from disappearing from matrix progress when official acceptance is interrupted.
- Prevent silent dependency installation and test execution from appearing hung while output is captured.
- Prevent cleanup of large temporary environments from blocking the terminal indefinitely after interruption.

## [0.37.0] — 2026-08-05

### Added

- Add `claim-plane validation init`, `status`, `prepare`, `run`, `collect`, `report`, and `bundle` as one comparative single-agent validation workflow.
- Discover runnable feature-level tasks from the frozen CooperBench checkout and select a deterministic repository-diverse preview or release corpus.
- Freeze identical Bare Codex, Claim Plane Observe, and Claim Plane Guarded plan cells with exact task, prompt, repository state, model, policy, seed label, and acceptance identity.
- Run one matrix cell end to end, including workspace preparation, interactive Codex execution, isolated official acceptance, measured result binding, and progress advancement.
- Collect task success, accepted delivery, undeclared and missed mutations, amendments, recovered inspection blocks, timing, token and cost fields, change size, public API drift, dependency drift, and evidence digests.
- Generate conservative matrix summaries and release gates without inventing missing measurements.
- Export a reproducible ZIP containing immutable inputs, measured results, summaries, gate decisions, manifests, run records, and acceptance evidence.
- Package public schemas for validation state, frozen selection, and exported bundles, plus `scripts/check-single-agent-validation.sh`.

### Changed

- Promote the single-agent Codex path from a three-task OSS pilot to a repeatable comparative validation surface suitable for release readiness and external reproduction.

## [0.36.9] — 2026-08-05

### Added

- Bind every new single-agent controlled run to a canonical candidate identity derived from the task digest, frozen base commit and tree, result-state digest, canonical change digest, and normalized changed paths.
- Seal versioned policy, adapter, acceptance-definition, and lifecycle snapshots into one deterministic input digest and final decision digest.
- Add fail-closed completeness diagnostics for malformed Git bindings, missing snapshots, base mismatches, invalid lifecycle evidence, and changed paths not covered by the final admitted scope.
- Surface deterministic verdict reason codes and decision digests in evidence reports and verify replay equivalence against the stored lifecycle head.
- Package a public `single-agent-determinism.schema.json` contract and add `scripts/check-single-agent-determinism.sh` as a focused regression gate.

### Fixed

- Prevent a nominally verified delivery from remaining green when its deterministic evidence is incomplete; such a run is downgraded to `REVIEW_REQUIRED` with stable machine-readable findings.
- Detect post-run tampering of candidate, policy, adapter, acceptance, lifecycle, or verdict inputs by recomputing the canonical deterministic record from durable evidence.

### Changed

- Bump the Codex connector revision to 13 so existing enrollments refresh the deterministic evidence contract before the next controlled run.

## [0.36.8] — 2026-08-05

### Added

- Allow bounded targeted test feedback during an admitted Codex task while keeping the configured authoritative acceptance command reserved for Claim Plane's independent final verifier.
- Recognize common project-native test runners, including pytest, tox, nox, Cargo, Go, npm-family commands, and Airflow Breeze testing.
- Classify untracked caches and build products from test feedback as managed artifacts without hiding tracked source, configuration, snapshot, or golden-file changes.
- Seal OSS pilot re-verification records to the frozen base commit, current candidate digest, manifest digest, logs, result classification, and evidence digest.
- Surface one current candidate verdict in OSS pilot status, evidence reports, and replay output.

### Fixed

- Remove the ambiguous `REJECTED` plus later `PASS` presentation by preserving the original run outcome while reporting `VERIFIED_AFTER_RECHECK`, `REJECTED_AFTER_RECHECK`, stale, or evaluator-error state for the current candidate.
- Preserve independent final acceptance even when Codex uses targeted tests to repair its implementation during the interactive session.

### Changed

- Bump the Codex connector revision to 12 so existing enrollments refresh the test-feedback authority and metrics contract.

## [0.36.7] — 2026-08-05

### Added

- Add bounded read-only shell pipelines for Codex inspection when every stage is independently classified as non-mutating.
- Add `scripts/check-inspection-friction.sh` as a focused regression gate for shell classification, privacy, diagnostics, and recovery metrics.
- Record shell-inspection friction metrics for allowed compound commands, admitted pipelines, unclassified denials, and later read-only recovery.
- Surface inspection metrics in `claim-plane codex-intent status` and durable evidence reports.

### Fixed

- Identify the exact shell segment and deterministic reason code that caused an inspection command to fail closed instead of returning one opaque repository-path message.
- Admit common repository-inspection forms such as `git show | head`, `rg | head`, `git log | tail`, and mixed `;` / `&&` chains without broadening mutation authority.
- Keep redirection, background execution, command substitution, mutating Git operations, in-place `sed`, mutating `find` actions, and unknown pipeline stages denied.

### Changed

- Bump the Codex connector revision to 11 so existing enrollments refresh the strengthened inspection grammar and diagnostics.

## [0.36.6] — 2026-08-05

### Added

- Add a frozen three-task single-agent OSS pilot covering Jinja, Click, and dirty-equals at one exact CooperBench source revision.
- Add `claim-plane oss-pilot list`, `prepare`, `run`, `verify`, and `status` for reproducible Guarded, Observe, and Bare workspaces.
- Bind every prepared workspace to the exact repository base commit, source task, prompt digest, initial authority, arm, and acceptance command.
- Run task-local CooperBench acceptance in an isolated temporary Git worktree so evaluator cleanup cannot modify the agent workspace.
- Add `benchmark/oss-pilot/selection.json`, operating guidance, and `scripts/check-oss-pilot.sh`.

### Fixed

- Keep interactive Codex project-root typing consistent across enrollment and hook dispatch so the complete mypy gate remains clean.
- Create frozen OSS evaluator repositories beneath the required `agent_workspace` boundary so task-local safety checks run in the intended isolated layout.
- Combine official task tests with candidate changes through a base-aware three-way merge, including the case where Codex already added the same regression coverage.
- Distinguish test failures, dependency setup failures, evaluator conflicts, workspace failures, and timeouts while preserving full acceptance logs beside the pilot evidence.

## [0.36.5] — 2026-08-05

### Added

- Add a provider-free interactive authority safety suite covering locked scope, legitimate and rejected amendments, acceptance failure, high-risk review, pre-existing user work, cancellation, timeout, and multi-turn finalization.
- Add `scripts/check-interactive-safety.sh` as a focused reproducible gate for Codex authority and lifecycle behavior.
- Record aggregate acceptance duration in controlled-run evidence and show it in the terminal verification card.

### Fixed

- Reject vague or non-causal scope-amendment rationales before authority expansion while consuming the exact one-time ticket and preserving the previously active intent.
- Preserve normalized `AgentStopped` evidence for every interactive Codex turn and allow a later prompt to continue under the same admitted intent without sealing the session early.
- Revoke unfinished authority on interactive cancellation and timeout, and keep final verification and `SessionEnded` emission launcher-owned and single-shot.
- Exclude unchanged pre-existing user paths from controlled-run change attribution and risk summaries without granting the agent mutation authority over those paths.
- Keep operator-requested test coverage as a structured completion obligation, so a scope-clean change cannot be marked verified merely because older tests still pass while the requested test update is missing.
- Present cancellation and timeout as skipped final verification with explicit authority revocation instead of failed scope and acceptance checks.
- Keep project-configured acceptance authoritative when Codex proposes different criteria; model-proposed checks remain audit metadata and cannot weaken or extend the trusted final-verification command set.

### Changed

- Bump the Codex connector revision to 10 and the built-in adapter revision to 4 so enrolled projects explicitly refresh the strengthened authority, acceptance, and lifecycle contracts.

## [0.36.4] — 2026-08-04

### Fixed

- Keep interactive Codex turn boundaries non-terminal: `Stop` now reports `AGENT TURN COMPLETED` with final verification pending, while configured acceptance and evidence sealing run exactly once after the user exits the TUI.
- Namespace native Codex hook idempotency keys by lifecycle event and payload so reused turn-level identifiers cannot make `PreToolUse` or `PostToolUse` fail with an adapter-cache conflict.
- Route read-only inspection and connector-control hooks outside the normalized mutation state machine, eliminating false hook failures before a ChangeIntent is admitted while preserving fail-closed mutation enforcement.
- Defer interactive `SessionEnd` normalization until launcher-owned verification completes, then seal the session in the correct lifecycle order.
- Distinguish untracked Claim Plane-managed `.codex/hooks.json` from user-authored dirty worktree changes in `claim-plane doctor`.

### Changed

- Bump the Codex connector revision to 7 so existing enrollments are explicitly refreshed with the corrected interactive lifecycle behavior.
- Preserve exact operator-provided initial scope in the TUI path; out-of-scope writes now reach the brokered amendment flow instead of being hidden by failed lifecycle hooks.

## [0.36.3] — 2026-08-04

### Added

- Add `claim-plane codex` as an interactive launcher that preserves the normal Codex TUI while binding the session to Claim Plane policy, scope, lifecycle, final acceptance, and durable evidence.
- Add optional initial prompts, model selection, operator-guided scope, scope locking, acceptance timeout, session timeout, and final JSON export to the interactive launcher.
- Record interactive runs with the same `claim-plane.controlled-run.v1` evidence contract used by one-shot execution.

### Changed

- Render denied writes with the exact target and operator boundary instead of exposing an opaque tool payload header.
- Show initial scope, brokered additions, and final scope as a compact scope-evolution block in the verification card and evidence report.
- Retry only transient operating-system resource-pressure errors when spawning headless swarm Codex workers, preventing isolated macOS `EAGAIN`/resource-deadlock failures from becoming false `SPAWN_FAILED` outcomes.

## [0.36.2] — 2026-08-04

### Added

- Add optional repeatable `claim-plane run --scope PATH` for operator-provided initial mutation authority while keeping automatic planner-generated scope as the default user experience.
- Add `--lock-scope` for CI, compliance, and deterministic experiments that must deny every out-of-scope mutation without opening an amendment ticket.
- Persist initial-scope mode, lock state, and amendment counts in controlled-run evidence and show brokered expansions in the human console summary.

### Changed

- Constrain model-proposed initial file operations server-side when explicit scope is present; genuinely required additional files must cross the existing exact-resource amendment path.
- Keep zero-friction automatic planning for normal runs: users only provide scope when they need a reproducible boundary or a strict authority ceiling.

## [0.36.1] — 2026-08-04

### Added

- Add a compact terminal narrative for controlled Codex runs with a stable header, explicit execution stages, verification results, changed-file summary, evidence location, and a clearly labelled untrusted agent summary.
- Add `claim-plane run --verbose` for raw Codex runtime diagnostics while keeping the default product output concise and human-readable.
- Add restrained automatic terminal colour with `NO_COLOR` support and plain-text output when redirected or captured by automation.

### Changed

- Replace raw Codex stderr in the default run experience with deduplicated policy notices and a concise hint to use `--verbose` for unclassified runtime diagnostics.
- Present scope, acceptance, risk, changed files, duration, and final delivery outcome as one scan-friendly verification card without changing the machine-readable controlled-run record.

## [0.36.0] — 2026-08-03

### Added

- Add the single-agent technical-preview packaging contract, public exit-code manifest, `claim-plane preview`, and machine-readable schema index.
- Bundle every public JSON Schema inside the wheel and add `claim-plane schemas list` and `schemas export` for exact release-matched schema distribution.
- Add `claim-plane config status` and atomic `config migrate` support for the documented legacy preview config, including dry-run, fail-closed unknown protocols, and a preserved backup.
- Add a five-minute Codex quickstart, CLI reference, guarantee model, troubleshooting guide, upgrade/uninstall guide, offline demo repository, preview issue form, and pull-request checklist.
- Add `scripts/check-technical-preview.sh` for version consistency, CLI/resource validation, offline demo execution, and optional clean wheel inspection.

### Changed

- Promote the public single-agent Codex path from research-only packaging to a clearly bounded technical preview while retaining explicit `0.x` limitations.
- Use the shared public exit-code constants for controlled-run terminal outcomes.
- Include packaged schemas and user documentation in source distributions, validate the preview contract in the repository quality gate and PyPI publish workflow, and require honest dogfood-gate status in release preparation.
- Make `claim-plane init` perform only supported config migrations during explicit re-enrollment and preserve a migration backup; full reset removes backups only with `--remove-config`.

## [0.35.0] — 2026-08-03

### Added

- Add `claim-plane.dogfood-suite.v1` for freezing repository commits, task prompts, source references, acceptance contracts, task/risk classes, coder seeds, and the fixed Bare/Observe/Guarded arms before execution.
- Add deterministic task × seed × arm planning with stable execution identities and a canonical plan digest.
- Add structured dogfood result, summary, and release-gate protocols covering task success, accepted delivery, scope and mutation findings, human repair, time, usage, cost, diff size, API drift, and dependency drift.
- Add `claim-plane dogfood freeze`, `validate`, `plan`, `record`, `aggregate`, and `gate` commands plus five JSON Schemas and a release-readiness helper.
- Add regression coverage for frozen-input tamper detection, release-grade corpus constraints, complete and incomplete matrices, deterministic planning, uncompensated success regressions, and CLI artifact flow.

### Changed

- Require the technical-preview gate to remain incomplete until every frozen task, seed, and arm has one measured result; missing cells are never imputed or represented as benchmark outcomes.
- Block preview readiness when guarded mode materially reduces task success without a configured accepted-delivery improvement.

## [0.34.0] — 2026-08-03

### Added

- Add `claim-plane report [run-id|latest]` for deterministic, secret-safe evidence reports reconstructed from durable controlled-run records and normalized lifecycle events.
- Add `claim-plane replay [run-id|latest]` for provider-free reconstruction of admission, mutation, amendment, verification, stop, and session chronology.
- Add final change summaries with file status, additions, deletions, binary classification, diff digests, and hunk coordinates without storing source content.
- Add acceptance, evidence-report, evidence-replay, and change-summary JSON Schemas plus regression coverage for restart-safe reconstruction, latest-run selection, CLI exports, redaction, and corrupt-journal refusal.

### Changed

- Extend controlled-run records with configured acceptance commands and a deterministic final Git change summary.
- Distinguish blocked, observed, amended, and post-verified activity in the public evidence view while preserving adapter guarantee provenance.
- Fail closed when the lifecycle journal no longer matches the run-bound durable head.

## [0.33.0] — 2026-08-03

### Added

- Add `claim-plane.policy.v1` with stable `observe`, `guarded`, `strict`, and `critical` semantics, canonical digests, and explicit behavior for unknown, destructive, network, secret, scope-expansion, and human-gated decisions.
- Add deterministic `low`, `medium`, `high`, and `critical` repository risk classes with built-in protected-resource rules and project-defined path rules.
- Add `claim-plane policy inspect` and `claim-plane policy classify` for machine-readable policy inspection, adapter compatibility, path classification, reason codes, and explanations.
- Add policy and risk JSON Schemas plus regression coverage for stable preset semantics, shadow-mode enforcement boundaries, critical-path review, deterministic digests, and run evidence.

### Changed

- Include the complete effective policy, policy digest, changed-path risk findings, and final policy action in every controlled-run record.
- Pin the canonical policy manifest into the controlled Codex session so project-config drift during execution cannot silently change mutation semantics.
- Make `claim-plane doctor` evaluate the configured policy even when `--policy` is omitted and fail readiness when the selected adapter cannot provide the required guarantees.
- Let `observe` continue supported would-deny mutations while preserving fail-closed control invariants and final Git verification.
- Convert otherwise verified high-risk deliveries to `REVIEW_REQUIRED` and policy-denied deliveries to `REJECTED`.

## [0.32.0] — 2026-08-03

### Added

- Add `claim-plane run` as the bounded single-agent Codex entry point with adapter handshake, project diagnostics, policy compatibility checks, wall-time control, and optional model override.
- Add `claim-plane.controlled-run.v1`, binding each execution to stable run/session/intent identities, initial and final Git-state digests, runtime summaries, lifecycle evidence, completion results, and cancellation outcomes.
- Add deterministic terminal states and exit codes for `VERIFIED`, `REJECTED`, `REVIEW_REQUIRED`, `CANCELLED`, `TIMED_OUT`, and `FAILED`, plus JSON output and durable private run records.
- Add the controlled-run JSON Schema and regression coverage for verified delivery, hook environment binding, machine-readable CLI output, and timeout-driven authority revocation.

### Changed

- Propagate controlled run identity through Codex lifecycle hooks so all normalized session events and private connector state bind to the same run.
- Hash raw task text, final messages, and unexpected error messages in durable controlled-run evidence rather than storing their contents.

## [0.31.0] — 2026-08-03

### Added

- Add `claim-plane.project-config.v1` with a stable project identity, repository identity, default-branch discovery, adapter settings, and automatically detected acceptance commands.
- Add project-level diagnostics for Git state, local-state permissions, acceptance command availability, credential hygiene, Codex authentication availability, sandbox characteristics, and adapter compatibility.
- Add `claim-plane reset` for removing only Claim Plane-owned state and hook handlers while preserving repository content, unrelated Codex hooks, and the project config by default.
- Add project configuration and doctor JSON Schemas plus regression coverage for idempotent enrollment, runtime metadata, secret-safe diagnostics, default doctor routing, and safe reset.

### Changed

- Let `claim-plane doctor` use Codex as the default adapter while preserving `claim-plane doctor codex`.
- Record detected Codex runtime and sandbox metadata during enrollment and create an exact adapter pin automatically when the runtime exposes a version.

## [0.30.0] — 2026-08-03

### Added

- Add `claim-plane.adapter-registry.v1` with built-in, programmatic, and `claim_plane.adapters` entry-point discovery.
- Add dependency-free semantic version ranges and `claim-plane.adapter-handshake.v1` negotiation before controlled session start.
- Add project-local `claim-plane.adapter-pin.v1` records that bind adapter, runtime, negotiated protocol, provider source, distribution, and capability-manifest digest.
- Add `claim-plane adapters list`, `adapters doctor`, and `adapters pin` commands with structured compatibility and migration findings.
- Add registry, handshake, and pin JSON Schemas plus regression coverage for incompatible ranges, runtime drift, external discovery, and pre-session refusal.

### Changed

- Bind negotiated protocol and project pin identity into Codex session-start and resume evidence.
- Require an existing adapter pin to match before Codex creates or resumes session authority.

## [0.29.0] — 2026-08-03

### Added

- Add `claim-plane.adapter-conformance.v1`, a reusable thirteen-scenario compatibility suite for every coding-agent adapter.
- Add the dependency-free `ReferenceAdapter` and reference conformance driver so Claim Plane Core can be tested without an external agent runtime.
- Add the Codex conformance driver and `claim-plane adapters conformance` command with structured JSON reports, canonical digests, and isolated Git fixtures.
- Add guarantee-to-scenario verification: every available manifest claim must have passing executable coverage or the adapter report is incompatible.
- Add the adapter conformance report JSON Schema and regression coverage for built-in adapter parity, uncovered claims, failed claims, report output, idempotency, recovery, cancellation, corruption, and redaction.

### Changed

- Run the dependency-free adapter conformance suite as part of the repository quality gate so capability claims cannot regress without failing CI.

## [0.28.0] — 2026-08-03

### Added

- Add `claim-plane.adapter-capabilities.v1`, separating runtime capabilities from effective guarantees with explicit enforcement levels and provider attribution.
- Add deterministic `observe`, `guarded`, `strict`, and `critical` policy compatibility checks that fail before authority-bearing execution when required guarantees are unavailable.
- Add `claim-plane adapters inspect codex` and policy-aware `claim-plane doctor codex` output, including adapter/runtime versions, capability levels, guarantee sources, and reasoned incompatibility findings.
- Add the adapter capability manifest JSON Schema and regression coverage for canonical digests, tamper detection, unsupported hard-guarantee rejection, CLI inspection, and policy refusal.

### Changed

- Bind the effective capability manifest digest, adapter/runtime versions, capabilities, guarantee levels, and guarantee providers into session-start lifecycle evidence and lifecycle reports.
- Declare project-local Codex interception honestly: supported tool writes may be hard-blocked when runtime coverage is complete, while bypassed host writes remain post-verified.

## [0.27.0] — 2026-08-03

### Added

- Add `claim-plane.lifecycle-event.v1`, a runtime-neutral append-only session chronology with deterministic event identities, causal links, sequence validation, and canonical digests.
- Add shared lifecycle report, replay, recovery validation, and canonical NDJSON export APIs for Codex and future adapters.
- Add secret-safe event normalization, duplicate suppression, atomic request batches, and fail-closed handling of corrupt, partial, out-of-order, or tampered event streams.
- Add lifecycle JSON Schema and regression coverage for replay, resume, redaction, evidence export, and invalid-state rejection.

### Changed

- Route Codex adapter operations through the normalized event store and validate the durable causal chain before resume, mutation, amendment, or completion verification.

## [0.26.0] — 2026-08-03

### Added

- Add the public `claim-plane.agent-adapter.v1` request and response contract with stable request, session, run, intent, and intent-version identities.
- Add a runtime-neutral `AgentAdapter` interface covering project enrollment, diagnostics, session lifecycle, task submission, intent proposal, mutation admission, scope amendment, completion verification, cancellation, and resume.
- Add structured adapter status and error taxonomies, persistent idempotency records, request-fingerprint conflict detection, and explicit timeout fields.
- Add `CodexAdapter` as the first complete implementation, including native hook dispatch, stale intent-version rejection, safe cancellation, and fail-closed resume behavior.
- Add JSON Schemas and regression coverage for the adapter boundary and guarded Codex delivery path.

### Changed

- Route the public Codex CLI lifecycle and headless swarm enrollment through `CodexAdapter` while preserving the existing Python connector functions for compatibility.
- Keep adapter request caches free of raw prompts and tool inputs; only request fingerprints and adapter responses are persisted.

## [0.25.0] — 2026-08-01

### Added

- Add the operator-facing `claim-plane.swarm-operator-snapshot.v1` view, which combines session, scheduler, worker, budget, recovery, worktree, merge, and verification state into one read-only status document.
- Add `swarm start` to prepare, dispatch, integrate, and verify an existing swarm session, or create and start one directly from `--spec`, without bypassing the underlying budget, admission, scheduler, worktree, merge, or evidence protocols.
- Add `swarm logs` with a normalized durable timeline across Codex JSONL events, worker lifecycle records, recovery actions, merge entries, and final verification.
- Add `swarm demo`, an offline deterministic three-worker example that demonstrates a parallel first wave, dependency-gated downstream execution, deterministic integration, and a final `SWARM VERIFIED` report.
- Add operator snapshot and event JSON Schemas, public Python APIs, CLI regression coverage, and an executable demo helper.

### Changed

- Upgrade `swarm status` from a planning-only view to a compact work-by-work operator display with phase, active capacity, token usage, scheduler state, run state, merge state, verification state, and next action.
- Make evidence-directory creation race-safe when multiple Codex workers start concurrently and create the same trusted parent namespace.
- Stop the high-level operator loop on control-plane or worktree exceptions instead of repeatedly dispatching the same scheduler item; ordinary agent failures continue through bounded fresh-identity replacement when policy permits.
- Keep `swarm start` idempotent for completed sessions and fail closed for dirty first-dispatch worktrees, paused sessions, stale authority, merge conflicts, exhausted budgets, and failed verification.

## [0.24.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-recovery.v1`, durable recovery events, worker heartbeat leases, orphan inspection, and fail-closed crash classification for reserved, running, and cancelling Codex runs.
- Add `swarm recovery-status`, `swarm recover`, and `swarm recovery-events` commands for detecting lost processes, reclaiming explicitly stale leases, reopening interrupted verification, and auditing recovery actions.
- Add `swarm pause`, `swarm resume`, and `swarm cancel` session controls without repeating already completed or integrated work.
- Add `swarm replace-codex`, which creates a fresh run and Codex thread only after current admission, scheduler, retry, launch, budget, dependency, and managed-worktree checks pass again.
- Add schema-v9 migration, recovery JSON Schema, run replacement lineage, recovery generations, and regression coverage for idempotent recovery, explicit worktree reset, interrupted verification, and durable session control.

### Changed

- Persist heartbeat and lease expiry on active Codex runs and classify a provably missing process as terminal `lost`, allowing the existing retry scheduler to release a bounded replacement attempt.
- Refuse to let a replacement worker silently inherit predecessor edits or commits; contaminated managed worktrees require explicit `--reset-worktree` and are restored to the predecessor's controlled execution base.
- Keep a live process with an expired heartbeat fail-closed by default. Recovery reports it as stale and requires explicit `--terminate-stale` before authority can be reclaimed.
- Preserve the distinction between recovery, execution success, integration, and verification: a recovered or replacement worker still must pass normal merge and final `SWARM VERIFIED` gates.

## [0.23.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-verification.v1`, a durable two-level evidence report bound to the exact repository, base commit, graph, budget, shared admission, merge queue, and integrated Git head.
- Add `swarm verify` and `swarm evidence` commands for executing and inspecting final swarm verification in human-readable or JSON form.
- Add per-work-item evidence for integrated paths, changed regions, source and integration commits, acceptance results, declared-scope violations, and verification status.
- Add root-level verification over the complete integration worktree using the union of admitted ChangeIntents, contract and preserve checks, root acceptance criteria, and immutable snapshot integrity.
- Add schema-v8 migration, JSON Schema, and regression coverage for successful verification, undeclared integrated changes, acceptance mutation, incomplete queues, and durable evidence retrieval.

### Changed

- Transition the swarm session to `VERIFYING` before evidence collection and to `COMPLETED` only after a clean report; failed verification transitions the session to `FAILED`.
- Keep process success and Git integration distinct from verified completion: only a clean two-level report produces `SWARM VERIFIED`.
- Restore the managed integration worktree to its durable integration head when acceptance commands mutate repository content, preserving the evidence boundary for later repair or recovery.
- Reject verification while workers are active, when the merge queue is incomplete or stale, when the integration worktree is dirty, or when its HEAD differs from the durable queue state.

## [0.22.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-merge-queue.v1`, a durable deterministic integration queue bound to the exact repository, base commit, graph, budget, shared-admission fingerprint, and Claim Plane-owned integration branch.
- Add `swarm merge-plan`, `swarm merge-queue`, `swarm merge-next`, and `swarm merge-all` commands for planning, inspecting, reserving, and draining integration work without mutating the user target branch.
- Add deterministic worker snapshot commits, ordered integration commits, conflict-path capture, no-op result handling, and rollback to the previous integration head after a failed cherry-pick.
- Add a managed integration worktree under the existing swarm ownership namespace and schema-v7 migration with JSON Schema and regression coverage.

### Changed

- Release effective dependencies only after their worker result is integrated when a merge queue exists; a successful process alone no longer makes downstream work runnable in that lifecycle.
- Advance a clean dependent worktree to the durable integration head before execution so the worker observes integrated prerequisite changes while the original session base remains the authority anchor.
- Keep integration separate from final verification: the queue produces an unverified integration branch and never updates the configured target branch.
- Fail closed when worker branches contain unexpected commits, integration state is dirty or stale, queue reservations race, or actual Git conflicts contradict the planner-declared concurrency model.

## [0.21.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-shared-admission.v1`, deriving one deterministic, repository- and base-bound `ChangeIntent` per work item and admitting the set against the whole swarm authority topology.
- Add effective dependency construction that preserves explicit work-graph dependencies and promotes adaptive-concurrency serialization constraints into durable scheduler prerequisites.
- Add `claim-plane.swarm-scheduler-snapshot.v1`, with dynamic states for runnable, capacity-queued, active, retryable, succeeded, failed, dependency-blocked, and replan-required work.
- Add `swarm admit`, `swarm admission`, and `swarm scheduler` commands with human-readable and JSON output.
- Add schema-v6 migration, JSON Schemas, and regression coverage for shared admission, dynamic dependency release, retry exhaustion, source invalidation, upgraded 0.20 sessions, and runner compatibility.

### Changed

- Replace static execution-wave gating in the Codex runner with an atomic scheduler decision based on the current shared admission, durable run records, dependency outcomes, retry ceilings, and remaining active-worker capacity.
- Bind each worker reservation to the exact shared-admission fingerprint inside the same SQLite transaction that reserves the worker slot, preventing concurrent launches from acting on stale scheduler state.
- Invalidate stored shared admission whenever the work graph, budget policy, or concurrency plan changes, while keeping repeated admission of identical sources idempotent.
- Keep process success separate from verification: `succeeded` releases execution dependencies, but merge integration, cross-intent verification, and final `SWARM VERIFIED` remain later stages.

## [0.20.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-codex-run.v1`, a durable execution record bound to one swarm session, work item, managed worktree, exact graph and budget versions, command, prompt digest, budget slice, evidence paths, Codex thread, usage, and terminal classification.
- Add a bounded headless Codex runner that launches only work items in the current executable wave after dependency, worktree-ownership, launch, restart, active-worker, token, and wall-time checks pass.
- Add `swarm run-codex`, `swarm runs`, `swarm run-status`, and `swarm cancel-codex` commands, including JSON output and durable JSONL/stdout/stderr evidence.
- Add timeout, cancellation, non-zero exit, token-overrun, spawn-failure, and connector-setup classifications without treating process success as verification.
- Add a private no-symlink evidence namespace under `.claim-plane/swarm/runs/`, rejecting repository-controlled path redirection before a worker reservation is persisted.
- Add schema-v5 migration, JSON Schema, installed-runner smoke coverage, and regression tests for wave gating, per-item concurrency, restart ceilings, accounting, cancellation, and worktree binding.

### Changed

- Transition a planned swarm session to `RUNNING` atomically with the first reserved worker run, freezing the execution graph and budget binding before an agent process starts.
- Protect active runner worktrees from cleanup and track Claim Plane-owned Codex enrollment metadata separately from user-authored repository changes.
- Keep 0.20.0 at the bounded execution boundary: a successful Codex exit is `succeeded`, not `VERIFIED`; shared admission, merge integration, and swarm-level verification remain later stages.
- Record token usage available from Codex JSONL while marking monetary cost metering unavailable rather than estimating or fabricating provider cost.
- Enforce `max_wall_time_seconds` as elapsed swarm execution time measured from the first reserved worker, so parallel workers share one real deadline instead of consuming an artificial sum of process durations.

## [0.19.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-managed-worktree.v1`, a durable ownership record bound to one swarm session, work item, repository identity, graph version, and pinned Git base.
- Add deterministic Claim Plane-owned branch and path allocation under `.claim-plane/worktrees/`, with one isolated linked worktree per planned work item.
- Add `swarm provision-worktrees`, `swarm worktrees`, and `swarm cleanup-worktrees` commands for idempotent provisioning, health inspection, selective cleanup, and machine-readable output.
- Add worktree health detection for dirty, stale-graph, missing, unregistered, branch-mismatch, and base-mismatch states, plus detection of unowned Git worktrees inside the managed session directory.
- Add JSON Schema, schema-v4 migration, and regression coverage for collision refusal, cleanup ownership boundaries, dirty-worktree protection, orphan preservation, and repeatable provisioning.

### Changed

- Require a current `ready` adaptive concurrency plan before worktrees can be provisioned.
- Refuse to overwrite pre-existing paths or branches, and remove only worktrees and branches backed by durable Claim Plane ownership records.
- Roll back newly created Git worktrees and branches when durable persistence fails, preventing partially provisioned sessions from being treated as executable.
- Keep 0.19.0 at the isolation boundary: worktrees are prepared and owned, but Codex worker processes, intent binding, and provider usage accounting are introduced by later swarm stages.

## [0.18.0] — 2026-08-01

### Added

- Add `claim-plane.swarm-concurrency-plan.v1`, a deterministic plan bound to exact work-graph and budget versions and fingerprints.
- Add an adaptive concurrency controller that respects explicit dependencies, packs independent work up to `max_active`, and emits stable execution waves without launching workers.
- Add conservative pairwise analysis for same-file regions, unknown path or semantic overlap, shared contracts, and schema-changing work.
- Add durable plan persistence, optimistic source-version checks, automatic invalidation after graph or budget replacement, and `swarm plan`, `swarm concurrency`, and `swarm validate-concurrency` commands.
- Add JSON Schema, protocol exports, and regression coverage for deterministic ordering, region-safe parallelism, serialization, denial, contingent scope, and migration-safe persistence.

### Changed

- Treat contingent operations as non-authoritative planning surfaces until a later admitted amendment promotes them; the controller recalculates concurrency after graph or budget changes.
- Return `replan_required` with no executable waves when a configured `deny` policy detects an unsafe decomposition.
- Upgrade the local swarm database to schema version 3 by adding a source-bound concurrency-plan table while preserving 0.16 and 0.17 session payloads.
- Keep 0.18.0 at the scheduling boundary: plans are computed and persisted, but worktrees and Codex worker processes are introduced in later stages.

## [0.17.0] — 2026-08-01

### Added

- Add the `claim-plane.swarm-budget-policy.v1` protocol with conservative defaults and hard ceilings for active workers, per-work-item concurrency, work-graph size, total launches, tokens, cost, wall time, replans, repairs, and agent restarts.
- Add explicit concurrency policy for region-safe same-file work and fail-closed serialization or denial of unknown overlap, shared contracts, and schema changes.
- Bind one independently versioned budget policy to every `SwarmSession`, including deterministic fingerprints, graph-capacity summaries, and backward-compatible migration of 0.16 swarm databases.
- Add `claim-plane swarm budget`, `replace-budget`, and `validate-budget`, plus JSON Schema and executable policy examples.

### Changed

- Validate a proposed work graph against its session budget before persistence and reject later graph or policy replacements that would exceed `max_work_items` or `max_total_launches`.
- Require optimistic budget-version checks while keeping repository identity, pinned Git base, root task, integration target, and work graph unchanged.
- Keep 0.17.0 at the planning boundary: budgets are durable authority ceilings, but worker accounting and adaptive scheduling are introduced by later swarm stages.

## [0.16.0] — 2026-08-01

### Added

- Add repository-bound `SwarmSession` planning state with Claim Plane-owned session identity, exact Git base pinning, repository identity binding, integration target, and durable SQLite persistence.
- Add planner-proposed work graphs with typed intent operations, preserves, acceptance criteria, explicit dependencies, deterministic topological order, roots, leaves, dependency layers, and SHA-256 graph fingerprints.
- Add `claim-plane swarm create`, `list`, `status`, `graph`, `replace-graph`, and `validate` commands.
- Add JSON Schemas and an executable example for the swarm session and work-graph protocols.

### Changed

- Reserve swarm lifecycle states for future budget, scheduling, worktree, execution, and integration stages without allowing 0.16.0 to launch workers prematurely.
- Reject invalid work graphs before persistence, including cycles, unknown dependencies, duplicate identifiers, repository escapes, and Claim Plane, Codex, or Git control-state scope.
- Require optimistic graph-version checks for planner refinements while keeping the session identity, repository binding, pinned base, root task, and integration target immutable.

## [0.15.0] — 2026-07-31

### Added

- Fingerprint pre-existing user worktree changes at Codex task bootstrap so unchanged local work is excluded from completion attribution while those paths remain protected from autonomous mutation.
- Recover `codex resume` sessions by renewing live intent authority or atomically re-admitting an expired execution contract on the unchanged pinned commit and branch.
- Expose connector hardening state through Codex intent status, including baseline paths, resume recovery, and concurrent-session blockers.
- Add `claim-plane codex-intent abandon` to release unfinished session authority explicitly before another Codex session takes ownership of the same worktree.
- Validate connector revision and canonical Claim Plane-owned hook definitions in `claim-plane doctor codex`.

### Changed

- Serialize mutation authority to one active Codex session per physical worktree; concurrent autonomous work is directed to separate Git worktrees for unambiguous evidence.
- Fail closed on intercepted mutation calls when project enrollment is missing, local session state is unreadable, the Git root cannot be resolved, or the task branch changed after bootstrap.
- Make reconnect upgrade-safe by recognizing older Claim Plane Codex dispatcher forms, replacing only connector-owned handlers, and preserving unrelated project hooks.

## [0.14.0] — 2026-07-31

### Added

- Verify active Codex tasks automatically at `Stop` against the admitted ChangeIntent, actual Git changes, preserve and contract policies, and declared acceptance commands.
- Mark clean Codex work as `VERIFIED`, complete the admitted intent, and expose a machine-readable `claim-plane.codex-completion.v1` result through session status and `claim-plane codex-intent verify`.
- Return bounded verification findings to Codex when completion fails so the current turn can continue and repair the admitted work before another completion attempt.
- Track authorized and denied mutation calls separately from read-only tool traffic, retain admitted scope-amendment history, and summarize changed files, acceptance outcome, and executed authority violations.
- Include untracked repository files in Git verification so newly created undeclared files cannot disappear from completion evidence.

### Changed

- Give the Codex `Stop` handler a bounded verification window suitable for acceptance execution while keeping `SessionEnd` lightweight.
- Avoid repeated Stop-hook continuation loops: one failed completion may request another model turn, while a still-failing continuation remains explicitly `UNVERIFIED` instead of blocking indefinitely.
- Exclude connector-owned control surfaces from task-change accounting while retaining them as non-grantable mutation surfaces under the pre-mutation guard.

## [0.13.0] — 2026-07-31

### Added

- Issue short-lived, session-bound scope-amendment tickets when a Codex guard denial identifies exact additional file authority.
- Add `claim-plane codex-intent amend` so Codex can provide rationale while Claim Plane owns the requested resource set, task identity, pinned base, and atomic re-admission.
- Support atomic amendment of multiple contingent resources that cannot be safely promoted one at a time within a single tool call.
- Expose amendment ticket, admitted/rejected counts, and the last amendment outcome through session status without storing denied edit payloads.
- Add `claim-plane.codex-scope-amendment.v1` and its JSON Schema.
- Add a session-local Codex control channel for intent admission, status, and scope amendment, including inline proposal JSON for mutation-free bootstrap.

### Changed

- Keep scope amendments monotonic: existing protections, acceptance requirements, identity, and base revision are preserved, and a rejected amendment leaves the active intent unchanged.
- Bind amendment tickets to the current intent fingerprint and base commit, reject stale or altered tickets, and avoid widening line-bounded declarations from whole-file hook observations.
- Restrict connector control commands to the current Codex session and repository instead of granting general Claim Plane shell authority.
- Reserve `.claim-plane/**`, `.git/**`, and `.codex/**` as connector control surfaces that cannot be granted by a session intent or scope amendment.

## [0.12.0] — 2026-07-31

### Added

- Authorize intercepted Codex repository mutations against the live session-bound ChangeIntent before the tool call executes.
- Classify built-in edit calls and a conservative direct shell subset into concrete file write, delete, and rename requests; allow known read-only discovery without requiring an active intent.
- Atomically promote one matching contingent mutation surface through normal admission before authorization, while rejecting multi-surface promotion as a single opaque action.
- Return structured model-visible denials for undeclared scope, stale task bases, missing intent authority, unknown mutating tools, and shell effects that cannot be proven.
- Expose guard counters, promotion count, last decision code, tool name, and affected paths in Codex intent status without storing raw tool arguments.
- Add Codex runtime compatibility diagnostics for the file-edit hook coverage required by pre-mutation authorization.

### Changed

- Keep successful Claim Plane authorization additive to Codex's own sandbox and approval flow instead of emitting a positive permission override.
- Treat lifecycle interception as a runtime integration boundary while retaining brokered execution as the hard reference-monitor boundary for non-bypassable mutation control.

## [0.11.0] — 2026-07-31

### Added

- Bind the first Codex task in each enrolled session to connector-owned task, intent, owner, and exact Git base identities.
- Inject a model-visible `UserPromptSubmit` bootstrap contract for read-only repository discovery followed by structured ChangeIntent proposal and atomic admission.
- Add `claim-plane codex-intent admit` and `claim-plane codex-intent status` for the session-bound execution contract.
- Add `claim-plane.codex-intent-proposal.v1` with committed and contingent operations, preserve requirements, acceptance checks, dependencies, and metadata.
- Renew active session intent leases from Codex prompt and tool lifecycle events instead of requiring model-authored heartbeat calls.

### Changed

- Keep raw Codex prompts out of connector state while retaining a SHA-256 digest and prompt length for local correlation.
- Reject session intent admission when Git `HEAD` changes after task bootstrap, and validate file/document resources as repository-relative before admission.
- Reuse the 0.10 lifecycle enrollment unchanged so existing connected repositories receive session bootstrap behavior after upgrading Claim Plane.

## [0.10.0] — 2026-07-31

### Added

- Add project-local Codex enrollment through `claim-plane init`, `claim-plane connect codex`, `claim-plane disconnect codex`, and `claim-plane doctor codex`.
- Add a stable Codex lifecycle dispatcher covering session start/end, prompt submission, pre/post tool use, and turn stop events without requiring an MCP call from the model.
- Record a minimal session handshake in local Claim Plane state without persisting prompt text or tool arguments.

### Changed

- Recommend `uv tool install claim-plane` for the CLI while retaining `pipx` as an isolated-tool alternative.
- Preserve unrelated `.codex/hooks.json` entries during enrollment changes and fail closed when project configuration explicitly disables Codex hooks.

## [0.9.4] — 2026-07-28

### Added

- Add live progress to the one-time confirmatory Planner v1 freeze, including the current pair and feature, elapsed time, ETA, per-plan cost, and cumulative spend.
- Report durable planner-freeze completion in `confirmatory status` so interrupted planning can be inspected without debug tooling.

### Changed

- Persist each completed feature declaration atomically instead of waiting for both declarations in a pair, allowing `freeze-plans` to resume at feature granularity after interruption.
- Handle `Ctrl+C` as a normal resumable research interruption without a Python traceback.

## [0.9.3] — 2026-07-28

### Added

- Add structured GitHub Issue Forms for bugs, features, research tasks, and reproducibility problems, with a Discussions-first path for questions and exploratory work.
- Add an issue-intake workflow that converts the required Area field into the repository's `area:*` taxonomy and an idempotent GitHub CLI script for synchronizing public labels.
- Add repository-level `CITATION.cff` metadata and a permanent `papers/` index for research publications.
- Link the 2026 Claim Plane arXiv preprint to its paper-specific BibTeX citation and executable six-pair CooperBench reproduction.

### Changed

- Pin Ruff to `0.15.21` for deterministic lint and formatting behavior across local development, pre-commit, and CI.
- Make the existing `scripts/check.sh` the canonical repository quality gate used by GitHub Actions on Python 3.10–3.13.
- Run pre-commit Ruff checks repository-wide instead of limiting them to files staged in the current commit.
- Report the commit, Python version, and Ruff version at the start of the quality gate so CI environment drift is immediately visible.
- Extend the quality gate to cover linting, formatting, typing, shell syntax, research environment validation, bytecode compilation, tests, and the protocol suite in one command.

## [0.9.2] — 2026-07-27

### Fixed

- Use the typed CooperBench `TaskInfo` contract throughout the published six-pair
  execution path instead of legacy notebook-style dictionary indexing.
- Keep task directory, clone URL, base commit, and feature paths on one typed execution
  path so gold sanity and paid study execution consume the same discovered inputs.

## [0.9.1] — 2026-07-27

### Fixed

- Place CooperBench execution worktrees directly under an `agent_workspace` directory,
  satisfying benchmark safety checks used by the Jinja tasks during gold sanity and
  official feature evaluation.
- Apply the same workspace normalization to the confirmatory 30×3 protocol so paper
  reproduction and follow-up experiments share the benchmark-compatible layout.

## [0.9.0] — 2026-07-27

### Added

- Add live terminal progress for the published six-pair reproduction with durable-unit
  percentage, current pair and arm, elapsed time, per-arm ETA estimation, execution
  result, logical cost, and wall-clock duration.
- Add stage visibility for frozen-input validation, CooperBench gold sanity, the
  24-execution study matrix, and final aggregation/reference comparison.
- Add resume-aware progress that starts from the durable checkpoint and reuses recorded
  wall-clock durations to improve ETA estimates after interrupted runs.
- Add the same execution progress surface to confirmatory coder shards.

### Changed

- Record wall-clock duration on completed research execution artifacts without changing
  the frozen study protocol or model inputs.
- Keep progress on stderr so stdout remains a stable machine-readable CLI result.


## [0.8.0] — 2026-07-27

### Added

- Add strict confirmatory-study aggregation that accepts results only after all nine
  shards are complete and all 360 pair/seed/arm executions are unique and protocol-aligned.
- Add arm, feature-pair, and repository-task cluster summaries with deterministic
  task-cluster bootstrap confidence intervals and paired arm deltas.
- Add machine-readable failure taxonomy, scope-promotion/block/serialization mechanism
  summaries, and study-level cost accounting with one-time frozen Planner v1 cost separated
  from coder execution cost.
- Add canonical JSON/CSV publication artifacts plus a SHA-256 publication manifest and
  an offline verification command for detecting missing or modified analysis files.
- Add Docker commands for aggregation and analysis verification, together with CI coverage
  for complete synthetic 30-pair × 3-seed result matrices and integrity verification.

### Changed

- Extend the CooperBench research documentation from execution through final analysis so a
  completed confirmatory study can be aggregated and verified without notebooks.

## [0.7.0] — 2026-07-27

### Added

- Add the CLI-native 30-pair, three-seed CooperBench confirmatory protocol derived from
  the frozen V9 study design.
- Add deterministic task-balanced 15/15 conflict-clean pair selection with CooperBench
  gold-feature validation and immutable protocol artifacts.
- Add one-time Planner v1 freezing with stable per-feature planner seeds, plan
  fingerprints, resumable planner checkpoints, and reuse across coder seeds and Claim
  Plane static/dynamic arms.
- Add nine resumable 10-pair execution shards covering coder seeds 101, 202, and 303,
  with per-shard manifests, checkpoints, traces, provider accounting, and frozen-plan
  provenance.
- Add Docker wrapper commands for preparing, freezing, running, and inspecting the
  confirmatory study.

### Changed

- Generalize the CooperBench execution harness so an explicit coder seed and a frozen
  planner declaration set can be supplied without changing the published six-pair
  reproduction path.

## [0.6.0] — 2026-07-27

### Added

- Add a pinned Linux research image for CooperBench execution with a fixed Python base
  image, `uv` version, locale, timezone, and benchmark Git identity.
- Add a host wrapper for building the image, inspecting its environment, validating a
  mounted CooperBench checkout, running the published study, and persisting resumable
  artifacts outside the container.
- Add offline environment diagnostics and a machine-readable research environment lock.
- Record the mounted CooperBench Git revision when available and a stable SHA-256 digest
  over the frozen dataset inputs used by the published six-pair study.

### Changed

- Document container-based reproduction alongside direct host execution without making
  Docker a runtime dependency of Claim Plane.

## [0.5.0] — 2026-07-27

### Added

- Add a CLI-oriented reproduction of the published six-pair CooperBench mechanism check,
  including the exact frozen pair order, conflict labels, coder seed, models, four arms,
  and execution budgets used by the study.
- Add the research-only coding-agent executor, Dynamic Scope mutation controller, frozen
  scope/gate helpers, CooperBench dataset preparation, and provider accounting needed to
  execute the published protocol without Jupyter.
- Persist one shared Planner v1 output per feature pair so static and dynamic Claim Plane
  consume the same declaration across normal and resumed runs.
- Add gold-feature sanity checks, durable per-unit results and traces, aggregate JSON/CSV
  summaries, and comparison against the mechanism counts reported in the paper.

### Changed

- Document the executable paper study, its oracle-localized context condition, logical
  parallel topology, resume semantics, and the distinction between the historical study
  version and the installed Claim Plane version used for a reproduction.

## [0.4.0] — 2026-07-27

### Added

- Preserve the final CooperBench Planner v1 policy as research-only code, including
  the primary planning prompt, retry behavior, OpenRouter request contract, and stable
  policy fingerprint.
- Add deterministic source-localization and uncertainty-analysis utilities from the
  final planner calibration, including bounded support candidates, insertion anchors,
  symbol/reference surfaces, and automatic contingent selection.
- Add planner CLI commands for inspecting policy identity, rendering localized context,
  building deterministic uncertainty candidates, and running the frozen planner with
  its calibration pass.
- Add provider cost and request diagnostics while keeping API keys outside persisted
  planner outputs and the runtime package.

### Changed

- Document the oracle-localized planner condition and the boundary between the
  model-agnostic Claim Plane runtime and research-only model execution.

## [0.3.0] — 2026-07-27

### Added

- Add a model-free CooperBench research foundation with typed study declarations,
  deterministic study fingerprints, stable run identities, and reproducible sharding.
- Add canonical research artifact directories, non-secret run provenance manifests, and
  atomic resumable checkpoints for long-running evaluations.
- Add machine-readable schemas for study declarations, run manifests, checkpoints, and
  per-arm result envelopes.
- Add a repository-local CooperBench utility for validating study declarations, creating
  run directories, and inspecting checkpoints without installing model-provider clients.

### Changed

- Extend repository quality checks to lint, format-check, type-check, and compile the
  CooperBench research infrastructure alongside the runtime package.

## [0.2.1] — 2026-07-17

### Fixed

- Make contingent-scope promotion region-aware. A concrete line mutation now promotes
  only the contingent region that covers that mutation instead of all bounded regions
  declared for the same path.
- Preserve broad contingent fallbacks while granting the narrow concrete path/region
  requested at runtime, enabling incremental scope expansion without accidental
  whole-file authority.
- Keep multiple committed line capabilities as an explicit region union in the broker;
  bounded regions no longer collapse into implicit whole-file mutation permission.
- Require whole-file write, append, delete, and rename operations to hold an unbounded
  committed capability, while `replace_lines` may execute inside any one admitted
  bounded interval.

### Changed

- `promote-scope` and the MCP `promote_contingent_scope` tool accept an optional
  concrete `region` such as `lines:20-24`. Broker-driven `replace_lines` promotions
  provide this region automatically.

## [0.2.0] — 2026-07-16

### Added

- Add committed and contingent operation commitments to `ChangeIntent`. Committed
  operations participate in initial admission and grant mutation authority, while
  contingent operations remain non-blocking planning hints until promoted.
- Add atomic contingent-scope promotion with re-admission. Rejected promotions leave
  the currently admitted intent unchanged; successful promotions preserve active
  lifecycle state and create an audited `intent_scope_expanded` event.
- Add broker-driven just-in-time scope promotion. A governed broker may inspect a
  predeclared contingent path and promote it before the first mutation; the broker
  capability is re-attested to the new intent content version in the same transaction.
- Add `promote-scope` CLI and `promote_contingent_scope` MCP entry points, plus
  commitment metadata in worker context packs and the public JSON Schema.

### Changed

- Admission, verification, region enforcement, collision checks, and broker mutation
  authorization now reason only over committed operations. Contingent surfaces never
  silently grant write authority.
- Read-only broker discovery may still inspect contingent path surfaces so workers can
  gather evidence before requesting or triggering promotion.

## [0.1.1] — 2026-07-15

### Added

- Add pre-commit Ruff lint-fix and formatting hooks so staged Python changes are
  normalized before they reach CI.

### Fixed

- Re-evaluate identical blocked intents against the current active set instead of
  permanently returning a cached rejection, enabling the normal wait-and-retry flow.
- Make `release_intent()` safe after successful completion while preserving
  `completed` as the terminal success state and audit history.
- Replace raw `KeyError` failures for malformed intent/resource/operation payloads
  with stable, human-readable required-field validation errors.

## [0.1.0] — Initial public release

### Coordination and semantic admission

- Add structured `ChangeIntent` admission for reads, writes, extensions, deletes,
  renames, documentation, tests, bounded line regions, concepts, contracts, routes,
  schemas, configuration, and documents.
- Add atomic leases, heartbeats, amendments, completion, release, append-only audit
  events, acyclic dependency validation, producer-first ordering, and transitive stale
  propagation.
- Add optional fail-closed Agent Lexicon integration for canonical concept identity and
  semantic-overlap detection.
- Add bounded worker context packs, deterministic preserve policies, Git hunk checks,
  contract verification, and targeted repair plans.

### Governed execution

- Add a capability-based repository broker with exact read, write, append, delete,
  rename, document, test, and allowlisted-command permissions.
- Add clean-root enforcement, durable prepare/commit journaling, rollback and startup
  recovery, live intent revalidation, broker attestations, and trusted observation
  sessions.
- Add one canonical OS writer lock per Git worktree, registry writer leases, monotonic
  fencing tokens, compare-and-swap Git-tree transitions, and rejection of out-of-band
  worktree mutations.
- Preserve POSIX file modes across broker mutations and recovery.
- Add Linux Bubblewrap support for a proxy-only worker boundary with no repository
  mount; macOS remains supported for broker and verification workflows with a documented
  isolation limitation.

### Verified integration and evidence

- Freeze every worker into an immutable Git tree and synthetic commit before verification.
- Generate one exact binary patch per worker, verify those bytes, and compose the same
  persisted patches in dependency order inside a neutral integration worktree.
- Add worker and integrated acceptance guards, sandbox policies, observed-access checks,
  result commits, reproducible result patches, SHA-256 evidence, and optional HMAC or
  Ed25519 attestation.
- Add complete storage contracts with `SQLitePlaneStore` as the single-host backend and
  `Plane.from_store(...)` as the backend injection boundary.
- Add CLI, stdio MCP, JSON Schemas, examples, CI, protocol tests, and an A/B/C benchmark
  harness for future comparative evaluation.

### Status

- Publish as a Research Preview. Claim Plane is not yet presented as a production-grade
  security boundary or as empirically superior to worktree-only coordination.
