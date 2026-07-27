"""Frozen coding-agent executor used by the published six-pair study.

This module is a direct CLI-oriented extraction of the V8.5 notebook executor.
The execution limits and tool protocol are frozen in :mod:`config`.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from claim_plane import AccessMode

from ..planner_v1.tools import read_context
from .config import (
    ACTION_RETRIES_PER_STEP,
    AUTO_TEST_AFTER_MUTATION,
    CODER_MAX_TOKENS,
    CODER_MODEL,
    EXPLORATION_NUDGE_INTERVAL,
    MAX_AGENT_STEPS,
    MAX_AGENT_TEST_RUNS,
    MAX_DIFF_CHARS,
    MAX_EXISTING_WRITE_FILE_CHARS,
    MAX_EXPLORATION_STEPS_BEFORE_EDIT,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    MAX_TEST_LOG_CHARS,
    MAX_TOOL_ERRORS,
    MAX_TOOL_RESULT_CHARS,
    NATIVE_TOOL_ATTEMPTS_PER_STEP,
    OFFICIAL_TEST_TIMEOUT_SECONDS,
    USE_JSON_MODE_FALLBACK,
    USE_NATIVE_TOOL_CALLS,
)
from .dataset import q, sh
from .provider import llm

AGENT_WORKSPACE_ROOT = Path(".claim-plane/cooperbench/worktrees").resolve()
AGENT_TRACE_LOGS: list[dict[str, object]] = []


def configure_workspace_root(path: str | Path) -> Path:
    """Set and create the CooperBench-safe worktree root for this process."""
    global AGENT_WORKSPACE_ROOT
    requested = Path(path).expanduser().resolve()
    AGENT_WORKSPACE_ROOT = (
        requested
        if requested.name == "agent_workspace"
        else requested / "agent_workspace"
    )
    AGENT_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return AGENT_WORKSPACE_ROOT


def reset_agent_traces() -> None:
    AGENT_TRACE_LOGS.clear()


class AgentExecutionError(RuntimeError):
    """Agent/provider/protocol failure with partial accounting and trace."""

    def __init__(
        self,
        message,
        *,
        logical_cost=0.0,
        logical_latency=0.0,
        pre_failure_cost=0.0,
        post_failure_cost=0.0,
        steps_used=0,
        tool_errors=0,
        test_runs=0,
        trace=None,
    ):
        super().__init__(message)
        self.logical_cost = logical_cost
        self.logical_latency = logical_latency
        self.pre_failure_cost = pre_failure_cost
        self.post_failure_cost = post_failure_cost
        self.steps_used = steps_used
        self.tool_errors = tool_errors
        self.test_runs = test_runs
        self.trace = trace or []


class DynamicScopeBlocked(RuntimeError):
    """Expected control-plane block, distinct from an agent/tool failure."""

    def __init__(
        self,
        message,
        *,
        intent_id,
        path,
        access,
        block_type,
        decision_kind=None,
        reason=None,
    ):
        super().__init__(message)
        self.intent_id = intent_id
        self.path = path
        self.access = AccessMode(access)
        self.block_type = block_type
        self.decision_kind = decision_kind
        self.reason = reason
        self.partial_result = None

    def attach_partial(self, partial_result):
        self.partial_result = dict(partial_result)
        return self


class DynamicScopeController:
    """Enforce committed regions and promote only the region being mutated."""

    def __init__(
        self,
        plane,
        intent_id,
        *,
        agent,
        event_sink,
    ):
        self.plane = plane
        self.intent_id = intent_id
        self.agent = agent
        self.event_sink = event_sink

    def _event(self, event_type, **payload):
        self.event_sink.append(
            {
                "event_type": event_type,
                "agent": self.agent,
                "intent_id": self.intent_id,
                **payload,
            }
        )

    @staticmethod
    def _parse_region(value):
        if not value:
            return None
        match = re.fullmatch(
            r"(?:lines?:)?\s*(\d+)\s*[-:]\s*(\d+)",
            str(value).strip(),
        )
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        if start <= 0 or end < start:
            return None
        return start, end

    @classmethod
    def _operation_covers_mutation(cls, operation, path, region):
        if not operation.resource.covers_path(path):
            return False
        if region is None:
            return operation.resource.region is None
        declared = cls._parse_region(operation.resource.region)
        if operation.resource.region is None:
            return True
        if declared is None:
            return False
        return declared[0] <= region[0] <= region[1] <= declared[1]

    def before_mutation(self, path, access, region=None):
        access = AccessMode(access)
        path = str(path).replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]

        if path == ".git" or path.startswith(".git/"):
            raise ValueError(f"Refusing to mutate repository metadata: {path!r}")

        if region is not None:
            region = (int(region[0]), int(region[1]))
            if region[0] <= 0 or region[1] < region[0]:
                raise ValueError(f"Invalid mutation region: {region!r}")

        intent = self.plane.intent(self.intent_id)
        if intent is None:
            raise RuntimeError(f"Scope intent disappeared: {self.intent_id}")

        committed_match = any(
            operation.committed
            and operation.access is access
            and self._operation_covers_mutation(operation, path, region)
            for operation in intent.operations
        )

        region_text = None if region is None else f"lines:{region[0]}-{region[1]}"

        if committed_match:
            self._event(
                "committed_scope_used",
                path=path,
                access=access.value,
                region=region_text,
            )
            return

        contingent_match = any(
            operation.contingent
            and operation.access is access
            and self._operation_covers_mutation(operation, path, region)
            for operation in intent.operations
        )

        if not contingent_match:
            self._event(
                "undeclared_scope_blocked",
                path=path,
                access=access.value,
                region=region_text,
            )
            raise DynamicScopeBlocked(
                (
                    f"Mutation {access.value} on {path!r}"
                    + (f" at {region_text}" if region_text else "")
                    + " was not present in committed or contingent scope."
                ),
                intent_id=self.intent_id,
                path=path,
                access=access,
                block_type="undeclared_scope",
                reason="No declared capability covers the concrete mutation.",
            )

        self._event(
            "promotion_attempted",
            path=path,
            access=access.value,
            region=region_text,
        )

        decision = self.plane.promote_contingent_scope(
            self.intent_id,
            path=path,
            modes=(access,),
            region=region_text,
        )

        if not decision.allowed:
            self._event(
                "promotion_rejected",
                path=path,
                access=access.value,
                region=region_text,
                decision_kind=decision.kind.value,
                reason=(decision.guidance or "; ".join(decision.constraints)),
            )
            raise DynamicScopeBlocked(
                (
                    f"Contingent scope promotion rejected for "
                    f"{self.intent_id}:{path}"
                    + (f" {region_text}" if region_text else "")
                    + f": {decision.kind.value}"
                ),
                intent_id=self.intent_id,
                path=path,
                access=access,
                block_type="promotion_rejected",
                decision_kind=decision.kind.value,
                reason=(decision.guidance or "; ".join(decision.constraints)),
            )

        self._event(
            "promotion_succeeded",
            path=path,
            access=access.value,
            region=region_text,
            decision_kind=decision.kind.value,
        )


def create_worktree(repo, path, commit):
    path = Path(path)

    if path.parent.resolve() != AGENT_WORKSPACE_ROOT.resolve():
        raise ValueError(
            f"CooperBench worktree must be a direct child of "
            f"{AGENT_WORKSPACE_ROOT}, got {path}"
        )

    shutil.rmtree(path, ignore_errors=True)
    sh(f"git -C {q(repo)} worktree prune")

    rc, out, err = sh(
        f"git -C {q(repo)} worktree add -q --detach {q(path)} {q(commit)}",
        timeout=120,
    )

    if rc != 0:
        raise RuntimeError(f"worktree creation failed: {(out + err)[-1000:]}")

    return path


def remove_worktree(repo, path):
    sh(
        f"git -C {q(repo)} worktree remove --force {q(path)}",
        timeout=120,
    )
    shutil.rmtree(path, ignore_errors=True)


def git_head(tree):
    rc, out, err = sh(f"git -C {q(tree)} rev-parse HEAD")

    if rc != 0:
        raise RuntimeError((out + err)[-1000:])

    return out.strip()


def changed_files_since(tree, base):
    rc, out, err = sh(f"git -C {q(tree)} diff --name-only {q(base)}..HEAD")

    if rc != 0:
        raise RuntimeError((out + err)[-1000:])

    return sorted(line.strip() for line in out.splitlines() if line.strip())


def changed_regions_since(
    tree,
    base,
):
    """Return actual changed pre-image regions from zero-context Git diff.

    Planner declarations refer to the repository before the agent writes.
    Therefore old-side hunk coordinates are used.

    Insertions with old_count=0 use the insertion anchor as a one-line region.
    New files use the 0-0 sentinel.
    """
    rc, out, err = sh(
        f"git -C {q(tree)} diff --unified=0 --no-ext-diff {q(base)}..HEAD --",
        timeout=120,
    )

    if rc != 0:
        raise RuntimeError((out + err)[-1000:])

    records = []
    current_path = None
    new_file = False

    hunk_pattern = re.compile(
        r"^@@ "
        r"-(\d+)"
        r"(?:,(\d+))? "
        r"\+(\d+)"
        r"(?:,(\d+))? "
        r"@@"
    )

    for line in out.splitlines():
        if line.startswith("--- "):
            old_path = line[4:].strip()

            new_file = old_path == "/dev/null"

            continue

        if line.startswith("+++ "):
            new_path = line[4:].strip()

            if new_path == "/dev/null":
                current_path = None
            elif new_path.startswith("b/"):
                current_path = new_path[2:]
            else:
                current_path = new_path

            continue

        match = hunk_pattern.match(line)

        if not match or not current_path:
            continue

        old_start = int(match.group(1))

        old_count = int(match.group(2) or 1)

        if new_file:
            line_start = 0
            line_end = 0
        else:
            line_start = old_start

            line_end = (
                old_start
                + max(
                    old_count,
                    1,
                )
                - 1
            )

        records.append(
            {
                "path": (current_path),
                "line_start": (line_start),
                "line_end": (line_end),
                "coordinate_space": ("preimage"),
            }
        )

    return records


def commit_changes(tree, message):
    rc, out, err = sh(f"git -C {q(tree)} add -A")

    if rc != 0:
        raise RuntimeError((out + err)[-1000:])

    rc, out, err = sh(f"git -C {q(tree)} diff --cached --quiet")

    if rc == 0:
        return False

    rc, out, err = sh(
        f"git -C {q(tree)} commit -qm {q(message)}",
        timeout=120,
    )

    if rc != 0:
        raise RuntimeError(f"commit failed: {(out + err)[-1000:]}")

    return True


def _safe_relative_path(tree, relative):
    relative_path = Path(str(relative))

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"Unsafe repository path: {relative!r}")

    root = Path(tree).resolve()
    target = (root / relative_path).resolve()

    if target != root and root not in target.parents:
        raise ValueError(f"Path escapes worktree: {relative!r}")

    return target


def _syntax_error_message(relative, source, exc):
    lines = source.splitlines()
    line_number = exc.lineno or 1

    left = max(0, line_number - 5)
    right = min(len(lines), line_number + 4)

    context = "\n".join(
        f"{index + 1:6d} | {lines[index]}" for index in range(left, right)
    )

    return (
        f"{relative}: {exc.__class__.__name__}: "
        f"{exc.msg} at line {line_number}\n"
        f"{context}"
    )


def validate_python_source(relative, source):
    if not str(relative).endswith(".py"):
        return

    try:
        ast.parse(
            source,
            filename=str(relative),
        )

    except SyntaxError as exc:
        raise ValueError(
            "Python syntax validation failed before tests:\n\n"
            + _syntax_error_message(
                relative,
                source,
                exc,
            )
        ) from exc


def run_official_feature_test(
    tree,
    task_dir,
    feature_dir,
    *,
    feature_patch=None,
):
    """Execute the task-local CooperBench test runner."""
    runner = task_dir / "run_tests.sh"
    tests_patch = feature_dir / "tests.patch"

    if not runner.exists():
        return False, f"Missing CooperBench runner: {runner}"

    if not tests_patch.exists():
        return False, f"Missing CooperBench tests.patch: {tests_patch}"

    command = [
        "bash",
        str(runner.resolve()),
        str(Path(tree).resolve()),
        str(tests_patch.resolve()),
    ]

    if feature_patch is not None:
        command.append(str(Path(feature_patch).resolve()))

    runner_env = os.environ.copy()

    inherited_uv_system_python = runner_env.pop(
        "UV_SYSTEM_PYTHON",
        None,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=OFFICIAL_TEST_TIMEOUT_SECONDS,
            env=runner_env,
        )

        rc = completed.returncode
        out = completed.stdout
        err = completed.stderr

    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""

        combined = (
            "exit_code=TIMEOUT\n"
            f"UV_SYSTEM_PYTHON_removed={inherited_uv_system_python!r}\n"
            f"--- stdout ---\n{out}\n"
            f"--- stderr ---\n{err}"
        )

        return False, combined[-MAX_TEST_LOG_CHARS:]

    combined = (
        f"exit_code={rc}\n"
        f"UV_SYSTEM_PYTHON_removed={inherited_uv_system_python!r}\n"
        f"--- stdout ---\n{out}\n"
        f"--- stderr ---\n{err}"
    )

    return rc == 0, combined[-MAX_TEST_LOG_CHARS:]


TOOL_AGENT_SYSTEM = r"""You are an autonomous coding agent working inside a Git repository.

