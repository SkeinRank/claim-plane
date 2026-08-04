"""Human-readable terminal presentation for controlled agent runs.

The renderer deliberately has no third-party dependency. It emits stable plain
text for redirected output and adds restrained ANSI colour only for interactive
terminals. Machine-readable output remains owned by the JSON CLI mode.
"""

from __future__ import annotations

import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BLOCKED_HOOK_RE = re.compile(
    r"Command blocked by PreToolUse hook:\s*(?P<reason>.+?)"
    r"(?:\.\s*Command:\s*(?P<command>.+?))?\s*$",
    re.IGNORECASE,
)
_WRITE_BLOCK_RE = re.compile(
    r"Claim Plane blocked write to (?P<target>.+?)\.\s*"
    r"(?P<boundary>(?:Outside|Locked).+?)(?=\s+(?:Mutation|Claim Plane|The operator))",
    re.IGNORECASE,
)


def _supports_colour(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


def _elapsed(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _truncate(value: str, *, width: int = 88) -> str:
    compact = " ".join(value.split())
    return textwrap.shorten(compact, width=width, placeholder="…")


def _relative_evidence(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


@dataclass(slots=True)
class ConsoleRenderer:
    """Render one controlled run as a compact, trustworthy terminal narrative."""

    stream: TextIO
    error_stream: TextIO
    verbose: bool = False
    colour: bool | None = None
    width: int = 72
    started_monotonic: float = field(default_factory=time.monotonic)
    _announced: set[str] = field(default_factory=set)
    _blocked_commands: set[str] = field(default_factory=set)
    _generic_runtime_warning_shown: bool = False

    def __post_init__(self) -> None:
        if self.colour is None:
            self.colour = _supports_colour(self.stream)

    def _paint(self, text: str, code: str) -> str:
        if not self.colour:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def _write(self, line: str = "") -> None:
        self.stream.write(line + "\n")
        self.stream.flush()

    def _write_error(self, line: str) -> None:
        self.error_stream.write(line + ("" if line.endswith("\n") else "\n"))
        self.error_stream.flush()

    def header(
        self,
        *,
        run_id: str,
        root: Path,
        policy: str,
        adapter: str,
        adapter_version: str,
        protocol_version: str | None,
        runtime_name: str,
        runtime_version: str | None,
        model: str | None,
        initial_scope: tuple[str, ...] = (),
        scope_locked: bool = False,
        title: str = "Claim Plane",
    ) -> None:
        self._write(self._paint(title, "1;36"))
        self._write("─" * self.width)
        self._write(f"Run         {run_id}")
        self._write(f"Repository  {root}")
        model_label = model or "runtime default"
        runtime_label = runtime_name
        if runtime_version:
            runtime_label += f" {runtime_version}"
        self._write(f"Agent       {runtime_label} · model {model_label}")
        protocol_label = protocol_version or "unavailable"
        control = (
            f"Control     {adapter} {adapter_version} · "
            f"protocol {protocol_label} · {policy}"
        )
        self._write(control)
        if initial_scope:
            scope_label = ", ".join(initial_scope[:3])
            if len(initial_scope) > 3:
                scope_label += f" · +{len(initial_scope) - 3} more"
            expansion = "locked" if scope_locked else "brokered amendments allowed"
            self._write(f"Scope       {scope_label} · {expansion}")
        self._write()

    def step(
        self,
        label: str,
        *,
        detail: str | None = None,
        elapsed_seconds: float | None = None,
        state: str = "passed",
        dedupe_key: str | None = None,
    ) -> None:
        key = dedupe_key or f"{state}:{label}"
        if key in self._announced:
            return
        self._announced.add(key)
        symbols = {
            "passed": self._paint("✓", "32"),
            "active": self._paint("●", "36"),
            "warning": self._paint("!", "33"),
            "failed": self._paint("✗", "31"),
            "info": self._paint("·", "2"),
        }
        symbol = symbols.get(state, symbols["info"])
        suffix_parts = []
        if detail:
            suffix_parts.append(detail)
        elapsed_label = _elapsed(elapsed_seconds)
        if elapsed_label:
            suffix_parts.append(elapsed_label)
        suffix = (
            f"  {self._paint(' · '.join(suffix_parts), '2')}" if suffix_parts else ""
        )
        self._write(f"{symbol} {label}{suffix}")

    def runtime_payload(
        self,
        payload: Mapping[str, Any],
        *,
        elapsed_seconds: float,
    ) -> None:
        event_type = str(payload.get("type") or "")
        if event_type == "thread.started":
            thread_id = payload.get("thread_id")
            detail = str(thread_id) if isinstance(thread_id, str) else None
            self.step(
                "Codex session started",
                detail=detail,
                elapsed_seconds=elapsed_seconds,
                dedupe_key="runtime:thread.started",
            )
        elif event_type == "turn.started":
            self.step(
                "Codex working",
                elapsed_seconds=elapsed_seconds,
                state="active",
                dedupe_key="runtime:turn.started",
            )
        elif event_type == "turn.completed":
            self.step(
                "Codex turn completed",
                elapsed_seconds=elapsed_seconds,
                dedupe_key="runtime:turn.completed",
            )
        elif event_type == "turn.failed":
            self.step(
                "Codex turn failed",
                elapsed_seconds=elapsed_seconds,
                state="failed",
                dedupe_key="runtime:turn.failed",
            )
        elif event_type == "error":
            self.step(
                "Codex runtime reported an error",
                elapsed_seconds=elapsed_seconds,
                state="failed",
                dedupe_key="runtime:error",
            )

    def runtime_stderr(self, line: str) -> None:
        if self.verbose:
            self._write_error(line.rstrip("\n"))
            return
        stripped = _ANSI_RE.sub("", line).strip()
        if not stripped:
            return
        blocked = _BLOCKED_HOOK_RE.search(stripped)
        if blocked:
            reason = blocked.group("reason") or ""
            write_block = _WRITE_BLOCK_RE.search(reason)
            if write_block:
                target = _truncate(write_block.group("target"), width=88)
                boundary = _truncate(write_block.group("boundary"), width=100)
                key = f"write:{target}:{boundary}"
                if key in self._blocked_commands:
                    return
                self._blocked_commands.add(key)
                self.step(
                    "Write blocked",
                    detail=target,
                    state="warning",
                    dedupe_key=f"blocked:{key}",
                )
                self._write(f"  {self._paint(boundary, '2')}")
                return
            raw_command = blocked.group("command") or reason
            command = _truncate(raw_command, width=100)
            if command in self._blocked_commands:
                return
            self._blocked_commands.add(command)
            self.step(
                "Command blocked by policy",
                detail=command,
                state="warning",
                dedupe_key=f"blocked:{command}",
            )
            return
        if not self._generic_runtime_warning_shown and "error" in stripped.lower():
            self._generic_runtime_warning_shown = True
            self.step(
                "Codex runtime emitted an error",
                detail="run with --verbose for the raw diagnostic",
                state="warning",
                dedupe_key="runtime:stderr:error",
            )

    def verification_started(self) -> None:
        self._write()
        self.step(
            "Final verification",
            state="active",
            dedupe_key="verification:started",
        )

    def finish(
        self,
        *,
        result: Mapping[str, Any],
        evidence_path: Path,
        root: Path,
        final_message: str | None,
    ) -> None:
        completion = result.get("completion")
        completion_map = completion if isinstance(completion, Mapping) else {}
        changes = result.get("changes")
        changes_map = changes if isinstance(changes, Mapping) else {}
        acceptance = result.get("acceptance")
        acceptance_map = acceptance if isinstance(acceptance, Mapping) else {}
        risk = result.get("risk")
        risk_map = risk if isinstance(risk, Mapping) else {}

        findings = completion_map.get("findings")
        finding_codes = {
            str(item.get("code") or "")
            for item in findings or ()
            if isinstance(item, Mapping)
        }
        scope_failure = any(
            code.startswith("scope_")
            or code
            in {
                "undeclared_change",
                "unauthorized_mutation",
                "contract_mismatch",
                "preserve_violation",
            }
            for code in finding_codes
        )
        verification_ran = completion_map.get("protocol") is not None
        scope_clean = (
            verification_ran
            and int(completion_map.get("executed_violations") or 0) == 0
            and not scope_failure
        )
        changed_files = int(changes_map.get("file_count") or 0)
        scope_label = "Scope verified" if scope_clean else "Scope not verified"
        self.step(
            scope_label,
            detail=f"{changed_files} file{'s' if changed_files != 1 else ''}",
            state="passed" if scope_clean else "failed",
            dedupe_key="verification:scope",
        )

        scope = result.get("scope")
        scope_map = scope if isinstance(scope, Mapping) else {}
        amendments = scope_map.get("amendments")
        amendment_map = amendments if isinstance(amendments, Mapping) else {}
        admitted_amendments = int(amendment_map.get("admitted") or 0)
        denied_amendments = int(amendment_map.get("denied") or 0)
        if admitted_amendments:
            self.step(
                "Scope amendment admitted",
                detail=(
                    f"{admitted_amendments} brokered "
                    f"expansion{'s' if admitted_amendments != 1 else ''}"
                ),
                state="passed",
                dedupe_key="verification:amendment:admitted",
            )
        if denied_amendments:
            self.step(
                "Scope amendment denied",
                detail=(
                    f"{denied_amendments} "
                    f"request{'s' if denied_amendments != 1 else ''}"
                ),
                state="warning",
                dedupe_key="verification:amendment:denied",
            )

        initial_scope = [
            str(item)
            for item in scope_map.get("initial") or ()
            if isinstance(item, str)
        ]
        final_scope = [
            str(item) for item in scope_map.get("final") or () if isinstance(item, str)
        ]
        history = amendment_map.get("history")
        added_scope: list[str] = []
        for item in history or ():
            if not isinstance(item, Mapping) or item.get("allowed") is not True:
                continue
            for resource in item.get("resources") or ():
                if isinstance(resource, str) and resource not in added_scope:
                    added_scope.append(resource)
        if initial_scope or added_scope:
            self.step(
                "Scope evolution",
                state="info",
                dedupe_key="verification:scope:evolution",
            )
            if initial_scope:
                self._write("  Initial  " + self._paint(", ".join(initial_scope), "2"))
            for resource in added_scope:
                self._write(
                    "  Added    " + self._paint(f"{resource} · brokered amendment", "2")
                )
            final_count = len(final_scope) or changed_files
            self._write(
                "  Final    "
                + self._paint(
                    f"{final_count} file{'s' if final_count != 1 else ''}",
                    "2",
                )
            )

        acceptance_passed = bool(acceptance_map.get("passed"))
        commands = acceptance_map.get("commands")
        command_list = [str(item) for item in commands or () if isinstance(item, str)]
        acceptance_detail = (
            _truncate(command_list[0], width=72)
            if len(command_list) == 1
            else f"{len(command_list)} checks"
        )
        if not command_list:
            acceptance_detail = "no configured checks"
        self.step(
            "Acceptance passed" if acceptance_passed else "Acceptance not verified",
            detail=acceptance_detail,
            state="passed" if acceptance_passed else "failed",
            dedupe_key="verification:acceptance",
        )

        risk_name = str(risk_map.get("highest_risk") or "unknown")
        action = str(risk_map.get("final_action") or "unknown")
        risk_state = "passed" if action == "ALLOW" else "warning"
        risk_label = "Risk allowed" if action == "ALLOW" else "Risk requires attention"
        self.step(
            risk_label,
            detail=f"{risk_name} · {action}",
            state=risk_state,
            dedupe_key="verification:risk",
        )

        files = changes_map.get("files")
        if isinstance(files, list) and files:
            visible = [
                str(item.get("path"))
                for item in files[:5]
                if isinstance(item, Mapping) and item.get("path")
            ]
            if visible:
                more = len(files) - len(visible)
                detail = ", ".join(visible)
                if more > 0:
                    detail += f" · +{more} more"
                self.step(
                    "Changed files",
                    detail=detail,
                    state="info",
                    dedupe_key="verification:files",
                )

        started_at = result.get("started_at")
        finished_at = result.get("finished_at")
        duration = ""
        if isinstance(started_at, str) and isinstance(finished_at, str):
            try:
                start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                duration = _elapsed(max(0.0, (finish - start).total_seconds()))
            except ValueError:
                duration = ""

        outcome = str(result.get("outcome") or "FAILED")
        error_value = result.get("error")
        if outcome not in {"VERIFIED", "REVIEW_REQUIRED"} and isinstance(
            error_value, Mapping
        ):
            error_code = str(error_value.get("code") or "verification_failed")
            self.step(
                "Run requires attention",
                detail=error_code.replace("_", " "),
                state="failed" if outcome in {"FAILED", "REJECTED"} else "warning",
                dedupe_key="verification:error",
            )
        outcome_colour = {
            "VERIFIED": "1;32",
            "REVIEW_REQUIRED": "1;33",
            "REJECTED": "1;31",
            "FAILED": "1;31",
            "TIMED_OUT": "1;33",
            "CANCELLED": "1;33",
        }.get(outcome, "1")
        self._write()
        self._write("─" * self.width)
        delivery = self._paint(
            f"DELIVERY {outcome.replace('_', ' ')}",
            outcome_colour,
        )
        self._write(delivery)
        summary_parts = [
            f"{changed_files} file{'s' if changed_files != 1 else ''} changed",
        ]
        additions = int(changes_map.get("total_additions") or 0)
        deletions = int(changes_map.get("total_deletions") or 0)
        if additions or deletions:
            summary_parts.append(f"+{additions} -{deletions}")
        acceptance_suffix = "s" if len(command_list) != 1 else ""
        summary_parts.append(f"{len(command_list)} acceptance check{acceptance_suffix}")
        if duration:
            summary_parts.append(duration)
        self._write(" · ".join(summary_parts))
        self._write(f"Evidence    {_relative_evidence(root, evidence_path)}")

        if final_message:
            self._write()
            self._write(self._paint("Agent summary (not verification evidence)", "1"))
            limit = None if self.verbose else 12
            lines = final_message.rstrip().splitlines()
            shown = lines if limit is None else lines[:limit]
            for line in shown:
                self._write(f"  {line}")
            if limit is not None and len(lines) > limit:
                self._write(f"  … {len(lines) - limit} more line(s); use --verbose")
