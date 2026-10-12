#!/usr/bin/env python3
"""Internal worker for an already approved, source-bound Starship simulation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "packages/missionos-core/src")]

from src.runtime.starship_mission_control import execute_worker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    for channel in ("request", "grant", "telemetry"):
        parser.add_argument("--inflight-"+channel+"-fd", type=int)
    args = parser.parse_args()
    try:
        return execute_worker(args.state_dir, args.run_id, inflight_fds=(args.inflight_request_fd, args.inflight_grant_fd, args.inflight_telemetry_fd))
    except Exception:
        # Never write raw subprocess/provider exceptions or local credentials to logs.
        print("starship_worker_request_rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