Work iteratively:
inspect → reason → edit → receive automatic test feedback → repair → finish.

When native repository tools are available:
- call EXACTLY ONE tool per turn;
- do not describe the call in prose;
- do not emit XML or pseudo-tool syntax.

If the transport falls back to text JSON mode, return EXACTLY ONE JSON object
matching one of the documented fallback actions.

Rules:
- Prefer read_file/search before editing code you have not inspected.
- Prefer replace_text for focused edits.
- `replace_text.old` must match exact current repository text exactly once.
- Never invent tool results.
- Python edits are syntax-checked before they are committed.
- A failed tool call does not modify the repository.
- After a successful mutation, the controller normally runs the official feature test automatically.
- Use that test output to repair the current implementation.
- `finish` is blocked unless the current HEAD has a passing official test.
- Do not modify unrelated files.
"""


REPOSITORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a numbered line range from a repository file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                },
                "required": [
                    "path",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search repository text for an exact substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                    },
                    "path": {
                        "type": "string",
                    },
                    "glob": {
                        "type": "string",
                    },
                },
                "required": [
                    "query",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": "Replace one exact existing text snippet atomically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                    "old": {
                        "type": "string",
                    },
                    "new": {
                        "type": "string",
                    },
                },
                "required": [
                    "path",
                    "old",
                    "new",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite one existing repository file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                    "content": {
                        "type": "string",
                    },
                },
                "required": [
                    "path",
                    "content",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create one new repository file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                    "content": {
                        "type": "string",
                    },
                },
                "required": [
                    "path",
                    "content",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete one existing repository file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                },
                "required": [
                    "path",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Inspect cumulative Git diff for this agent.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Inspect current HEAD and changed files.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run this feature's official CooperBench test.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish only after the current HEAD has a passing feature test.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
]


ALLOWED_AGENT_TOOLS = {
    "read_file",
    "search",
    "replace_text",
    "write_file",
    "create_file",
    "delete_file",
    "git_diff",
    "git_status",
    "run_tests",
    "finish",
}

READ_ONLY_BATCH_TOOLS = {
    "read_file",
    "search",
    "git_diff",
    "git_status",
}

MUTATING_AGENT_TOOLS = {
    "replace_text",
    "write_file",
    "create_file",
    "delete_file",
}


TOOL_NAME_ALIASES = {
    "grep": "search",
    "search_file": "search",
    "search_files": "search",
    "find": "search",
    "find_text": "search",
    "read": "read_file",
    "cat": "read_file",
    "open_file": "read_file",
    "replace": "replace_text",
    "edit": "replace_text",
    "replace_in_file": "replace_text",
    "write": "write_file",
    "overwrite_file": "write_file",
    "create": "create_file",
    "delete": "delete_file",
    "remove_file": "delete_file",
    "diff": "git_diff",
    "status": "git_status",
    "test": "run_tests",
    "run_test": "run_tests",
    "done": "finish",
    "complete": "finish",
}


def _decode_maybe_json_object(value):
    if isinstance(
        value,
        dict,
    ):
        return dict(value)

    if isinstance(
        value,
        str,
    ):
        stripped = value.strip()

        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                decoded = json.loads(stripped)

                if isinstance(
                    decoded,
                    dict,
                ):
                    return decoded

            except Exception:
                pass

    return {}


def _normalize_line_range(
    normalized,
):
    for alias in [
        "range",
        "line_range",
        "lines",
    ]:
        value = normalized.pop(
            alias,
            None,
        )

        if (
            isinstance(
                value,
                (
                    list,
                    tuple,
                ),
            )
            and len(value) == 2
        ):
            normalized.setdefault(
                "start_line",
                value[0],
            )

            normalized.setdefault(
                "end_line",
                value[1],
            )

            continue

        if isinstance(
            value,
            str,
        ):
            match = re.fullmatch(
                r"\s*(\d+)\s*(?:-|:|\.\.)\s*(\d+)\s*",
                value,
            )

            if match:
                normalized.setdefault(
                    "start_line",
                    int(match.group(1)),
                )

                normalized.setdefault(
                    "end_line",
                    int(match.group(2)),
                )

    aliases = {
        "start": ("start_line"),
        "end": ("end_line"),
        "from_line": ("start_line"),
        "to_line": ("end_line"),
        "line_start": ("start_line"),
        "line_end": ("end_line"),
    }

    for source, target in aliases.items():
        if target not in normalized and source in normalized:
            normalized[target] = normalized.pop(source)


def normalize_agent_action(
    action,
):
    """Normalize provider-specific JSON tool-call dialects."""
    if not isinstance(
        action,
        dict,
    ):
        raise ValueError("Tool action must be a JSON object.")

    normalized = dict(action)

    # OpenAI-like textual function envelope:
    # {"type":"function","function":{"name":"read_file","parameters":{...}}}
    function_value = normalized.pop(
        "function",
        None,
    )

    if isinstance(
        function_value,
        dict,
    ):
        function_name = function_value.get("name")

        if not normalized.get("tool") and function_name:
            normalized["tool"] = function_name

        nested_parameters = {}

        for key in [
            "parameters",
            "arguments",
            "args",
        ]:
            candidate = function_value.get(key)

            decoded = _decode_maybe_json_object(candidate)

            if decoded:
                nested_parameters.update(decoded)

        for key, value in nested_parameters.items():
            normalized.setdefault(
                key,
                value,
            )

    elif isinstance(
        function_value,
        str,
    ):
        if not normalized.get("tool"):
            normalized["tool"] = function_value

    # Additional provider envelopes occasionally emitted in JSON mode.
    # Examples:
    # {"type":"read_file", ...}
    # {"function_call":{"name":"read_file","arguments":{...}}}
    for envelope_key in [
        "function_call",
        "tool_call",
        "call",
    ]:
        envelope = normalized.pop(
            envelope_key,
            None,
        )

        if not isinstance(
            envelope,
            dict,
        ):
            continue

        if not normalized.get("tool") and isinstance(
            envelope.get("name"),
            str,
        ):
            normalized["tool"] = envelope["name"]

        for key in [
            "arguments",
            "parameters",
            "args",
            "input",
        ]:
            decoded = _decode_maybe_json_object(envelope.get(key))

            for nested_key, nested_value in decoded.items():
                normalized.setdefault(
                    nested_key,
                    nested_value,
                )

    if (
        not normalized.get("tool")
        and isinstance(
            normalized.get("type"),
            str,
        )
        and normalized.get("type")
        not in {
            "function",
            "tool_call",
            "function_call",
            "object",
        }
    ):
        normalized["tool"] = normalized["type"]

    # Other common textual wrappers.
    if not normalized.get("tool") and isinstance(
        normalized.get("action"),
        str,
    ):
        normalized["tool"] = normalized.pop("action")

    if not normalized.get("tool") and isinstance(
        normalized.get("tool_name"),
        str,
    ):
        normalized["tool"] = normalized.pop("tool_name")

    if not normalized.get("tool") and isinstance(
        normalized.get("name"),
        str,
    ):
        normalized["tool"] = normalized.pop("name")

    # Merge top-level argument wrappers after extracting the tool name.
    for wrapper in [
        "arguments",
        "parameters",
        "params",
        "input",
    ]:
        wrapped = normalized.pop(
            wrapper,
            None,
        )

        decoded = _decode_maybe_json_object(wrapped)

        for key, value in decoded.items():
            normalized.setdefault(
                key,
                value,
            )

    tool = normalized.get("tool")

    if isinstance(
        tool,
        str,
    ):
        tool = tool.strip()

        tool = {
            "edit_text": "replace_text",
            "str_replace_editor": "replace_text",
            "apply_edit": "replace_text",
            **TOOL_NAME_ALIASES,
        }.get(
            tool,
            tool,
        )

        normalized["tool"] = tool

    # Generic path aliases.
    if "path" not in normalized:
        for alias in [
            "file",
            "filename",
            "file_path",
        ]:
            if alias in normalized:
                normalized["path"] = normalized.pop(alias)
                break

    _normalize_line_range(normalized)

    # Tool-specific argument aliases.
    if tool == "search":
        if "query" not in normalized:
            for alias in [
                "pattern",
                "needle",
                "search",
                "text",
            ]:
                if alias in normalized:
                    normalized["query"] = normalized.pop(alias)
                    break

    elif tool == "replace_text":
        if "old" not in normalized:
            for alias in [
                "old_text",
                "old_content",
                "find",
                "search_text",
            ]:
                if alias in normalized:
                    normalized["old"] = normalized.pop(alias)
                    break

        if "new" not in normalized:
            for alias in [
                "new_text",
                "new_content",
                "replacement",
                "replace_with",
            ]:
                if alias in normalized:
                    normalized["new"] = normalized.pop(alias)
                    break

    elif tool in {
        "write_file",
        "create_file",
    }:
        if "content" not in normalized:
            for alias in [
                "text",
                "body",
                "file_content",
            ]:
                if alias in normalized:
                    normalized["content"] = normalized.pop(alias)
                    break

    elif tool == "finish":
        if "summary" not in normalized and isinstance(
            normalized.get("message"),
            str,
        ):
            normalized["summary"] = normalized.pop("message")

    # Provider envelope metadata is not part of the internal tool action.
    normalized.pop(
        "type",
        None,
    )

    if tool not in (ALLOWED_AGENT_TOOLS):
        raise ValueError(
            f"Unknown tool {tool!r}. Allowed: {sorted(ALLOWED_AGENT_TOOLS)}"
        )

    return normalized


def parse_native_tool_actions(result):
    """Parse one native call or a safe batch of read-only native calls."""
    tool_calls = result.get("tool_calls") or []
    if not tool_calls:
        raise ValueError("No native tool calls were returned.")

    parsed = []
    for tool_call in tool_calls:
        function = tool_call.get("function", {}) or {}
        name = function.get("name")
        arguments = function.get("arguments", "{}")

        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")

        if not isinstance(arguments, dict):
            raise ValueError("Native tool arguments must decode to an object.")

        action = normalize_agent_action(
            {
                "tool": name,
                **arguments,
            }
        )

        parsed.append(
            {
                "action": action,
                "tool_call_id": tool_call.get("id"),
            }
        )

    if len(parsed) > 1:
        tools = {item["action"]["tool"] for item in parsed}
        unsafe = tools - READ_ONLY_BATCH_TOOLS
        if unsafe:
            raise ValueError(
                "Native multi-tool batches may contain only read-only tools. "
                f"Unsafe batch tools: {sorted(unsafe)}"
            )

    return parsed


def parse_agent_action(text):
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in agent response.")

    decoder = json.JSONDecoder()

    try:
        action, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON tool action: {exc}") from exc

    return normalize_agent_action(action)


def _truncate_tool_result(text):
    text = str(text)

    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text

    half = MAX_TOOL_RESULT_CHARS // 2

    return text[:half] + "\n\n... TOOL OUTPUT TRUNCATED ...\n\n" + text[-half:]


def _number_file_range(path, start_line, end_line):
    source = path.read_text(errors="replace")

    lines = source.splitlines()

    if not lines:
        return "<EMPTY FILE>"

    start = max(1, int(start_line))
    end = min(
        len(lines),
        int(end_line),
    )

    if start > end:
        raise ValueError(
            f"Invalid line range {start}-{end}; file has {len(lines)} lines."
        )

    if end - start + 1 > MAX_READ_LINES:
        end = start + MAX_READ_LINES - 1

    return "\n".join(
        f"{index + 1:6d} | {lines[index]}"
        for index in range(
            start - 1,
            end,
        )
    )


def _search_repository(tree, query, path="", glob=""):
    if not query:
        raise ValueError("search.query must not be empty.")

    root = (
        _safe_relative_path(
            tree,
            path,
        )
        if path
        else Path(tree).resolve()
    )

    if not root.exists():
        raise FileNotFoundError(f"Search path does not exist: {path!r}")

    matches = []
    candidates = [root] if root.is_file() else root.rglob("*")

    for candidate in candidates:
        if len(matches) >= MAX_SEARCH_RESULTS:
            break

        if not candidate.is_file():
            continue

        relative = candidate.relative_to(Path(tree).resolve())

        if ".git" in relative.parts:
            continue

        if glob and not candidate.match(glob):
            continue

        try:
            text = candidate.read_text(errors="replace")

        except Exception:
            continue

        for line_number, line in enumerate(
            text.splitlines(),
            start=1,
        ):
            if query in line:
                matches.append(f"{relative}:{line_number}: {line}")

                if len(matches) >= MAX_SEARCH_RESULTS:
                    break

    if not matches:
        return "No matches."

    return "\n".join(matches)


def _record_trace(trace, kind, **payload):
    trace.append(
        {
            "index": len(trace),
            "kind": kind,
            **payload,
        }
    )


def _run_agent_test(
    tree,
    task_dir,
    feature_dir,
    test_state,
    *,
    automatic,
):
    if test_state["test_runs"] >= MAX_AGENT_TEST_RUNS:
        return {
            "executed": False,
            "passed": None,
            "log": (
                "TEST SKIPPED: test budget exhausted "
                f"(MAX_AGENT_TEST_RUNS={MAX_AGENT_TEST_RUNS})."
            ),
        }

    passed, log = run_official_feature_test(
        tree,
        task_dir,
        feature_dir,
    )

    test_state["test_runs"] += 1

    if automatic:
        test_state["auto_test_runs"] += 1
    else:
        test_state["manual_test_runs"] += 1

    test_state["last_test_head"] = git_head(tree)
    test_state["last_test_pass"] = bool(passed)
    test_state["last_test_log"] = log

    if not passed:
        test_state["seen_failed_test"] = True

    return {
        "executed": True,
        "passed": bool(passed),
        "log": log,
    }


def _changed_preimage_regions_for_replace(
    old,
    new,
    absolute_start_line,
):
    """Return precise old-side line regions actually changed by replace_text.

    `old` may contain a large amount of unchanged context.  Claim Plane should
    authorize only the lines whose old-side content is replaced/deleted, plus a
    stable adjacent pre-image anchor for insertion-only hunks.
    """
    if old == new:
        return []

    diff_lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        n=0,
        lineterm="",
    )

    hunk_pattern = re.compile(
        r"^@@ "
        r"-(\d+)"
        r"(?:,(\d+))? "
        r"\+(\d+)"
        r"(?:,(\d+))? "
        r"@@"
    )

    regions = []

    for line in diff_lines:
        match = hunk_pattern.match(line)

        if not match:
            continue

        old_start = int(match.group(1))

        old_count = int(match.group(2) or 1)

        if old_count == 0:
            # Git represents insertion-only hunks with an old-side count of
            # zero.  Use the nearest concrete pre-image line as the mutation
            # anchor so region-aware admission remains enforceable.
            relative_anchor = max(
                old_start,
                1,
            )

            line_start = absolute_start_line + relative_anchor - 1

            line_end = line_start

        else:
            line_start = absolute_start_line + old_start - 1

            line_end = line_start + old_count - 1

        regions.append(
            (
                int(line_start),
                int(line_end),
            )
        )

    # Merge only overlapping/adjacent actual hunks.  Unchanged contextual gaps
    # remain outside the authorization request.
    regions.sort()
    merged = []

    for start, end in regions:
        if not merged or start > merged[-1][1] + 1:
            merged.append(
                [
                    start,
                    end,
                ]
            )
        else:
            merged[-1][1] = max(
                merged[-1][1],
                end,
            )

    return [
        (
            start,
            end,
        )
        for start, end in merged
    ]


def _merge_line_regions(
    regions,
):
    normalized = sorted(
        (
            int(start),
            int(end),
        )
        for start, end in regions
        if int(start) > 0 and int(end) >= int(start)
    )

    merged = []

    for start, end in normalized:
        if not merged or start > merged[-1][1] + 1:
            merged.append(
                [
                    start,
                    end,
                ]
            )
        else:
            merged[-1][1] = max(
                merged[-1][1],
                end,
            )

    return [
        (
            start,
            end,
        )
        for start, end in merged
    ]


def _map_current_regions_to_preimage_from_text(
    base_text,
    current_text,
    current_regions,
):
    """Map current-file line regions back to planner/base pre-image coordinates.

    Planner declarations are anchored to the repository revision used during
    planning.  During execution, earlier edits can shift later current-file line
    numbers.  This mapper keeps authorization checks in the planner's stable
    pre-image coordinate space.

    Equal blocks map line-for-line.  Inserted current-only blocks map to their
    nearest pre-image insertion anchor.  Unequal replacement blocks map
    conservatively to the full replaced pre-image span.
    """
    base_lines = base_text.splitlines()
    current_lines = current_text.splitlines()

    matcher = difflib.SequenceMatcher(
        a=base_lines,
        b=current_lines,
        autojunk=False,
    )

    opcodes = matcher.get_opcodes()
    mapped = []

    for raw_start, raw_end in current_regions:
        current_start = int(raw_start)
        current_end = int(raw_end)

        if current_start <= 0 or current_end < current_start:
            raise ValueError(
                f"Invalid current mutation region: {(current_start, current_end)!r}"
            )

        # Convert to zero-based half-open current coordinates.
        requested_start = current_start - 1
        requested_end = current_end

        region_mapped = []

        for (
            tag,
            base_start,
            base_end,
            current_block_start,
            current_block_end,
        ) in opcodes:
            overlap_start = max(
                requested_start,
                current_block_start,
            )

            overlap_end = min(
                requested_end,
                current_block_end,
            )

            if overlap_start >= overlap_end:
                continue

            if tag == "equal":
                mapped_start = base_start + (overlap_start - current_block_start) + 1

                mapped_end = base_start + (overlap_end - current_block_start)

                region_mapped.append(
                    (
                        mapped_start,
                        mapped_end,
                    )
                )

                continue

            if tag == "insert":
                # SequenceMatcher uses an empty base interval for a current-only
                # insertion.  Match Git's old-side insertion anchor semantics:
                # insertion before base index N is anchored after pre-image
                # line N.  At file start, line 1 is the conservative anchor.
                anchor = max(
                    1,
                    base_start,
                )

                region_mapped.append(
                    (
                        anchor,
                        anchor,
                    )
                )

                continue

            if tag == "replace":
                base_count = base_end - base_start

                current_count = current_block_end - current_block_start

                if base_count > 0 and base_count == current_count:
                    mapped_start = (
                        base_start + (overlap_start - current_block_start) + 1
                    )

                    mapped_end = base_start + (overlap_end - current_block_start)

                    region_mapped.append(
                        (
                            mapped_start,
                            mapped_end,
                        )
                    )

                elif base_count > 0:
                    # The exact one-to-one correspondence inside a replacement
                    # with changed line cardinality is ambiguous.  Authorize
                    # conservatively against the full pre-image replacement
                    # span instead of inventing unstable current coordinates.
                    region_mapped.append(
                        (
                            base_start + 1,
                            base_end,
                        )
                    )

                else:
                    anchor = max(
                        1,
                        base_start,
                    )

                    region_mapped.append(
                        (
                            anchor,
                            anchor,
                        )
                    )

                continue

            # `delete` has an empty current interval and therefore cannot
            # overlap a concrete current mutation region.

        if not region_mapped:
            raise ValueError(
                "Could not map current mutation region "
                f"{(current_start, current_end)!r} "
                "to planner pre-image coordinates."
            )

        mapped.extend(region_mapped)

    return _merge_line_regions(mapped)


def _read_git_file_at_revision(
    tree,
    revision,
    relative_path,
):
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(tree),
            "show",
            f"{revision}:{relative_path}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if completed.returncode != 0:
        return None

    return completed.stdout.decode(
        "utf-8",
        errors="replace",
    )


def _map_current_regions_to_scope_preimage(
    tree,
    scope_base_commit,
    relative_path,
    current_regions,
    *,
    current_text=None,
):
    """Translate runtime current-file regions to the planner's base revision."""
    if not current_regions:
        return []

    if not scope_base_commit:
        return _merge_line_regions(current_regions)

    base_text = _read_git_file_at_revision(
        tree,
        scope_base_commit,
        relative_path,
    )

    # A file that did not exist in the planner/base revision has no meaningful
    # line-level pre-image.  File-wide/new-file authority is checked by the
    # normal Claim Plane resource capability.
    if base_text is None:
        return _merge_line_regions(current_regions)

    if current_text is None:
        current_path = _safe_relative_path(
            tree,
            relative_path,
        )

        current_text = current_path.read_text(
            errors="replace",
        )

    return _map_current_regions_to_preimage_from_text(
        base_text,
        current_text,
        current_regions,
    )


