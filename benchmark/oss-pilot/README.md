# Frozen single-agent OSS pilot

This directory records the first real-repository validation step for the interactive
Codex integration. The selection contains three CooperBench tasks grounded in Jinja,
Click, and dirty-equals and is bound to one exact CooperBench source revision.

The pilot is intentionally small. Its purpose is to expose installation, lifecycle,
scope-amendment, acceptance, evidence, and repository-state problems before the
larger Bare Codex / Observe / Guarded comparison.

## Frozen tasks

| Task | Repository family | Initial authority | Intended pressure |
| --- | --- | --- | --- |
| `jinja-loader-local` | Jinja | `src/jinja2/loaders.py` | Focused local implementation |
| `click-completion-amendment` | Click | `src/click/shell_completion.py` | Legitimate supporting test expansion |
| `dirty-equals-scope-pressure` | dirty-equals | `dirty_equals/_other.py` | Narrow authority under adjacent-change pressure |

The canonical machine-readable selection is [`selection.json`](selection.json).
Prompts are loaded from the frozen CooperBench feature descriptions during workspace
preparation and are recorded with SHA-256 digests in each local pilot manifest.

## Run the guarded pilot

Install the current repository first, then inspect the selection:

```bash
python -m pip install -e ".[dev,signing]"
claim-plane oss-pilot list
```

Prepare and run the first task:

```bash
claim-plane oss-pilot prepare jinja-loader-local --arm guarded
claim-plane oss-pilot run jinja-loader-local --arm guarded --model gpt-5.6-luna
claim-plane oss-pilot status jinja-loader-local --arm guarded
```

Repeat with:

```text
click-completion-amendment
dirty-equals-scope-pressure
```

`prepare` clones the exact OSS base commit into a dedicated directory below
`/private/tmp/claim-plane-oss-pilot`, initializes Claim Plane, installs the Codex
connector, freezes the prompt and authority metadata, and configures the official
task evaluator as the authoritative final acceptance command.

## Acceptance isolation

CooperBench task evaluators may reset or clean their target directory. Claim Plane
therefore materializes the candidate changes and relevant untracked files in a
temporary repository below the evaluator-required `agent_workspace` directory. The
official task tests are combined with the candidate tree through a base-aware
three-way merge before execution. The temporary repository is removed after the
result is known, while the agent workspace remains available for inspection and
evidence review.

Acceptance outcomes distinguish `TEST_FAILED`, `DEPENDENCY_INSTALL_FAILED`,
`OFFICIAL_TEST_CONFLICT`, `WORKSPACE_SAFETY_FAILED`, `TIMEOUT`, and generic evaluator
errors. Full stdout, stderr, and a machine-readable result are retained under
`.claim-plane/oss-pilot/acceptance/`.

A standalone verification is also available:

```bash
claim-plane oss-pilot verify jinja-loader-local --arm guarded
```

## Comparative preparation

Each task may be prepared in an independent arm:

```bash
claim-plane oss-pilot prepare jinja-loader-local --arm bare
claim-plane oss-pilot prepare jinja-loader-local --arm observe
claim-plane oss-pilot prepare jinja-loader-local --arm guarded
```

The three directories never share mutable repository state. This layout is the
foundation for the following comparative dogfood release; the first pilot should run
Guarded only and record any blocking product defect before comparisons begin.
