from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from experiments.cooperbench.cli import main as experiment_main
from experiments.cooperbench.planner_v1 import (
    CompletionResult,
    PLANNER_MODEL,
    PLANNER_POLICY_FINGERPRINT,
    PlannerExecutionError,
    PlannerV1,
    normalize_plan,
    plan_fingerprint,
)
from experiments.cooperbench.planner_v1.tools import (
    build_uncertainty_candidates_v2,
    read_context,
)


class FakeProvider:
    def __init__(self, contents: Sequence[str | Exception]) -> None:
        self.contents = list(contents)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        seed: int,
        max_tokens: int,
        role: str,
        phase: str,
    ) -> CompletionResult:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "seed": seed,
                "max_tokens": max_tokens,
                "role": role,
                "phase": phase,
            }
        )
        if not self.contents:
            raise RuntimeError("unexpected provider call")
        item = self.contents.pop(0)
        if isinstance(item, Exception):
            raise item
        return CompletionResult(
            content=item,
            cost=0.01,
            latency_seconds=0.25,
            cached=False,
            finish_reason="stop",
            model=model,
            role=role,
            phase=phase,
        )


def _feature_fixture(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "tree"
    feature = tmp_path / "feature1"
    (tree / "pkg").mkdir(parents=True)
    feature.mkdir()

    (tree / "pkg" / "mod.py").write_text(
        "import os\n\n"
        "REGISTRY = {}\n\n"
        "def target(value):\n"
        "    result = value + 1\n"
        "    return result\n\n"
        "def helper():\n"
        "    return target(1)\n",
        encoding="utf-8",
    )
    (feature / "feature.md").write_text(
        "Update target behavior while preserving helper registration.",
        encoding="utf-8",
    )
    (feature / "feature.patch").write_text(
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -5,3 +5,3 @@\n"
        " def target(value):\n"
        "-    result = value + 1\n"
        "+    result = value + 2\n"
        "     return result\n",
        encoding="utf-8",
    )
    return tree, feature


def test_context_uses_gold_only_for_localization(tmp_path: Path) -> None:
    tree, feature = _feature_fixture(tmp_path)
    context = read_context(tree, feature)

    assert "pkg/mod.py" in context
    assert "result = value + 1" in context
    assert "result = value + 2" not in context


def test_candidate_builder_is_deterministic_and_bounded(tmp_path: Path) -> None:
    tree, feature = _feature_fixture(tmp_path)
    primary = {
        "files": [
            {
                "path": "pkg/mod.py",
                "action": "modify",
                "line_start": 5,
                "line_end": 7,
                "commitment": "committed",
                "what": "update target",
            }
        ]
    }
    task = (feature / "feature.md").read_text(encoding="utf-8")

    first = build_uncertainty_candidates_v2(tree, task, primary)
    second = build_uncertainty_candidates_v2(tree, task, primary)

    assert first == second
    assert first
    assert [item["candidate_id"] for item in first] == [
        f"C{index:03d}" for index in range(1, len(first) + 1)
    ]
    assert all(item["path"] == "pkg/mod.py" for item in first)
    assert all(item["line_start"] > 0 for item in first)
    assert all(item["line_end"] >= item["line_start"] for item in first)


def test_planner_v1_primary_and_calibration_seeds_are_frozen(tmp_path: Path) -> None:
    tree, feature = _feature_fixture(tmp_path)
    provider = FakeProvider(
        [
            json.dumps(
                {
                    "files": [
                        {
                            "path": "pkg/mod.py",
                            "action": "modify",
                            "line_start": 5,
                            "line_end": 7,
                            "commitment": "committed",
                            "what": "update target",
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "downgrade_item_indices": [],
                    "selected_candidate_ids": [],
                    "reasoning_summary": "automatic bounded candidates are sufficient",
                }
            ),
        ]
    )
    planner = PlannerV1(provider)

    result = planner.get_calibrated_plan(tree, feature, seed=101)

    assert result["valid"] is True
    assert result["calibration_valid"] is True
    assert result["calibration_applied"] is True
    assert result["logical_cost"] == pytest.approx(0.02)
    assert provider.calls[0]["model"] == PLANNER_MODEL
    assert provider.calls[0]["seed"] == 101
    assert provider.calls[0]["phase"] == "plan"
    assert provider.calls[1]["seed"] == 80_102
    assert provider.calls[1]["phase"] == "uncertainty_calibration_v2"
    assert result["calibration_candidate_count"] > 0
    assert any(
        item.get("commitment") == "contingent" for item in result["plan"]["files"]
    )


def test_shared_plan_cache_reuses_identical_calibrated_result(tmp_path: Path) -> None:
    tree, feature = _feature_fixture(tmp_path)
    provider = FakeProvider(
        [
            '{"files":[{"path":"pkg/mod.py","action":"modify",'
            '"line_start":5,"line_end":7,"commitment":"committed"}]}',
            '{"downgrade_item_indices":[],"selected_candidate_ids":[]}',
        ]
    )
    planner = PlannerV1(provider)

    first = planner.get_shared_calibrated_plan("same", tree, feature, seed=101)
    second = planner.get_shared_calibrated_plan("same", tree, feature, seed=101)

    assert len(provider.calls) == 2
    assert first["plan"] == second["plan"]
    assert first["shared_plan_cache_hit"] is False
    assert second["shared_plan_cache_hit"] is True


def test_provider_failure_is_not_misclassified_as_invalid_declaration(
    tmp_path: Path,
) -> None:
    tree, feature = _feature_fixture(tmp_path)
    provider = FakeProvider(
        [
            RuntimeError("provider down"),
            RuntimeError("provider down"),
            RuntimeError("provider down"),
        ]
    )

    with pytest.raises(PlannerExecutionError, match="before producing"):
        PlannerV1(provider).get_plan(tree, feature, seed=101)

    assert len(provider.calls) == 3


def test_plan_normalization_and_fingerprint_are_order_insensitive() -> None:
    plan = normalize_plan(
        {
            "files": [
                {"path": "b.py", "line_start": 8, "line_end": 4},
                {
                    "path": "a.py",
                    "line_start": 1,
                    "line_end": 2,
                    "commitment": "CONTINGENT",
                },
            ]
        }
    )
    reversed_plan = {"files": list(reversed(plan["files"]))}

    assert plan["files"][0]["commitment"] == "committed"
    assert plan["files"][1]["commitment"] == "contingent"
    assert plan_fingerprint(plan) == plan_fingerprint(reversed_plan)


def test_planner_policy_cli_is_model_free(capsys: pytest.CaptureFixture[str]) -> None:
    assert experiment_main(["planner", "policy"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["planner_policy_version"] == "planner-v1"
    assert payload["planner_model"] == PLANNER_MODEL
    assert payload["planner_policy_fingerprint"] == PLANNER_POLICY_FINGERPRINT
    assert len(payload["planner_policy_fingerprint"]) == 64


def test_plan_adapter_preserves_dynamic_scope_and_static_override() -> None:
    from experiments.cooperbench.planner_v1 import plan_to_intent

    plan = {
        "files": [
            {
                "path": "pkg/mod.py",
                "action": "modify",
                "line_start": 5,
                "line_end": 7,
                "commitment": "contingent",
            }
        ]
    }

    dynamic = plan_to_intent("A", "agent-a", plan)
    static = plan_to_intent("A", "agent-a", plan, force_all_committed=True)

    assert dynamic is not None
    assert static is not None
    assert dynamic.operations[0].commitment.value == "contingent"
    assert static.operations[0].commitment.value == "committed"
