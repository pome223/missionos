#!/usr/bin/env python3
"""Run the preregistered minimal LIBERO recovery capture cohort."""

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


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_RECOVERY_CAPTURE_COHORT"
SCHEMA_VERSION = "missionos.libero_recovery_capture_cohort.v1"
PREREGISTERED_CANDIDATES = ((0, 101), (1, 102), (2, 103), (3, 104))
PREREGISTERED_EVALUATION_HOLDOUTS = ((4, 0), (12, 0), (15, 0))
COHORTS = {
    "training": PREREGISTERED_CANDIDATES,
    "evaluation": PREREGISTERED_EVALUATION_HOLDOUTS,
}


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def parse_candidate(value: str) -> tuple[int, int]:
    try:
        init_text, seed_text = value.split(":", maxsplit=1)
        candidate = (int(init_text), int(seed_text))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("candidate must be INIT_INDEX:ENVIRONMENT_SEED") from exc
    if candidate not in set(PREREGISTERED_CANDIDATES + PREREGISTERED_EVALUATION_HOLDOUTS):
        raise argparse.ArgumentTypeError("candidate is outside the preregistered cohorts")
    return candidate


def execute(
    *, output_dir: Path, candidates: list[tuple[int, int]], cohort: str = "training"
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_recovery_capture_cohort_opt_in_required")
    if output_dir.exists():
        raise ValueError("libero_recovery_capture_cohort_output_exists")
    if not candidates or len(candidates) != len(set(candidates)):
        raise ValueError("libero_recovery_capture_cohort_candidates_invalid")
    if cohort not in COHORTS:
        raise ValueError("libero_recovery_capture_cohort_kind_invalid")
    if any(candidate not in COHORTS[cohort] for candidate in candidates):
        raise ValueError("libero_recovery_capture_cohort_candidate_not_preregistered")
    output_dir.mkdir(parents=True)

    records: list[dict[str, Any]] = []
    for init_index, environment_seed in candidates:
        for module in (fixture, probe, oracle):
            module.EPISODE_INIT_STATE_INDEX = init_index
            module.ENVIRONMENT_SEED = environment_seed
        oracle.MAXIMUM_ACTIONS = 128
        candidate_dir = output_dir / f"init-{init_index:02d}-seed-{environment_seed}"
        fixture_dir = candidate_dir / "fixture"
        oracle_dir = candidate_dir / "oracle"
        try:
            fixture_result = fixture.generate(output_dir=fixture_dir)
            oracle_result = oracle.execute_live(
                snapshot_path=fixture_dir / "fixture.npz",
                output_dir=oracle_dir,
                capture_transition=True,
            )
            capture_manifest = json.loads(
                (oracle_dir / "transition-capture/manifest.json").read_text()
            )
            records.append(
                {
                    "episode_init_state_index": init_index,
                    "environment_seed": environment_seed,
                    "status": "captured" if oracle_result["stable_success_observed"] else "failed",
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
                    "source_simulator_state_sha256": capture_manifest["source"][
                        "source_simulator_state_sha256"
                    ],
                    "source_fixture_sha256": fixture_result["fixture_sha256"],
                    "capture": oracle_result["transition_capture"],
                }
            )
        except Exception as exc:  # preserve bounded negative candidates
            records.append(
                {
                    "episode_init_state_index": init_index,
                    "environment_seed": environment_seed,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    captured = sum(record["status"] == "captured" for record in records)
    result_without_digest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if captured == len(records) else "complete_with_negative_results",
        "cohort": cohort,
        "candidate_count": len(records),
        "captured_count": captured,
        "records": records,
        "claim_boundary": {
            "privileged_oracle_generation": True,
            "training_examples_admitted": False,
            "evaluation_holdout_data_used_for_training": False,
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
    parser.add_argument("--cohort", choices=sorted(COHORTS), default="training")
    parser.add_argument("--candidate", type=parse_candidate, action="append")
    args = parser.parse_args()
    candidates = args.candidate or list(COHORTS[args.cohort])
    result = execute(
        output_dir=args.output_dir.resolve(), candidates=candidates, cohort=args.cohort
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["captured_count"] == result["candidate_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
