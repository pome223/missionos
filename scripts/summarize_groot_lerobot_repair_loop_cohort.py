"""Aggregate external governed Repair loop JSON artifacts as K/5 loops."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.runtime.groot_lerobot_repair_loop_cohort import (
    DEFAULT_MAX_ATTEMPTS_PER_LOOP,
    DEFAULT_PLANNED_LOOP_COUNT,
    build_repair_loop_cohort,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("loop_artifacts", nargs="+", type=Path)
    parser.add_argument("--planned-loop-count", type=int, default=DEFAULT_PLANNED_LOOP_COUNT)
    parser.add_argument(
        "--max-attempts-per-loop",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS_PER_LOOP,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        loop_results: list[dict[str, Any]] = []
        for path in args.loop_artifacts:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError(f"loop_artifact_object_required:{path}")
            loop_results.append(payload)
        report = build_repair_loop_cohort(
            loop_results,
            planned_loop_count=args.planned_loop_count,
            max_attempts_per_loop=args.max_attempts_per_loop,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, sort_keys=True))
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
