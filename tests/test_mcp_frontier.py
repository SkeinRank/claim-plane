from __future__ import annotations

import json
from pathlib import Path

from claim_plane.mcp.server import McpServer, McpServerConfig


def _call(server: McpServer, name: str, arguments: dict, request_id: int = 1):
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    assert "error" not in response
    return json.loads(response["result"]["content"][0]["text"])


def test_mcp_admit_context_and_route(tmp_path: Path):
    server = McpServer(McpServerConfig(db_path=str(tmp_path / "plane.db")))
    payload = {
        "intent_id": "docs",
        "task_id": "docs",
        "owner": "agent-docs",
        "base_revision": "main",
        "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "operations": [
            {"access": "document", "kind": "document", "identifier": "docs/**"}
        ],
        "acceptance": ["markdownlint docs"],
        "preserves": ["public terminology"],
    }
    decision = _call(server, "admit_change_intent", {"intent": payload})
    assert decision["allowed"] is True
    context = _call(server, "get_worker_context", {"intent_id": "docs"}, 2)
    assert context["intent_id"] == "docs"
    route = _call(server, "recommend_worker_tier", {"intent_id": "docs"}, 3)
    assert route["tier"] in {"economy", "standard", "frontier"}
    graph = _call(server, "get_dependency_graph", {}, 4)
    assert graph["acyclic"] is True
    assert "docs" in graph["topological_order"]
