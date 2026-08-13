from __future__ import annotations

import json
from pathlib import Path

from experiments.cooperbench.cli import main as experiment_main
from experiments.cooperbench.environment import load_environment_lock


ROOT = Path(__file__).resolve().parents[1]


def test_environment_lock_matches_dockerfile() -> None:
    lock = load_environment_lock()
    dockerfile = (
        ROOT / "experiments" / "cooperbench" / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert lock["environment_id"] == "cooperbench-linux-v3"
    assert lock["python_version"] == "3.12.13"
    assert lock["uv_version"] == "0.11.29"
    assert lock["node_version"] == "20.19.4"
    assert lock["scip_python_version"] == "0.6.6"
    assert lock["base_image"] in dockerfile
    first_from = dockerfile.index("FROM ")
    assert dockerfile.index("ARG BASE_IMAGE=") < first_from
    assert dockerfile.index("ARG NODE_IMAGE=") < first_from
    assert f"ARG PYTHON_VERSION={lock['python_version']}" in dockerfile
    assert f"ARG UV_VERSION={lock['uv_version']}" in dockerfile
    assert f"ARG NODE_VERSION={lock['node_version']}" in dockerfile
    assert f"ARG SCIP_PYTHON_VERSION={lock['scip_python_version']}" in dockerfile
    assert lock["node_image"] in dockerfile
    assert f"@sourcegraph/scip-python@${{SCIP_PYTHON_VERSION}}" in dockerfile


def test_environment_cli_never_prints_secret_value(capsys, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "should-never-appear")

    assert experiment_main(["environment"]) == 0
    payload = json.loads(capsys.readouterr().out)
    encoded = json.dumps(payload)

    assert payload["runtime"]["openrouter_api_key_present"] is True
    assert "should-never-appear" not in encoded
