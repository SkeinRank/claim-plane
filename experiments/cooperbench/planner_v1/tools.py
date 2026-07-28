"""Deterministic repository inspection used by Planner v1.

Gold feature data is used only to localize current-source context, matching the
oracle-localized condition disclosed for the published mechanism check.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import re
from typing import Any

from claim_plane import ScopeCommitment

from .policy import (
    CALIBRATION_V2_AUTO_SYMBOL_MAX_SPAN,
    CALIBRATION_V2_BOUNDARY_HALO,
    CALIBRATION_V2_MAX_AUTO_BOUNDARY,
    CALIBRATION_V2_MAX_AUTO_SYMBOLS,
    CALIBRATION_V2_MAX_CANDIDATES,
    CALIBRATION_V2_MAX_MODEL_SELECTED,
    CALIBRATION_V2_MAX_PROMPT_CHARS,
    CALIBRATION_V2_SNIPPET_RADIUS,
    CALIBRATION_V2_SYMBOL_MAX_SPAN,
    CALIBRATION_V2_TASK_HIT_RADIUS,
    CALIBRATION_V3_ALIAS_BLOCK_MAX_SPAN,
    CALIBRATION_V3_INSERTION_ANCHOR_RADIUS,
    CALIBRATION_V3_MAX_AUTO_ALIAS_BLOCKS,
    CALIBRATION_V3_MAX_AUTO_INSERTION_ANCHORS,
    CALIBRATION_V3_MAX_AUTO_REFERENCED_SYMBOLS,
    CALIBRATION_V3_REFERENCE_SYMBOL_MAX_SPAN,
    FULL_FILE_CONTEXT_LIMIT,
    TARGET_CONTEXT_RADIUS,
)

# fmt: off
# Frozen notebook-derived analysis code is kept source-stable for parity review.
FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
OLD_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.M)

_CALIBRATION_V2_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "because",
    "before",
    "being",
    "between",
    "change",
    "class",
    "code",
    "could",
    "create",
    "does",
    "feature",
    "from",
    "function",
    "implementation",
    "into",
    "must",
    "need",
    "only",
    "other",
    "should",
    "task",
    "that",
    "their",
    "there",
    "these",
    "this",
    "through",
    "using",
    "when",
    "where",
    "which",
    "with",
    "without",
}

def gold_files(feature_dir):
    patch = (feature_dir / "feature.patch").read_text(errors="replace")
    return sorted(set(FILE_RE.findall(patch)))

def gold_preimage_ranges(feature_dir):
    patch = (feature_dir / "feature.patch").read_text(errors="replace")
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            ranges.setdefault(current_file, [])
            continue
        match = OLD_HUNK_RE.match(line)
        if match and current_file:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            end = max(start, start + max(count, 1) - 1)
            ranges[current_file].append((start, end))
    return ranges

def _numbered_lines(lines, start_index, end_index):
    return "\n".join(
        f"{index + 1:6d} | {lines[index]}"
        for index in range(start_index, end_index)
    )

def read_context(tree, feature_dir):
    """Show current source, using gold only to localize relevant regions."""
    ranges_by_file = gold_preimage_ranges(feature_dir)
    chunks = []
    for relative in gold_files(feature_dir):
        path = tree / relative
        if not path.exists():
            chunks.append(
                f"### {relative}\n<NEW FILE — not present in the current tree>"
            )
            continue

        text = path.read_text(errors="replace")
        lines = text.splitlines()
        if len(text) <= FULL_FILE_CONTEXT_LIMIT:
            chunks.append(
                f"### {relative} (complete current file; numbered)\n"
                "```text\n" + _numbered_lines(lines, 0, len(lines)) + "\n```"
            )
            continue

        windows = []
        for start, end in ranges_by_file.get(relative, []):
            left = max(0, start - 1 - TARGET_CONTEXT_RADIUS)
            right = min(len(lines), end + TARGET_CONTEXT_RADIUS)
            windows.append((left, right))
        if not windows:
            windows = [(0, min(len(lines), TARGET_CONTEXT_RADIUS * 2))]

        windows.sort()
        merged: list[list[int]] = []
        for left, right in windows:
            if not merged or left > merged[-1][1]:
                merged.append([left, right])
            else:
                merged[-1][1] = max(merged[-1][1], right)

        rendered = []
        for left, right in merged:
            rendered.append(
                f"# current lines {left + 1}-{right}\n"
                + _numbered_lines(lines, left, right)
            )
        chunks.append(
            f"### {relative} (targeted current-file windows)\n"
            "```text\n" + "\n\n".join(rendered) + "\n```"
        )
    return "\n\n".join(chunks)

def _safe_planner_relative_path(path):
    path = str(
        path
        or ""
    ).strip()

    if (
        not path
        or path.startswith("/")
        or path.startswith("\\")
    ):
        return None

    parts = [
        part
        for part in re.split(
            r"[\\/]+",
            path,
        )
        if part
        and part != "."
    ]

    if (
        not parts
        or any(
            part == ".."
            for part in parts
        )
    ):
        return None

    return "/".join(
        parts
    )

def _calibration_v2_interval_is_covered(
    primary_plan,
    path,
    start,
    end,
):
    for item in primary_plan.get(
        "files",
        [],
    ):
        item_path = _safe_planner_relative_path(
            item.get(
                "path"
            )
        )

        if item_path != path:
            continue

        item_start = int(
            item.get(
                "line_start",
                0,
            )
            or 0
        )

        item_end = int(
            item.get(
                "line_end",
                0,
            )
            or 0
        )

        if (
            item_start <= 0
            or item_end <= 0
        ):
            continue

        item_start, item_end = (
            min(
                item_start,
                item_end,
            ),
            max(
                item_start,
                item_end,
            ),
        )

        if (
            item_start <= start
            and item_end >= end
        ):
            return True

    return False

def _calibration_v2_task_terms(
    feature_text,
    primary_plan,
):
    sources = [
        feature_text
    ]

    for item in primary_plan.get(
        "files",
        [],
    ):
        sources.append(
            str(
                item.get(
                    "what",
                    ""
                )
            )
        )

    combined = "\n".join(
        sources
    )

    explicit = re.findall(
        r"`([A-Za-z_][A-Za-z0-9_.:-]{2,})`",
        combined,
    )

    identifiers = re.findall(
        r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b",
        combined,
    )

    terms = []
    seen = set()

    for raw in (
        explicit
        + identifiers
    ):
        term = raw.strip(
            "._:-"
        )

        normalized = term.lower()

        if (
            len(
                normalized
            )
            < 4
            or normalized
            in _CALIBRATION_V2_STOPWORDS
            or normalized
            in seen
        ):
            continue

        seen.add(
            normalized
        )

        terms.append(
            term
        )

        if len(
            terms
        ) >= 28:
            break

    return terms

def _calibration_v2_python_symbols(
    text,
):
    try:
        tree = ast.parse(
            text
        )
    except SyntaxError:
        return []

    symbols = []

    for node in ast.walk(
        tree
    ):
        if not isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        ):
            continue

        start = int(
            getattr(
                node,
                "lineno",
                0,
            )
            or 0
        )

        end = int(
            getattr(
                node,
                "end_lineno",
                start,
            )
            or start
        )

        if (
            start <= 0
            or end < start
        ):
            continue

        symbols.append({
            "name": getattr(
                node,
                "name",
                "<anonymous>",
            ),
            "node_type": (
                type(
                    node
                ).__name__
            ),
            "line_start": start,
            "line_end": end,
            "span": (
                end
                - start
                + 1
            ),
        })

    symbols.sort(
        key=lambda item: (
            item[
                "span"
            ],
            item[
                "line_start"
            ],
        )
    )

    return symbols

def _calibration_v2_smallest_enclosing_symbol(
    symbols,
    start,
    end,
    *,
    max_span,
):
    for symbol in symbols:
        if (
            symbol[
                "span"
            ]
            <= max_span
            and symbol[
                "line_start"
            ]
            <= start
            and symbol[
                "line_end"
            ]
            >= end
        ):
            return symbol

    return None

def _calibration_v2_snippet(
    lines,
    start,
    end,
):
    left = max(
        1,
        start
        - CALIBRATION_V2_SNIPPET_RADIUS,
    )

    right = min(
        len(
            lines
        ),
        end
        + CALIBRATION_V2_SNIPPET_RADIUS,
    )

    rendered = []

    for line_number in range(
        left,
        right + 1,
    ):
        rendered.append(
            f"{line_number:>5}: "
            + lines[
                line_number
                - 1
            ]
        )

    return "\n".join(
        rendered
    )

def _calibration_v3_identifiers_from_primary_regions(
    text,
    primary_items,
):
    """Collect exact identifiers mentioned in the primary planned source."""
    lines = text.splitlines()
    identifiers = set()

    for _, item in primary_items:
        start = int(
            item.get(
                "line_start",
                0,
            )
            or 0
        )

        end = int(
            item.get(
                "line_end",
                0,
            )
            or 0
        )

        if (
            start <= 0
            or end <= 0
        ):
            continue

        start, end = (
            min(
                start,
                end,
            ),
            max(
                start,
                end,
            ),
        )

        snippet = "\n".join(
            lines[
                max(
                    0,
                    start - 1
                ):
                min(
                    len(
                        lines
                    ),
                    end
                )
            ]
        )

        for identifier in re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*\b",
            snippet,
        ):
            if (
                len(
                    identifier
                )
                >= 3
                and identifier
                not in {
                    "self",
                    "cls",
                    "True",
                    "False",
                    "None",
                    "return",
                    "class",
                    "def",
                    "from",
                    "import",
                    "with",
                    "while",
                    "for",
                    "if",
                    "else",
                    "elif",
                }
            ):
                identifiers.add(
                    identifier
                )

    return identifiers

def _calibration_v3_top_level_nodes(
    text,
):
    try:
        parsed = ast.parse(
            text
        )
    except SyntaxError:
        return []

    nodes = []

    for node in parsed.body:
        start = int(
            getattr(
                node,
                "lineno",
                0,
            )
            or 0
        )

        end = int(
            getattr(
                node,
                "end_lineno",
                start,
            )
            or start
        )

        if (
            start <= 0
            or end < start
        ):
            continue

        name = getattr(
            node,
            "name",
            None,
        )

        if isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        ):
            kind = (
                type(
                    node
                ).__name__
            )

        elif isinstance(
            node,
            (
                ast.Assign,
                ast.AnnAssign,
            ),
        ):
            kind = "assignment"

        else:
            kind = (
                type(
                    node
                ).__name__
            )

        nodes.append({
            "node": node,
            "name": name,
            "kind": kind,
            "line_start": start,
            "line_end": end,
            "span": (
                end
                - start
                + 1
            ),
        })

    return nodes

def _calibration_v3_alias_blocks(
    top_level_nodes,
):
    """Find compact runs of module-level assignments near the module tail."""
    assignment_nodes = [
        item
        for item in top_level_nodes
        if item[
            "kind"
        ]
        == "assignment"
    ]

    if not assignment_nodes:
        return []

    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    for item in assignment_nodes:
        if not current:
            current = [
                item
            ]
            continue

        previous = current[
            -1
        ]

        if (
            item[
                "line_start"
            ]
            <= previous[
                "line_end"
            ]
            + 3
        ):
            current.append(
                item
            )

        else:
            if len(
                current
            ) >= 2:
                blocks.append(
                    current
                )

            current = [
                item
            ]

    if len(
        current
    ) >= 2:
        blocks.append(
            current
        )

    compact = []

    for block in blocks:
        start = block[
            0
        ][
            "line_start"
        ]

        end = block[
            -1
        ][
            "line_end"
        ]

        if (
            end
            - start
            + 1
            <= CALIBRATION_V3_ALIAS_BLOCK_MAX_SPAN
        ):
            compact.append(
                (
                    start,
                    end,
                )
            )

    return compact

def build_uncertainty_candidates_v2(
    tree,
    feature_text,
    primary_plan,
):
    """Build bounded structural candidates without granting write authority."""
    items_by_path: dict[str, list[tuple[int, dict[str, Any]]]] = {}

    for item_index, item in enumerate(
        primary_plan.get(
            "files",
            [],
        )
    ):
        path = _safe_planner_relative_path(
            item.get(
                "path"
            )
        )

        if not path:
            continue

        items_by_path.setdefault(
            path,
            [],
        ).append(
            (
                item_index,
                item,
            )
        )

    task_terms = _calibration_v2_task_terms(
        feature_text,
        primary_plan,
    )

    raw_candidates = []
    candidate_keys = set()

    def add_candidate(
        *,
        path,
        start,
        end,
        kind,
        reason,
        score,
        source_item_index=None,
        auto_add=False,
    ):
        start = int(
            start
        )

        end = int(
            end
        )

        if (
            start <= 0
            or end < start
        ):
            return

        key = (
            path,
            start,
            end,
            kind,
        )

        if key in candidate_keys:
            return

        if _calibration_v2_interval_is_covered(
            primary_plan,
            path,
            start,
            end,
        ):
            return

        candidate_keys.add(
            key
        )

        raw_candidates.append({
            "path": path,
            "line_start": start,
            "line_end": end,
            "kind": kind,
            "reason": reason,
            "score": float(
                score
            ),
            "source_item_index": (
                source_item_index
            ),
            "auto_add": bool(
                auto_add
            ),
        })

    for path, indexed_items in sorted(
        items_by_path.items()
    ):
        file_path = (
            Path(
                tree
            )
            / path
        )

        if not file_path.exists():
            continue

        text = file_path.read_text(
            errors="replace"
        )

        lines = text.splitlines()

        if not lines:
            continue

        symbols = (
            _calibration_v2_python_symbols(
                text
            )
            if path.endswith(
                ".py"
            )
            else []
        )

        top_level_nodes = (
            _calibration_v3_top_level_nodes(
                text
            )
            if path.endswith(
                ".py"
            )
            else []
        )

        primary_identifiers = (
            _calibration_v3_identifiers_from_primary_regions(
                text,
                indexed_items,
            )
        )

        # ---------------------------------------------------------------
        # V8.5 structural reachability.
        #
        # 1) If the primary planned code references a helper symbol that is
        #    defined elsewhere in the same file, expose that exact definition
        #    as bounded contingent scope. This is the Click _resolve_context
        #    miss class.
        # ---------------------------------------------------------------
        auto_referenced = 0

        for symbol in symbols:
            if (
                symbol[
                    "name"
                ]
                not in primary_identifiers
                or symbol[
                    "span"
                ]
                > CALIBRATION_V3_REFERENCE_SYMBOL_MAX_SPAN
            ):
                continue

            add_candidate(
                path=path,
                start=symbol[
                    "line_start"
                ],
                end=symbol[
                    "line_end"
                ],
                kind="referenced_symbol",
                reason=(
                    f"primary planned source references "
                    f"{symbol['node_type']} {symbol['name']}"
                ),
                score=132,
                auto_add=(
                    auto_referenced
                    < CALIBRATION_V3_MAX_AUTO_REFERENCED_SYMBOLS
                ),
            )

            auto_referenced += 1

        # ---------------------------------------------------------------
        # 2) Add a bounded pre-insertion anchor before each primary range.
        #    This captures implementations that insert a new top-level class
        #    or helper shortly before the exact line the planner named.
        # ---------------------------------------------------------------
        auto_insertion = 0

        for item_index, item in indexed_items:
            start = int(
                item.get(
                    "line_start",
                    0,
                )
                or 0
            )

            if start <= 1:
                continue

            anchor_start = max(
                1,
                start
                - CALIBRATION_V3_INSERTION_ANCHOR_RADIUS,
            )

            anchor_end = (
                start
                - 1
            )

            if anchor_start <= anchor_end:
                add_candidate(
                    path=path,
                    start=anchor_start,
                    end=anchor_end,
                    kind="pre_insertion_anchor",
                    reason=(
                        "bounded insertion slot immediately before "
                        f"primary item {item_index}"
                    ),
                    score=118,
                    source_item_index=(
                        item_index
                    ),
                    auto_add=(
                        auto_insertion
                        < CALIBRATION_V3_MAX_AUTO_INSERTION_ANCHORS
                    ),
                )

                auto_insertion += 1

        # ---------------------------------------------------------------
        # 3) Compact module-level assignment/alias blocks are common
        #    registration points for extensions and public aliases. Add a
        #    small number as contingent surfaces. This is the Jinja alias
        #    block miss class.
        # ---------------------------------------------------------------
        auto_alias_blocks = 0

        for alias_start, alias_end in (
            _calibration_v3_alias_blocks(
                top_level_nodes
            )
        ):
            add_candidate(
                path=path,
                start=alias_start,
                end=alias_end,
                kind="module_alias_block",
                reason=(
                    "compact module-level alias/registry assignment block"
                ),
                score=124,
                auto_add=(
                    auto_alias_blocks
                    < CALIBRATION_V3_MAX_AUTO_ALIAS_BLOCKS
                ),
            )

            auto_alias_blocks += 1

        # A bounded module import/header candidate.
        if path.endswith(
            ".py"
        ):
            try:
                parsed = ast.parse(
                    text
                )

                import_nodes = [
                    node
                    for node in parsed.body
                    if isinstance(
                        node,
                        (
                            ast.Import,
                            ast.ImportFrom,
                        ),
                    )
                ]

                if import_nodes:
                    import_start = min(
                        int(
                            node.lineno
                        )
                        for node in import_nodes
                    )

                    import_end = max(
                        int(
                            getattr(
                                node,
                                "end_lineno",
                                node.lineno,
                            )
                        )
                        for node in import_nodes
                    )

                    add_candidate(
                        path=path,
                        start=import_start,
                        end=import_end,
                        kind="import_header",
                        reason=(
                            "bounded module import/header support surface"
                        ),
                        score=82,
                    )

            except SyntaxError:
                pass

        # Small boundary halos around every bounded primary declaration.
        for item_index, item in indexed_items:
            start = int(
                item.get(
                    "line_start",
                    0,
                )
                or 0
            )

            end = int(
                item.get(
                    "line_end",
                    0,
                )
                or 0
            )

            if (
                start <= 0
                or end <= 0
            ):
                continue

            start, end = (
                min(
                    start,
                    end,
                ),
                max(
                    start,
                    end,
                ),
            )

            before_start = max(
                1,
                start
                - CALIBRATION_V2_BOUNDARY_HALO,
            )

            before_end = (
                start
                - 1
            )

            if before_start <= before_end:
                add_candidate(
                    path=path,
                    start=before_start,
                    end=before_end,
                    kind="boundary_halo",
                    reason=(
                        "small pre-boundary support surface "
                        f"for primary item {item_index}"
                    ),
                    score=100,
                    source_item_index=(
                        item_index
                    ),
                    auto_add=True,
                )

            after_start = (
                end
                + 1
            )

            after_end = min(
                len(
                    lines
                ),
                end
                + CALIBRATION_V2_BOUNDARY_HALO,
            )

            if after_start <= after_end:
                add_candidate(
                    path=path,
                    start=after_start,
                    end=after_end,
                    kind="boundary_halo",
                    reason=(
                        "small post-boundary support surface "
                        f"for primary item {item_index}"
                    ),
                    score=100,
                    source_item_index=(
                        item_index
                    ),
                    auto_add=True,
                )

            enclosing = (
                _calibration_v2_smallest_enclosing_symbol(
                    symbols,
                    start,
                    end,
                    max_span=(
                        CALIBRATION_V2_SYMBOL_MAX_SPAN
                    ),
                )
            )

            if enclosing is not None:
                add_candidate(
                    path=path,
                    start=enclosing[
                        "line_start"
                    ],
                    end=enclosing[
                        "line_end"
                    ],
                    kind="enclosing_symbol",
                    reason=(
                        f"{enclosing['node_type']} "
                        f"{enclosing['name']} encloses "
                        f"primary item {item_index}"
                    ),
                    score=88,
                    source_item_index=(
                        item_index
                    ),
                    auto_add=(
                        enclosing[
                            "span"
                        ]
                        <= CALIBRATION_V2_AUTO_SYMBOL_MAX_SPAN
                    ),
                )

        # Task-term hits inside already-planned files.
        lowered_lines = [
            line.lower()
            for line in lines
        ]

        term_hits = 0

        for term in task_terms:
            needle = term.lower()

            for line_index, lowered in enumerate(
                lowered_lines,
                start=1,
            ):
                if needle not in lowered:
                    continue

                enclosing = (
                    _calibration_v2_smallest_enclosing_symbol(
                        symbols,
                        line_index,
                        line_index,
                        max_span=(
                            CALIBRATION_V2_SYMBOL_MAX_SPAN
                        ),
                    )
                )

                if enclosing is not None:
                    add_candidate(
                        path=path,
                        start=enclosing[
                            "line_start"
                        ],
                        end=enclosing[
                            "line_end"
                        ],
                        kind="task_term_symbol",
                        reason=(
                            f"task term {term!r} occurs in "
                            f"{enclosing['node_type']} "
                            f"{enclosing['name']}"
                        ),
                        score=74,
                    )

                else:
                    add_candidate(
                        path=path,
                        start=max(
                            1,
                            line_index
                            - CALIBRATION_V2_TASK_HIT_RADIUS,
                        ),
                        end=min(
                            len(
                                lines
                            ),
                            line_index
                            + CALIBRATION_V2_TASK_HIT_RADIUS,
                        ),
                        kind="task_term_window",
                        reason=(
                            f"task term {term!r} occurs here"
                        ),
                        score=66,
                    )

                term_hits += 1

                if term_hits >= 14:
                    break

            if term_hits >= 14:
                break

        # Common extension / registry / export support surfaces.
        special_pattern = re.compile(
            r"("
            r"__all__"
            r"|register"
            r"|registry"
            r"|export"
            r"|extension"
            r"|plugin"
            r"|loader"
            r"|completion"
            r")",
            re.IGNORECASE,
        )

        special_hits = 0

        for line_index, line in enumerate(
            lines,
            start=1,
        ):
            if not special_pattern.search(
                line
            ):
                continue

            enclosing = (
                _calibration_v2_smallest_enclosing_symbol(
                    symbols,
                    line_index,
                    line_index,
                    max_span=(
                        CALIBRATION_V2_SYMBOL_MAX_SPAN
                    ),
                )
            )

            if enclosing is not None:
                add_candidate(
                    path=path,
                    start=enclosing[
                        "line_start"
                    ],
                    end=enclosing[
                        "line_end"
                    ],
                    kind="structural_surface",
                    reason=(
                        "common registration/export/extension "
                        f"surface in {enclosing['name']}"
                    ),
                    score=58,
                )

            else:
                add_candidate(
                    path=path,
                    start=max(
                        1,
                        line_index
                        - CALIBRATION_V2_TASK_HIT_RADIUS,
                    ),
                    end=min(
                        len(
                            lines
                        ),
                        line_index
                        + CALIBRATION_V2_TASK_HIT_RADIUS,
                    ),
                    kind="structural_surface",
                    reason=(
                        "common registration/export/extension "
                        "support surface"
                    ),
                    score=54,
                )

            special_hits += 1

            if special_hits >= 8:
                break

    raw_candidates.sort(
        key=lambda item: (
            -item[
                "score"
            ],
            item[
                "path"
            ],
            item[
                "line_start"
            ],
            item[
                "line_end"
            ],
        )
    )

    boundary_auto = 0
    symbol_auto = 0
    final_candidates = []

    for raw in raw_candidates:
        auto_add = bool(
            raw[
                "auto_add"
            ]
        )

        if (
            auto_add
            and raw[
                "kind"
            ]
            == "boundary_halo"
        ):
            if (
                boundary_auto
                >= CALIBRATION_V2_MAX_AUTO_BOUNDARY
            ):
                auto_add = False
            else:
                boundary_auto += 1

        if (
            auto_add
            and raw[
                "kind"
            ]
            == "enclosing_symbol"
        ):
            if (
                symbol_auto
                >= CALIBRATION_V2_MAX_AUTO_SYMBOLS
            ):
                auto_add = False
            else:
                symbol_auto += 1

        raw = {
            **raw,
            "auto_add": (
                auto_add
            ),
        }

        final_candidates.append(
            raw
        )

        if len(
            final_candidates
        ) >= CALIBRATION_V2_MAX_CANDIDATES:
            break

    rendered = []

    for index, candidate in enumerate(
        final_candidates,
        start=1,
    ):
        candidate_id = (
            f"C{index:03d}"
        )

        candidate[
            "candidate_id"
        ] = candidate_id

        file_path = (
            Path(
                tree
            )
            / candidate[
                "path"
            ]
        )

        lines = file_path.read_text(
            errors="replace"
        ).splitlines()

        candidate[
            "snippet"
        ] = _calibration_v2_snippet(
            lines,
            candidate[
                "line_start"
            ],
            candidate[
                "line_end"
            ],
        )

        rendered.append(
            candidate
        )

    return rendered

def _calibration_v2_candidate_prompt_payload(
    candidates,
):
    payload = []

    rendered_chars = 0

    for candidate in candidates:
        item = {
            "candidate_id": candidate[
                "candidate_id"
            ],
            "path": candidate[
                "path"
            ],
            "line_start": candidate[
                "line_start"
            ],
            "line_end": candidate[
                "line_end"
            ],
            "kind": candidate[
                "kind"
            ],
            "auto": bool(
                candidate[
                    "auto_add"
                ]
            ),
            "reason": candidate[
                "reason"
            ],
            "snippet": candidate[
                "snippet"
            ],
        }

        encoded = json.dumps(
            item,
            ensure_ascii=False,
        )

        if (
            rendered_chars
            + len(
                encoded
            )
            > CALIBRATION_V2_MAX_PROMPT_CHARS
        ):
            break

        rendered_chars += len(
            encoded
        )

        payload.append(
            item
        )

    return payload

def apply_uncertainty_calibration_v2(
    primary_plan,
    candidates,
    payload,
):
    """Apply only monotonic uncertainty changes and candidate-backed scope."""
    files = copy.deepcopy(
        primary_plan.get(
            "files",
            [],
        )
    )

    downgraded = 0

    raw_downgrades = payload.get(
        "downgrade_item_indices",
        [],
    )

    if not isinstance(
        raw_downgrades,
        list,
    ):
        raw_downgrades = []

    for raw_index in raw_downgrades:
        try:
            item_index = int(
                raw_index
            )
        except Exception:
            continue

        if not (
            0
            <= item_index
            < len(
                files
            )
        ):
            continue

        current = str(
            files[
                item_index
            ].get(
                "commitment",
                ScopeCommitment.COMMITTED.value,
            )
        ).strip().lower()

        if (
            current
            != ScopeCommitment.CONTINGENT.value
        ):
            files[
                item_index
            ][
                "commitment"
            ] = (
                ScopeCommitment.CONTINGENT.value
            )

            downgraded += 1

    candidate_by_id = {
        candidate[
            "candidate_id"
        ]: candidate
        for candidate in candidates
    }

    auto_ids = [
        candidate[
            "candidate_id"
        ]
        for candidate in candidates
        if candidate[
            "auto_add"
        ]
    ]

    raw_selected = payload.get(
        "selected_candidate_ids",
        [],
    )

    if not isinstance(
        raw_selected,
        list,
    ):
        raw_selected = []

    model_selected = []

    for candidate_id in raw_selected:
        candidate_id = str(
            candidate_id
        ).strip()

        if (
            candidate_id
            not in candidate_by_id
            or candidate_id
            in auto_ids
            or candidate_id
            in model_selected
        ):
            continue

        model_selected.append(
            candidate_id
        )

        if len(
            model_selected
        ) >= CALIBRATION_V2_MAX_MODEL_SELECTED:
            break

    selected_ids = []

    for candidate_id in (
        auto_ids
        + model_selected
    ):
        if candidate_id not in selected_ids:
            selected_ids.append(
                candidate_id
            )

    existing = {
        (
            _safe_planner_relative_path(
                item.get(
                    "path"
                )
            ),
            str(
                item.get(
                    "action",
                    "modify",
                )
            ).lower(),
            int(
                item.get(
                    "line_start",
                    0,
                )
                or 0
            ),
            int(
                item.get(
                    "line_end",
                    0,
                )
                or 0
            ),
            str(
                item.get(
                    "commitment",
                    ScopeCommitment.COMMITTED.value,
                )
            ).lower(),
        )
        for item in files
    }

    added = []
    selected_kinds: dict[str, int] = {}

    for candidate_id in selected_ids:
        candidate = candidate_by_id[
            candidate_id
        ]

        item = {
            "path": candidate[
                "path"
            ],
            "action": "modify",
            "line_start": candidate[
                "line_start"
            ],
            "line_end": candidate[
                "line_end"
            ],
            "commitment": (
                ScopeCommitment.CONTINGENT.value
            ),
            "what": (
                f"[{candidate['kind']}] "
                f"{candidate['reason']}"
            )[:240],
        }

        key = (
            item[
                "path"
            ],
            item[
                "action"
            ],
            item[
                "line_start"
            ],
            item[
                "line_end"
            ],
            item[
                "commitment"
            ],
        )

        if key in existing:
            continue

        existing.add(
            key
        )

        added.append(
            item
        )

        selected_kinds[
            candidate[
                "kind"
            ]
        ] = (
            selected_kinds.get(
                candidate[
                    "kind"
                ],
                0,
            )
            + 1
        )

    return {
        "plan": {
            **copy.deepcopy(
                primary_plan
            ),
            "files": (
                files
                + added
            ),
        },
        "downgraded_count": downgraded,
        "added_contingent_count": len(
            added
        ),
        "auto_added_count": sum(
            1
            for candidate_id in auto_ids
            if candidate_id
            in selected_ids
        ),
        "model_selected_count": len(
            model_selected
        ),
        "selected_candidate_ids": (
            selected_ids
        ),
        "auto_candidate_ids": (
            auto_ids
        ),
        "model_selected_candidate_ids": (
            model_selected
        ),
        "selected_candidate_kinds": (
            selected_kinds
        ),
    }


# fmt: on
