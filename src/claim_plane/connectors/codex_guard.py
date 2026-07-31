"""Pre-mutation authorization for the project-local Codex connector.

The guard consumes Codex ``PreToolUse`` payloads and proves repository mutations
against the live session-bound ``ChangeIntent`` before the runtime executes them.
It deliberately emits no positive permission decision: an authorized tool call
continues through Codex's normal sandbox and approval flow.  A denied call receives
an explicit hook decision with model-visible guidance.

This module is intentionally conservative.  It understands the built-in apply-patch
mutation format, a small set of direct file-mutating shell commands, and a bounded set
of read-only shell commands.  Opaque shell execution and unknown hook-emitting tools
fail closed because their repository effects cannot be proven from the hook input.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.core import AccessMode, ChangeIntent, IntentOperation, ResourceKind

CODEX_GUARD_PROTOCOL = "claim-plane.codex-pre-mutation-guard.v1"

_WRITE_MODES = frozenset({AccessMode.WRITE, AccessMode.DOCUMENT, AccessMode.TEST})

_READ_ONLY_TOOLS = frozenset(
    {
        "read",
        "view",
        "grep",
        "glob",
        "list_dir",
        "view_image",
        "webfetch",
        "websearch",
        "web_fetch",
        "web_search",
        "askuserquestion",
        "ask_user",
        "todowrite",
        "update_todo",
        "update_plan",
        "plan",
        "goal",
        "tool_search",
        "tool_suggest",
    }
)

_APPLY_PATCH_TOOLS = frozenset(
    {
        "apply_patch",
        "applypatch",
        "edit",
    }
)

_SHELL_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "shell_command",
        "exec_command",
        "unified_exec",
        "powershell",
    }
)

# Shell commands in this set are accepted only after extra argument validation below.
_READ_ONLY_SHELL = frozenset(
    {
        "pwd",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "stat",
        "file",
        "find",
        "sed",
        "tree",
        "du",
        "basename",
        "dirname",
        "realpath",
        "echo",
        "printf",
        "test",
        "true",
        "false",
        "rg",
        "grep",
        "git",
    }
)

_SHELL_CONTROL_CHARS = re.compile(r"(?:\n|\r|&&|\|\||[;|`]|\$\(|\$\{|<|>)")
_PATCH_HEADER = re.compile(r"^\*\*\* (Add|Delete|Update) File: (.+?)\s*$")
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[/\\]")


def protected_control_path(path: str) -> bool:
    """Return whether a repository-relative path controls the connector boundary."""

    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = posixpath.normpath(normalized)
    return (
        normalized == ".claim-plane"
        or normalized.startswith(".claim-plane/")
        or normalized == ".git"
        or normalized.startswith(".git/")
        or normalized == ".codex"
        or normalized.startswith(".codex/")
    )


@dataclass(frozen=True, slots=True)
class MutationRequest:
    """One concrete repository mutation inferred from a Codex tool call."""

    access: AccessMode
    path: str
    target_path: str | None = None
    source: str = "tool"


@dataclass(frozen=True, slots=True)
class GuardEvaluation:
    """Deterministic result of classifying and authorizing one tool call."""

    allowed: bool
    mutating: bool
    tool_name: str
    classification: str
    reason_code: str
    reason: str
    mutations: tuple[MutationRequest, ...] = ()
    promotion: MutationRequest | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        result: list[str] = []
        for mutation in self.mutations:
            if mutation.path not in result:
                result.append(mutation.path)
            if mutation.target_path and mutation.target_path not in result:
                result.append(mutation.target_path)
        return tuple(result)


def _normalized_tool_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().casefold().replace("-", "_").replace(" ", "")


def _tool_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("tool_input")
    if value is None:
        value = payload.get("toolInput")
    return value if isinstance(value, Mapping) else {}


def _command_from_input(payload: Mapping[str, Any]) -> str | None:
    raw = _tool_input(payload).get("command")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = [str(item) for item in raw]
        return " ".join(shlex.quote(item) for item in items)
    return None


def _relative_path(root: Path, cwd: Path, raw: str) -> str:
    value = raw.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value or value == ".":
        raise ValueError("mutation path must not be empty")
    if value.startswith("/") or _WINDOWS_ABS.match(value):
        candidate = Path(raw).expanduser().resolve()
    else:
        candidate = (cwd / Path(raw)).resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("mutation path escapes the enrolled repository") from exc
    normalized = posixpath.normpath(relative)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("mutation path must name a repository file")
    return normalized


def _patch_mutations(
    root: Path, cwd: Path, command: str
) -> tuple[MutationRequest, ...]:
    current: MutationRequest | None = None
    mutations: list[MutationRequest] = []
    saw_begin = False
    saw_end = False

    for line in command.splitlines():
        stripped = line.rstrip("\n")
        if stripped == "*** Begin Patch":
            if saw_begin:
                raise ValueError("apply_patch input contains multiple begin markers")
            saw_begin = True
            continue
        if stripped == "*** End Patch":
            saw_end = True
            current = None
            continue

        header = _PATCH_HEADER.match(stripped)
        if header:
            if not saw_begin or saw_end:
                raise ValueError(
                    "apply_patch file operation is outside the patch envelope"
                )
            action, raw_path = header.groups()
            path = _relative_path(root, cwd, raw_path)
            access = {
                "Add": AccessMode.WRITE,
                "Update": AccessMode.WRITE,
                "Delete": AccessMode.DELETE,
            }[action]
            current = MutationRequest(access=access, path=path, source="apply_patch")
            mutations.append(current)
            continue

        move = _PATCH_MOVE.match(stripped)
        if move:
            if current is None or current.access is not AccessMode.WRITE:
                raise ValueError("apply_patch move must follow an update operation")
            target = _relative_path(root, cwd, move.group(1))
            replacement = MutationRequest(
                access=AccessMode.RENAME,
                path=current.path,
                target_path=target,
                source="apply_patch",
            )
            mutations[-1] = replacement
            current = replacement

    if not saw_begin or not saw_end:
        raise ValueError("apply_patch input is missing its patch envelope")
    if not mutations:
        raise ValueError("apply_patch input contains no file operations")
    return tuple(mutations)


def _has_shell_metacharacters(command: str) -> bool:
    return _SHELL_CONTROL_CHARS.search(command) is not None


def _git_read_only(argv: Sequence[str]) -> bool:
    if len(argv) < 2:
        return False
    subcommand = argv[1]
    safe = {
        "status",
        "diff",
        "log",
        "show",
        "grep",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "cat-file",
        "name-rev",
        "describe",
        "shortlog",
        "blame",
    }
    return subcommand in safe


def _rg_read_only(argv: Sequence[str]) -> bool:
    dangerous = {"--pre", "--pre-glob"}
    return not any(item in dangerous or item.startswith("--pre=") for item in argv[1:])


def _simple_read_only_shell(command: str) -> bool:
    if _has_shell_metacharacters(command):
        return False
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not argv:
        return True
    executable = posixpath.basename(argv[0])
    if executable not in _READ_ONLY_SHELL:
        return False
    if executable == "git":
        return _git_read_only(argv)
    if executable == "rg":
        return _rg_read_only(argv)
    if executable == "find":
        dangerous = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        return not any(item in dangerous for item in argv[1:])
    if executable == "sed":
        return not any(item == "-i" or item.startswith("-i") for item in argv[1:])
    return True


def _parse_simple_shell_mutation(
    root: Path, cwd: Path, command: str
) -> tuple[MutationRequest, ...] | None:
    """Parse a small, explicit file-mutating shell subset.

    ``None`` means the command is not in the supported mutation grammar.  Complex
    shell syntax is never guessed: the caller must deny it instead of broadening
    authority accidentally.
    """

    if _has_shell_metacharacters(command):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv:
        return ()
    executable = posixpath.basename(argv[0])

    if executable == "touch":
        paths = [item for item in argv[1:] if not item.startswith("-")]
        if not paths:
            return None
        return tuple(
            MutationRequest(
                AccessMode.WRITE, _relative_path(root, cwd, item), source="shell"
            )
            for item in paths
        )

    if executable == "rm":
        paths = [item for item in argv[1:] if not item.startswith("-")]
        if not paths:
            return None
        return tuple(
            MutationRequest(
                AccessMode.DELETE, _relative_path(root, cwd, item), source="shell"
            )
            for item in paths
        )

    if executable == "cp":
        positional = [item for item in argv[1:] if not item.startswith("-")]
        if len(positional) != 2:
            return None
        return (
            MutationRequest(
                AccessMode.WRITE,
                _relative_path(root, cwd, positional[1]),
                source="shell",
            ),
        )

    if executable == "mv":
        positional = [item for item in argv[1:] if not item.startswith("-")]
        if len(positional) != 2:
            return None
        return (
            MutationRequest(
                AccessMode.RENAME,
                _relative_path(root, cwd, positional[0]),
                target_path=_relative_path(root, cwd, positional[1]),
                source="shell",
            ),
        )

    if executable == "sed" and "-i" in argv[1:]:
        # POSIX and GNU sed differ around -i suffix parsing.  Accept only the
        # unambiguous no-suffix form and one final repository-relative path.
        if argv[-1].startswith("-") or argv[-1] == "-i":
            return None
        return (
            MutationRequest(
                AccessMode.WRITE,
                _relative_path(root, cwd, argv[-1]),
                source="shell",
            ),
        )

    return None


def _rename_target(operation: IntentOperation) -> str | None:
    for source in (operation.metadata, operation.resource.metadata):
        for key in ("rename_to", "target", "to"):
            value = source.get(key)
            if value:
                return str(value).replace("\\", "/").lstrip("./")
    return None


def _candidate_operations(
    intent: ChangeIntent, mutation: MutationRequest
) -> tuple[IntentOperation, ...]:
    modes: set[AccessMode]
    if mutation.access is AccessMode.WRITE:
        modes = set(_WRITE_MODES)
    else:
        modes = {mutation.access}
    return tuple(
        operation
        for operation in intent.operations
        if operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
        and operation.resource.covers_path(mutation.path)
        and operation.access in modes
    )


def _operation_authorizes(
    operation: IntentOperation, mutation: MutationRequest
) -> bool:
    # apply_patch and the direct shell grammar expose whole-file authority, not an
    # exact pre-image line interval.  A bounded declaration must therefore be
    # enforced by the region-aware broker rather than widened here.
    if operation.resource.region is not None:
        return False
    if mutation.access is AccessMode.RENAME:
        target = _rename_target(operation)
        return bool(target and target == mutation.target_path)
    return True


def _authorization_state(
    intent: ChangeIntent, mutation: MutationRequest
) -> tuple[str, tuple[IntentOperation, ...]]:
    candidates = tuple(
        operation
        for operation in _candidate_operations(intent, mutation)
        if _operation_authorizes(operation, mutation)
    )
    if any(operation.committed for operation in candidates):
        return "committed", candidates
    if any(operation.contingent for operation in candidates):
        return "contingent", candidates
    return "outside", candidates


def _mutation_modes(mutation: MutationRequest) -> tuple[AccessMode, ...]:
    if mutation.access is AccessMode.WRITE:
        return tuple(sorted(_WRITE_MODES, key=lambda item: item.value))
    return (mutation.access,)


def _parse_control_options(
    argv: Sequence[str], *, boolean_flags: frozenset[str] = frozenset()
) -> dict[str, str | bool] | None:
    options: dict[str, str | bool] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            return None
        raw = token[2:]
        if not raw:
            return None

        value: str | bool

        if "=" in raw:
            key, value = raw.split("=", 1)
            if key in boolean_flags or not value:
                return None
        else:
            key = raw
            if key in boolean_flags:
                value = True
            else:
                index += 1
                if index >= len(argv) or argv[index].startswith("--"):
                    return None
                value = argv[index]
        if key in options:
            return None
        options[key] = value
        index += 1
    return options


def _claim_plane_control_command(command: str, *, session_id: str | None) -> bool:
    """Allow only the session-local connector control surface.

    The control channel cannot point at another repository or session and cannot read
    a proposal from an arbitrary file. Admission uses inline JSON; amendment scope is
    derived from a guard-issued ticket rather than caller-selected coordinates.
    """

    if _has_shell_metacharacters(command):
        return False
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    if len(argv) < 3 or posixpath.basename(argv[0]) != "claim-plane":
        return False
    if argv[1] != "codex-intent":
        return False
    action = argv[2]
    schemas = {
        "admit": ({"session-id", "repo", "proposal-json"}, frozenset()),
        "status": ({"session-id", "repo", "json"}, frozenset({"json"})),
        "verify": ({"session-id", "repo", "acceptance-timeout"}, frozenset()),
        "amend": ({"session-id", "ticket", "reason", "repo"}, frozenset()),
        "abandon": ({"session-id", "repo"}, frozenset()),
    }
    schema = schemas.get(action)
    if schema is None:
        return False
    allowed, boolean_flags = schema
    options = _parse_control_options(argv[3:], boolean_flags=boolean_flags)
    if options is None or not set(options).issubset(allowed):
        return False
    if options.get("repo", ".") not in {".", "./"}:
        return False
    if not session_id or options.get("session-id") != session_id:
        return False
    if action == "admit":
        return isinstance(options.get("proposal-json"), str)
    if action == "amend":
        return bool(options.get("ticket")) and bool(options.get("reason"))
    if action == "verify" and "acceptance-timeout" in options:
        try:
            return int(str(options["acceptance-timeout"])) > 0
        except ValueError:
            return False
    return True


def amendment_mutations(
    intent: ChangeIntent, mutations: Iterable[MutationRequest]
) -> tuple[MutationRequest, ...]:
    """Return exact mutations that still require a committed capability."""

    result: list[MutationRequest] = []
    seen: set[tuple[str, str, str | None]] = set()
    for mutation in mutations:
        state, _ = _authorization_state(intent, mutation)
        if state == "committed":
            continue
        if any(
            operation.resource.region is not None
            for operation in _candidate_operations(intent, mutation)
        ):
            # A whole-file hook mutation cannot safely widen a line-bounded declaration.
            continue
        key = (mutation.access.value, mutation.path, mutation.target_path)
        if key not in seen:
            seen.add(key)
            result.append(mutation)
    return tuple(result)


def classify_tool_call(
    root: Path, payload: Mapping[str, Any]
) -> tuple[str, tuple[MutationRequest, ...]]:
    """Return ``(classification, mutations)`` without consulting authority state."""

    tool_name = _normalized_tool_name(
        payload.get("tool_name") or payload.get("toolName")
    )
    cwd_raw = payload.get("cwd")
    cwd = Path(cwd_raw).resolve() if isinstance(cwd_raw, str) and cwd_raw else root
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("Codex tool cwd is outside the enrolled repository") from exc

    if tool_name in _READ_ONLY_TOOLS:
        return "read_only", ()

    if tool_name in _APPLY_PATCH_TOOLS:
        command = _command_from_input(payload)
        if command is None:
            raise ValueError("Codex apply_patch hook input has no command payload")
        return "mutation", _patch_mutations(root, cwd, command)

    if tool_name in _SHELL_TOOLS:
        command = _command_from_input(payload)
        if command is None:
            raise ValueError("Codex shell hook input has no command payload")
        session_value = payload.get("session_id")
        session_id = session_value if isinstance(session_value, str) else None
        if _claim_plane_control_command(command, session_id=session_id):
            return "control_plane", ()
        if _simple_read_only_shell(command):
            return "read_only", ()
        mutations = _parse_simple_shell_mutation(root, cwd, command)
        if mutations is not None:
            return "mutation", mutations
        return "opaque_shell", ()

    # MCP and new tool handlers are intentionally not assumed read-only.  Tool hook
    # coverage changes over time, so unknown surfaces must be explicitly classified
    # before they receive repository mutation authority.
    return "unknown_tool", ()


def evaluate_pre_tool_use(
    *,
    root: Path,
    payload: Mapping[str, Any],
    intent: ChangeIntent | None,
    intent_is_active: bool,
    base_commit_matches: bool,
) -> GuardEvaluation:
    """Classify one Codex tool call and evaluate it against the current intent."""

    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "unknown")
    try:
        classification, mutations = classify_tool_call(root, payload)
    except ValueError as exc:
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification="invalid",
            reason_code="unprovable_tool_input",
            reason=f"Claim Plane could not prove this tool call safe: {exc}",
        )

    if classification == "read_only":
        return GuardEvaluation(
            allowed=True,
            mutating=False,
            tool_name=tool_name,
            classification=classification,
            reason_code="read_only",
            reason="read-only tool call",
        )

    if classification == "control_plane":
        return GuardEvaluation(
            allowed=True,
            mutating=False,
            tool_name=tool_name,
            classification=classification,
            reason_code="control_plane",
            reason="connector-owned Claim Plane control command",
        )

    if classification == "opaque_shell":
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification=classification,
            reason_code="opaque_shell",
            reason=(
                "Claim Plane cannot prove this shell command is read-only or map its "
                "repository writes to admitted resources. Use the built-in edit path, "
                "a supported direct file operation, or re-plan through a brokered command."
            ),
        )

    if classification == "unknown_tool":
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification=classification,
            reason_code="unknown_tool_surface",
            reason=(
                f"Claim Plane does not yet classify Codex tool {tool_name!r}; "
                "the call is denied because its repository effects are not provable."
            ),
        )

    if intent is None or not intent_is_active:
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification=classification,
            reason_code="intent_not_active",
            reason=(
                "No active ChangeIntent is bound to this Codex task. Inspect the "
                "repository read-only, admit the session intent, then retry the mutation."
            ),
            mutations=mutations,
        )

    if not base_commit_matches:
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification=classification,
            reason_code="base_changed",
            reason=(
                "Repository HEAD no longer matches the task's pinned base commit. "
                "Start a fresh task bootstrap before mutating the worktree."
            ),
            mutations=mutations,
        )

    for mutation in mutations:
        protected = protected_control_path(mutation.path) or (
            mutation.target_path is not None
            and protected_control_path(mutation.target_path)
        )
        if protected:
            target = (
                f" -> {mutation.target_path}"
                if mutation.target_path is not None
                else ""
            )
            return GuardEvaluation(
                allowed=False,
                mutating=True,
                tool_name=tool_name,
                classification=classification,
                reason_code="protected_control_surface",
                reason=(
                    f"Mutation {mutation.access.value} {mutation.path}{target} targets "
                    "Claim Plane, Git, or Codex connector control state. This surface "
                    "cannot be granted through a session ChangeIntent."
                ),
                mutations=mutations,
            )

    contingent: list[MutationRequest] = []
    for mutation in mutations:
        state, candidates = _authorization_state(intent, mutation)
        if state == "committed":
            continue
        if state == "contingent":
            contingent.append(mutation)
            continue
        bounded = any(
            operation.resource.region is not None
            for operation in _candidate_operations(intent, mutation)
        )
        suffix = (
            " The matching declaration is line-bounded and this tool call does not "
            "carry a provable pre-image region."
            if bounded
            else ""
        )
        target = (
            f" -> {mutation.target_path}" if mutation.target_path is not None else ""
        )
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification=classification,
            reason_code="outside_admitted_scope",
            reason=(
                f"Mutation {mutation.access.value} {mutation.path}{target} is outside "
                f"the admitted ChangeIntent.{suffix} Amend the task scope before retrying."
            ),
            mutations=mutations,
        )

    unique_promotions: list[MutationRequest] = []
    seen: set[tuple[str, str, str | None]] = set()
    for mutation in contingent:
        key = (mutation.access.value, mutation.path, mutation.target_path)
        if key not in seen:
            seen.add(key)
            unique_promotions.append(mutation)
    if len(unique_promotions) > 1:
        return GuardEvaluation(
            allowed=False,
            mutating=True,
            tool_name=tool_name,
            classification=classification,
            reason_code="multiple_scope_promotions",
            reason=(
                "This tool call would require more than one contingent scope promotion. "
                "Split the mutation or amend the ChangeIntent first so authorization "
                "remains atomic and inspectable."
            ),
            mutations=mutations,
        )

    return GuardEvaluation(
        allowed=True,
        mutating=True,
        tool_name=tool_name,
        classification=classification,
        reason_code="authorized",
        reason="all repository mutations are covered by the admitted ChangeIntent",
        mutations=mutations,
        promotion=(unique_promotions[0] if unique_promotions else None),
    )


def promotion_modes(mutation: MutationRequest) -> tuple[AccessMode, ...]:
    """Return the exact access modes accepted for a contingent promotion."""

    return _mutation_modes(mutation)


def denied_hook_output(evaluation: GuardEvaluation) -> dict[str, Any]:
    """Build the current Codex ``PreToolUse`` deny response."""

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": evaluation.reason,
        }
    }
