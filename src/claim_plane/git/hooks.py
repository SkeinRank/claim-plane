"""Git integration: a pre-merge verification helper.

Installs a check that, before a branch merges into main, runs every changed
Python file's defined artifacts through the plane and blocks the merge if any
collide with another owner's grants. This is the enforcement point — the moment
where undeclared drift is caught deterministically instead of by a human in
review.

Usage (manual, no magic):
    # in CI or a pre-merge hook, for the branch being merged:
    python -m claim_plane.git.hooks --owner branch-a --files $(git diff --name-only main...HEAD)

Exit code 2 on collision makes it CI-friendly.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from claim_plane.core import Plane
from claim_plane.core.extract import artifacts_to_claims


def verify_files(db_path: str, owner: str, files: list[str]) -> int:
    plane = Plane.open(db_path)
    all_problems = []
    for f in files:
        path = Path(f)
        if path.suffix != ".py" or not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        defined = artifacts_to_claims(text, owner=owner)
        problems = plane.verify_merge(defined)
        for v in problems:
            all_problems.append((f, v))
    plane.close()

    if not all_problems:
        print(f"claim-plane: clean — no undeclared collisions for {owner}.")
        return 0

    print(f"claim-plane: MERGE BLOCKED — {len(all_problems)} collision(s):")
    for f, v in all_problems:
        print(f"  {f}: {v.guidance}")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claim-plane-hook")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--db", default=".claim-plane/plane.db")
    parser.add_argument("--files", nargs="*", default=[])
    args = parser.parse_args(argv)
    return verify_files(args.db, args.owner, args.files)


if __name__ == "__main__":
    raise SystemExit(main())
