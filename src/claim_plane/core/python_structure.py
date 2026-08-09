"""Deterministic Python structural extraction for semantic resource authority.

The extractor parses source with the Python standard-library AST without importing or
executing repository code.  It maps lexical class/function definitions onto Semantic
Resource IR v2 symbol coordinates, records source ownership spans and signatures, and
provides deterministic line-to-owner lookup for later Git-hunk admission.
"""

from __future__ import annotations

import ast
import hashlib
import json
import tokenize
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.core.models import ResourceKind, ResourceRef
from claim_plane.core.resource_ir import SemanticResource, normalize_resource_ref

PYTHON_STRUCTURAL_INDEX_PROTOCOL = "claim-plane.python-structural-index.v1"


class PythonSymbolKind(str, Enum):
    """Lexical Python definitions that can own a source mutation."""

    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    ASYNC_METHOD = "async_method"


class PythonStructuralExtractionError(ValueError):
    """Raised when a Python source file cannot be structurally indexed."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        location = path
        if line is not None:
            location += f":{line}"
            if column is not None:
                location += f":{column}"
        super().__init__(f"{location}: {message}")
        self.path = path
        self.line = line
        self.column = column


def _normal_repository_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    candidate = PurePosixPath(normalized)
    if not normalized or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Python structural paths must be repository-relative")
    return candidate.as_posix()


def _region(start: int, end: int) -> str:
    return f"lines:{start}-{end}"


def _expr(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive fallback across AST versions
        return type(node).__name__


def _arg(argument: ast.arg, default: ast.AST | None = None) -> str:
    text = argument.arg
    annotation = _expr(argument.annotation)
    if annotation:
        text += f": {annotation}"
    if default is not None:
        rendered = _expr(default)
        text += f" = {rendered if rendered is not None else '...'}"
    return text


def _format_arguments(arguments: ast.arguments) -> str:
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.AST | None] = [None] * (
        len(positional) - len(arguments.defaults)
    ) + list(arguments.defaults)
    parts = [_arg(arg, default) for arg, default in zip(positional, defaults)]

    if arguments.posonlyargs:
        parts.insert(len(arguments.posonlyargs), "/")

    if arguments.vararg is not None:
        parts.append("*" + _arg(arguments.vararg))
    elif arguments.kwonlyargs:
        parts.append("*")

    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        parts.append(_arg(argument, default))

    if arguments.kwarg is not None:
        parts.append("**" + _arg(arguments.kwarg))

    return ", ".join(parts)


def _function_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef, qualified_name: str
) -> str:
    signature = f"{qualified_name}({_format_arguments(node.args)})"
    returns = _expr(node.returns)
    if returns:
        signature += f" -> {returns}"
    return signature


def _class_signature(node: ast.ClassDef, qualified_name: str) -> str:
    parameters = [value for base in node.bases if (value := _expr(base))]
    for keyword in node.keywords:
        value = _expr(keyword.value) or "..."
        parameters.append(f"{keyword.arg}={value}" if keyword.arg else f"**{value}")
    return (
        f"{qualified_name}({', '.join(parameters)})"
        if parameters
        else qualified_name
    )


def _decorators(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    return tuple(value for item in node.decorator_list if (value := _expr(item)))


def _definition_start(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    lines = [node.lineno]
    lines.extend(item.lineno for item in node.decorator_list)
    return min(lines)


def _definition_end(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", None) or getattr(node, "lineno", 1))


def _body_start(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    if node.body:
        return int(node.body[0].lineno)
    return int(node.lineno)


def _is_public(qualified_name: str) -> bool:
    return all(not segment.startswith("_") for segment in qualified_name.split("."))


@dataclass(frozen=True, slots=True)
class PythonSymbolDefinition:
    """One lexical definition and its stable Semantic Resource IR coordinate."""

    resource: SemanticResource
    symbol_kind: PythonSymbolKind
    qualified_name: str
    owner_qualified_name: str | None
    owner_identity: str | None
    definition_start_line: int
    definition_line: int
    body_start_line: int
    end_line: int
    depth: int
    decorators: tuple[str, ...] = ()
    occurrence: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.resource, SemanticResource):
            object.__setattr__(
                self,
                "resource",
                SemanticResource.from_dict(self.resource),  # type: ignore[arg-type]
            )
        object.__setattr__(self, "symbol_kind", PythonSymbolKind(self.symbol_kind))
        if self.resource.kind is not ResourceKind.SYMBOL:
            raise ValueError(
                "Python symbol definitions must reference symbol resources"
            )
        if self.resource.qualified_name != self.qualified_name:
            raise ValueError("qualified_name must match the semantic resource")
        if self.definition_start_line < 1 or self.definition_line < 1:
            raise ValueError("definition lines must be positive")
        if not (
            self.definition_start_line
            <= self.definition_line
            <= self.body_start_line
            <= self.end_line
        ):
            raise ValueError("invalid Python symbol source span")
        if self.depth < 0 or self.occurrence < 1:
            raise ValueError("depth and occurrence must be non-negative/positive")
        object.__setattr__(self, "decorators", tuple(self.decorators))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def region(self) -> str:
        return _region(self.definition_start_line, self.end_line)

    @property
    def body_region(self) -> str:
        return _region(self.body_start_line, self.end_line)

    def contains_line(self, line: int) -> bool:
        return self.definition_start_line <= line <= self.end_line

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource.to_dict(),
            "symbol_kind": self.symbol_kind.value,
            "qualified_name": self.qualified_name,
            "owner_qualified_name": self.owner_qualified_name,
            "owner_identity": self.owner_identity,
            "definition_start_line": self.definition_start_line,
            "definition_line": self.definition_line,
            "body_start_line": self.body_start_line,
            "end_line": self.end_line,
            "depth": self.depth,
            "decorators": list(self.decorators),
            "occurrence": self.occurrence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PythonSymbolDefinition":
        return cls(
            resource=SemanticResource.from_dict(data["resource"]),
            symbol_kind=PythonSymbolKind(data["symbol_kind"]),
            qualified_name=str(data["qualified_name"]),
            owner_qualified_name=data.get("owner_qualified_name"),
            owner_identity=data.get("owner_identity"),
            definition_start_line=int(data["definition_start_line"]),
            definition_line=int(data["definition_line"]),
            body_start_line=int(data["body_start_line"]),
            end_line=int(data["end_line"]),
            depth=int(data["depth"]),
            decorators=tuple(str(item) for item in data.get("decorators") or ()),
            occurrence=int(data.get("occurrence", 1)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class PythonStructuralIndex:
    """Versioned structural index for one Python source file."""

    path: str
    source_digest: str
    file_resource: SemanticResource
    definitions: tuple[PythonSymbolDefinition, ...]
    protocol: str = PYTHON_STRUCTURAL_INDEX_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != PYTHON_STRUCTURAL_INDEX_PROTOCOL:
            raise ValueError(f"unsupported Python structural index {self.protocol!r}")
        path = _normal_repository_path(self.path)
        object.__setattr__(self, "path", path)
        if not isinstance(self.file_resource, SemanticResource):
            object.__setattr__(
                self,
                "file_resource",
                SemanticResource.from_dict(  # type: ignore[arg-type]
                    self.file_resource
                ),
            )
        if self.file_resource.kind is not ResourceKind.FILE:
            raise ValueError("file_resource must be a file semantic resource")
        if self.file_resource.path != path:
            raise ValueError("file_resource path must match structural index path")
        definitions = tuple(
            item
            if isinstance(item, PythonSymbolDefinition)
            else PythonSymbolDefinition.from_dict(item)  # type: ignore[arg-type]
            for item in self.definitions
        )
        ordered = tuple(
            sorted(
                definitions,
                key=lambda item: (
                    item.definition_start_line,
                    item.depth,
                    item.end_line,
                    item.qualified_name,
                    item.occurrence,
                ),
            )
        )
        object.__setattr__(self, "definitions", ordered)
        if len(self.source_digest) != 64:
            raise ValueError("source_digest must be a SHA-256 hex digest")

    def definitions_for_symbol(
        self, qualified_name: str
    ) -> tuple[PythonSymbolDefinition, ...]:
        return tuple(
            item for item in self.definitions if item.qualified_name == qualified_name
        )

    def owner_for_line(self, line: int) -> SemanticResource:
        """Return the most specific lexical owner for one source line."""

        if line < 1:
            raise ValueError("line must be positive")
        candidates = [item for item in self.definitions if item.contains_line(line)]
        if not candidates:
            return self.file_resource
        owner = max(
            candidates,
            key=lambda item: (
                item.depth,
                item.definition_start_line,
                -item.end_line,
                item.occurrence,
            ),
        )
        return owner.resource

    def owners_for_lines(self, lines: Iterable[int]) -> tuple[SemanticResource, ...]:
        """Return direct lexical owners for exact changed line numbers."""

        owners: dict[str, SemanticResource] = {}
        for line in sorted(set(int(value) for value in lines)):
            owner = self.owner_for_line(line)
            owners.setdefault(owner.identity, owner)
        return tuple(owners.values())

    def owners_for_region(
        self, start_line: int, end_line: int
    ) -> tuple[SemanticResource, ...]:
        """Return direct owners for an inclusive changed-line region."""

        if start_line < 1 or end_line < start_line:
            raise ValueError("invalid changed-line region")
        return self.owners_for_lines(range(start_line, end_line + 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "path": self.path,
            "source_digest": self.source_digest,
            "file_resource": self.file_resource.to_dict(),
            "definitions": [item.to_dict() for item in self.definitions],
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PythonStructuralIndex":
        return cls(
            protocol=str(data.get("protocol") or PYTHON_STRUCTURAL_INDEX_PROTOCOL),
            path=str(data["path"]),
            source_digest=str(data["source_digest"]),
            file_resource=SemanticResource.from_dict(data["file_resource"]),
            definitions=tuple(
                PythonSymbolDefinition.from_dict(item)
                for item in data.get("definitions") or ()
            ),
        )


class _DefinitionCollector(ast.NodeVisitor):
    def __init__(self, *, path: str) -> None:
        self.path = path
        self.stack: list[PythonSymbolDefinition] = []
        self.definitions: list[PythonSymbolDefinition] = []
        self._occurrences: dict[str, int] = {}

    def _visit_definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        owner = self.stack[-1] if self.stack else None
        qualified_name = (
            f"{owner.qualified_name}.{node.name}" if owner is not None else node.name
        )
        if isinstance(node, ast.ClassDef):
            kind = PythonSymbolKind.CLASS
            signature = _class_signature(node, qualified_name)
        else:
            direct_class_owner = (
                owner is not None and owner.symbol_kind is PythonSymbolKind.CLASS
            )
            if isinstance(node, ast.AsyncFunctionDef):
                kind = (
                    PythonSymbolKind.ASYNC_METHOD
                    if direct_class_owner
                    else PythonSymbolKind.ASYNC_FUNCTION
                )
            else:
                kind = (
                    PythonSymbolKind.METHOD
                    if direct_class_owner
                    else PythonSymbolKind.FUNCTION
                )
            signature = _function_signature(node, qualified_name)

        start = _definition_start(node)
        end = _definition_end(node)
        body_start = _body_start(node)
        occurrence = self._occurrences.get(qualified_name, 0) + 1
        self._occurrences[qualified_name] = occurrence
        owner_identity = owner.resource.identity if owner is not None else None
        decorators = _decorators(node)
        metadata = {
            "path": self.path,
            "language": "python",
            "qualified_identifier": qualified_name,
            "symbol_kind": kind.value,
            "owner_qualified_identifier": owner.qualified_name if owner else None,
            "owner_identity": owner_identity,
            "definition_start_line": start,
            "definition_line": int(node.lineno),
            "body_start_line": body_start,
            "end_line": end,
            "body_region": _region(body_start, end),
            "decorators": list(decorators),
            "is_public": _is_public(qualified_name),
            "occurrence": occurrence,
        }
        resource = normalize_resource_ref(
            ResourceRef(
                ResourceKind.SYMBOL,
                node.name,
                signature=signature,
                region=_region(start, end),
                metadata=metadata,
            )
        )
        definition = PythonSymbolDefinition(
            resource=resource,
            symbol_kind=kind,
            qualified_name=qualified_name,
            owner_qualified_name=owner.qualified_name if owner else None,
            owner_identity=owner_identity,
            definition_start_line=start,
            definition_line=int(node.lineno),
            body_start_line=body_start,
            end_line=end,
            depth=len(self.stack),
            decorators=decorators,
            occurrence=occurrence,
            metadata={"is_public": _is_public(qualified_name)},
        )
        self.definitions.append(definition)
        self.stack.append(definition)
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_definition(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_definition(node)


def extract_python_structure(source: str, *, path: str) -> PythonStructuralIndex:
    """Parse Python source into deterministic Semantic Resource IR v2 symbols.

    The function does not import or execute the target module.  Syntax errors fail
    closed with source coordinates so callers cannot silently fall back to coarse
    file-level concurrency when structural evidence is unavailable.
    """

    repository_path = _normal_repository_path(path)
    try:
        tree = ast.parse(source, filename=repository_path, type_comments=True)
    except SyntaxError as exc:
        raise PythonStructuralExtractionError(
            exc.msg,
            path=repository_path,
            line=exc.lineno,
            column=exc.offset,
        ) from exc

    collector = _DefinitionCollector(path=repository_path)
    collector.visit(tree)
    file_resource = normalize_resource_ref(
        ResourceRef(
            ResourceKind.FILE,
            repository_path,
            metadata={"language": "python"},
        )
    )
    return PythonStructuralIndex(
        path=repository_path,
        source_digest=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        file_resource=file_resource,
        definitions=tuple(collector.definitions),
    )


def extract_python_file(
    file_path: str | Path, *, repository_root: str | Path
) -> PythonStructuralIndex:
    """Read and structurally index a Python file inside a repository root."""

    root = Path(repository_root).resolve()
    target = Path(file_path)
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Python source must be inside repository_root") from exc
    if target.suffix not in {".py", ".pyi"}:
        raise ValueError("Python structural extraction requires .py or .pyi source")
    try:
        with tokenize.open(target) as handle:
            source = handle.read()
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise PythonStructuralExtractionError(
            str(exc), path=relative
        ) from exc
    return extract_python_structure(source, path=relative)


def extract_python_files(
    paths: Sequence[str | Path], *, repository_root: str | Path
) -> tuple[PythonStructuralIndex, ...]:
    """Index a deterministic set of repository-relative Python files."""

    indexes = [
        extract_python_file(path, repository_root=repository_root) for path in paths
    ]
    return tuple(sorted(indexes, key=lambda item: item.path))
