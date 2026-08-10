from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from claim_plane import (
    SEMANTIC_DEPENDENCY_GRAPH_PROTOCOL,
    DependencyRelation,
    DependencyResolution,
    SemanticDependencyGraph,
    build_python_dependency_graph,
    build_python_repository_dependency_graph,
)
from claim_plane.core.python_structure import PythonStructuralExtractionError


def _fixture_sources() -> dict[str, str]:
    return {
        "src/demo/__init__.py": "from .models import User\n",
        "src/demo/models.py": dedent(
            """
            class User:
                name: str

                @classmethod
                def parse(cls, value: str) -> "User":
                    return cls()

            class Admin(User):
                pass
            """
        ).lstrip(),
        "src/demo/service.py": dedent(
            """
            from .models import User

            CACHE: dict[str, User] = {}

            def normalize(user: User) -> User:
                return user

            def parse_user(value: str) -> User:
                return User.parse(value)

            class Service:
                def remember(self, user: User) -> User:
                    self.last = user
                    CACHE[user.name] = user
                    return normalize(user)

                def current(self) -> User | None:
                    return self.last
            """
        ).lstrip(),
        "tests/test_service.py": dedent(
            """
            from demo.service import Service

            def test_remember():
                service = Service()
                assert service.remember(None) is None
            """
        ).lstrip(),
    }


def _edges(graph: SemanticDependencyGraph, relation: DependencyRelation):
    return [edge for edge in graph.edges if edge.relation is relation]


def test_dependency_graph_v2_builds_repository_relationships() -> None:
    graph = build_python_dependency_graph(_fixture_sources())

    assert graph.protocol == SEMANTIC_DEPENDENCY_GRAPH_PROTOCOL
    assert graph.metadata["language"] == "python"
    assert len(graph.source_digests) == 4

    identities = {node.identity for node in graph.nodes}
    assert "file:src/demo/service.py" in identities
    assert "symbol:src/demo/service.py#Service.remember" in identities
    assert "symbol:src/demo/service.py#CACHE" in identities
    assert "symbol:src/demo/models.py#User" in identities

    imports = _edges(graph, DependencyRelation.IMPORTS)
    assert any(
        edge.source_identity == "file:src/demo/service.py"
        and edge.target_identity == "symbol:src/demo/models.py#User"
        and edge.resolution is DependencyResolution.INTERNAL
        for edge in imports
    )

    calls = _edges(graph, DependencyRelation.CALLS)
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#Service.remember"
        and edge.target_identity == "symbol:src/demo/service.py#normalize"
        for edge in calls
    )
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#parse_user"
        and edge.target_identity == "symbol:src/demo/models.py#User.parse"
        and edge.resolution is DependencyResolution.INTERNAL
        for edge in calls
    )

    inherits = _edges(graph, DependencyRelation.INHERITS)
    assert any(
        edge.source_identity == "symbol:src/demo/models.py#Admin"
        and edge.target_identity == "symbol:src/demo/models.py#User"
        for edge in inherits
    )

    typed = _edges(graph, DependencyRelation.TYPES)
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#Service.remember"
        and edge.target_identity == "symbol:src/demo/models.py#User"
        for edge in typed
    )

    writes = _edges(graph, DependencyRelation.WRITES)
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#Service.remember"
        and edge.target_identity == "symbol:src/demo/service.py#Service.last"
        for edge in writes
    )
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#Service.remember"
        and edge.target_identity == "symbol:src/demo/service.py#CACHE"
        for edge in writes
    )

    reads = _edges(graph, DependencyRelation.READS)
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#Service.current"
        and edge.target_identity == "symbol:src/demo/service.py#Service.last"
        for edge in reads
    )


def test_dependency_graph_marks_public_api_and_test_relationships() -> None:
    graph = build_python_dependency_graph(_fixture_sources())

    public_api = _edges(graph, DependencyRelation.PUBLIC_API)
    assert any(
        edge.source_identity == "file:src/demo/service.py"
        and edge.target_identity == "symbol:src/demo/service.py#Service"
        for edge in public_api
    )
    assert any(
        edge.source_identity == "symbol:src/demo/service.py#Service"
        and edge.target_identity == "symbol:src/demo/service.py#Service.remember"
        for edge in public_api
    )

    tests = _edges(graph, DependencyRelation.TESTS)
    assert any(
        edge.source_identity == "file:tests/test_service.py"
        and edge.target_identity == "symbol:src/demo/service.py#Service"
        for edge in tests
    )


def test_dependency_graph_queries_dependents_transitively() -> None:
    graph = build_python_dependency_graph(_fixture_sources())

    direct = graph.dependents(
        "symbol:src/demo/models.py#User",
        relations={DependencyRelation.TYPES, DependencyRelation.INHERITS},
    )
    direct_ids = {node.identity for node in direct}
    assert "symbol:src/demo/models.py#Admin" in direct_ids
    assert "symbol:src/demo/service.py#Service.remember" in direct_ids

    dependencies = graph.dependencies(
        "symbol:src/demo/service.py#Service.remember",
        relations={DependencyRelation.CALLS, DependencyRelation.TYPES},
    )
    dependency_ids = {node.identity for node in dependencies}
    assert "symbol:src/demo/service.py#normalize" in dependency_ids
    assert "symbol:src/demo/models.py#User" in dependency_ids


def test_dependency_graph_serialization_is_deterministic() -> None:
    first = build_python_dependency_graph(_fixture_sources())
    second = build_python_dependency_graph(
        dict(reversed(list(_fixture_sources().items())))
    )

    assert first.fingerprint == second.fingerprint
    assert first.to_dict() == second.to_dict()
    restored = SemanticDependencyGraph.from_dict(
        json.loads(json.dumps(first.to_dict()))
    )
    assert restored.fingerprint == first.fingerprint
    assert restored == first


def test_external_and_unresolved_targets_are_explicit() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                import json

                def run(value):
                    return json.loads(value) + missing(value)
                """
            ).lstrip()
        }
    )

    assert any(
        edge.relation is DependencyRelation.IMPORTS
        and edge.target_identity == "symbol:json"
        and edge.resolution is DependencyResolution.EXTERNAL
        for edge in graph.edges
    )
    assert any(
        edge.relation is DependencyRelation.CALLS
        and edge.target_identity == "symbol:json.loads"
        and edge.resolution is DependencyResolution.EXTERNAL
        for edge in graph.edges
    )
    assert any(
        edge.relation is DependencyRelation.CALLS
        and edge.target_identity == "symbol:missing"
        and edge.resolution is DependencyResolution.UNRESOLVED
        for edge in graph.edges
    )

    typed_graph = build_python_dependency_graph(
        {"typed.py": "def render(value: str) -> str:\n    return value\n"}
    )
    assert any(
        edge.relation is DependencyRelation.TYPES
        and edge.target_identity == "symbol:builtins.str"
        and edge.resolution is DependencyResolution.EXTERNAL
        for edge in typed_graph.edges
    )


def test_repository_builder_does_not_execute_source_and_respects_root(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed.txt"
    source = tmp_path / "app.py"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )

    graph = build_python_repository_dependency_graph(tmp_path)
    assert graph.node("file:app.py") is not None
    assert not marker.exists()

    outside = tmp_path.parent / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside repository_root"):
        build_python_repository_dependency_graph(tmp_path, paths=[outside])


def test_dependency_graph_fails_closed_on_syntax_error() -> None:
    with pytest.raises(PythonStructuralExtractionError):
        build_python_dependency_graph({"broken.py": "def broken(:\n    pass\n"})
