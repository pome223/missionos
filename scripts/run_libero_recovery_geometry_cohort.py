#!/usr/bin/env python3
"""Capture the preregistered one-init recovery geometry probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts import generate_libero_registered_skill_fixture as fixture
from scripts import probe_libero_displacement_curriculum as probe
from scripts import run_libero_curriculum_oracle as oracle


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_RECOVERY_GEOMETRY_COHORT"
SCHEMA_VERSION = "missionos.libero_recovery_geometry_cohort.v1"
DISTANCES_METRES = (0.01, 0.03, 0.05)
DIRECTIONS = ("positive_x", "negative_x", "positive_y", "negative_y")
BASELINE = (0.03, "negative_x")
PREREGISTERED_GEOMETRY_PROBE = tuple(
    (distance, direction)
    for distance in DISTANCES_METRES
    for direction in DIRECTIONS
    if (distance, direction) != BASELINE
)
EPISODE_INIT_STATE_INDEX = 0
ENVIRONMENT_SEED = 101


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def execute(*, output_dir: Path) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_recovery_geometry_cohort_opt_in_required")
    if output_dir.exists():
        raise ValueError("libero_recovery_geometry_cohort_output_exists")
    output_dir.mkdir(parents=True)
    for module in (fixture, probe, oracle):
        module.EPISODE_INIT_STATE_INDEX = EPISODE_INIT_STATE_INDEX
        module.ENVIRONMENT_SEED = ENVIRONMENT_SEED
    oracle.MAXIMUM_ACTIONS = 128

    records: list[dict[str, Any]] = []
    for distance, direction in PREREGISTERED_GEOMETRY_PROBE:
        fixture.DISPLACEMENT_METRES = distance
        fixture.DISPLACEMENT_DIRECTION = direction
        label = f"distance-{round(distance * 100):02d}cm-{direction}"
        candidate_dir = output_dir / label
        try:
            fixture_result = fixture.generate(output_dir=candidate_dir / "fixture")
            oracle_result = oracle.execute_live(
                snapshot_path=candidate_dir / "fixture/fixture.npz",
                output_dir=candidate_dir / "oracle",
                capture_transition=True,
            )
            records.append(
                {
                    "distance_metres": distance,
                    "direction": direction,
                    "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
                    "environment_seed": ENVIRONMENT_SEED,
                    "status": (
                        "captured" if oracle_result["stable_success_observed"] else "failed"
                    ),
                    "fixture_status": fixture_result["status"],
                    "oracle_status": oracle_result["status"],
                    "actions_applied": oracle_result["actions_applied"],
                    "stable_success_steps_completed": oracle_result[
                        "stable_success_steps_completed"
                    ],
                    "stable_success_observed": oracle_result["stable_success_observed"],
                    "preservation_violation_observed": oracle_result[
                        "preservation_violation_observed"
                    ],
                    "source_simulator_state_sha256": fixture_result["snapshot"][
                        "simulator_state_sha256"
                    ],
                    "source_fixture_sha256": fixture_result["fixture_sha256"],
                    "capture": oracle_result["transition_capture"],
                }
            )
        except Exception as exc:  # preserve negative geometry results
            records.append(
                {
                    "distance_metres": distance,
                    "direction": direction,
                    "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
                    "environment_seed": ENVIRONMENT_SEED,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    captured = sum(record["status"] == "captured" for record in records)
    result_without_digest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if captured == len(records) else "complete_with_negative_results",
        "probe_scope": "one_init_seed_across_remaining_preregistered_geometry",
        "candidate_count": len(records),
        "captured_count": captured,
        "records": records,
        "claim_boundary": {
            "privileged_oracle_generation": True,
            "training_examples_admitted": False,
            "training_invoked": False,
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
        },
    }
    result = {
        **result_without_digest,
        "result_sha256": _canonical_sha256(result_without_digest),
    }
    (output_dir / "cohort-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = execute(output_dir=args.output_dir.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["captured_count"] == result["candidate_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
