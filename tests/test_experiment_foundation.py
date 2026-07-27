from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.cooperbench.common import (
    Arm,
    Checkpoint,
    CheckpointStore,
    PairRef,
    ShardSpec,
    StudySpec,
    build_run_identity,
    create_run,
)
from experiments.cooperbench.common.identity import study_fingerprint


def _study() -> StudySpec:
    return StudySpec(
        study_id="paper-six-pair",
        description="Stable test declaration",
        claim_plane_version="0.4.0",
        planner_policy_version="planner-v1",
        planner_model="provider/planner",
        coder_model="provider/coder",
        pair_selection_seed=42,
        coder_seeds=(101, 202, 303),
        arms=(
            Arm.PARALLEL,
            Arm.CLAIM_PLANE_STATIC,
            Arm.CLAIM_PLANE_DYNAMIC,
            Arm.ALWAYS_SERIAL,
        ),
        pairs=(
            PairRef("repo-a", 1, 1, 2, True),
            PairRef("repo-b", 2, 3, 4, False),
            PairRef("repo-c", 3, 5, 6, None),
        ),
        metadata={"purpose": "test"},
    )


def test_study_round_trip_and_fingerprint_are_deterministic() -> None:
    study = _study()
    restored = StudySpec.from_dict(study.to_dict())

    assert restored == study
    assert study_fingerprint(restored) == study_fingerprint(study)
    assert len(study_fingerprint(study)) == 64


def test_pair_key_is_order_independent_but_duplicate_features_are_rejected() -> None:
    left = PairRef("repo", 7, 3, 9)
    right = PairRef("repo", 7, 9, 3)

    assert left.key == right.key
    with pytest.raises(ValueError, match="distinct"):
        PairRef("repo", 7, 3, 3)


def test_shards_partition_frozen_pair_order_without_overlap() -> None:
    pairs = _study().pairs
    first = ShardSpec(1, 2).select(pairs)
    second = ShardSpec(2, 2).select(pairs)

    assert [pair.key for pair in first] == [pairs[0].key, pairs[2].key]
    assert [pair.key for pair in second] == [pairs[1].key]
    assert set(first).isdisjoint(second)
    assert set(first) | set(second) == set(pairs)


def test_run_identity_is_stable_for_same_study_seed_and_shard() -> None:
    study = _study()
    shard = ShardSpec(2, 3)

    first = build_run_identity(study, coder_seed=202, shard=shard)
    second = build_run_identity(study, coder_seed=202, shard=shard)

    assert first == second
    assert "seed-202" in first.run_id
    assert "shard-02-of-03" in first.run_id


def test_unknown_seed_is_rejected() -> None:
    with pytest.raises(ValueError, match="not declared"):
        build_run_identity(_study(), coder_seed=999)


def test_checkpoint_store_is_resumable(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.json")
    checkpoint = Checkpoint("run-1").mark_completed("pair-a/parallel")
    checkpoint = checkpoint.mark_failed("pair-b/parallel", "boom")
    store.save(checkpoint)

    restored = store.load()
    assert restored.completed_units == ("pair-a/parallel",)
    assert restored.failed_units == {"pair-b/parallel": "boom"}
    assert restored.state == "running"


def test_create_run_writes_non_secret_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")
    study = _study()

    run, layout = create_run(
        study,
        coder_seed=101,
        artifact_root=tmp_path / "artifacts",
        repo_root=tmp_path,
    )

    assert layout.run_dir.exists()
    assert layout.checkpoint_file.exists()
    assert (layout.study_dir / "study.json").exists()
    assert (layout.study_dir / "pairs.json").exists()

    manifest = json.loads(layout.manifest_file.read_text(encoding="utf-8"))
    encoded = json.dumps(manifest)
    assert manifest["run"]["run_id"] == run.run_id
    assert "secret-value" not in encoded
    assert "OPENROUTER_API_KEY" not in manifest["environment_keys"]
