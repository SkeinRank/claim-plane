"""Deterministic Python surface extraction used by collectors and legacy checks."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from claim_plane.core.models import Claim, ClaimType

_FENCE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL)
_CLASS_RE = re.compile(r"\bclass\s+(\w+)")
_DEF_RE = re.compile(r"\bdef\s+(\w+)\s*\(([^)]*)\)")


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: ClaimType
    identifier: str
    signature: str | None = None
    qualified_identifier: str | None = None
    subject_identifier: str | None = None
    line_start: int | None = None
    line_end: int | None = None


def strip_fences(text: str) -> str:
    blocks = _FENCE.findall(text)
    return "\n\n".join(blocks) if blocks else text


def _annotation(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _default_text(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _sig_from_node(name: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    positional = list(args.posonlyargs + args.args)
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(args.defaults)
    ) + list(args.defaults)
    parts: list[str] = []
    for argument, default in zip(positional, defaults):
        if argument.arg in {"self", "cls"}:
            continue
        value = argument.arg
        annotation = _annotation(argument.annotation)
        if annotation:
            value += f": {annotation}"
        default_text = _default_text(default)
        if default_text is not None:
            value += f"={default_text}"
        parts.append(value)
    if args.vararg:
        value = "*" + args.vararg.arg
        annotation = _annotation(args.vararg.annotation)
        if annotation:
            value += f": {annotation}"
        parts.append(value)
    elif args.kwonlyargs:
        parts.append("*")
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        value = argument.arg
        annotation = _annotation(argument.annotation)
        if annotation:
            value += f": {annotation}"
        default_text = _default_text(default)
        if default_text is not None:
            value += f"={default_text}"
        parts.append(value)
    if args.kwarg:
        value = "**" + args.kwarg.arg
        annotation = _annotation(args.kwarg.annotation)
        if annotation:
            value += f": {annotation}"
        parts.append(value)
    result = f"{name}(" + ", ".join(parts) + ")"
    returns = _annotation(node.returns)
    if returns:
        result += f"->{returns}"
    return result


def _module_name(path: str | None) -> str | None:
    if not path:
        return None
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if parts and parts[0] in {"src", "lib"}:
        parts.pop(0)
    return ".".join(parts) or None


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str | None) -> None:
        self.module = module
        self.stack: list[str] = []
        self.artifacts: list[Artifact] = []
        self.seen: set[tuple[str, str]] = set()

    def _qualified(self, name: str) -> str:
        return ".".join(part for part in [self.module, *self.stack, name] if part)

    def _add(
        self,
        kind: ClaimType,
        identifier: str,
        *,
        signature: str | None = None,
        subject: str | None = None,
        qualified: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> None:
        if identifier.startswith("_"):
            return
        marker = (kind.value, qualified or identifier)
        if marker in self.seen:
            return
        self.seen.add(marker)
        self.artifacts.append(
            Artifact(
                kind=kind,
                identifier=identifier,
                signature=signature,
                qualified_identifier=qualified,
                subject_identifier=subject,
                line_start=line_start,
                line_end=line_end,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        self._add(
            ClaimType.NAME,
            node.name,
            qualified=qualified,
            line_start=getattr(node, "lineno", None),
            line_end=getattr(node, "end_lineno", None),
        )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        subject = self.stack[-1] if self.stack else None
        qualified = self._qualified(node.name)
        self._add(
            ClaimType.NAME,
            node.name,
            qualified=qualified,
            subject=subject,
            line_start=getattr(node, "lineno", None),
            line_end=getattr(node, "end_lineno", None),
        )
        self._add(
            ClaimType.CONTRACT,
            node.name,
            signature=_sig_from_node(node.name, node),
            qualified=qualified,
            subject=subject,
            line_start=getattr(node, "lineno", None),
            line_end=getattr(node, "end_lineno", None),
        )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def extract_artifacts(text: str, *, path: str | None = None) -> list[Artifact]:
    """Extract public classes/functions and qualified typed contracts."""
    code = strip_fences(text)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        artifacts: list[Artifact] = []
        seen: set[tuple[str, str]] = set()
        for name in _CLASS_RE.findall(code):
            if not name.startswith("_") and (ClaimType.NAME.value, name) not in seen:
                seen.add((ClaimType.NAME.value, name))
                artifacts.append(Artifact(ClaimType.NAME, name))
        for name, rawargs in _DEF_RE.findall(code):
            if name.startswith("_"):
                continue
            args = [
                part.strip().split(":")[0].split("=")[0].strip()
                for part in rawargs.split(",")
                if part.strip() and part.strip() not in {"self", "cls"}
            ]
            if (ClaimType.NAME.value, name) not in seen:
                seen.add((ClaimType.NAME.value, name))
                artifacts.append(Artifact(ClaimType.NAME, name))
            if (ClaimType.CONTRACT.value, name) not in seen:
                seen.add((ClaimType.CONTRACT.value, name))
                artifacts.append(
                    Artifact(
                        ClaimType.CONTRACT, name, f"{name}(" + ", ".join(args) + ")"
                    )
                )
        return artifacts
    visitor = _Visitor(_module_name(path))
    visitor.visit(tree)
    return visitor.artifacts


def artifacts_to_claims(
    text: str, owner: str, task_id: str | None = None
) -> list[Claim]:
    return [
        Claim(a.kind, a.identifier, owner=owner, signature=a.signature, task_id=task_id)
        for a in extract_artifacts(text)
    ]
