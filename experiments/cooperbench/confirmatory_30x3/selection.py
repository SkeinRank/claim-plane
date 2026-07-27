"""Deterministic pair selection matching the frozen V9 study protocol."""

from __future__ import annotations

import itertools
import random
from collections import defaultdict
from typing import Iterable, Mapping

from ..common import PairRef
from ..paper_6pair.dataset import TaskInfo
from .config import CONFLICT_SHARE, N_PAIRS, PAIR_SELECTION_SEED

ConflictKey = tuple[str, int, frozenset[int]]


def enumerate_pairs(
    tasks: Mapping[tuple[str, int], TaskInfo],
    conflicts: set[ConflictKey],
) -> tuple[PairRef, ...]:
    pairs: list[PairRef] = []
    for (repo, task_id), task in sorted(tasks.items()):
        for first, second in itertools.combinations(sorted(task.features), 2):
            pairs.append(
                PairRef(
                    repo=repo,
                    task_id=task_id,
                    feature_a=first,
                    feature_b=second,
                    gold_conflict=(
                        repo,
                        task_id,
                        frozenset((first, second)),
                    )
                    in conflicts,
                )
            )
    return tuple(pairs)


def task_balanced_round_robin(
    candidates: Iterable[PairRef], *, seed: int
) -> tuple[PairRef, ...]:
    by_task: dict[tuple[str, int], list[PairRef]] = defaultdict(list)
    for pair in candidates:
        by_task[(pair.repo, pair.task_id)].append(pair)

    rng = random.Random(seed)
    task_keys = sorted(by_task)
    rng.shuffle(task_keys)
    for key in task_keys:
        rng.shuffle(by_task[key])

    output: list[PairRef] = []
    while True:
        progressed = False
        for key in task_keys:
            if by_task[key]:
                output.append(by_task[key].pop())
                progressed = True
        if not progressed:
            return tuple(output)


def select_candidate_stream(
    all_pairs: Iterable[PairRef],
) -> tuple[tuple[PairRef, ...], tuple[PairRef, ...]]:
    """Return the initial 30 candidates plus deterministic replacement reserve."""
    pairs = tuple(all_pairs)
    conflict_order = task_balanced_round_robin(
        (pair for pair in pairs if pair.gold_conflict is True),
        seed=PAIR_SELECTION_SEED + 11,
    )
    clean_order = task_balanced_round_robin(
        (pair for pair in pairs if pair.gold_conflict is False),
        seed=PAIR_SELECTION_SEED + 29,
    )

    target_conflict = round(N_PAIRS * CONFLICT_SHARE)
    target_clean = N_PAIRS - target_conflict
    chosen = list(conflict_order[:target_conflict]) + list(clean_order[:target_clean])
    used = {pair.key for pair in chosen}
    reserve = [
        pair
        for pair in (*conflict_order[target_conflict:], *clean_order[target_clean:])
        if pair.key not in used
    ]

    if len(chosen) < N_PAIRS:
        needed = N_PAIRS - len(chosen)
        chosen.extend(reserve[:needed])
        reserve = reserve[needed:]

    rng = random.Random(PAIR_SELECTION_SEED)
    rng.shuffle(chosen)
    return tuple(chosen), tuple(reserve)


def freeze_gold_valid_pairs(
    initial: Iterable[PairRef],
    reserve: Iterable[PairRef],
    validity: Mapping[str, bool],
) -> tuple[PairRef, ...]:
    """Apply the V9 15/15 gold-valid quota to a deterministic candidate stream."""
    target_conflict = round(N_PAIRS * CONFLICT_SHARE)
    target_clean = N_PAIRS - target_conflict
    valid_conflict: list[PairRef] = []
    valid_clean: list[PairRef] = []
    seen: set[str] = set()

    for pair in (*tuple(initial), *tuple(reserve)):
        if pair.key in seen:
            continue
        seen.add(pair.key)
        if not validity.get(pair.key, False):
            continue
        if pair.gold_conflict is True and len(valid_conflict) < target_conflict:
            valid_conflict.append(pair)
        elif pair.gold_conflict is False and len(valid_clean) < target_clean:
            valid_clean.append(pair)
        if len(valid_conflict) == target_conflict and len(valid_clean) == target_clean:
            break

    frozen = valid_conflict + valid_clean
    if len(frozen) != N_PAIRS:
        raise RuntimeError(
            f"confirmatory study needs {N_PAIRS} gold-valid pairs, found {len(frozen)}"
        )
    rng = random.Random(PAIR_SELECTION_SEED)
    rng.shuffle(frozen)
    return tuple(frozen)
