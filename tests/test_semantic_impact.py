from __future__ import annotations

import json
from textwrap import dedent

from claim_plane import (
    SEMANTIC_IMPACT_PROTOCOL,
    DependencyRelation,
    DependencyResolution,
    SemanticChange,
    SemanticChangeKind,
    SemanticImpactReport,
    analyze_graph_change_impact,
    analyze_semantic_impact,
    build_python_dependency_graph,
    compare_semantic_graphs,
    project_contract_resource,
)


def _before_sources() -> dict[str, str]:
    return {
        "src/demo/models.py": dedent(
            """
            class User:
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

            def parse_user(value: str) -> User:
                return User.parse(value)

            def remember(user: User) -> User:
                CACHE["user"] = user
                return user
            """
        ).lstrip(),
        "tests/test_service.py": dedent(
            """
            from demo.service import parse_user

            def test_parse_user():
                assert parse_user("x") is not None
            """
        ).lstrip(),
    }


def _after_sources() -> dict[str, str]:
    result = _before_sources()
    result["src/demo/models.py"] = result["src/demo/models.py"].replace(
        'def parse(cls, value: str) -> "User":',
        'def parse(cls, value: str, *, strict: bool = False) -> "User":',
    )
    return result


def test_compare_graphs_detects_contract_change_with_stable_identity() -> None:
    before = build_python_dependency_graph(_before_sources())
    after = build_python_dependency_graph(_after_sources())

    changes = compare_semantic_graphs(before, after)
    change = next(
        item
        for item in changes
        if item.identity == "symbol:src/demo/models.py#User.parse"
    )

    assert change.kind is SemanticChangeKind.CONTRACT
    assert change.before_resource is not None
    assert change.after_resource is not None
    assert change.before_resource.identity == change.after_resource.identity
    assert change.before_resource.signature != change.after_resource.signature

    contract = change.contract_resource
    assert contract is not None
    assert contract.identity == "contract:src/demo/models.py#User.parse"
    assert contract.parent_identity == change.identity
    assert contract.signature == change.after_resource.signature


def test_contract_change_propagates_to_callers_and_tests() -> None:
    before = build_python_dependency_graph(_before_sources())
    after = build_python_dependency_graph(_after_sources())

    report = analyze_graph_change_impact(before, after)

    caller = report.impacted_resource("symbol:src/demo/service.py#parse_user")
    assert caller is not None
    assert caller.contract_sensitive is True
    assert DependencyRelation.CALLS in caller.relations
    assert caller.min_distance == 1

    test_file = report.impacted_resource("file:tests/test_service.py")
    assert test_file is not None
    assert test_file.node.test is True
    assert test_file.contract_sensitive is True
    assert test_file.min_distance == 2
    path = test_file.paths[0]
    assert path.identities == (
        "symbol:src/demo/models.py#User.parse",
        "symbol:src/demo/service.py#parse_user",
        "file:tests/test_service.py",
    )
    assert path.relations == (
        DependencyRelation.CALLS,
        DependencyRelation.TESTS,
    )


def test_contract_change_on_type_propagates_to_type_users_and_subclasses() -> None:
    graph = build_python_dependency_graph(_before_sources())
    user = graph.node("symbol:src/demo/models.py#User")
    assert user is not None
    change = SemanticChange(
        identity=user.identity,
        kind=SemanticChangeKind.CONTRACT,
        before_resource=user.resource,
        after_resource=user.resource,
    )

    report = analyze_semantic_impact(graph, (change,))

    admin = report.impacted_resource("symbol:src/demo/models.py#Admin")
    remember = report.impacted_resource("symbol:src/demo/service.py#remember")
    assert admin is not None
    assert remember is not None
    assert DependencyRelation.INHERITS in admin.relations
    assert DependencyRelation.TYPES in remember.relations
    assert admin.contract_sensitive is True
    assert remember.contract_sensitive is True


