#!/usr/bin/env python3
"""Fixture deployment -> observed inventory -> one numerical return forecast.

Development boundary smoke only. No live models, return dispatch, or Gateway
authority. Each invocation uses a fresh evidence directory and never retries.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_deployment_forecast import CASES, run_fixture  # noqa: E402
from src.runtime.starship_deployment_handoff_verifier import verify  # noqa: E402
from src.runtime.starship_replanning_verifier import metrics  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.approve_simulation:
        parser.error("explicit --approve-simulation required")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("fresh output directory required; failed attempts are preserved")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
        study = run_fixture(profile, args.case, args.output_dir)
        forecast = (args.output_dir / "forecast.json").read_bytes()
        verdict = verify(study, forecast)
        write_verified_input(args.output_dir / "verification.json", verdict)
        summary = {
            "schema": "missionos.m2_handoff_summary.v1",
            "case": args.case,
            "verification": verdict,
            "released_count": study["handoff"]["released_count"],
            "retained_count": study["handoff"]["retained_count"],
            "execution_state_time_s": study["handoff"]["execution_state"]["time_s"],
            "prediction_contact": metrics(study["prediction"]),
            "model_calls": 0,
            "ship_return_dispatched": False,
            "mission_completed": False,
        }
        write_verified_input(args.output_dir / "summary.json", summary)
        print(json.dumps(summary), flush=True)
        return 0 if verdict["passed"] else 1
    except Exception as error:
        write_verified_input(args.output_dir / "failure.json",
                             {"type": type(error).__name__, "message": str(error),
                              "retry_performed": False})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
