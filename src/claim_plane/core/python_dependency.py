"""Python frontend for Semantic Dependency Graph v2.

The builder parses source with the standard library AST, reuses the Python structural
index for stable symbol identities, and emits deterministic repository relationships.
It never imports or executes target code.  Resolution is intentionally conservative:
repository-local targets are marked internal, known external module/symbol targets are
marked external, and ambiguous lexical names remain unresolved for later admission.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import tokenize
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

from claim_plane.core.dependency_graph import (
    DependencyEdge,
    DependencyNode,
    DependencyRelation,
    DependencyResolution,
    SemanticDependencyGraph,
)
from claim_plane.core.models import ResourceKind, ResourceRef
from claim_plane.core.python_structure import (
    PythonStructuralExtractionError,
    PythonStructuralIndex,
    extract_python_structure,
)
from claim_plane.core.resource_ir import SemanticResource, normalize_resource_ref

_BUILTIN_NAMES = frozenset(dir(builtins))

_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def _normal_repository_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    candidate = PurePosixPath(normalized)
    if not normalized or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Python dependency paths must be repository-relative")
    if candidate.suffix not in {".py", ".pyi"}:
        raise ValueError("Python dependency graph requires .py or .pyi source")
    return candidate.as_posix()


def _is_test_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    name = candidate.name
    return (
        "tests" in candidate.parts
        or "test" in candidate.parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _state_resource(path: str, qualified_name: str) -> SemanticResource:
    return normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            qualified_name.rsplit(".", 1)[-1],
            metadata={
                "path": path,
                "language": "python",
                "qualified_identifier": qualified_name,
                "symbol_kind": "state",
            },
        )
    )


def _external_symbol(qualified_name: str, *, kind: str = "symbol") -> SemanticResource:
    return normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            qualified_name,
            metadata={
                "language": "python",
                "qualified_identifier": qualified_name,
                "external": True,
                "symbol_kind": kind,
            },
        )
    )


def _module_candidates(path: str, package_dirs: set[str]) -> tuple[str, ...]:
    candidate = PurePosixPath(path)
    stem_parts = list(candidate.with_suffix("").parts)
    if stem_parts and stem_parts[-1] == "__init__":
        stem_parts.pop()
    if not stem_parts:
        return ()

    # Prefer the importable package rooted at the first contiguous __init__.py chain.
    primary_start = len(stem_parts) - 1
    parent_parts = list(candidate.parent.parts)
    while primary_start > 0:
        parent = "/".join(stem_parts[:primary_start])
        if parent not in package_dirs:
            break
        primary_start -= 1
    # If the file is inside an explicit package, strip non-package source roots
    # such as src/.
    if parent_parts:
        package_start = len(parent_parts)
        while (
            package_start > 0
            and "/".join(parent_parts[:package_start]) in package_dirs
        ):
            package_start -= 1
        if package_start < len(parent_parts):
            primary_start = min(primary_start, package_start)

    values: list[str] = []
    primary = ".".join(stem_parts[primary_start:])
    if primary:
        values.append(primary)
    full = ".".join(stem_parts)
    if full and full not in values:
        values.append(full)
    # Suffix aliases make namespace/src layouts resolvable when unique.
    for index in range(1, len(stem_parts)):
        value = ".".join(stem_parts[index:])
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _module_package(module_name: str, path: str) -> str:
    if PurePosixPath(path).stem == "__init__":
        return module_name
    return module_name.rpartition(".")[0]


def _resolve_relative_module(
    *, module: str | None, level: int, current_package: str
) -> str:
    if level <= 0:
        return module or ""
    parts = current_package.split(".") if current_package else []
    trim = level - 1
    if trim > len(parts):
        base: list[str] = []
    else:
        base = parts[: len(parts) - trim]
    if module:
        base.extend(module.split("."))
    return ".".join(part for part in base if part)


@dataclass(frozen=True, slots=True)
class _RepositoryIndex:
    sources: Mapping[str, str]
    structural: Mapping[str, PythonStructuralIndex]
    trees: Mapping[str, ast.Module]
    module_by_path: Mapping[str, str]
    path_by_module: Mapping[str, str]
    symbols_by_path: Mapping[str, Mapping[str, SemanticResource]]
    symbol_by_module_name: Mapping[tuple[str, str], SemanticResource]


def _prepare_repository(sources: Mapping[str, str]) -> _RepositoryIndex:
    normalized: dict[str, str] = {}
    for raw_path, source in sources.items():
        path = _normal_repository_path(str(raw_path))
        if path in normalized and normalized[path] != source:
            raise ValueError(
                f"duplicate Python source path with different content: {path}"
            )
        normalized[path] = str(source)
    if not normalized:
        return _RepositoryIndex({}, {}, {}, {}, {}, {}, {})

    structural: dict[str, PythonStructuralIndex] = {}
    trees: dict[str, ast.Module] = {}
    package_dirs = {
        str(PurePosixPath(path).parent)
        for path in normalized
        if PurePosixPath(path).name == "__init__.py"
    }
    candidate_owners: dict[str, list[str]] = defaultdict(list)
    candidates_by_path: dict[str, tuple[str, ...]] = {}
    for path, source in sorted(normalized.items()):
        structural[path] = extract_python_structure(source, path=path)
        try:
            trees[path] = ast.parse(source, filename=path, type_comments=True)
        except SyntaxError as exc:  # structural extraction should already catch this
            raise PythonStructuralExtractionError(
                exc.msg, path=path, line=exc.lineno, column=exc.offset
            ) from exc
        candidates = _module_candidates(path, package_dirs)
        candidates_by_path[path] = candidates
        for candidate in candidates:
            candidate_owners[candidate].append(path)

    path_by_module: dict[str, str] = {}
    for module, paths in candidate_owners.items():
        unique = sorted(set(paths))
        if len(unique) == 1:
            path_by_module[module] = unique[0]

    module_by_path: dict[str, str] = {}
    for path, candidates in candidates_by_path.items():
        module_by_path[path] = next(
            (
                candidate
                for candidate in candidates
                if path_by_module.get(candidate) == path
            ),
            candidates[0] if candidates else PurePosixPath(path).stem,
        )

    symbols_by_path: dict[str, dict[str, SemanticResource]] = {}
    symbol_by_module_name: dict[tuple[str, str], SemanticResource] = {}
    for path, index in structural.items():
        by_name: dict[str, SemanticResource] = {}
        for definition in index.definitions:
            # Repeated overloads intentionally share one stable identity.
            by_name.setdefault(definition.qualified_name, definition.resource)
        symbols_by_path[path] = by_name
        module = module_by_path[path]
        for qualified_name, resource in by_name.items():
            symbol_by_module_name.setdefault((module, qualified_name), resource)

    return _RepositoryIndex(
        sources=dict(sorted(normalized.items())),
        structural=structural,
        trees=trees,
        module_by_path=module_by_path,
        path_by_module=path_by_module,
        symbols_by_path=symbols_by_path,
        symbol_by_module_name=symbol_by_module_name,
    )


class _GraphAccumulator:
    def __init__(self) -> None:
        self.nodes: dict[str, DependencyNode] = {}
        self.edges: dict[tuple[str, str, str, str], DependencyEdge] = {}

    def node(
        self,
        resource: SemanticResource,
        *,
        public: bool = False,
        test: bool = False,
        external: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        current = self.nodes.get(resource.identity)
        if current is None:
            self.nodes[resource.identity] = DependencyNode(
                resource=resource,
                public=public,
                test=test,
                external=external,
                metadata=dict(metadata or {}),
            )
            return
        self.nodes[resource.identity] = DependencyNode(
            resource=current.resource,
            public=current.public or public,
            test=current.test or test,
            external=current.external or external,
            metadata={**current.metadata, **dict(metadata or {})},
        )

    def edge(
        self,
        source: SemanticResource,
        target: SemanticResource,
        relation: DependencyRelation,
        *,
        resolution: DependencyResolution,
        line: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.node(source)
        self.node(target, external=resolution is DependencyResolution.EXTERNAL)
        edge = DependencyEdge(
            source_identity=source.identity,
            target_identity=target.identity,
            relation=relation,
            resolution=resolution,
            locations=() if line is None else (line,),
            metadata=dict(metadata or {}),
        )
        previous = self.edges.get(edge.key)
        if previous is None:
            self.edges[edge.key] = edge
        else:
            self.edges[edge.key] = DependencyEdge(
                source_identity=edge.source_identity,
                target_identity=edge.target_identity,
                relation=edge.relation,
                resolution=edge.resolution,
                locations=(*previous.locations, *edge.locations),
                metadata={**previous.metadata, **edge.metadata},
            )


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    resource: SemanticResource
    resolution: DependencyResolution


class _PythonDependencyCollector(ast.NodeVisitor):
    def __init__(
        self,
        *,
        repository: _RepositoryIndex,
        path: str,
        accumulator: _GraphAccumulator,
        shared_states: Mapping[str, SemanticResource],
    ) -> None:
        self.repository = repository
        self.path = path
        self.index = repository.structural[path]
        self.module = repository.module_by_path[path]
        self.package = _module_package(self.module, path)
        self.accumulator = accumulator
        self.shared_states = dict(shared_states)
        self.imports_by_owner: dict[str, dict[str, _ResolvedTarget]] = defaultdict(dict)
        self.test_source = _is_test_path(path)

    def _owner(self, node: ast.AST) -> SemanticResource:
        line = int(getattr(node, "lineno", 1))
        return self.index.owner_for_line(line)

    def _owner_chain(self, owner: SemanticResource) -> tuple[str, ...]:
        identities = [owner.identity]
        current_identity = owner.identity
        visited = {current_identity}
        by_identity = {
            definition.resource.identity: definition
            for definition in self.index.definitions
        }
        while True:
            definition = by_identity.get(current_identity)
            parent_identity = (
                definition.owner_identity if definition is not None else None
            )
            if parent_identity is None or parent_identity in visited:
                break
            identities.append(parent_identity)
            visited.add(parent_identity)
            current_identity = parent_identity
        identities.append(self.index.file_resource.identity)
        return tuple(dict.fromkeys(identities))

    def _aliases(self, owner: SemanticResource) -> Mapping[str, _ResolvedTarget]:
        merged: dict[str, _ResolvedTarget] = {}
        # File aliases first, then nearest lexical owner overrides them.
        for identity in reversed(self._owner_chain(owner)):
            merged.update(self.imports_by_owner.get(identity, {}))
        return merged

    def _internal_symbol(
        self, path: str, qualified_name: str
    ) -> SemanticResource | None:
        return self.repository.symbols_by_path.get(path, {}).get(qualified_name)

    def _resolve_module(self, module: str) -> _ResolvedTarget:
        path = self.repository.path_by_module.get(module)
        if path is not None:
            return _ResolvedTarget(
                self.repository.structural[path].file_resource,
                DependencyResolution.INTERNAL,
            )
        return _ResolvedTarget(
            _external_symbol(module, kind="module"), DependencyResolution.EXTERNAL
        )

    def _resolve_from_symbol(self, module: str, name: str) -> _ResolvedTarget:
        path = self.repository.path_by_module.get(module)
        if path is not None:
            resource = self._internal_symbol(path, name)
            if resource is not None:
                return _ResolvedTarget(resource, DependencyResolution.INTERNAL)
        nested_module = f"{module}.{name}" if module else name
        nested_path = self.repository.path_by_module.get(nested_module)
        if nested_path is not None:
            return _ResolvedTarget(
                self.repository.structural[nested_path].file_resource,
                DependencyResolution.INTERNAL,
            )
        return _ResolvedTarget(
            _external_symbol(nested_module), DependencyResolution.EXTERNAL
        )

    def _resolve_name(self, name: str, owner: SemanticResource) -> _ResolvedTarget:
        aliases = self._aliases(owner)
        imported = aliases.get(name)
        if imported is not None:
            return imported

        owner_name = owner.qualified_name or ""
        if owner_name:
            segments = owner_name.split(".")
            # Nested lexical definitions first, then class/module peers.
            for cut in range(len(segments), 0, -1):
                candidate = ".".join([*segments[:cut], name])
                resource = self._internal_symbol(self.path, candidate)
                if resource is not None:
                    return _ResolvedTarget(resource, DependencyResolution.INTERNAL)
        resource = self._internal_symbol(self.path, name)
        if resource is not None:
            return _ResolvedTarget(resource, DependencyResolution.INTERNAL)
        state = self.shared_states.get(name)
        if state is not None:
            return _ResolvedTarget(state, DependencyResolution.INTERNAL)
        if name in _BUILTIN_NAMES:
            return _ResolvedTarget(
                _external_symbol(f"builtins.{name}"), DependencyResolution.EXTERNAL
            )
        return _ResolvedTarget(
            _external_symbol(name), DependencyResolution.UNRESOLVED
        )

    def _resolve_attribute(
        self, node: ast.Attribute, owner: SemanticResource
    ) -> _ResolvedTarget:
        if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
            owner_name = owner.qualified_name or ""
            class_name = owner_name.rsplit(".", 1)[0] if "." in owner_name else ""
            qualified = f"{class_name}.{node.attr}" if class_name else node.attr
            resource = self._internal_symbol(self.path, qualified)
            if resource is not None:
                return _ResolvedTarget(resource, DependencyResolution.INTERNAL)
            state = self.shared_states.get(qualified)
            if state is None:
                state = _state_resource(self.path, qualified)
            return _ResolvedTarget(state, DependencyResolution.INTERNAL)

        dotted = _dotted_name(node)
        if dotted:
            root, _, suffix = dotted.partition(".")
            alias = self._aliases(owner).get(root)
            if alias is not None:
                if (
                    alias.resource.path
                    and alias.resolution is DependencyResolution.INTERNAL
                ):
                    if alias.resource.kind is ResourceKind.FILE:
                        target_name = suffix
                        display_base = self.repository.module_by_path.get(
                            alias.resource.path, root
                        )
                    else:
                        base_name = (
                            alias.resource.qualified_name or alias.resource.identifier
                        )
                        target_name = f"{base_name}.{suffix}"
                        display_base = base_name
                    resource = self._internal_symbol(alias.resource.path, target_name)
                    if resource is not None:
                        return _ResolvedTarget(resource, DependencyResolution.INTERNAL)
                    return _ResolvedTarget(
                        _external_symbol(f"{display_base}.{suffix}"),
                        DependencyResolution.UNRESOLVED,
                    )
                base = alias.resource.qualified_name or alias.resource.identifier
                return _ResolvedTarget(
                    _external_symbol(f"{base}.{suffix}"), alias.resolution
                )

            local_root = self._internal_symbol(self.path, root)
            if local_root is not None:
                resource = self._internal_symbol(self.path, dotted)
                if resource is not None:
                    return _ResolvedTarget(resource, DependencyResolution.INTERNAL)
        return _ResolvedTarget(
            _external_symbol(dotted or node.attr), DependencyResolution.UNRESOLVED
        )

    def _resolve_expr(
        self, node: ast.AST, owner: SemanticResource
    ) -> _ResolvedTarget | None:
        if isinstance(node, ast.Name):
            return self._resolve_name(node.id, owner)
        if isinstance(node, ast.Attribute):
            return self._resolve_attribute(node, owner)
        if isinstance(node, ast.Subscript):
            return self._resolve_expr(node.value, owner)
        return None

    def _edge(
        self,
        source: SemanticResource,
        target: _ResolvedTarget,
        relation: DependencyRelation,
        *,
        line: int | None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.accumulator.edge(
            source,
            target.resource,
            relation,
            resolution=target.resolution,
            line=line,
            metadata=metadata,
        )
        if (
            self.test_source
            and relation in {DependencyRelation.CALLS, DependencyRelation.IMPORTS}
            and target.resolution is DependencyResolution.INTERNAL
            and target.resource.path
            and not _is_test_path(target.resource.path)
        ):
            self.accumulator.edge(
                source,
                target.resource,
                DependencyRelation.TESTS,
                resolution=DependencyResolution.INTERNAL,
                line=line,
                metadata={"derived_from": relation.value},
            )

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        owner = self._owner(node)
        for alias in node.names:
            target = self._resolve_module(alias.name)
            self._edge(owner, target, DependencyRelation.IMPORTS, line=node.lineno)
            local = alias.asname or alias.name.split(".", 1)[0]
            self.imports_by_owner[owner.identity][local] = target
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        owner = self._owner(node)
        module = _resolve_relative_module(
            module=node.module, level=node.level, current_package=self.package
        )
        for alias in node.names:
            if alias.name == "*":
                target = self._resolve_module(module)
                self._edge(owner, target, DependencyRelation.IMPORTS, line=node.lineno)
                continue
            target = self._resolve_from_symbol(module, alias.name)
            self._edge(owner, target, DependencyRelation.IMPORTS, line=node.lineno)
            self.imports_by_owner[owner.identity][alias.asname or alias.name] = target
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        owner = self._owner(node)
        target = self._resolve_expr(node.func, owner)
        if target is not None:
            self._edge(owner, target, DependencyRelation.CALLS, line=node.lineno)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        owner = self._owner(node)
        for base in node.bases:
            target = self._resolve_expr(base, owner)
            if target is not None:
                self._edge(owner, target, DependencyRelation.INHERITS, line=node.lineno)
        for keyword in node.keywords:
            target = self._resolve_expr(keyword.value, owner)
            if target is not None:
                self._edge(owner, target, DependencyRelation.INHERITS, line=node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._record_function_types(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._record_function_types(node)
        self.generic_visit(node)

    def _record_function_types(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        owner = self._owner(node)
        annotations: list[ast.AST] = []
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]:
            if argument.annotation is not None:
                annotations.append(argument.annotation)
        if node.args.vararg and node.args.vararg.annotation is not None:
            annotations.append(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation is not None:
            annotations.append(node.args.kwarg.annotation)
        if node.returns is not None:
            annotations.append(node.returns)
        for annotation in annotations:
            for target in self._annotation_targets(annotation, owner):
                self._edge(owner, target, DependencyRelation.TYPES, line=node.lineno)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        owner = self._owner(node)
        state = self._assignment_target(node.target, owner)
        if state is not None:
            for target in self._annotation_targets(node.annotation, owner):
                self._edge(state, target, DependencyRelation.TYPES, line=node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        owner = self._owner(node)
        state = self.shared_states.get(node.id)
        if (
            state is None
            and owner.metadata.get("symbol_kind") == "class"
            and owner.qualified_name
        ):
            state = self.shared_states.get(f"{owner.qualified_name}.{node.id}")
        if state is not None and state.identity != owner.identity:
            relation = (
                DependencyRelation.WRITES
                if isinstance(node.ctx, (ast.Store, ast.Del))
                else DependencyRelation.READS
            )
            self._edge(
                owner,
                _ResolvedTarget(state, DependencyResolution.INTERNAL),
                relation,
                line=node.lineno,
            )
        self.generic_visit(node)


    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(
            node.value, ast.Name
        ):
            owner = self._owner(node)
            state = self.shared_states.get(node.value.id)
            if state is not None:
                self._edge(
                    owner,
                    _ResolvedTarget(state, DependencyResolution.INTERNAL),
                    DependencyRelation.WRITES,
                    line=node.lineno,
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
            owner = self._owner(node)
            target = self._resolve_attribute(node, owner)
            relation = (
                DependencyRelation.WRITES
                if isinstance(node.ctx, (ast.Store, ast.Del))
                else DependencyRelation.READS
            )
            self._edge(owner, target, relation, line=node.lineno)
        self.generic_visit(node)

    def _assignment_target(
        self, target: ast.AST, owner: SemanticResource
    ) -> SemanticResource | None:
        if isinstance(target, ast.Name):
            return self.shared_states.get(target.id)
        if isinstance(target, ast.Attribute):
            resolved = self._resolve_attribute(target, owner)
            if resolved.resolution is DependencyResolution.INTERNAL:
                return resolved.resource
        return None

    def _annotation_targets(
        self, annotation: ast.AST, owner: SemanticResource
    ) -> tuple[_ResolvedTarget, ...]:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return ()
        found: dict[str, _ResolvedTarget] = {}
        for item in ast.walk(annotation):
            if isinstance(item, ast.Name):
                target = self._resolve_name(item.id, owner)
                found.setdefault(target.resource.identity, target)
            elif isinstance(item, ast.Attribute):
                target = self._resolve_attribute(item, owner)
                found.setdefault(target.resource.identity, target)
        return tuple(found[key] for key in sorted(found))


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _shared_states(
    *, path: str, tree: ast.Module, index: PythonStructuralIndex
) -> dict[str, SemanticResource]:
    states: dict[str, SemanticResource] = {}

    def add_target(target: ast.AST, owner_name: str | None) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                add_target(element, owner_name)
            return
        if not isinstance(target, ast.Name):
            return
        qualified = f"{owner_name}.{target.id}" if owner_name else target.id
        resource = _state_resource(path, qualified)
        states.setdefault(qualified, resource)
        if owner_name is None:
            states.setdefault(target.id, resource)

    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            for target in targets:
                add_target(target, None)
        if isinstance(statement, ast.ClassDef):
            class_name = statement.name
            for child in statement.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        child.targets
                        if isinstance(child, ast.Assign)
                        else [child.target]
                    )
                    for target in targets:
                        add_target(target, class_name)
    # Instance attributes can be discovered from the AST before edge collection.
    for item in ast.walk(tree):
        if not isinstance(item, ast.Attribute) or not isinstance(item.value, ast.Name):
            continue
        if item.value.id not in {"self", "cls"} or not hasattr(item, "lineno"):
            continue
        owner = index.owner_for_line(int(item.lineno))
        owner_name = owner.qualified_name or ""
        if "." not in owner_name:
            continue
        class_name = owner_name.rsplit(".", 1)[0]
        qualified = f"{class_name}.{item.attr}"
        states.setdefault(qualified, _state_resource(path, qualified))
    return states


def build_python_dependency_graph(
    sources: Mapping[str, str],
) -> SemanticDependencyGraph:
    """Build a deterministic semantic dependency graph for Python repository sources."""

    repository = _prepare_repository(sources)
    accumulator = _GraphAccumulator()

    for path, index in sorted(repository.structural.items()):
        is_test = _is_test_path(path)
        accumulator.node(
            index.file_resource,
            public=not is_test,
            test=is_test,
            metadata={"module": repository.module_by_path[path]},
        )
        definitions = {item.resource.identity: item for item in index.definitions}
        for definition in index.definitions:
            is_public = bool(definition.metadata.get("is_public", False))
            accumulator.node(
                definition.resource,
                public=is_public,
                test=is_test,
                metadata={
                    "symbol_kind": definition.symbol_kind.value,
                    "module": repository.module_by_path[path],
                },
            )
            parent = (
                definitions[definition.owner_identity].resource
                if definition.owner_identity in definitions
                else index.file_resource
            )
            accumulator.edge(
                parent,
                definition.resource,
                DependencyRelation.DEFINES,
                resolution=DependencyResolution.INTERNAL,
                line=definition.definition_line,
            )
            if is_public and definition.depth <= 1:
                accumulator.edge(
                    parent,
                    definition.resource,
                    DependencyRelation.PUBLIC_API,
                    resolution=DependencyResolution.INTERNAL,
                    line=definition.definition_line,
                )

        shared_states = _shared_states(
            path=path, tree=repository.trees[path], index=index
        )
        for qualified_name, state in sorted(shared_states.items()):
            # Only add each stable resource once; short aliases point at the same
            # resource.
            if state.identity in accumulator.nodes:
                continue
            owner_name = qualified_name.rpartition(".")[0]
            owner = repository.symbols_by_path[path].get(
                owner_name, index.file_resource
            )
            accumulator.node(
                state, public=not state.identifier.startswith("_"), test=is_test
            )
            accumulator.edge(
                owner,
                state,
                DependencyRelation.DEFINES,
                resolution=DependencyResolution.INTERNAL,
            )
            if not state.identifier.startswith("_") and not is_test:
                accumulator.edge(
                    owner,
                    state,
                    DependencyRelation.PUBLIC_API,
                    resolution=DependencyResolution.INTERNAL,
                )

        collector = _PythonDependencyCollector(
            repository=repository,
            path=path,
            accumulator=accumulator,
            shared_states=shared_states,
        )
        collector.visit(repository.trees[path])

    return SemanticDependencyGraph(
        nodes=tuple(accumulator.nodes.values()),
        edges=tuple(accumulator.edges.values()),
        source_digests={
            path: hashlib.sha256(source.encode("utf-8")).hexdigest()
            for path, source in repository.sources.items()
        },
        metadata={
            "language": "python",
            "source_count": len(repository.sources),
            "internal_resolution": "repository-local lexical/import resolution",
        },
    )


def build_python_repository_dependency_graph(
    repository_root: str | Path,
    *,
    paths: Sequence[str | Path] | None = None,
) -> SemanticDependencyGraph:
    """Read Python sources under ``repository_root`` and build Dependency Graph v2."""

    root = Path(repository_root).resolve()
    selected: Iterable[Path]
    if paths is None:
        selected = (
            path
            for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix in {".py", ".pyi"}
            and not any(
                part in _EXCLUDED_DIRECTORY_NAMES
                for part in path.relative_to(root).parts
            )
        )
    else:
        resolved: list[Path] = []
        for raw in paths:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    "Python source must be inside repository_root"
                ) from exc
            if candidate.suffix not in {".py", ".pyi"}:
                raise ValueError("Python dependency graph requires .py or .pyi source")
            resolved.append(candidate)
        selected = resolved

    sources: dict[str, str] = {}
    for target in sorted(selected):
        relative = target.relative_to(root).as_posix()
        try:
            with tokenize.open(target) as handle:
                sources[relative] = handle.read()
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise PythonStructuralExtractionError(str(exc), path=relative) from exc
    return build_python_dependency_graph(sources)
