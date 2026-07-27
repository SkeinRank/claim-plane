"""Frozen Planner v1 policy used by the CooperBench research studies.

The values in this module are intentionally explicit. Changing any prompt or
calibration constant creates a new planner policy identity.
"""

from __future__ import annotations

PLANNER_POLICY_VERSION = "planner-v1"
PLANNER_MODEL = "deepseek/deepseek-v4-pro"

TEMPERATURE = 0.0
HTTP_RETRIES = 4
PLAN_RETRIES = 3
PLANNER_MAX_TOKENS = 3200
PLANNER_FALLBACK_MAX_TOKENS = 5200
PLANNER_FALLBACK_CONTEXT_CHARS = 7000

CALIBRATION_RETRIES = 2
CALIBRATION_MAX_TOKENS = 2200

CALIBRATION_V2_MAX_CANDIDATES = 48
CALIBRATION_V2_MAX_MODEL_SELECTED = 6
CALIBRATION_V2_BOUNDARY_HALO = 6
CALIBRATION_V2_MAX_AUTO_BOUNDARY = 10
CALIBRATION_V2_MAX_AUTO_SYMBOLS = 4
CALIBRATION_V2_AUTO_SYMBOL_MAX_SPAN = 90
CALIBRATION_V2_TASK_HIT_RADIUS = 10
CALIBRATION_V2_SYMBOL_MAX_SPAN = 240
CALIBRATION_V2_SNIPPET_RADIUS = 4
CALIBRATION_V2_MAX_PROMPT_CHARS = 26000

CALIBRATION_V3_REFERENCE_SYMBOL_MAX_SPAN = 220
CALIBRATION_V3_MAX_AUTO_REFERENCED_SYMBOLS = 6
CALIBRATION_V3_INSERTION_ANCHOR_RADIUS = 36
CALIBRATION_V3_MAX_AUTO_INSERTION_ANCHORS = 6
CALIBRATION_V3_ALIAS_BLOCK_MAX_SPAN = 48
CALIBRATION_V3_MAX_AUTO_ALIAS_BLOCKS = 3

FULL_FILE_CONTEXT_LIMIT = 80_000
TARGET_CONTEXT_RADIUS = 160

RUN_PLANNER_UNCERTAINTY_CALIBRATION = True

PLAN_PROMPT = """You are the planning layer for a coding agent.

Before any code is written, declare the repository scope the coding agent may
need to mutate.

Return STRICT JSON and nothing else:
{
  "files": [
    {
      "path": "relative/path.py",
      "action": "modify|create|delete|rename",
      "line_start": 10,
      "line_end": 30,
      "commitment": "committed|contingent",
      "what": "short description"
    }
  ]
}

Commitment semantics:
- `committed`: you have high confidence this surface is required for the task.
  It participates in initial write admission.
- `contingent`: the surface is a plausible fallback or optional mutation target,
  but the coder may not need it. It is declared now but does NOT reserve write
  ownership until the coder actually attempts to mutate it.
- Do not mark every possible file as committed merely to be safe.
- Do not omit a plausible mutation surface merely to make the plan look narrow.

Rules:
- line_start and line_end refer to the CURRENT numbered repository version.
- Use the narrowest honest line range you expect to modify.
- The same file may appear multiple times when separate non-overlapping regions have different commitments.
- Do not collapse a small committed edit and an optional fallback region into one broad committed range.
- For a new file, use 0 and 0.
- Declare every file you reasonably expect the coder might mutate.
- Use `contingent` for genuine uncertainty, not as a synonym for low importance.
- Do not invent unrelated files.
- This is a pre-write declaration. Do not implement the task.

TASK:
%s

CURRENT REPOSITORY CONTEXT:
%s

%s
"""

