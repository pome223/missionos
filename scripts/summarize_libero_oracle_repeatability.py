#!/usr/bin/env python3
"""Summarize repeated same-fixture LIBERO oracle runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import fmean
from typing import Any


PRESERVATION_LIMIT_METRES = 0.005


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def summarize(*, reports: list[Path], output_path: Path) -> dict[str, Any]:
    if len(reports) < 3:
        raise ValueError("libero_oracle_repeatability_requires_at_least_three_runs")
    materials = [json.loads(path.read_text(encoding="utf-8")) for path in reports]
    for material in materials:
        supplied = material.get("result_sha256")
        without_digest = {
            key: value for key, value in material.items() if key != "result_sha256"
        }
        if supplied != _canonical_sha256(without_digest):
            raise RuntimeError("libero_oracle_repeatability_digest_mismatch")
    snapshots = {item.get("snapshot_sha256") for item in materials}
    if len(snapshots) != 1:
        raise RuntimeError("libero_oracle_repeatability_snapshot_mismatch")

    displacements = [
        float(item["protected_maximum_displacement_metres"]) for item in materials
    ]
    action_counts = [int(item["actions_applied"]) for item in materials]
    successful = [
        item.get("stable_success_observed") is True
        and item.get("terminal_goal_predicate_vector") == [True, True, True]
        and item.get("preservation_violation_observed") is False
        and float(item["protected_maximum_displacement_metres"])
        <= PRESERVATION_LIMIT_METRES
        for item in materials
    ]
    result_without_digest = {
        "schema_version": "missionos.libero_oracle_repeatability.v1",
        "status": (
            "same_fixture_oracle_repeatability_observed"
            if all(successful)
            else "same_fixture_oracle_repeatability_not_observed"
        ),
        "snapshot_sha256": next(iter(snapshots)),
        "run_count": len(materials),
        "all_runs_stable_success_within_preregistered_preservation_limit": all(successful),
        "preservation_limit_metres": PRESERVATION_LIMIT_METRES,
        "protected_displacement_metres": {
            "minimum": min(displacements),
            "maximum": max(displacements),
            "mean": fmean(displacements),
            "range": max(displacements) - min(displacements),
        },
        "action_counts": {
            "minimum": min(action_counts),
            "maximum": max(action_counts),
            "mean": fmean(action_counts),
            "range": max(action_counts) - min(action_counts),
            "values": action_counts,
        },
        "terminal_predicate_vectors": [
            item.get("terminal_goal_predicate_vector") for item in materials
        ],
        "runs": [
            {
                "report_sha256": item["result_sha256"],
                "stable_success_observed": item.get("stable_success_observed"),
                "actions_applied": item.get("actions_applied"),
                "success_first_observed_after_action": item.get(
                    "success_first_observed_after_action"
                ),
                "trajectory_events": item.get("trajectory_events"),
                "protected_maximum_displacement_metres": item.get(
                    "protected_maximum_displacement_metres"
                ),
            }
            for item in materials
        ],
        "claim_boundary": {
            "authority": "diagnostic_only",
            "controller_relative_repeatability_only": True,
            "preservation_limit_changed_from_preregistered_value": False,
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
        },
    }
    result = {
        **result_without_digest,
        "result_sha256": _canonical_sha256(result_without_digest),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(
        reports=[path.resolve() for path in args.report],
        output_path=args.output.resolve(),
    )
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result["all_runs_stable_success_within_preregistered_preservation_limit"]
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
