"""Execute one frozen OSS pilot acceptance contract in an isolated Git worktree."""

from __future__ import annotations

import argparse

from claim_plane.oss_pilot import run_oss_pilot_acceptance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m claim_plane.oss_pilot_acceptance")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--timeout", type=float, default=1200.0)
    args = parser.parse_args(argv)
    return run_oss_pilot_acceptance(args.repo, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
