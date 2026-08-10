from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.cooperbench.common.identity import study_fingerprint
from experiments.cooperbench.confirmatory_30x3.config import ConfirmatoryPaths, build_study
from experiments.cooperbench.confirmatory_30x3.final import (
    ConfirmatoryMode,
    DETERMINISTIC_CONFIRMATORY_PROTOCOL,
    _pair_dir,
    build_confirmatory_report,
    confirmatory_status,
    parse_coder_seeds,
    parse_confirmatory_modes,
)
from experiments.cooperbench.common.models import PairRef


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _study_paths(tmp_path: Path) -> tuple[ConfirmatoryPaths, object]:
    pairs = tuple(
        PairRef(
            "pallets_jinja_task",
            index,
            1,
            2,
            True if index < 15 else False,
        )
        for index in range(30)
    )
    study = build_study(pairs)
    paths = ConfirmatoryPaths.from_values(
        tmp_path / "cooperbench",
        artifact_root=tmp_path / "artifacts",
        repo_cache=tmp_path / "repos",
        workspace_root=tmp_path / "worktrees",
    )
    _write_json(paths.study_file, study.to_dict())
    _write_json(paths.frozen_plan_manifest_file, {"study_fingerprint": study_fingerprint(study)})
    return paths, study


def test_confirmatory_mode_and_seed_parsers_are_strict() -> None:
    assert parse_confirmatory_modes(
        "deterministic_v2,always_serial,deterministic_v2"
    ) == (ConfirmatoryMode.DETERMINISTIC_V2, ConfirmatoryMode.ALWAYS_SERIAL)
    assert parse_coder_seeds("101,303,101") == (101, 303)
    with pytest.raises(ValueError):
        parse_confirmatory_modes("")
    with pytest.raises(ValueError):
        parse_confirmatory_modes("unknown")
    with pytest.raises(ValueError):
        parse_coder_seeds("999")


def test_confirmatory_report_pairs_wall_clock_against_serial(tmp_path: Path) -> None:
    paths, study = _study_paths(tmp_path)
    fingerprint = study_fingerprint(study)
    modes = (
        ConfirmatoryMode.NAIVE_PARALLEL,
        ConfirmatoryMode.LEGACY_STATIC,
        ConfirmatoryMode.DETERMINISTIC_V2,
        ConfirmatoryMode.ALWAYS_SERIAL,
    )
    mode_key = "+".join(sorted(mode.value for mode in modes))
    result_name = f"result-{hashlib.sha256(mode_key.encode()).hexdigest()[:12]}.json"
    pair = study.pairs[0]

    def row(
        mode: ConfirmatoryMode,
        *,
        wall: float,
        serialized: bool,
        pair_pass: bool,
        integration_success: bool,
        physical: bool,
    ) -> dict[str, object]:
        return {
            "pair": pair.key,
            "coder_seed": 101,
            "confirmatory_mode": mode.value,
            "confirmatory_wall_time_seconds": wall,
            "serialized": serialized,
            "pair_pass": pair_pass,
            "integration_success": integration_success,
            "physical_concurrency_observed": physical,
            "physical_overlap_seconds": 2.0 if physical else 0.0,
            "physical_overlap_fraction_of_shorter": 0.5 if physical else 0.0,
            "coder_cost": 0.1,
        }

    rows = [
        row(
            ConfirmatoryMode.NAIVE_PARALLEL,
            wall=5.0,
            serialized=False,
            pair_pass=False,
            integration_success=False,
            physical=True,
        ),
        row(
            ConfirmatoryMode.LEGACY_STATIC,
            wall=9.0,
            serialized=True,
            pair_pass=True,
            integration_success=True,
            physical=False,
        ),
        row(
            ConfirmatoryMode.DETERMINISTIC_V2,
            wall=6.0,
            serialized=False,
            pair_pass=True,
            integration_success=True,
            physical=True,
        ),
        row(
            ConfirmatoryMode.ALWAYS_SERIAL,
            wall=10.0,
            serialized=True,
            pair_pass=True,
            integration_success=True,
            physical=False,
        ),
    ]
    path = _pair_dir(paths, fingerprint=fingerprint, coder_seed=101, pair_index=1) / result_name
    _write_json(
        path,
        {
            "protocol": DETERMINISTIC_CONFIRMATORY_PROTOCOL,
            "complete": True,
            "rows": rows,
        },
    )

    report = build_confirmatory_report(
        paths,
        seeds=(101,),
        pair_indexes=(1,),
        modes=modes,
        require_complete=True,
    )

    assert report["complete"] is True
    assert report["observed_rows"] == 4
    assert report["paired_speedup_vs_serial"]["deterministic_v2"][
        "mean_speedup_vs_serial"
    ] == pytest.approx(10 / 6)
    delta = report["deterministic_v2_vs_legacy_static"]
    assert delta["serialization_rate_delta_v2_minus_legacy"] == pytest.approx(-1.0)
    assert delta["pair_pass_rate_delta_v2_minus_legacy"] == pytest.approx(0.0)
    summaries = {item["mode"]: item for item in report["mode_summary"]}
    assert summaries["deterministic_v2"]["physical_concurrency_rate"] == pytest.approx(1.0)


def test_confirmatory_report_refuses_incomplete_matrix(tmp_path: Path) -> None:
    paths, _study = _study_paths(tmp_path)
    with pytest.raises(RuntimeError, match="incomplete"):
        build_confirmatory_report(
            paths,
            seeds=(101,),
            pair_indexes=(1,),
            require_complete=True,
        )


def test_confirmatory_status_is_offline_before_prepare(tmp_path: Path) -> None:
    paths = ConfirmatoryPaths.from_values(
        tmp_path / "cooperbench",
        artifact_root=tmp_path / "artifacts",
        repo_cache=tmp_path / "repos",
        workspace_root=tmp_path / "worktrees",
    )
    status = confirmatory_status(paths)
    assert status["prepared"] is False
    assert status["complete"] is False
    assert status["expected_pair_units"] == 90
