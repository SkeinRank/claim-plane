from __future__ import annotations

import hashlib

import pytest

from claim_plane import (
    DependencyEdge,
    DependencyEvidence,
    DependencyRelation,
    SemanticDependencyGraph,
    SemanticGraphRevisionCache,
    SemanticGraphSnapshot,
    StaleSemanticGraphError,
    assert_semantic_graph_fresh,
    plan_semantic_graph_invalidation,
    refresh_python_dependency_graph_incrementally,
)


def _revision(char: str) -> str:
    return char * 40


def _digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _node_payloads_for_path(graph: SemanticDependencyGraph, path: str) -> list[dict]:
    return [node.to_dict() for node in graph.nodes if node.resource.path == path]


def test_incremental_refresh_rebuilds_only_dependency_component() -> None:
    first_sources = {
        "a.py": "from b import value\n\ndef read():\n    return value()\n",
        "b.py": "def value():\n    return 1\n",
        "c.py": "def untouched():\n    return 3\n",
    }
    previous, _ = refresh_python_dependency_graph_incrementally(
        None, first_sources, revision=_revision("a")
    )
    untouched_before = _node_payloads_for_path(previous, "c.py")

    second_sources = dict(first_sources)
    second_sources["b.py"] = "def value():\n    return 2\n"
    refreshed, plan = refresh_python_dependency_graph_incrementally(
        previous, second_sources, revision=_revision("b")
    )

    assert plan is not None
    assert plan.full_rebuild is False
    assert plan.changed_paths == ("b.py",)
    assert "a.py" in plan.affected_paths
    assert "b.py" in plan.affected_paths
    assert "c.py" not in plan.affected_paths
    assert refreshed.metadata["refresh_mode"] == "incremental"
    assert refreshed.metadata["source_revision"] == _revision("b")
    assert refreshed.metadata["incremental_retained_node_count"] > 0
    assert _node_payloads_for_path(refreshed, "c.py") == untouched_before
    assert refreshed.source_digests["b.py"] == _digest(second_sources["b.py"])
    assert_semantic_graph_fresh(refreshed, expected_revision=_revision("b"))


def test_added_source_forces_full_rebuild() -> None:
    first_sources = {"a.py": "def read():\n    return 1\n"}
    previous, _ = refresh_python_dependency_graph_incrementally(
        None, first_sources, revision=_revision("a")
    )
    second_sources = {
        **first_sources,
        "new_module.py": "def value():\n    return 2\n",
    }
    plan = plan_semantic_graph_invalidation(
        previous,
        {path: _digest(source) for path, source in second_sources.items()},
    )
    assert plan.full_rebuild is True
    assert plan.added_paths == ("new_module.py",)
    assert "new_source_can_resolve_previous_unknown_edge" in plan.reasons

    refreshed, applied = refresh_python_dependency_graph_incrementally(
        previous, second_sources, revision=_revision("b")
    )
    assert applied is not None and applied.full_rebuild is True
    assert refreshed.metadata["refresh_mode"] == "full"
    assert any(node.resource.path == "new_module.py" for node in refreshed.nodes)


def test_removed_source_and_dependents_are_refreshed_without_stale_nodes() -> None:
    first_sources = {
        "a.py": "from b import value\n\ndef read():\n    return value()\n",
        "b.py": "def value():\n    return 1\n",
        "c.py": "def untouched():\n    return 3\n",
    }
    previous, _ = refresh_python_dependency_graph_incrementally(
        None, first_sources, revision=_revision("a")
    )
    second_sources = {key: value for key, value in first_sources.items() if key != "b.py"}
    refreshed, plan = refresh_python_dependency_graph_incrementally(
        previous, second_sources, revision=_revision("b")
    )

    assert plan is not None
    assert plan.removed_paths == ("b.py",)
    assert plan.full_rebuild is False
    assert not any(node.resource.path == "b.py" for node in refreshed.nodes)
    assert all(
        refreshed.node(edge.source_identity) is not None
        and refreshed.node(edge.target_identity) is not None
        for edge in refreshed.edges
    )
    assert any(node.resource.path == "c.py" for node in refreshed.nodes)



def test_shared_external_dependency_does_not_join_incremental_components() -> None:
    first_sources = {
        "a.py": "import os\n\ndef left():\n    return os.getcwd()\n",
        "c.py": "import os\n\ndef right():\n    return os.getcwd()\n",
    }
    previous, _ = refresh_python_dependency_graph_incrementally(
        None, first_sources, revision=_revision("a")
    )
    second_sources = dict(first_sources)
    second_sources["a.py"] = "import os\n\ndef left():\n    return os.curdir\n"
    refreshed, plan = refresh_python_dependency_graph_incrementally(
        previous, second_sources, revision=_revision("b")
    )

    assert plan is not None and plan.full_rebuild is False
    assert "a.py" in plan.affected_paths
    assert "c.py" not in plan.affected_paths
    assert any(node.resource.path == "c.py" for node in refreshed.nodes)

def test_revision_cache_round_trip_is_source_bound(tmp_path) -> None:
    graph, _ = refresh_python_dependency_graph_incrementally(
        None,
        {"a.py": "def value():\n    return 1\n"},
        revision=_revision("a"),
    )
    repository_identity = "1" * 64
    snapshot = SemanticGraphSnapshot(
        repository_identity=repository_identity,
        revision=_revision("a"),
        graph=graph,
    )
    cache = SemanticGraphRevisionCache(tmp_path)
    cache.store(snapshot)

    loaded = cache.load_exact(repository_identity, _revision("a"))
    assert loaded is not None
    assert loaded.graph.fingerprint == graph.fingerprint
    assert cache.load_exact(repository_identity, _revision("b")) is None
    latest = cache.load_latest(repository_identity)
    assert latest is not None and latest.revision == _revision("a")


def test_stale_dependency_evidence_is_fenced() -> None:
    base, _ = refresh_python_dependency_graph_incrementally(
        None,
        {
            "a.py": "from b import value\n\ndef read():\n    return value()\n",
            "b.py": "def value():\n    return 1\n",
        },
        revision=_revision("b"),
    )
    original = next(
        edge
        for edge in base.edges
        if edge.relation is DependencyRelation.IMPORTS
    )
    stale = DependencyEdge(
        original.source_identity,
        original.target_identity,
        original.relation,
        resolution=original.resolution,
        locations=original.locations,
        evidence=(
            DependencyEvidence(
                provider_id="scip",
                evidence_type="occurrence",
                revision=_revision("a"),
                workspace_fingerprint="f" * 64,
            ),
        ),
    )
    graph = SemanticDependencyGraph(
        nodes=base.nodes,
        edges=tuple(stale if edge.key == original.key else edge for edge in base.edges),
        source_digests=base.source_digests,
        metadata={**dict(base.metadata), "source_revision": _revision("b")},
    )

    with pytest.raises(StaleSemanticGraphError, match="stale dependency evidence"):
        assert_semantic_graph_fresh(graph, expected_revision=_revision("b"))