def _guard_mutation(
    mutation_guard,
    path,
    access,
    region=None,
):
    if mutation_guard is not None:
        mutation_guard(
            path,
            access,
            region,
        )


def execute_agent_tool(
    tree,
    task_dir,
    feature_dir,
    start_head,
    action,
    *,
    agent_step,
    test_state,
    mutation_guard=None,
    scope_base_commit=None,
):
    """Execute one model-selected tool call.

    Mutating tools are atomic and committed immediately.
    `finish` is accepted only when the current HEAD has already passed.
    """
    tool = action["tool"]

    if tool == "read_file":
        path = _safe_relative_path(
            tree,
            action.get("path", ""),
        )

        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {action.get('path')!r}")

        start_line = int(
            action.get(
                "start_line",
                1,
            )
        )

        end_line = int(
            action.get(
                "end_line",
                start_line + MAX_READ_LINES - 1,
            )
        )

        return {
            "ok": True,
            "mutated": False,
            "finished": False,
            "finish_blocked": False,
            "output": (
                f"FILE {action['path']}\n"
                + _number_file_range(
                    path,
                    start_line,
                    end_line,
                )
            ),
        }

    if tool == "search":
        return {
            "ok": True,
            "mutated": False,
            "finished": False,
            "finish_blocked": False,
            "output": _search_repository(
                tree,
                str(
                    action.get(
                        "query",
                        "",
                    )
                ),
                path=str(
                    action.get(
                        "path",
                        "",
                    )
                ),
                glob=str(
                    action.get(
                        "glob",
                        "",
                    )
                ),
            ),
        }

    if tool == "replace_text":
        relative = action.get("path", "")
        path = _safe_relative_path(
            tree,
            relative,
        )

        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative!r}")

        old = action.get("old")
        new = action.get("new")

        if not isinstance(old, str) or not old:
            raise ValueError("replace_text.old must be a non-empty string.")

        if not isinstance(new, str):
            raise ValueError("replace_text.new must be a string.")

        original = path.read_text(errors="replace")

        count = original.count(old)

        if count != 1:
            raise ValueError(
                f"replace_text expected exactly one match, found {count}. "
                "Read the current file and use a more precise exact snippet."
            )

        match_offset = original.index(old)
        prefix = original[:match_offset]
        match_start_line = prefix.count("\n") + 1

        mutation_regions = _changed_preimage_regions_for_replace(
            old,
            new,
            match_start_line,
        )

        updated = original.replace(
            old,
            new,
            1,
        )

        scope_regions = _map_current_regions_to_scope_preimage(
            tree,
            (scope_base_commit or start_head),
            relative,
            mutation_regions,
            current_text=original,
        )

        for scope_region in scope_regions:
            _guard_mutation(
                mutation_guard,
                relative,
                AccessMode.WRITE,
                scope_region,
            )

        validate_python_source(
            relative,
            updated,
        )

        path.write_text(
            updated,
            encoding="utf-8",
        )

        committed = commit_changes(
            tree,
            f"agent tool step {agent_step}: replace {relative}",
        )

        return {
            "ok": True,
            "mutated": committed,
            "finished": False,
            "finish_blocked": False,
            "output": (f"Replaced exact text in {relative}."),
        }

    if tool == "write_file":
        relative = action.get("path", "")
        path = _safe_relative_path(
            tree,
            relative,
        )

        if not path.is_file():
            raise FileNotFoundError(
                f"write_file requires an existing file: {relative!r}"
            )

        content = action.get("content")

        if not isinstance(content, str):
            raise ValueError("write_file.content must be a string.")

        if len(content) > MAX_EXISTING_WRITE_FILE_CHARS:
            raise ValueError(
                "write_file refused a large whole-file rewrite "
                f"({len(content)} chars > "
                f"{MAX_EXISTING_WRITE_FILE_CHARS}). "
                "Use replace_text for focused edits."
            )

        _guard_mutation(
            mutation_guard,
            relative,
            AccessMode.WRITE,
        )

        validate_python_source(
            relative,
            content,
        )

        path.write_text(
            content,
            encoding="utf-8",
        )

        committed = commit_changes(
            tree,
            f"agent tool step {agent_step}: write {relative}",
        )

        return {
            "ok": True,
            "mutated": committed,
            "finished": False,
            "finish_blocked": False,
            "output": (f"Wrote {relative}."),
        }

    if tool == "create_file":
        relative = action.get("path", "")
        path = _safe_relative_path(
            tree,
            relative,
        )

        if path.exists():
            raise FileExistsError(f"File already exists: {relative!r}")

        content = action.get("content")

        if not isinstance(content, str):
            raise ValueError("create_file.content must be a string.")

        validate_python_source(
            relative,
            content,
        )

        _guard_mutation(
            mutation_guard,
            relative,
            AccessMode.WRITE,
        )

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        path.write_text(
            content,
            encoding="utf-8",
        )

        committed = commit_changes(
            tree,
            f"agent tool step {agent_step}: create {relative}",
        )

        return {
            "ok": True,
            "mutated": committed,
            "finished": False,
            "finish_blocked": False,
            "output": (f"Created {relative}."),
        }

    if tool == "delete_file":
        relative = action.get("path", "")
        path = _safe_relative_path(
            tree,
            relative,
        )

        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative!r}")

        _guard_mutation(
            mutation_guard,
            relative,
            AccessMode.DELETE,
        )

        path.unlink()

        committed = commit_changes(
            tree,
            f"agent tool step {agent_step}: delete {relative}",
        )

        return {
            "ok": True,
            "mutated": committed,
            "finished": False,
            "finish_blocked": False,
            "output": (f"Deleted {relative}."),
        }

    if tool == "git_diff":
        rc, stat, stat_err = sh(f"git -C {q(tree)} diff --stat {q(start_head)}..HEAD")

        if rc != 0:
            raise RuntimeError((stat + stat_err)[-1000:])

        rc, diff, diff_err = sh(
            f"git -C {q(tree)} diff {q(start_head)}..HEAD",
            timeout=120,
        )

        if rc != 0:
            raise RuntimeError((diff + diff_err)[-1000:])

        if len(diff) > MAX_DIFF_CHARS:
            half = MAX_DIFF_CHARS // 2
            diff = diff[:half] + "\n\n... DIFF TRUNCATED ...\n\n" + diff[-half:]

        return {
            "ok": True,
            "mutated": False,
            "finished": False,
            "finish_blocked": False,
            "output": (f"DIFF STAT\n{stat}\n\nDIFF\n{diff or '<NO DIFF>'}"),
        }

    if tool == "git_status":
        return {
            "ok": True,
            "mutated": False,
            "finished": False,
            "finish_blocked": False,
            "output": json.dumps(
                {
                    "changed_files": changed_files_since(
                        tree,
                        start_head,
                    ),
                },
                indent=2,
            ),
        }

    if tool == "run_tests":
        outcome = _run_agent_test(
            tree,
            task_dir,
            feature_dir,
            test_state,
            automatic=False,
        )

        return {
            "ok": True,
            "mutated": False,
            "finished": False,
            "finish_blocked": False,
            "test_pass": outcome["passed"],
            "output": (
                f"OFFICIAL FEATURE TEST "
                f"EXECUTED={outcome['executed']} "
                f"PASS={outcome['passed']}\n\n"
                f"{outcome['log']}"
            ),
        }

    if tool == "finish":
        current_head = git_head(tree)

        can_finish = bool(
            test_state["last_test_head"] == current_head
            and test_state["last_test_pass"] is True
        )

        if not can_finish:
            return {
                "ok": False,
                "mutated": False,
                "finished": False,
                "finish_blocked": True,
                "output": (
                    "FINISH BLOCKED: the current HEAD has not passed the "
                    "official feature test. Continue implementing or run tests."
                ),
            }

        return {
            "ok": True,
            "mutated": False,
            "finished": True,
            "finish_blocked": False,
            "output": (
                "Finish accepted on a passing current HEAD. "
                f"Summary: {action.get('summary', '')}"
            ),
        }

    raise ValueError(f"Unhandled tool: {tool}")


