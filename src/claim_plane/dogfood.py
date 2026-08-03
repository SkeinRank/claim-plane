"""Frozen single-agent dogfood suites and release-readiness metrics.

The dogfood layer freezes task/repository inputs before provider execution, expands
those inputs into a deterministic three-arm run plan, validates structured results,
and computes a conservative technical-preview gate. It never invents benchmark
measurements and does not call a coding-agent provider itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DOGFOOD_SUITE_PROTOCOL = "claim-plane.dogfood-suite.v1"
DOGFOOD_PLAN_PROTOCOL = "claim-plane.dogfood-plan.v1"
DOGFOOD_RESULT_PROTOCOL = "claim-plane.dogfood-result.v1"
DOGFOOD_SUMMARY_PROTOCOL = "claim-plane.dogfood-summary.v1"
DOGFOOD_GATE_PROTOCOL = "claim-plane.dogfood-release-gate.v1"
DOGFOOD_ARMS = ("bare-codex", "claim-plane-observe", "claim-plane-guarded")


class DogfoodError(RuntimeError):
    """Base error for immutable dogfood inputs and derived artifacts."""


class DogfoodArm(str, Enum):
    BARE_CODEX = "bare-codex"
    OBSERVE = "claim-plane-observe"
    GUARDED = "claim-plane-guarded"


class DogfoodGateStatus(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    INCOMPLETE = "INCOMPLETE"


def _canonical_json(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: object, *, field_name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{field_name} must be finite and >= {minimum}")
    return result


def _integer(value: object, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _boolean(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class DogfoodRepository:
    """One exact repository state used by the frozen task corpus."""

    repository_id: str
    clone_url: str
    base_commit: str
    language: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DogfoodRepository":
        repository = cls(
            repository_id=_text(
                payload.get("repository_id"), field_name="repository_id"
            ),
            clone_url=_text(payload.get("clone_url"), field_name="clone_url"),
            base_commit=_text(
                payload.get("base_commit"), field_name="base_commit"
            ).lower(),
            language=_text(payload.get("language"), field_name="language").lower(),
        )
        if not all(
            character in "0123456789abcdef"
            for character in repository.base_commit
        ):
            raise ValueError("base_commit must be hexadecimal")
        if not 7 <= len(repository.base_commit) <= 64:
            raise ValueError("base_commit must contain 7 to 64 hexadecimal characters")
        return repository

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "clone_url": self.clone_url,
            "base_commit": self.base_commit,
            "language": self.language,
        }


@dataclass(frozen=True, slots=True)
class GoldenTask:
    """One frozen single-agent task and its acceptance contract."""

    task_id: str
    repository_id: str
    prompt: str
    prompt_sha256: str
    source_ref: str
    task_class: str
    risk_class: str
    acceptance: tuple[str, ...]
    split: str = "dogfood"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GoldenTask":
        prompt = _text(payload.get("prompt"), field_name="prompt")
        expected = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        declared = str(payload.get("prompt_sha256") or expected).lower()
        if declared != expected:
            raise ValueError(
                f"prompt_sha256 mismatch for task {payload.get('task_id')!r}"
            )
        acceptance_raw = payload.get("acceptance")
        if not isinstance(acceptance_raw, list) or not acceptance_raw:
            raise ValueError("acceptance must be a non-empty list")
        acceptance = tuple(
            _text(command, field_name="acceptance command")
            for command in acceptance_raw
        )
        risk = _text(payload.get("risk_class"), field_name="risk_class").lower()
        if risk not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"unsupported risk_class: {risk}")
        return cls(
            task_id=_text(payload.get("task_id"), field_name="task_id"),
            repository_id=_text(
                payload.get("repository_id"), field_name="repository_id"
            ),
            prompt=prompt,
            prompt_sha256=expected,
            source_ref=_text(payload.get("source_ref"), field_name="source_ref"),
            task_class=_text(
                payload.get("task_class"), field_name="task_class"
            ).lower(),
            risk_class=risk,
            acceptance=acceptance,
            split=_text(payload.get("split") or "dogfood", field_name="split").lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository_id": self.repository_id,
            "prompt": self.prompt,
            "prompt_sha256": self.prompt_sha256,
            "source_ref": self.source_ref,
            "task_class": self.task_class,
            "risk_class": self.risk_class,
            "acceptance": list(self.acceptance),
            "split": self.split,
        }


@dataclass(frozen=True, slots=True)
class GoldenSuite:
    """Immutable task selection reused by all coder arms and seeds."""

    suite_id: str
    description: str
    frozen_at: str
    selection_seed: int
    coder_seeds: tuple[int, ...]
    repositories: tuple[DogfoodRepository, ...]
    tasks: tuple[GoldenTask, ...]
    arms: tuple[DogfoodArm, ...]
    digest: str
    protocol: str = DOGFOOD_SUITE_PROTOCOL

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        require_release_grade: bool = False,
    ) -> "GoldenSuite":
        if payload.get("protocol") != DOGFOOD_SUITE_PROTOCOL:
            raise ValueError(
                "unsupported dogfood suite protocol: "
                f"{payload.get('protocol')!r}"
            )
        repositories_raw = payload.get("repositories")
        tasks_raw = payload.get("tasks")
        seeds_raw = payload.get("coder_seeds")
        arms_raw = payload.get("arms")
        if not isinstance(repositories_raw, list) or not repositories_raw:
            raise ValueError("repositories must be a non-empty list")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise ValueError("tasks must be a non-empty list")
        if not isinstance(seeds_raw, list) or not seeds_raw:
            raise ValueError("coder_seeds must be a non-empty list")
        if not isinstance(arms_raw, list):
            raise ValueError("arms must be a list")
        repositories = tuple(
            DogfoodRepository.from_dict(item) for item in repositories_raw
        )
        tasks = tuple(GoldenTask.from_dict(item) for item in tasks_raw)
        seeds = tuple(_integer(seed, field_name="coder seed") for seed in seeds_raw)
        arms = tuple(DogfoodArm(str(item)) for item in arms_raw)
        suite = cls(
            suite_id=_text(payload.get("suite_id"), field_name="suite_id"),
            description=_text(payload.get("description"), field_name="description"),
            frozen_at=_text(payload.get("frozen_at"), field_name="frozen_at"),
            selection_seed=_integer(
                payload.get("selection_seed"), field_name="selection_seed"
            ),
            coder_seeds=seeds,
            repositories=repositories,
            tasks=tasks,
            arms=arms,
            digest=_text(payload.get("digest"), field_name="digest").lower(),
        )
        suite.validate(require_release_grade=require_release_grade)
        if suite.digest != _digest(suite._unsigned_dict()):
            raise ValueError("dogfood suite digest does not match frozen inputs")
        return suite

    def _unsigned_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "suite_id": self.suite_id,
            "description": self.description,
            "frozen_at": self.frozen_at,
            "selection_seed": self.selection_seed,
            "coder_seeds": list(self.coder_seeds),
            "arms": [arm.value for arm in self.arms],
            "repositories": [item.to_dict() for item in self.repositories],
            "tasks": [item.to_dict() for item in self.tasks],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._unsigned_dict(), "digest": self.digest}

    def validate(self, *, require_release_grade: bool = False) -> None:
        repository_ids = [item.repository_id for item in self.repositories]
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("repository_id values must be unique")
        task_ids = [item.task_id for item in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_id values must be unique")
        if len(self.coder_seeds) != len(set(self.coder_seeds)):
            raise ValueError("coder_seeds must be unique")
        if tuple(arm.value for arm in self.arms) != DOGFOOD_ARMS:
            raise ValueError("arms must use the frozen bare/observe/guarded order")
        known = set(repository_ids)
        missing = sorted({task.repository_id for task in self.tasks} - known)
        if missing:
            raise ValueError(
                "tasks reference unknown repositories: " + ", ".join(missing)
            )
        if require_release_grade:
            findings: list[str] = []
            if not 20 <= len(self.tasks) <= 30:
                findings.append("task count must be between 20 and 30")
            if not 5 <= len(self.repositories) <= 10:
                findings.append("repository count must be between 5 and 10")
            if len(self.coder_seeds) < 2:
                findings.append("at least two coder seeds are required")
            if len({task.task_class for task in self.tasks}) < 3:
                findings.append("at least three task classes are required")
            if len({task.risk_class for task in self.tasks}) < 3:
                findings.append("at least three risk classes are required")
            represented = {task.repository_id for task in self.tasks}
            if len(represented) < 5:
                findings.append("tasks must cover at least five repositories")
            if any(
                len(repository.base_commit) != 40
                for repository in self.repositories
            ):
                findings.append(
                    "release-grade repository commits must be full 40-character SHAs"
                )
            if findings:
                raise ValueError(
                    "release-grade dogfood suite invalid: " + "; ".join(findings)
                )


def freeze_golden_suite(
    candidate: Mapping[str, Any],
    *,
    frozen_at: str | None = None,
    require_release_grade: bool = False,
) -> GoldenSuite:
    """Canonicalize a candidate corpus and bind every task prompt to a digest."""

    repositories_raw = candidate.get("repositories")
    tasks_raw = candidate.get("tasks")
    if not isinstance(repositories_raw, list) or not isinstance(tasks_raw, list):
        raise ValueError("candidate requires repositories and tasks lists")
    repositories = sorted(
        (DogfoodRepository.from_dict(item) for item in repositories_raw),
        key=lambda item: item.repository_id,
    )
    tasks = sorted(
        (GoldenTask.from_dict(item) for item in tasks_raw),
        key=lambda item: item.task_id,
    )
    unsigned = {
        "protocol": DOGFOOD_SUITE_PROTOCOL,
        "suite_id": _text(candidate.get("suite_id"), field_name="suite_id"),
        "description": _text(candidate.get("description"), field_name="description"),
        "frozen_at": frozen_at or _utc_now(),
        "selection_seed": _integer(
            candidate.get("selection_seed"), field_name="selection_seed"
        ),
        "coder_seeds": [
            _integer(seed, field_name="coder seed")
            for seed in candidate.get("coder_seeds") or []
        ],
        "arms": list(DOGFOOD_ARMS),
        "repositories": [item.to_dict() for item in repositories],
        "tasks": [item.to_dict() for item in tasks],
    }
    payload = {**unsigned, "digest": _digest(unsigned)}
    return GoldenSuite.from_dict(payload, require_release_grade=require_release_grade)


def load_golden_suite(
    path: str | Path,
    *,
    require_release_grade: bool = False,
) -> GoldenSuite:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("dogfood suite must be a JSON object")
    return GoldenSuite.from_dict(payload, require_release_grade=require_release_grade)


@dataclass(frozen=True, slots=True)
class DogfoodPlanEntry:
    execution_id: str
    task_id: str
    repository_id: str
    base_commit: str
    prompt: str
    prompt_sha256: str
    acceptance: tuple[str, ...]
    task_class: str
    risk_class: str
    seed: int
    arm: DogfoodArm
    policy: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "repository_id": self.repository_id,
            "base_commit": self.base_commit,
            "prompt": self.prompt,
            "prompt_sha256": self.prompt_sha256,
            "acceptance": list(self.acceptance),
            "task_class": self.task_class,
            "risk_class": self.risk_class,
            "seed": self.seed,
            "arm": self.arm.value,
            "policy": self.policy,
        }


@dataclass(frozen=True, slots=True)
class DogfoodPlan:
    suite_id: str
    suite_digest: str
    created_at: str
    model: str | None
    entries: tuple[DogfoodPlanEntry, ...]
    digest: str
    protocol: str = DOGFOOD_PLAN_PROTOCOL

    def _unsigned_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "suite_id": self.suite_id,
            "suite_digest": self.suite_digest,
            "created_at": self.created_at,
            "model": self.model,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._unsigned_dict(), "digest": self.digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DogfoodPlan":
        if payload.get("protocol") != DOGFOOD_PLAN_PROTOCOL:
            raise ValueError("unsupported dogfood plan protocol")
        entries_raw = payload.get("entries")
        if not isinstance(entries_raw, list) or not entries_raw:
            raise ValueError("dogfood plan entries must be a non-empty list")
        entries: list[DogfoodPlanEntry] = []
        for item in entries_raw:
            if not isinstance(item, Mapping):
                raise ValueError("dogfood plan entry must be an object")
            acceptance_raw = item.get("acceptance")
            if not isinstance(acceptance_raw, list) or not acceptance_raw:
                raise ValueError("dogfood plan entry acceptance must be non-empty")
            entries.append(
                DogfoodPlanEntry(
                    execution_id=_text(
                        item.get("execution_id"), field_name="execution_id"
                    ),
                    task_id=_text(item.get("task_id"), field_name="task_id"),
                    repository_id=_text(
                        item.get("repository_id"), field_name="repository_id"
                    ),
                    base_commit=_text(
                        item.get("base_commit"), field_name="base_commit"
                    ),
                    prompt=_text(item.get("prompt"), field_name="prompt"),
                    prompt_sha256=_text(
                        item.get("prompt_sha256"), field_name="prompt_sha256"
                    ),
                    acceptance=tuple(
                        _text(command, field_name="acceptance command")
                        for command in acceptance_raw
                    ),
                    task_class=_text(item.get("task_class"), field_name="task_class"),
                    risk_class=_text(item.get("risk_class"), field_name="risk_class"),
                    seed=_integer(item.get("seed"), field_name="seed"),
                    arm=DogfoodArm(str(item.get("arm"))),
                    policy=(
                        None
                        if item.get("policy") is None
                        else str(item.get("policy"))
                    ),
                )
            )
            if entries[-1].prompt_sha256 != hashlib.sha256(
                entries[-1].prompt.encode("utf-8")
            ).hexdigest():
                raise ValueError(
                    f"prompt_sha256 mismatch for plan task {entries[-1].task_id!r}"
                )
        plan = cls(
            suite_id=_text(payload.get("suite_id"), field_name="suite_id"),
            suite_digest=_text(payload.get("suite_digest"), field_name="suite_digest"),
            created_at=_text(payload.get("created_at"), field_name="created_at"),
            model=(None if payload.get("model") is None else str(payload.get("model"))),
            entries=tuple(entries),
            digest=_text(payload.get("digest"), field_name="digest"),
        )
        if plan.digest != _digest(plan._unsigned_dict()):
            raise ValueError("dogfood plan digest does not match entries")
        if len({entry.execution_id for entry in plan.entries}) != len(plan.entries):
            raise ValueError("dogfood plan execution_id values must be unique")
        return plan


def build_dogfood_plan(
    suite: GoldenSuite,
    *,
    model: str | None = None,
    created_at: str | None = None,
) -> DogfoodPlan:
    repositories = {item.repository_id: item for item in suite.repositories}
    entries: list[DogfoodPlanEntry] = []
    for task in sorted(suite.tasks, key=lambda item: item.task_id):
        repository = repositories[task.repository_id]
        for seed in sorted(suite.coder_seeds):
            for arm in suite.arms:
                identity = {
                    "suite_digest": suite.digest,
                    "task_id": task.task_id,
                    "seed": seed,
                    "arm": arm.value,
                }
                execution_id = "dogfood-" + _digest(identity)[:24]
                policy = {
                    DogfoodArm.BARE_CODEX: None,
                    DogfoodArm.OBSERVE: "observe",
                    DogfoodArm.GUARDED: "guarded",
                }[arm]
                entries.append(
                    DogfoodPlanEntry(
                        execution_id=execution_id,
                        task_id=task.task_id,
                        repository_id=task.repository_id,
                        base_commit=repository.base_commit,
                        prompt=task.prompt,
                        prompt_sha256=task.prompt_sha256,
                        acceptance=task.acceptance,
                        task_class=task.task_class,
                        risk_class=task.risk_class,
                        seed=seed,
                        arm=arm,
                        policy=policy,
                    )
                )
    unsigned = {
        "protocol": DOGFOOD_PLAN_PROTOCOL,
        "suite_id": suite.suite_id,
        "suite_digest": suite.digest,
        "created_at": created_at or _utc_now(),
        "model": model,
        "entries": [entry.to_dict() for entry in entries],
    }
    return DogfoodPlan(
        suite_id=suite.suite_id,
        suite_digest=suite.digest,
        created_at=str(unsigned["created_at"]),
        model=model,
        entries=tuple(entries),
        digest=_digest(unsigned),
    )


@dataclass(frozen=True, slots=True)
class DogfoodResult:
    """One evaluated task/seed/arm execution."""

    execution_id: str
    plan_digest: str
    suite_digest: str
    task_id: str
    repository_id: str
    seed: int
    arm: DogfoodArm
    outcome: str
    evaluation_complete: bool
    task_success: bool
    accepted_delivery: bool
    undeclared_mutations: int
    scope_amendments: int
    false_blocks: int
    missed_mutations: int
    human_repairs: int
    retries: int
    wall_time_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    files_changed: int
    lines_added: int
    lines_deleted: int
    public_api_drift: bool
    dependency_drift: bool
    evidence_digest: str | None
    protocol: str = DOGFOOD_RESULT_PROTOCOL

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DogfoodResult":
        if payload.get("protocol") != DOGFOOD_RESULT_PROTOCOL:
            raise ValueError("unsupported dogfood result protocol")

        def optional_integer(name: str) -> int | None:
            value = payload.get(name)
            return None if value is None else _integer(value, field_name=name)

        def optional_number(name: str) -> float | None:
            value = payload.get(name)
            return None if value is None else _number(value, field_name=name)

        return cls(
            execution_id=_text(payload.get("execution_id"), field_name="execution_id"),
            plan_digest=_text(payload.get("plan_digest"), field_name="plan_digest"),
            suite_digest=_text(payload.get("suite_digest"), field_name="suite_digest"),
            task_id=_text(payload.get("task_id"), field_name="task_id"),
            repository_id=_text(
                payload.get("repository_id"), field_name="repository_id"
            ),
            seed=_integer(payload.get("seed"), field_name="seed"),
            arm=DogfoodArm(str(payload.get("arm"))),
            outcome=_text(payload.get("outcome"), field_name="outcome").upper(),
            evaluation_complete=_boolean(
                payload.get("evaluation_complete"), field_name="evaluation_complete"
            ),
            task_success=_boolean(
                payload.get("task_success"), field_name="task_success"
            ),
            accepted_delivery=_boolean(
                payload.get("accepted_delivery"), field_name="accepted_delivery"
            ),
            undeclared_mutations=_integer(
                payload.get("undeclared_mutations"), field_name="undeclared_mutations"
            ),
            scope_amendments=_integer(
                payload.get("scope_amendments"), field_name="scope_amendments"
            ),
            false_blocks=_integer(
                payload.get("false_blocks"), field_name="false_blocks"
            ),
            missed_mutations=_integer(
                payload.get("missed_mutations"), field_name="missed_mutations"
            ),
            human_repairs=_integer(
                payload.get("human_repairs"), field_name="human_repairs"
            ),
            retries=_integer(payload.get("retries"), field_name="retries"),
            wall_time_seconds=_number(
                payload.get("wall_time_seconds"), field_name="wall_time_seconds"
            ),
            input_tokens=optional_integer("input_tokens"),
            output_tokens=optional_integer("output_tokens"),
            cost_usd=optional_number("cost_usd"),
            files_changed=_integer(
                payload.get("files_changed"), field_name="files_changed"
            ),
            lines_added=_integer(payload.get("lines_added"), field_name="lines_added"),
            lines_deleted=_integer(
                payload.get("lines_deleted"), field_name="lines_deleted"
            ),
            public_api_drift=_boolean(
                payload.get("public_api_drift"), field_name="public_api_drift"
            ),
            dependency_drift=_boolean(
                payload.get("dependency_drift"), field_name="dependency_drift"
            ),
            evidence_digest=(
                None
                if payload.get("evidence_digest") is None
                else str(payload.get("evidence_digest"))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "suite_digest": self.suite_digest,
            "task_id": self.task_id,
            "repository_id": self.repository_id,
            "seed": self.seed,
            "arm": self.arm.value,
            "outcome": self.outcome,
            "evaluation_complete": self.evaluation_complete,
            "task_success": self.task_success,
            "accepted_delivery": self.accepted_delivery,
            "undeclared_mutations": self.undeclared_mutations,
            "scope_amendments": self.scope_amendments,
            "false_blocks": self.false_blocks,
            "missed_mutations": self.missed_mutations,
            "human_repairs": self.human_repairs,
            "retries": self.retries,
            "wall_time_seconds": self.wall_time_seconds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "public_api_drift": self.public_api_drift,
            "dependency_drift": self.dependency_drift,
            "evidence_digest": self.evidence_digest,
        }


def build_dogfood_result(
    plan: DogfoodPlan,
    execution_id: str,
    evaluation: Mapping[str, Any],
) -> DogfoodResult:
    """Bind measured evaluator output to one immutable plan cell."""

    entry = next(
        (item for item in plan.entries if item.execution_id == execution_id),
        None,
    )
    if entry is None:
        raise ValueError(f"execution_id is not present in dogfood plan: {execution_id}")
    protected = {
        "protocol": DOGFOOD_RESULT_PROTOCOL,
        "execution_id": entry.execution_id,
        "plan_digest": plan.digest,
        "suite_digest": plan.suite_digest,
        "task_id": entry.task_id,
        "repository_id": entry.repository_id,
        "seed": entry.seed,
        "arm": entry.arm.value,
    }
    for name, expected in protected.items():
        if name in evaluation and evaluation[name] != expected:
            raise ValueError(f"evaluation attempts to replace protected field {name}")
    return DogfoodResult.from_dict({**dict(evaluation), **protected})


def load_dogfood_results(paths: Iterable[str | Path]) -> tuple[DogfoodResult, ...]:
    results: list[DogfoodResult] = []
    for raw_path in paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        payloads: list[Any]
        if stripped.startswith("["):
            loaded = json.loads(text)
            if not isinstance(loaded, list):
                raise ValueError(f"result file must contain a list: {path}")
            payloads = loaded
        elif stripped.startswith("{"):
            loaded = json.loads(text)
            if isinstance(loaded, Mapping) and isinstance(loaded.get("results"), list):
                payloads = list(loaded["results"])
            else:
                payloads = [loaded]
        else:
            payloads = [json.loads(line) for line in text.splitlines() if line.strip()]
        for payload in payloads:
            if not isinstance(payload, Mapping):
                raise ValueError(f"dogfood result must be an object: {path}")
            results.append(DogfoodResult.from_dict(payload))
    return tuple(results)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def _arm_summary(results: Sequence[DogfoodResult]) -> dict[str, Any]:
    evaluated = [result for result in results if result.evaluation_complete]
    completed = [result for result in evaluated if result.outcome == "COMPLETED"]
    known_cost = [
        result.cost_usd for result in evaluated if result.cost_usd is not None
    ]
    known_input = [
        result.input_tokens
        for result in evaluated
        if result.input_tokens is not None
    ]
    known_output = [
        result.output_tokens
        for result in evaluated
        if result.output_tokens is not None
    ]
    return {
        "expected_count": len(results),
        "evaluated_count": len(evaluated),
        "completed_count": len(completed),
        "task_success_count": sum(result.task_success for result in evaluated),
        "task_success_rate": _rate(
            sum(result.task_success for result in evaluated), len(evaluated)
        ),
        "accepted_delivery_count": sum(
            result.accepted_delivery for result in evaluated
        ),
        "accepted_delivery_rate": _rate(
            sum(result.accepted_delivery for result in evaluated), len(evaluated)
        ),
        "undeclared_mutations": sum(
            result.undeclared_mutations for result in evaluated
        ),
        "scope_amendments": sum(result.scope_amendments for result in evaluated),
        "false_blocks": sum(result.false_blocks for result in evaluated),
        "missed_mutations": sum(result.missed_mutations for result in evaluated),
        "human_repairs": sum(result.human_repairs for result in evaluated),
        "retries": sum(result.retries for result in evaluated),
        "wall_time_seconds_total": sum(
            result.wall_time_seconds for result in evaluated
        ),
        "wall_time_seconds_mean": _mean(
            [result.wall_time_seconds for result in evaluated]
        ),
        "input_tokens_total": None if not known_input else sum(known_input),
        "output_tokens_total": None if not known_output else sum(known_output),
        "cost_usd_total": None if not known_cost else sum(known_cost),
        "files_changed_total": sum(result.files_changed for result in evaluated),
        "lines_added_total": sum(result.lines_added for result in evaluated),
        "lines_deleted_total": sum(result.lines_deleted for result in evaluated),
        "public_api_drift_count": sum(result.public_api_drift for result in evaluated),
        "dependency_drift_count": sum(result.dependency_drift for result in evaluated),
    }


def aggregate_dogfood_results(
    suite: GoldenSuite,
    plan: DogfoodPlan,
    results: Sequence[DogfoodResult],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Validate plan coverage and aggregate only supplied, measured result records."""

    if plan.suite_digest != suite.digest:
        raise ValueError("dogfood plan does not belong to the supplied suite")
    expected = {entry.execution_id: entry for entry in plan.entries}
    observed: dict[str, DogfoodResult] = {}
    duplicates: list[str] = []
    unexpected: list[str] = []
    mismatched: list[str] = []
    for result in results:
        if result.execution_id in observed:
            duplicates.append(result.execution_id)
            continue
        observed[result.execution_id] = result
        entry = expected.get(result.execution_id)
        if entry is None:
            unexpected.append(result.execution_id)
            continue
        if (
            result.plan_digest != plan.digest
            or result.suite_digest != suite.digest
            or result.task_id != entry.task_id
            or result.repository_id != entry.repository_id
            or result.seed != entry.seed
            or result.arm != entry.arm
        ):
            mismatched.append(result.execution_id)
    missing = sorted(set(expected) - set(observed))
    matched = [
        result
        for key, result in observed.items()
        if key in expected and key not in mismatched
    ]
    by_arm: dict[str, dict[str, Any]] = {}
    for arm in suite.arms:
        metrics = _arm_summary([result for result in matched if result.arm == arm])
        metrics["expected_count"] = sum(entry.arm == arm for entry in plan.entries)
        by_arm[arm.value] = metrics
    completeness = {
        "expected": len(expected),
        "observed": len(observed),
        "matched": len(matched),
        "missing": missing,
        "unexpected": sorted(unexpected),
        "duplicates": sorted(duplicates),
        "mismatched": sorted(mismatched),
        "complete": not (missing or unexpected or duplicates or mismatched),
    }
    unsigned = {
        "protocol": DOGFOOD_SUMMARY_PROTOCOL,
        "suite_id": suite.suite_id,
        "suite_digest": suite.digest,
        "plan_digest": plan.digest,
        "generated_at": generated_at or _utc_now(),
        "task_count": len(suite.tasks),
        "repository_count": len(suite.repositories),
        "coder_seeds": list(suite.coder_seeds),
        "arms": by_arm,
        "completeness": completeness,
    }
    return {**unsigned, "digest": _digest(unsigned)}


