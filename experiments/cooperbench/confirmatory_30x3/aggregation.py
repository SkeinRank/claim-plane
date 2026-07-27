"""Deterministic publication aggregation for the frozen 30x3 study."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..common import ArtifactLayout, CheckpointStore, ShardSpec, build_run_identity
from ..common.identity import study_fingerprint
from ..common.models import PairRef, StudySpec
from .config import ConfirmatoryPaths, SHARD_COUNT, SHARD_SIZE
from .runner import contiguous_shard, load_confirmatory_study

ANALYSIS_SCHEMA_VERSION = 1
DEFAULT_BOOTSTRAP_SAMPLES = 5_000
DEFAULT_BOOTSTRAP_SEED = 20_260_727
CI_LEVEL = 0.95

_BOOL_METRICS = {
    "pair_pass_rate": "pair_pass",
    "integration_success_rate": "integration_success",
    "initial_serialization_rate": "initial_serialized",
    "effective_serialization_rate": "serialized",
}
_COUNT_METRICS = {
    "promotion_attempts": "scope_promotion_attempts",
    "promotions": "scope_promotions_succeeded",
    "rejected_promotions": "scope_promotions_rejected",
    "undeclared_blocks": "scope_undeclared_blocks",
    "dynamic_restarts": "dynamic_restart_count",
}
_SUM_METRICS = {
    "coder_cost_total": "coder_cost",
    "dynamic_wasted_coder_cost_total": "dynamic_wasted_coder_cost",
}
_MEAN_METRICS = {
    "mean_coder_cost": "coder_cost",
    "mean_logical_system_cost": "logical_system_cost_estimate",
    "mean_logical_critical_path": "logical_llm_critical_path",
}
_FAILURE_FLAGS = (
    "planner_failure",
    "scope_enforcement_failure",
    "agent_execution_failure",
    "harness_failure",
)


@dataclass(frozen=True, slots=True)
class LoadedStudyResults:
    """Validated complete result matrix plus immutable study provenance."""

    study: StudySpec
    rows: tuple[dict[str, Any], ...]
    run_ids: tuple[str, ...]
    study_dir: Path
    frozen_plan_manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AnalysisLayout:
    """Canonical output files for one confirmatory-study analysis."""

    root: Path

    @property
    def results_json(self) -> Path:
        return self.root / "arm_results.json"

    @property
    def results_csv(self) -> Path:
        return self.root / "arm_results.csv"

    @property
    def arm_summary_json(self) -> Path:
        return self.root / "arm_summary.json"

    @property
    def arm_summary_csv(self) -> Path:
        return self.root / "arm_summary.csv"

    @property
    def feature_pair_json(self) -> Path:
        return self.root / "feature_pair_summary.json"

    @property
    def feature_pair_csv(self) -> Path:
        return self.root / "feature_pair_summary.csv"

    @property
    def task_cluster_json(self) -> Path:
        return self.root / "task_cluster_summary.json"

    @property
    def task_cluster_csv(self) -> Path:
        return self.root / "task_cluster_summary.csv"

    @property
    def bootstrap_json(self) -> Path:
        return self.root / "bootstrap_ci.json"

    @property
    def bootstrap_csv(self) -> Path:
        return self.root / "bootstrap_ci.csv"

    @property
    def failure_json(self) -> Path:
        return self.root / "failure_taxonomy.json"

    @property
    def failure_csv(self) -> Path:
        return self.root / "failure_taxonomy.csv"

    @property
    def mechanism_json(self) -> Path:
        return self.root / "mechanism_summary.json"

    @property
    def mechanism_csv(self) -> Path:
        return self.root / "mechanism_summary.csv"

    @property
    def cost_json(self) -> Path:
        return self.root / "cost_summary.json"

    @property
    def manifest_json(self) -> Path:
        return self.root / "publication_manifest.json"

    def payload_files(self) -> tuple[Path, ...]:
        return (
            self.results_json,
            self.results_csv,
            self.arm_summary_json,
            self.arm_summary_csv,
            self.feature_pair_json,
            self.feature_pair_csv,
            self.task_cluster_json,
            self.task_cluster_csv,
            self.bootstrap_json,
            self.bootstrap_csv,
            self.failure_json,
            self.failure_csv,
            self.mechanism_json,
            self.mechanism_csv,
            self.cost_json,
        )


def analysis_layout(paths: ConfirmatoryPaths, study: StudySpec) -> AnalysisLayout:
    fingerprint = study_fingerprint(study)
    root = paths.artifact_root / study.study_id / fingerprint[:12] / "analysis"
    return AnalysisLayout(root=root)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        stream.write(text)
        stream.flush()
        temporary = Path(stream.name)
    temporary.replace(path)


def _atomic_json(path: Path, payload: object) -> None:
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_csv(
    path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})
        stream.flush()
        temporary = Path(stream.name)
    temporary.replace(path)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_object_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _float(row: Mapping[str, Any], field: str) -> float:
    value = row.get(field, 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mean(values: Iterable[float]) -> float | None:
    collected = list(values)
    return sum(collected) / len(collected) if collected else None


def _pair_metadata(study: StudySpec) -> dict[str, PairRef]:
    return {pair.key: pair for pair in study.pairs}


def _result_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("pair", "")),
        int(row.get("coder_seed", -1)),
        str(row.get("arm", "")),
    )


def _expected_keys(study: StudySpec) -> set[tuple[str, int, str]]:
    return {
        (pair.key, seed, arm.value)
        for pair in study.pairs
        for seed in study.coder_seeds
        for arm in study.arms
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected one JSON object: {path}")
    return payload


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(
        isinstance(row, dict) for row in payload
    ):
        raise RuntimeError(f"expected a JSON array of objects: {path}")
    return payload


def load_complete_results(paths: ConfirmatoryPaths) -> LoadedStudyResults:
    """Load all nine shards and reject incomplete or mismatched result matrices."""
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    study_dir = paths.artifact_root / study.study_id / fingerprint[:12]
    if not paths.frozen_plan_manifest_file.exists():
        raise RuntimeError("frozen planner manifest is required before aggregation")
    frozen_manifest = _load_json_object(paths.frozen_plan_manifest_file)
    if frozen_manifest.get("study_fingerprint") != fingerprint:
        raise RuntimeError("frozen planner manifest belongs to a different study")
    frozen_manifest_sha256 = _canonical_object_sha256(frozen_manifest)

    rows: list[dict[str, Any]] = []
    run_ids: list[str] = []
    seen: set[tuple[str, int, str]] = set()

    for seed in study.coder_seeds:
        for shard_index in range(1, SHARD_COUNT + 1):
            run = build_run_identity(
                study,
                coder_seed=seed,
                shard=ShardSpec(shard_index, SHARD_COUNT),
            )
            layout = ArtifactLayout.for_run(paths.artifact_root, run)
            run_ids.append(run.run_id)
            if not layout.checkpoint_file.exists():
                raise RuntimeError(
                    f"missing shard checkpoint: {layout.checkpoint_file}"
                )
            checkpoint = CheckpointStore(layout.checkpoint_file).load()
            expected_units = SHARD_SIZE * len(study.arms)
            if (
                checkpoint.state != "completed"
                or len(checkpoint.completed_units) != expected_units
            ):
                raise RuntimeError(
                    f"shard {run.run_id} is incomplete: state={checkpoint.state}, "
                    f"completed={len(checkpoint.completed_units)}/{expected_units}"
                )

            protocol_file = layout.run_dir / "protocol.json"
            if not protocol_file.exists():
                raise RuntimeError(
                    f"missing shard protocol provenance: {protocol_file}"
                )
            protocol = _load_json_object(protocol_file)
            expected_pairs = [
                pair.key for pair in contiguous_shard(study.pairs, shard_index)
            ]
            if protocol.get("study_fingerprint") != fingerprint:
                raise RuntimeError(f"study fingerprint mismatch in {protocol_file}")
            if int(protocol.get("coder_seed", -1)) != seed:
                raise RuntimeError(f"coder seed mismatch in {protocol_file}")
            if int(protocol.get("shard_index", -1)) != shard_index:
                raise RuntimeError(f"shard index mismatch in {protocol_file}")
            if int(protocol.get("shard_count", -1)) != SHARD_COUNT:
                raise RuntimeError(f"shard count mismatch in {protocol_file}")
            if int(protocol.get("coder_seed_index", -1)) != list(
                study.coder_seeds
            ).index(seed):
                raise RuntimeError(f"coder seed index mismatch in {protocol_file}")
            if protocol.get("pair_keys") != expected_pairs:
                raise RuntimeError(f"pair order mismatch in {protocol_file}")
            if protocol.get("frozen_plan_manifest_sha256") != frozen_manifest_sha256:
                raise RuntimeError(
                    f"frozen planner provenance mismatch in {protocol_file}"
                )

            results_file = layout.run_dir / "results.json"
            if not results_file.exists():
                raise RuntimeError(f"missing shard result matrix: {results_file}")
            shard_rows = _load_json_array(results_file)
            if len(shard_rows) != expected_units:
                raise RuntimeError(
                    f"shard {run.run_id} has {len(shard_rows)} rows; "
                    f"expected {expected_units}"
                )
            expected_pair_set = set(expected_pairs)
            expected_arm_set = {arm.value for arm in study.arms}
            for row in shard_rows:
                key = _result_key(row)
                pair_key, row_seed, arm = key
                if pair_key not in expected_pair_set:
                    raise RuntimeError(
                        f"unexpected pair {pair_key!r} in {results_file}"
                    )
                if row_seed != seed:
                    raise RuntimeError(
                        f"unexpected coder seed {row_seed} in {results_file}"
                    )
                if arm not in expected_arm_set:
                    raise RuntimeError(f"unexpected arm {arm!r} in {results_file}")
                if int(row.get("shard_index", -1)) != shard_index:
                    raise RuntimeError(f"row shard index mismatch in {results_file}")
                if key in seen:
                    raise RuntimeError(f"duplicate result row: {key}")
                seen.add(key)
                rows.append(dict(row))

    expected = _expected_keys(study)
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise RuntimeError(
            f"result matrix mismatch: missing={len(missing)}, extra={len(extra)}; "
            f"first_missing={missing[:3]}, first_extra={extra[:3]}"
        )

    order = {pair.key: index for index, pair in enumerate(study.pairs)}
    arm_order = {arm.value: index for index, arm in enumerate(study.arms)}
    seed_order = {seed: index for index, seed in enumerate(study.coder_seeds)}
    rows.sort(
        key=lambda row: (
            order[str(row["pair"])],
            seed_order[int(row["coder_seed"])],
            arm_order[str(row["arm"])],
        )
    )
    return LoadedStudyResults(
        study=study,
        rows=tuple(rows),
        run_ids=tuple(run_ids),
        study_dir=study_dir,
        frozen_plan_manifest=frozen_manifest,
    )


def _summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    summary: dict[str, Any] = {"n": n}
    for output, field in _BOOL_METRICS.items():
        count_name = output.removesuffix("_rate")
        count = sum(bool(row.get(field)) for row in rows)
        summary[count_name] = count
        summary[output] = count / n if n else None
    for output, field in _COUNT_METRICS.items():
        summary[output] = sum(_int(row, field) for row in rows)
    for output, field in _SUM_METRICS.items():
        summary[output] = sum(_float(row, field) for row in rows)
    for output, field in _MEAN_METRICS.items():
        summary[output] = _mean(_float(row, field) for row in rows)
    for flag in _FAILURE_FLAGS:
        summary[flag] = sum(bool(row.get(flag)) for row in rows)
    summary["runtime_serialized"] = sum(
        bool(row.get("runtime_serialized")) for row in rows
    )
    summary["dynamic_wasted_steps"] = sum(
        _int(row, "dynamic_wasted_steps") for row in rows
    )
    return summary


def arm_summary(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for arm in (item.value for item in study.arms):
        arm_rows = [row for row in rows if row.get("arm") == arm]
        output.append({"arm": arm, "stratum": "all", **_summarize_rows(arm_rows)})
        for gold_conflict, label in ((True, "conflict"), (False, "clean")):
            stratum = [
                row
                for row in arm_rows
                if bool(row.get("gold_conflict")) is gold_conflict
            ]
            output.append({"arm": arm, "stratum": label, **_summarize_rows(stratum)})
    return output


def feature_pair_summary(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    metadata = _pair_metadata(study)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["pair"]), str(row["arm"]))].append(row)

    output: list[dict[str, Any]] = []
    arm_order = {arm.value: index for index, arm in enumerate(study.arms)}
    pair_order = {pair.key: index for index, pair in enumerate(study.pairs)}
    for (pair_key, arm), group in sorted(
        grouped.items(),
        key=lambda item: (pair_order[item[0][0]], arm_order[item[0][1]]),
    ):
        pair = metadata[pair_key]
        output.append(
            {
                "pair": pair_key,
                "repo": pair.repo,
                "task_id": pair.task_id,
                "feature_a": pair.feature_a,
                "feature_b": pair.feature_b,
                "gold_conflict": pair.gold_conflict,
                "arm": arm,
                "coder_seeds": sorted(int(row["coder_seed"]) for row in group),
                **_summarize_rows(group),
            }
        )
    return output


def task_cluster_summary(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    metadata = _pair_metadata(study)
    grouped: dict[tuple[str, int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        pair = metadata[str(row["pair"])]
        grouped[(pair.repo, pair.task_id, str(row["arm"]))].append(row)

    output: list[dict[str, Any]] = []
    for (repo, task_id, arm), group in sorted(grouped.items()):
        pair_keys = sorted({str(row["pair"]) for row in group})
        conflict_pairs = sum(metadata[key].gold_conflict is True for key in pair_keys)
        clean_pairs = sum(metadata[key].gold_conflict is False for key in pair_keys)
        output.append(
            {
                "repo": repo,
                "task_id": task_id,
                "arm": arm,
                "pair_count": len(pair_keys),
                "conflict_pair_count": conflict_pairs,
                "clean_pair_count": clean_pairs,
                "coder_seed_count": len({int(row["coder_seed"]) for row in group}),
                **_summarize_rows(group),
            }
        )
    return output


def _primary_failure(row: Mapping[str, Any]) -> str:
    if bool(row.get("pair_pass")):
        return "pass"
    if bool(row.get("harness_failure")):
        return "harness_failure"
    if bool(row.get("planner_failure")):
        return "planner_failure"
    if bool(row.get("scope_enforcement_failure")):
        return "scope_enforcement_failure"
    if bool(row.get("agent_execution_failure")):
        return "agent_execution_failure"
    if not bool(row.get("integration_success")):
        return "integration_failure"
    return "feature_or_pair_test_failure"


def failure_taxonomy(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    categories = (
        "pass",
        "planner_failure",
        "scope_enforcement_failure",
        "agent_execution_failure",
        "harness_failure",
        "integration_failure",
        "feature_or_pair_test_failure",
    )
    output: list[dict[str, Any]] = []
    for arm in (item.value for item in study.arms):
        arm_rows = [row for row in rows if row.get("arm") == arm]
        counts = Counter(_primary_failure(row) for row in arm_rows)
        for category in categories:
            output.append(
                {
                    "arm": arm,
                    "category": category,
                    "count": counts.get(category, 0),
                    "rate": (
                        counts.get(category, 0) / len(arm_rows) if arm_rows else None
                    ),
                }
            )
    return output


def mechanism_summary(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for arm in (item.value for item in study.arms):
        group = [row for row in rows if row.get("arm") == arm]
        order_counts = Counter(
            str(row.get("dynamic_serialization_order"))
            for row in group
            if row.get("dynamic_serialization_order")
        )
        output.append(
            {
                "arm": arm,
                "n": len(group),
                "initial_serialized": sum(
                    bool(row.get("initial_serialized")) for row in group
                ),
                "runtime_serialized": sum(
                    bool(row.get("runtime_serialized")) for row in group
                ),
                "effective_serialized": sum(
                    bool(row.get("serialized")) for row in group
                ),
                "promotion_attempts": sum(
                    _int(row, "scope_promotion_attempts") for row in group
                ),
                "promotions_succeeded": sum(
                    _int(row, "scope_promotions_succeeded") for row in group
                ),
                "promotions_rejected": sum(
                    _int(row, "scope_promotions_rejected") for row in group
                ),
                "undeclared_blocks": sum(
                    _int(row, "scope_undeclared_blocks") for row in group
                ),
                "dynamic_restarts": sum(
                    _int(row, "dynamic_restart_count") for row in group
                ),
                "dynamic_wasted_steps": sum(
                    _int(row, "dynamic_wasted_steps") for row in group
                ),
                "dynamic_wasted_coder_cost": sum(
                    _float(row, "dynamic_wasted_coder_cost") for row in group
                ),
                "serialization_orders": dict(sorted(order_counts.items())),
            }
        )
    return output


def cost_summary(
    rows: Sequence[Mapping[str, Any]],
    study: StudySpec,
    frozen_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    by_arm: list[dict[str, Any]] = []
    total_coder_cost = 0.0
    for arm in (item.value for item in study.arms):
        group = [row for row in rows if row.get("arm") == arm]
        coder = sum(_float(row, "coder_cost") for row in group)
        total_coder_cost += coder
        by_arm.append(
            {
                "arm": arm,
                "n": len(group),
                "coder_cost_total": coder,
                "mean_coder_cost": coder / len(group) if group else None,
                "dynamic_wasted_coder_cost_total": sum(
                    _float(row, "dynamic_wasted_coder_cost") for row in group
                ),
                "mean_logical_system_cost_estimate": _mean(
                    _float(row, "logical_system_cost_estimate") for row in group
                ),
            }
        )
    planner_cost = float(frozen_manifest.get("total_planner_logical_cost", 0.0) or 0.0)
    return {
        "planner_freeze": {
            "executed_once": True,
            "pair_count": int(frozen_manifest.get("pair_count", len(study.pairs))),
            "total_logical_cost": planner_cost,
            "allocation_note": (
                "The frozen Planner v1 cost is reported once for the study and is not "
                "duplicated across coder seeds or Claim Plane arms."
            ),
        },
        "coder": {"total_logical_cost": total_coder_cost, "by_arm": by_arm},
        "study_total_logical_cost": planner_cost + total_coder_cost,
    }


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _metric_rate(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(bool(row.get(field)) for row in rows) / len(rows) if rows else 0.0


def _metric_mean(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return _mean(_float(row, field) for row in rows) or 0.0


def _task_cluster_key(
    row: Mapping[str, Any], metadata: Mapping[str, PairRef]
) -> tuple[str, int]:
    pair = metadata[str(row["pair"])]
    return pair.repo, pair.task_id


def _bootstrap_samples(
    rows: Sequence[Mapping[str, Any]],
    study: StudySpec,
    *,
    samples: int,
    seed: int,
    estimator: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, float]],
) -> dict[str, list[float]]:
    if samples < 1:
        raise ValueError("bootstrap samples must be at least 1")
    metadata = _pair_metadata(study)
    by_cluster: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cluster[_task_cluster_key(row, metadata)].append(row)
    clusters = sorted(by_cluster)
    if not clusters:
        raise RuntimeError("cannot bootstrap an empty study")
    rng = random.Random(seed)
    output: dict[str, list[float]] = defaultdict(list)
    for _ in range(samples):
        sampled: list[Mapping[str, Any]] = []
        for _cluster_position in range(len(clusters)):
            key = clusters[rng.randrange(len(clusters))]
            sampled.extend(by_cluster[key])
        estimates = estimator(sampled)
        for name, value in estimates.items():
            output[name].append(float(value))
    return output


def bootstrap_intervals(
    rows: Sequence[Mapping[str, Any]],
    study: StudySpec,
    *,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    """Task-cluster percentile intervals for arm metrics and paired arm deltas."""
    arm_values = tuple(arm.value for arm in study.arms)

    def estimator(sampled: Sequence[Mapping[str, Any]]) -> Mapping[str, float]:
        output: dict[str, float] = {}
        grouped = {
            arm: [row for row in sampled if row.get("arm") == arm] for arm in arm_values
        }
        for arm, group in grouped.items():
            output[f"arm|{arm}|pair_pass_rate"] = _metric_rate(group, "pair_pass")
            output[f"arm|{arm}|integration_success_rate"] = _metric_rate(
                group, "integration_success"
            )
            output[f"arm|{arm}|effective_serialization_rate"] = _metric_rate(
                group, "serialized"
            )
            output[f"arm|{arm}|mean_coder_cost"] = _metric_mean(group, "coder_cost")
        serial_pass = output["arm|always-serial|pair_pass_rate"]
        serial_integration = output["arm|always-serial|integration_success_rate"]
        for arm in arm_values:
            if arm == "always-serial":
                continue
            output[f"delta|{arm}|always-serial|pair_pass_rate"] = (
                output[f"arm|{arm}|pair_pass_rate"] - serial_pass
            )
            output[f"delta|{arm}|always-serial|integration_success_rate"] = (
                output[f"arm|{arm}|integration_success_rate"] - serial_integration
            )
        output["delta|claim-plane-dynamic|claim-plane-static|pair_pass_rate"] = (
            output["arm|claim-plane-dynamic|pair_pass_rate"]
            - output["arm|claim-plane-static|pair_pass_rate"]
        )
        return output

    observed = estimator(rows)
    sampled = _bootstrap_samples(
        rows, study, samples=samples, seed=seed, estimator=estimator
    )
    alpha = (1.0 - CI_LEVEL) / 2.0
    output: list[dict[str, Any]] = []
    for key in sorted(observed):
        values = sorted(sampled[key])
        parts = key.split("|")
        if parts[0] == "arm":
            _, arm, metric = parts
            comparison = None
            kind = "arm"
        else:
            _, arm, comparison, metric = parts
            kind = "paired_delta"
        output.append(
            {
                "kind": kind,
                "arm": arm,
                "comparison_arm": comparison,
                "metric": metric,
                "estimate": observed[key],
                "ci_level": CI_LEVEL,
                "ci_lower": _quantile(values, alpha),
                "ci_upper": _quantile(values, 1.0 - alpha),
                "bootstrap_samples": samples,
                "bootstrap_seed": seed,
                "cluster_unit": "repo+task_id",
            }
        )
    return output


def _canonical_result_rows(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    metadata = _pair_metadata(study)
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        pair = metadata[str(row["pair"])]
        row.setdefault("repo", pair.repo)
        row.setdefault("task_id", pair.task_id)
        row.setdefault("feature_a", pair.feature_a)
        row.setdefault("feature_b", pair.feature_b)
        output.append(row)
    return output


def _result_csv_rows(
    rows: Sequence[Mapping[str, Any]], study: StudySpec
) -> list[dict[str, Any]]:
    metadata = _pair_metadata(study)
    output: list[dict[str, Any]] = []
    for row in rows:
        pair = metadata[str(row["pair"])]
        output.append(
            {
                "pair": pair.key,
                "repo": pair.repo,
                "task_id": pair.task_id,
                "feature_a": pair.feature_a,
                "feature_b": pair.feature_b,
                "gold_conflict": pair.gold_conflict,
                "coder_seed": row.get("coder_seed"),
                "shard_index": row.get("shard_index"),
                "arm": row.get("arm"),
                "pair_pass": row.get("pair_pass"),
                "integration_success": row.get("integration_success"),
                "initial_serialized": row.get("initial_serialized"),
                "runtime_serialized": row.get("runtime_serialized"),
                "serialized": row.get("serialized"),
                "scope_promotion_attempts": row.get("scope_promotion_attempts"),
                "scope_promotions_succeeded": row.get("scope_promotions_succeeded"),
                "scope_promotions_rejected": row.get("scope_promotions_rejected"),
                "scope_undeclared_blocks": row.get("scope_undeclared_blocks"),
                "dynamic_restart_count": row.get("dynamic_restart_count"),
                "dynamic_wasted_steps": row.get("dynamic_wasted_steps"),
                "dynamic_wasted_coder_cost": row.get("dynamic_wasted_coder_cost"),
                "coder_cost": row.get("coder_cost"),
                "logical_system_cost_estimate": row.get("logical_system_cost_estimate"),
                "logical_llm_critical_path": row.get("logical_llm_critical_path"),
                "planner_failure": row.get("planner_failure"),
                "scope_enforcement_failure": row.get("scope_enforcement_failure"),
                "agent_execution_failure": row.get("agent_execution_failure"),
                "harness_failure": row.get("harness_failure"),
                "primary_outcome": _primary_failure(row),
            }
        )
    return output


def _flat_fields(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    return fields


def aggregate_study(
    paths: ConfirmatoryPaths,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Validate all study outputs and write deterministic publication artifacts."""
    loaded = load_complete_results(paths)
    study = loaded.study
    rows = list(loaded.rows)
    layout = analysis_layout(paths, study)
    layout.root.mkdir(parents=True, exist_ok=True)

    canonical_rows = _canonical_result_rows(rows, study)
    arm_rows = arm_summary(rows, study)
    pair_rows = feature_pair_summary(rows, study)
    cluster_rows = task_cluster_summary(rows, study)
    bootstrap_rows = bootstrap_intervals(
        rows, study, samples=bootstrap_samples, seed=bootstrap_seed
    )
    failure_rows = failure_taxonomy(rows, study)
    mechanism_rows = mechanism_summary(rows, study)
    costs = cost_summary(rows, study, loaded.frozen_plan_manifest)

    _atomic_json(layout.results_json, canonical_rows)
    result_csv_rows = _result_csv_rows(rows, study)
    _write_csv(layout.results_csv, result_csv_rows, _flat_fields(result_csv_rows))
    _atomic_json(layout.arm_summary_json, arm_rows)
    _write_csv(layout.arm_summary_csv, arm_rows, _flat_fields(arm_rows))
    _atomic_json(layout.feature_pair_json, pair_rows)
    _write_csv(layout.feature_pair_csv, pair_rows, _flat_fields(pair_rows))
    _atomic_json(layout.task_cluster_json, cluster_rows)
    _write_csv(layout.task_cluster_csv, cluster_rows, _flat_fields(cluster_rows))
    _atomic_json(layout.bootstrap_json, bootstrap_rows)
    _write_csv(layout.bootstrap_csv, bootstrap_rows, _flat_fields(bootstrap_rows))
    _atomic_json(layout.failure_json, failure_rows)
    _write_csv(layout.failure_csv, failure_rows, _flat_fields(failure_rows))
    _atomic_json(layout.mechanism_json, mechanism_rows)
    _write_csv(layout.mechanism_csv, mechanism_rows, _flat_fields(mechanism_rows))
    _atomic_json(layout.cost_json, costs)

    fingerprint = study_fingerprint(study)
    manifest = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "complete": True,
        "pair_count": len(study.pairs),
        "coder_seeds": list(study.coder_seeds),
        "arms": [arm.value for arm in study.arms],
        "arm_executions": len(rows),
        "run_ids": list(loaded.run_ids),
        "bootstrap": {
            "method": "nonparametric percentile bootstrap",
            "cluster_unit": "repo+task_id",
            "samples": bootstrap_samples,
            "seed": bootstrap_seed,
            "ci_level": CI_LEVEL,
        },
        "inputs": {
            str(paths.study_file.relative_to(paths.artifact_root)): {
                "sha256": _sha256(paths.study_file),
                "bytes": paths.study_file.stat().st_size,
            },
            str(paths.frozen_plan_manifest_file.relative_to(paths.artifact_root)): {
                "sha256": _sha256(paths.frozen_plan_manifest_file),
                "bytes": paths.frozen_plan_manifest_file.stat().st_size,
            },
            **{
                str(source.relative_to(paths.artifact_root)): {
                    "sha256": _sha256(source),
                    "bytes": source.stat().st_size,
                }
                for run_id in loaded.run_ids
                for source in (
                    loaded.study_dir / "runs" / run_id / "protocol.json",
                    loaded.study_dir / "runs" / run_id / "results.json",
                )
            },
        },
        "files": {
            path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in layout.payload_files()
        },
    }
    _atomic_json(layout.manifest_json, manifest)
    return {
        "complete": True,
        "study_fingerprint": fingerprint,
        "arm_executions": len(rows),
        "analysis_dir": str(layout.root),
        "publication_manifest": str(layout.manifest_json),
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": bootstrap_seed,
    }


