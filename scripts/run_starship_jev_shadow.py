#!/usr/bin/env python3
"""Run bounded Jev fault routing beside an unchanged local dispenser policy."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "packages/missionos-core/src")]

from scripts.start_starship_gateway import _read_secret  # noqa: E402
from src.runtime.starship_jev_shadow import run_shadow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approve-synthetic", action="store_true")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--fixture", action="store_true")
    choice.add_argument("--approve-live-jev-shadow", action="store_true",
                        help="At most 22 Jev calls; no model output reaches the simulator")
    parser.add_argument("--project")
    parser.add_argument("--secret", default="jev-api-key")
    args = parser.parse_args()
    if not args.approve_synthetic:
        parser.error("explicit synthetic simulation approval required")
    if args.output_dir.exists():
        parser.error("existing output is preserved; use a new directory")
    if args.approve_live_jev_shadow and (not args.project or any(
        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) is None for item in (args.project, args.secret)
    )):
        parser.error("a Secret Manager project and valid secret locator are required")
    try:
        if args.approve_live_jev_shadow:
            os.environ["TYPESAFE_API_KEY"] = _read_secret(args.project, args.secret)
            os.environ["MISSIONOS_STARSHIP_JEV_SHADOW_MODE"] = "live"
        result = run_shadow(args.output_dir, mode="fixture" if args.fixture else "live")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["verified"] else 2
    except Exception:
        print(json.dumps({"status": "failed", "reason": "jev_shadow_execution_failed",
                          "physical_execution": False}))
        return 2
    finally:
        if args.approve_live_jev_shadow:
            os.environ.pop("TYPESAFE_API_KEY", None)
            os.environ.pop("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", None)


if __name__ == "__main__":
    raise SystemExit(main())
