#!/usr/bin/env python3
"""Run the maintained offline Action Feasibility corpus boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.runtime.px4_gazebo_route.action_feasibility_corpus import (
    verify_action_feasibility_corpus_through_core,
)


DEFAULT_MANIFEST = Path(
    "tests/golden/action_feasibility/px4_v1/manifest.json"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    verdict = verify_action_feasibility_corpus_through_core(args.manifest)
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    if verdict["status"] != "verified":
        return 1
    if verdict["case_count"] < 2:
        return 1
    statuses = {
        item["adapter_result"]["feasibility_status"]
        for item in verdict["case_verdicts"]
    }
    if "verified_feasible" not in statuses:
        return 1
    if not ({"blocked", "unverified"} & statuses):
        return 1
    if any(
        verdict[key] is not False
        for key in (
            "approval_created",
            "dispatch_authority_created",
            "execution_invoked",
            "progress_claimed",
            "completion_claimed",
        )
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