def evaluate_dogfood_release_gate(
    summary: Mapping[str, Any],
    *,
    max_task_success_drop: float = 0.05,
    min_accepted_delivery_gain: float = 0.02,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    """Block preview when guarded harms success without reliability compensation."""

    max_drop = _number(
        max_task_success_drop, field_name="max_task_success_drop", minimum=0.0
    )
    min_gain = _number(
        min_accepted_delivery_gain,
        field_name="min_accepted_delivery_gain",
        minimum=0.0,
    )
    if summary.get("protocol") != DOGFOOD_SUMMARY_PROTOCOL:
        raise ValueError("unsupported dogfood summary protocol")
    completeness = summary.get("completeness")
    arms = summary.get("arms")
    if not isinstance(completeness, Mapping) or not isinstance(arms, Mapping):
        raise ValueError("dogfood summary is missing completeness or arm metrics")
    findings: list[dict[str, str]] = []
    status = DogfoodGateStatus.PASSED
    bare = arms.get(DogfoodArm.BARE_CODEX.value)
    guarded = arms.get(DogfoodArm.GUARDED.value)
    if not completeness.get("complete"):
        status = DogfoodGateStatus.INCOMPLETE
        findings.append(
            {
                "code": "incomplete_run_matrix",
                "message": (
                    "Every frozen task, seed, and arm must have exactly one result."
                ),
            }
        )
    if not isinstance(bare, Mapping) or not isinstance(guarded, Mapping):
        status = DogfoodGateStatus.INCOMPLETE
        findings.append(
            {
                "code": "missing_required_arms",
                "message": "Bare Codex and Claim Plane Guarded metrics are required.",
            }
        )
        success_drop = None
        reliability_gain = None
    else:
        bare_success = bare.get("task_success_rate")
        guarded_success = guarded.get("task_success_rate")
        bare_delivery = bare.get("accepted_delivery_rate")
        guarded_delivery = guarded.get("accepted_delivery_rate")
        if None in {bare_success, guarded_success, bare_delivery, guarded_delivery}:
            status = DogfoodGateStatus.INCOMPLETE
            findings.append(
                {
                    "code": "missing_evaluated_results",
                    "message": "Required arms do not contain evaluated task outcomes.",
                }
            )
            success_drop = None
            reliability_gain = None
        else:
            success_drop = float(bare_success) - float(guarded_success)
            reliability_gain = float(guarded_delivery) - float(bare_delivery)
            if success_drop > max_drop and reliability_gain < min_gain:
                status = DogfoodGateStatus.BLOCKED
                findings.append(
                    {
                        "code": "guarded_success_regression",
                        "message": (
                            "Guarded mode reduced task success beyond the allowed "
                            "threshold "
                            "without a compensating accepted-delivery gain."
                        ),
                    }
                )
            else:
                findings.append(
                    {
                        "code": "guarded_tradeoff_acceptable",
                        "message": (
                            "Guarded mode stayed within the success threshold or "
                            "provided "
                            "the required accepted-delivery improvement."
                        ),
                    }
                )
    unsigned = {
        "protocol": DOGFOOD_GATE_PROTOCOL,
        "summary_digest": summary.get("digest"),
        "evaluated_at": evaluated_at or _utc_now(),
        "status": status.value,
        "release_allowed": status is DogfoodGateStatus.PASSED,
        "thresholds": {
            "max_task_success_drop": max_drop,
            "min_accepted_delivery_gain": min_gain,
        },
        "comparison": {
            "task_success_drop": success_drop,
            "accepted_delivery_gain": reliability_gain,
        },
        "findings": findings,
    }
    return {**unsigned, "digest": _digest(unsigned)}


def load_dogfood_plan(path: str | Path) -> DogfoodPlan:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("dogfood plan must be a JSON object")
    return DogfoodPlan.from_dict(payload)


def load_dogfood_summary(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("dogfood summary must be a JSON object")
    if payload.get("protocol") != DOGFOOD_SUMMARY_PROTOCOL:
        raise ValueError("unsupported dogfood summary protocol")
    declared = payload.get("digest")
    unsigned = dict(payload)
    unsigned.pop("digest", None)
    if declared != _digest(unsigned):
        raise ValueError("dogfood summary digest does not match metrics")
    return dict(payload)