CALIBRATION_V2_PROMPT = """You are the second-pass uncertainty calibrator for a coding-agent planner.

A primary pre-write plan already exists. A deterministic repository analysis has
generated bounded SUPPORT CANDIDATES. Your task is to select only plausible
contingent mutation surfaces.

Return STRICT JSON only:

{
  "downgrade_item_indices": [0],
  "selected_candidate_ids": ["C004", "C011"],
  "reasoning_summary": "one short sentence"
}

Semantics:

- Primary committed items are surfaces expected to be mutated.
- A CONTINGENT surface is plausible supporting work that may be needed, but it
  must NOT reserve write ownership before the coder actually touches it.
- Selected candidates become CONTINGENT only.
- You cannot invent paths or ranges.
- You cannot add committed authority.
- You may downgrade a primary committed item to contingent when it is genuinely
  optional rather than central.

Selection policy:

1. Select a candidate when a normal implementation could reasonably need it.
2. Pay special attention to:
   - imports and module headers;
   - a few lines immediately adjacent to a planned edit;
   - the remainder of the same enclosing function/class;
   - registration, export, plugin, extension, loader, or completion wiring;
   - task-named symbols elsewhere in an already-planned file.
3. Do not select candidates merely because they exist.
4. Prefer a small high-recall set over broad whole-file authority.
5. Select at most %d non-auto candidates.
6. Candidates marked AUTO are already included deterministically; do not repeat
   them in selected_candidate_ids.
7. Zero model-selected candidates is valid.

TASK:
%s

PRIMARY PLAN:
%s

SUPPORT CANDIDATES:
%s

%s
"""


def policy_payload() -> dict[str, object]:
    """Return the frozen inputs that define Planner v1 behavior."""
    return {
        "policy_version": PLANNER_POLICY_VERSION,
        "model": PLANNER_MODEL,
        "temperature": TEMPERATURE,
        "http_retries": HTTP_RETRIES,
        "plan_retries": PLAN_RETRIES,
        "planner_max_tokens": PLANNER_MAX_TOKENS,
        "planner_fallback_max_tokens": PLANNER_FALLBACK_MAX_TOKENS,
        "planner_fallback_context_chars": PLANNER_FALLBACK_CONTEXT_CHARS,
        "calibration_retries": CALIBRATION_RETRIES,
        "calibration_max_tokens": CALIBRATION_MAX_TOKENS,
        "calibration_v2_max_candidates": CALIBRATION_V2_MAX_CANDIDATES,
        "calibration_v2_max_model_selected": CALIBRATION_V2_MAX_MODEL_SELECTED,
        "calibration_v2_boundary_halo": CALIBRATION_V2_BOUNDARY_HALO,
        "calibration_v2_max_auto_boundary": CALIBRATION_V2_MAX_AUTO_BOUNDARY,
        "calibration_v2_max_auto_symbols": CALIBRATION_V2_MAX_AUTO_SYMBOLS,
        "calibration_v2_auto_symbol_max_span": CALIBRATION_V2_AUTO_SYMBOL_MAX_SPAN,
        "calibration_v2_task_hit_radius": CALIBRATION_V2_TASK_HIT_RADIUS,
        "calibration_v2_symbol_max_span": CALIBRATION_V2_SYMBOL_MAX_SPAN,
        "calibration_v2_snippet_radius": CALIBRATION_V2_SNIPPET_RADIUS,
        "calibration_v2_max_prompt_chars": CALIBRATION_V2_MAX_PROMPT_CHARS,
        "calibration_v3_reference_symbol_max_span": CALIBRATION_V3_REFERENCE_SYMBOL_MAX_SPAN,
        "calibration_v3_max_auto_referenced_symbols": CALIBRATION_V3_MAX_AUTO_REFERENCED_SYMBOLS,
        "calibration_v3_insertion_anchor_radius": CALIBRATION_V3_INSERTION_ANCHOR_RADIUS,
        "calibration_v3_max_auto_insertion_anchors": CALIBRATION_V3_MAX_AUTO_INSERTION_ANCHORS,
        "calibration_v3_alias_block_max_span": CALIBRATION_V3_ALIAS_BLOCK_MAX_SPAN,
        "calibration_v3_max_auto_alias_blocks": CALIBRATION_V3_MAX_AUTO_ALIAS_BLOCKS,
        "full_file_context_limit": FULL_FILE_CONTEXT_LIMIT,
        "target_context_radius": TARGET_CONTEXT_RADIUS,
        "uncertainty_calibration": RUN_PLANNER_UNCERTAINTY_CALIBRATION,
        "plan_prompt": PLAN_PROMPT,
        "calibration_prompt": CALIBRATION_V2_PROMPT,
    }


def policy_fingerprint() -> str:
    """SHA-256 identity for the frozen Planner v1 policy."""
    import hashlib
    import json

    encoded = json.dumps(
        policy_payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


PLANNER_POLICY_FINGERPRINT = policy_fingerprint()