def sanitize_agent_visible_text(
    text,
    *,
    tree,
    start_head,
):
    """Remove irrelevant per-worktree identifiers from model-visible feedback."""
    text = str(text)

    replacements = [
        (
            str(Path(tree).resolve()),
            "<REPO>",
        ),
        (
            str(AGENT_WORKSPACE_ROOT.resolve()),
            "<WORKSPACE>",
        ),
        (
            str(start_head),
            "<BASE_REVISION>",
        ),
    ]

    try:
        current_head = git_head(tree)

        replacements.append(
            (
                str(current_head),
                "<CURRENT_REVISION>",
            )
        )

    except Exception:
        pass

    for raw, replacement in replacements:
        if raw:
            text = text.replace(
                raw,
                replacement,
            )

    return text


def run_live_agent(
    tree,
    task_dir,
    feature_dir,
    *,
    seed,
    message,
    trace_id,
    mutation_guard=None,
    scope_base_commit=None,
):
    """Run a bounded native-tool-first coding agent against one feature."""
    start_head = git_head(tree)

    feature = (feature_dir / "feature.md").read_text(errors="replace")[:14000]

    initial_context = read_context(
        tree,
        feature_dir,
    )

    trace = {
        "trace_id": trace_id,
        "message": message,
        "seed": seed,
        "start_head": start_head,
        "events": [],
    }

    events = trace["events"]

    messages = [
        {
            "role": "system",
            "content": TOOL_AGENT_SYSTEM,
        },
        {
            "role": "user",
            "content": (
                "Implement the following feature in the current repository.\n\n"
                f"FEATURE:\n{feature}\n\n"
                "INITIAL LOCALIZED SOURCE CONTEXT:\n"
                f"{initial_context}\n\n"
                "Choose your first repository tool now."
            ),
        },
    ]

    logical_cost = 0.0
    logical_latency = 0.0
    pre_failure_cost = 0.0
    post_failure_cost = 0.0

    steps_used = 0
    tool_errors = 0
    protocol_errors = 0
    native_tool_actions = 0
    native_tool_batches = 0
    json_fallback_actions = 0
    accepted_llm_responses = 0
    llm_cache_hits = 0
    exploration_nudges = 0
    consecutive_exploration_steps = 0
    last_exploration_nudge_at = 0
    finish_blocked_count = 0
    finish_reason = "max_steps"

    test_state = {
        "test_runs": 0,
        "auto_test_runs": 0,
        "manual_test_runs": 0,
        "last_test_head": None,
        "last_test_pass": None,
        "last_test_log": "",
        "seen_failed_test": False,
    }

    def persist_trace(status, error=None):
        trace.update(
            {
                "status": status,
                "error": error,
                "end_head": git_head(tree),
                "logical_cost": logical_cost,
                "logical_latency": logical_latency,
                "steps_used": steps_used,
                "tool_errors": tool_errors,
                "protocol_errors": protocol_errors,
                "native_tool_actions": native_tool_actions,
                "native_tool_batches": native_tool_batches,
                "json_fallback_actions": json_fallback_actions,
                "accepted_llm_responses": accepted_llm_responses,
                "llm_cache_hits": llm_cache_hits,
                "exploration_nudges": exploration_nudges,
                "finish_blocked_count": finish_blocked_count,
                "test_runs": test_state["test_runs"],
                "auto_test_runs": test_state["auto_test_runs"],
                "manual_test_runs": test_state["manual_test_runs"],
            }
        )
        AGENT_TRACE_LOGS.append(trace)

    for step_index in range(MAX_AGENT_STEPS):
        steps_used = step_index + 1
        parsed_calls = None
        transport = None
        accepted_result = None
        last_action_error = ""

        for action_attempt in range(ACTION_RETRIES_PER_STEP):
            use_native = bool(
                USE_NATIVE_TOOL_CALLS and action_attempt < NATIVE_TOOL_ATTEMPTS_PER_STEP
            )

            use_json_mode = bool(not use_native and USE_JSON_MODE_FALLBACK)

            phase = (
                "tool_action_native"
                if use_native
                else (
                    "tool_action_json_fallback"
                    if use_json_mode
                    else "tool_action_text_fallback"
                )
            )

            try:
                result = llm(
                    messages,
                    model=CODER_MODEL,
                    seed=(seed + step_index * 100 + action_attempt),
                    max_tokens=CODER_MAX_TOKENS,
                    role="coder",
                    phase=phase,
                    tools=(REPOSITORY_TOOLS if use_native else None),
                    tool_choice=("required" if use_native else None),
                    parallel_tool_calls=(False if use_native else None),
                    response_format=(
                        {"type": "json_object"} if use_json_mode else None
                    ),
                )
            except Exception as exc:
                last_action_error = str(exc)
                _record_trace(
                    events,
                    "provider_error",
                    step=steps_used,
                    attempt=action_attempt + 1,
                    transport=phase,
                    error=last_action_error,
                )
                continue

            logical_cost += result["cost"]
            logical_latency += result["latency_seconds"]
            accepted_llm_responses += 1
            llm_cache_hits += int(
                result.get(
                    "cached",
                    False,
                )
            )

            if test_state["seen_failed_test"]:
                post_failure_cost += result["cost"]
            else:
                pre_failure_cost += result["cost"]

            _record_trace(
                events,
                "model_response",
                step=steps_used,
                attempt=action_attempt + 1,
                transport=phase,
                raw_response=result.get(
                    "content",
                    "",
                ),
                native_tool_calls=result.get(
                    "tool_calls",
                    [],
                ),
                cost=result["cost"],
                latency_seconds=result["latency_seconds"],
                cached=result["cached"],
            )

            try:
                if use_native and result.get("tool_calls"):
                    parsed_calls = parse_native_tool_actions(result)
                    transport = "native"
                else:
                    parsed_calls = [
                        {
                            "action": parse_agent_action(
                                result.get(
                                    "content",
                                    "",
                                )
                            ),
                            "tool_call_id": None,
                        }
                    ]
                    transport = "json_fallback" if use_json_mode else "text_fallback"

                accepted_result = result

                _record_trace(
                    events,
                    "parsed_actions",
                    step=steps_used,
                    attempt=action_attempt + 1,
                    transport=transport,
                    actions=[item["action"] for item in parsed_calls],
                )
                break

            except Exception as exc:
                protocol_errors += 1
                last_action_error = str(exc)

                _record_trace(
                    events,
                    "protocol_error",
                    step=steps_used,
                    attempt=action_attempt + 1,
                    transport=phase,
                    raw_response=result.get(
                        "content",
                        "",
                    ),
                    native_tool_calls=result.get(
                        "tool_calls",
                        [],
                    ),
                    error=last_action_error,
                )

                if not result.get("tool_calls") and result.get("content"):
                    messages.append(
                        {
                            "role": "assistant",
                            "content": result["content"],
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "PROTOCOL ERROR: "
                                f"{last_action_error}\n"
                                "Return exactly one JSON tool action. "
                                "Accepted envelopes include `tool`, `action`, "
                                "`name` + `arguments`, or a function envelope. "
                                "Use focused replace_text edits instead of large "
                                "whole-file write_file responses."
                            ),
                        }
                    )

        if not parsed_calls:
            error = (
                "Coding agent failed to produce a valid tool action "
                f"after {ACTION_RETRIES_PER_STEP} transport attempts. "
                f"Last error: {last_action_error}"
            )
            persist_trace(
                "protocol_failure",
                error,
            )
            raise AgentExecutionError(
                error,
                logical_cost=logical_cost,
                logical_latency=logical_latency,
                pre_failure_cost=pre_failure_cost,
                post_failure_cost=post_failure_cost,
                steps_used=steps_used,
                tool_errors=tool_errors,
                test_runs=test_state["test_runs"],
                trace=trace,
            )

        if transport == "native":
            native_tool_actions += len(parsed_calls)
            if len(parsed_calls) > 1:
                native_tool_batches += 1
            messages.append(accepted_result["assistant_message"])
        else:
            json_fallback_actions += 1
            messages.append(
                {
                    "role": "assistant",
                    "content": accepted_result.get(
                        "content",
                        "",
                    ),
                }
            )

        tool_results = []
        any_mutation = False
        any_finished = False
        turn_tools = []

        for call_index, call in enumerate(parsed_calls):
            action = call["action"]
            turn_tools.append(action["tool"])

            try:
                tool_result = execute_agent_tool(
                    tree,
                    task_dir,
                    feature_dir,
                    start_head,
                    action,
                    agent_step=steps_used,
                    test_state=test_state,
                    mutation_guard=mutation_guard,
                    scope_base_commit=(scope_base_commit or start_head),
                )

            except DynamicScopeBlocked as exc:
                partial = {
                    "logical_cost": logical_cost,
                    "logical_latency": logical_latency,
                    "pre_failure_cost": pre_failure_cost,
                    "post_failure_cost": post_failure_cost,
                    "steps_used": steps_used,
                    "tool_errors": tool_errors,
                    "protocol_errors": protocol_errors,
                    "test_runs": test_state["test_runs"],
                    "auto_test_runs": test_state["auto_test_runs"],
                    "manual_test_runs": test_state["manual_test_runs"],
                    "accepted_llm_responses": accepted_llm_responses,
                    "llm_cache_hits": llm_cache_hits,
                    "exploration_nudges": exploration_nudges,
                    "finish_blocked_count": finish_blocked_count,
                    "written_files": changed_files_since(
                        tree,
                        start_head,
                    ),
                    "written_regions": changed_regions_since(
                        tree,
                        start_head,
                    ),
                }

                exc.attach_partial(partial)

                _record_trace(
                    events,
                    "scope_blocked",
                    step=steps_used,
                    call_index=call_index,
                    action=action,
                    block_type=exc.block_type,
                    path=exc.path,
                    access=exc.access.value,
                    decision_kind=exc.decision_kind,
                    reason=exc.reason,
                )

                persist_trace(
                    "scope_blocked",
                    str(exc),
                )

                raise

            except Exception as exc:
                tool_errors += 1
                tool_result = {
                    "ok": False,
                    "mutated": False,
                    "finished": False,
                    "finish_blocked": False,
                    "output": (f"TOOL ERROR: {exc}"),
                }
                _record_trace(
                    events,
                    "tool_error",
                    step=steps_used,
                    call_index=call_index,
                    action=action,
                    error=str(exc),
                )

            _record_trace(
                events,
                "tool_result",
                step=steps_used,
                call_index=call_index,
                action=action,
                transport=transport,
                ok=tool_result["ok"],
                mutated=tool_result["mutated"],
                finished=tool_result["finished"],
                finish_blocked=tool_result.get(
                    "finish_blocked",
                    False,
                ),
                output=tool_result["output"],
            )

            visible_tool_output = sanitize_agent_visible_text(
                tool_result["output"],
                tree=tree,
                start_head=start_head,
            )

            feedback = "TOOL RESULT:\n" + _truncate_tool_result(visible_tool_output)

            if tool_result.get("finish_blocked"):
                finish_blocked_count += 1
                _record_trace(
                    events,
                    "finish_blocked",
                    step=steps_used,
                    call_index=call_index,
                    head=git_head(tree),
                )

            if tool_result.get("mutated"):
                any_mutation = True

                if AUTO_TEST_AFTER_MUTATION:
                    outcome = _run_agent_test(
                        tree,
                        task_dir,
                        feature_dir,
                        test_state,
                        automatic=True,
                    )

                    auto_test_output = (
                        "\n\nCONTROLLER AUTOMATIC TEST:\n"
                        f"EXECUTED={outcome['executed']} "
                        f"PASS={outcome['passed']}\n\n"
                        f"{outcome['log']}"
                    )

                    visible_auto_test_output = sanitize_agent_visible_text(
                        auto_test_output,
                        tree=tree,
                        start_head=start_head,
                    )

                    feedback += _truncate_tool_result(visible_auto_test_output)

                    _record_trace(
                        events,
                        "automatic_test",
                        step=steps_used,
                        call_index=call_index,
                        executed=outcome["executed"],
                        passed=outcome["passed"],
                        head=git_head(tree),
                        log=outcome["log"],
                    )

            if tool_result.get("finished"):
                any_finished = True

            tool_results.append(
                {
                    "call": call,
                    "feedback": feedback,
                }
            )

        if transport == "native":
            for item in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["call"]["tool_call_id"],
                        "content": item["feedback"],
                    }
                )
        else:
            combined_feedback = "\n\n".join(item["feedback"] for item in tool_results)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        combined_feedback + "\n\nChoose the next repository tool."
                    ),
                }
            )

        # Count exploration in model turns, not raw batched read calls.
        if turn_tools and all(tool in READ_ONLY_BATCH_TOOLS for tool in turn_tools):
            consecutive_exploration_steps += 1
        elif any_mutation:
            consecutive_exploration_steps = 0
            last_exploration_nudge_at = 0

        should_nudge = bool(
            consecutive_exploration_steps >= MAX_EXPLORATION_STEPS_BEFORE_EDIT
            and (
                last_exploration_nudge_at == 0
                or (
                    consecutive_exploration_steps - last_exploration_nudge_at
                    >= EXPLORATION_NUDGE_INTERVAL
                )
            )
        )

        if should_nudge:
            exploration_nudges += 1
            last_exploration_nudge_at = consecutive_exploration_steps
            nudge = (
                "IMPLEMENTATION NUDGE: You have used "
                f"{consecutive_exploration_steps} consecutive model turns "
                "only for repository exploration without a mutation. "
                "Use the context already gathered and attempt the smallest "
                "reasonable implementation now. Read more only when a "
                "specific missing fact blocks the edit."
            )
            messages.append(
                {
                    "role": "user",
                    "content": nudge,
                }
            )
            _record_trace(
                events,
                "exploration_nudge",
                step=steps_used,
                consecutive_exploration_steps=(consecutive_exploration_steps),
                message=nudge,
            )

        if tool_errors >= MAX_TOOL_ERRORS:
            finish_reason = "tool_error_budget_exhausted"
            break

        if any_finished:
            finish_reason = "agent_finish_after_passing_test"
            break

    final_head = git_head(tree)

    if (
        test_state["last_test_head"] == final_head
        and test_state["last_test_pass"] is not None
    ):
        feature_pass = bool(test_state["last_test_pass"])
        final_test_log = test_state["last_test_log"]
    else:
        feature_pass, final_test_log = run_official_feature_test(
            tree,
            task_dir,
            feature_dir,
        )
        test_state["test_runs"] += 1

    persist_trace(
        "completed",
        None,
    )

    return {
        "committed": (final_head != start_head),
        "head": final_head,
        "written_files": (
            changed_files_since(
                tree,
                start_head,
            )
        ),
        "written_regions": (
            changed_regions_since(
                tree,
                start_head,
            )
        ),
        "feature_pass": bool(feature_pass),
        "final_test_log": final_test_log,
        "logical_cost": logical_cost,
        "logical_latency": logical_latency,
        "pre_failure_cost": pre_failure_cost,
        "post_failure_cost": post_failure_cost,
        "steps_used": steps_used,
        "tool_errors": tool_errors,
        "protocol_errors": protocol_errors,
        "native_tool_actions": (native_tool_actions),
        "native_tool_batches": (native_tool_batches),
        "json_fallback_actions": (json_fallback_actions),
        "accepted_llm_responses": (accepted_llm_responses),
        "llm_cache_hits": (llm_cache_hits),
        "exploration_nudges": (exploration_nudges),
        "test_runs": (test_state["test_runs"]),
        "auto_test_runs": (test_state["auto_test_runs"]),
        "manual_test_runs": (test_state["manual_test_runs"]),
        "finish_blocked_count": (finish_blocked_count),
        "finish_reason": finish_reason,
    }