def verify_analysis(paths: ConfirmatoryPaths) -> dict[str, Any]:
    """Verify final analysis hashes against its publication manifest."""
    study = load_confirmatory_study(paths)
    layout = analysis_layout(paths, study)
    if not layout.manifest_json.exists():
        return {
            "valid": False,
            "reason": "publication_manifest_missing",
            "analysis_dir": str(layout.root),
        }
    manifest = _load_json_object(layout.manifest_json)
    if manifest.get("study_fingerprint") != study_fingerprint(study):
        return {
            "valid": False,
            "reason": "study_fingerprint_mismatch",
            "analysis_dir": str(layout.root),
        }
    files = manifest.get("files")
    inputs = manifest.get("inputs")
    if not isinstance(files, dict):
        return {
            "valid": False,
            "reason": "manifest_files_invalid",
            "analysis_dir": str(layout.root),
        }
    if not isinstance(inputs, dict):
        return {
            "valid": False,
            "reason": "manifest_inputs_invalid",
            "analysis_dir": str(layout.root),
        }
    mismatches: list[dict[str, Any]] = []

    def verify_group(entries: Mapping[str, Any], *, root: Path, group: str) -> None:
        resolved_root = root.resolve()
        for name, expected in sorted(entries.items()):
            path = (root / name).resolve()
            if path != resolved_root and resolved_root not in path.parents:
                mismatches.append(
                    {"group": group, "file": name, "reason": "outside_root"}
                )
                continue
            if not path.exists():
                mismatches.append({"group": group, "file": name, "reason": "missing"})
                continue
            expected_hash = (
                expected.get("sha256") if isinstance(expected, dict) else None
            )
            observed_hash = _sha256(path)
            if observed_hash != expected_hash:
                mismatches.append(
                    {
                        "group": group,
                        "file": name,
                        "reason": "sha256_mismatch",
                        "expected": expected_hash,
                        "observed": observed_hash,
                    }
                )

    verify_group(files, root=layout.root, group="analysis")
    verify_group(inputs, root=paths.artifact_root, group="input")
    return {
        "valid": not mismatches,
        "study_fingerprint": study_fingerprint(study),
        "analysis_dir": str(layout.root),
        "files_verified": len(files),
        "inputs_verified": len(inputs),
        "mismatches": mismatches,
    }