def test_implementation_change_uses_narrower_propagation_surface() -> None:
    graph = build_python_dependency_graph(_before_sources())
    user = graph.node("symbol:src/demo/models.py#User")
    assert user is not None

    report = analyze_semantic_impact(
        graph,
        (
            SemanticChange(
                identity=user.identity,
                kind=SemanticChangeKind.IMPLEMENTATION,
                before_resource=user.resource,
                after_resource=user.resource,
            ),
        ),
    )

    assert report.impacted_resource("symbol:src/demo/models.py#Admin") is None
    assert report.impacted_resource("symbol:src/demo/service.py#remember") is None
    assert report.impacted_resource(user.identity) is not None


def test_explicit_mutation_detects_implementation_change_when_graph_shape_is_same() -> (
    None
):
    before = build_python_dependency_graph(
        {"app.py": "def answer() -> int:\n    return 1\n"}
    )
    after = build_python_dependency_graph(
        {"app.py": "def answer() -> int:\n    return 2\n"}
    )
    identity = "symbol:app.py#answer"

    assert compare_semantic_graphs(before, after) == ()
    changes = compare_semantic_graphs(before, after, changed_identities=(identity,))
    assert len(changes) == 1
    assert changes[0].identity == identity
    assert changes[0].kind is SemanticChangeKind.IMPLEMENTATION
    assert changes[0].metadata["explicit_mutation"] is True


def test_state_change_propagates_to_readers_and_writers() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                CACHE = {}

                def read_cache():
                    return CACHE

                def write_cache(value):
                    CACHE["x"] = value
                """
            ).lstrip()
        }
    )
    state = graph.node("symbol:app.py#CACHE")
    assert state is not None

    report = analyze_semantic_impact(
        graph,
        (
            SemanticChange(
                identity=state.identity,
                kind=SemanticChangeKind.STATE,
                before_resource=state.resource,
                after_resource=state.resource,
            ),
        ),
    )

    reader = report.impacted_resource("symbol:app.py#read_cache")
    writer = report.impacted_resource("symbol:app.py#write_cache")
    assert reader is not None
    assert writer is not None
    assert DependencyRelation.READS in reader.relations
    assert DependencyRelation.WRITES in writer.relations


def test_external_and_unresolved_boundaries_remain_visible() -> None:
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
    run = graph.node("symbol:app.py#run")
    assert run is not None

    report = analyze_semantic_impact(
        graph,
        (
            SemanticChange(
                identity=run.identity,
                kind=SemanticChangeKind.CONTRACT,
                before_resource=run.resource,
                after_resource=run.resource,
            ),
        ),
    )

    assert any(
        item.target_identity == "symbol:json.loads"
        and item.resolution is DependencyResolution.EXTERNAL
        for item in report.boundaries
    )
    assert any(
        item.target_identity == "symbol:missing"
        and item.resolution is DependencyResolution.UNRESOLVED
        for item in report.boundaries
    )


def test_contract_projection_requires_signed_symbol() -> None:
    graph = build_python_dependency_graph({"app.py": "VALUE = 1\n"})
    state = graph.node("symbol:app.py#VALUE")
    assert state is not None
    assert project_contract_resource(state.resource) is None


def test_semantic_impact_roundtrip_and_fingerprint_are_deterministic() -> None:
    before = build_python_dependency_graph(_before_sources())
    after = build_python_dependency_graph(_after_sources())
    first = analyze_graph_change_impact(before, after)
    second = analyze_graph_change_impact(before, after)

    assert first.protocol == SEMANTIC_IMPACT_PROTOCOL
    assert first.fingerprint == second.fingerprint
    assert first.to_dict() == second.to_dict()
    restored = SemanticImpactReport.from_dict(json.loads(json.dumps(first.to_dict())))
    assert restored == first
    assert restored.fingerprint == first.fingerprint
    assert restored.test_impacts
    assert restored.public_impacts
