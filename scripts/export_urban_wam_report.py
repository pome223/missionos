#!/usr/bin/env python3
"""Export only reviewed, identity-free simulator measurements for a public report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.verify_urban_wam_trial import verify  # noqa: E402


def export(roots, output, gpu_cleanup=None):
    runs = []
    for root in roots:
        result = verify(root)
        session = root / "session"
        events = [
            json.loads(line)
            for line in (session / "events.jsonl").read_text().splitlines()
        ]
        begin = next(e for e in events if e["event"] == "urban_route_dispatch")[
            "observed_start"
        ]
        end = next(e for e in events if e["event"] == "urban_goal_observed")["observed"]
        first, last = begin["pose_simulation_time_ns"], end["pose_simulation_time_ns"]
        records = [
            json.loads(line)
            for line in (session / "telemetry.jsonl").read_text().splitlines()
        ]
        records = (
            [begin]
            + [
                r
                for r in records
                if r["pose_simulation_time_ns"] is not None
                and first < r["pose_simulation_time_ns"] < last
            ]
            + [end]
        )
        trajectory = [
            [(r["pose_simulation_time_ns"] - first) / 1e9, *r["gazebo_pose_enu_m"]]
            for r in records
        ]
        # verify() constructs an explicit measurement whitelist. It emits no
        # session/instruction IDs, machine paths, credentials or cloud identities.
        run = {
            "verification": result,
            "trajectory_columns": [
                "simulation_seconds",
                "east_m",
                "north_m",
                "altitude_m",
            ],
            "trajectory": trajectory,
        }
        if result["model_invoked"]:
            request = json.loads((root / "input/request.json").read_text())
            receipt = json.loads((root / "forecast/result.json").read_text())[
                "runtime_invocation_evidence"
            ]
            selection = json.loads((session / "urban-selection.json").read_text())
            run["model"] = {
                key: receipt[key]
                for key in (
                    "model_sha256",
                    "actual_model_calls",
                    "sampling_steps",
                    "context_size",
                    "model_load_seconds",
                    "forecast_seconds",
                    "peak_allocated_gpu_bytes",
                )
            }
            run["model"].update(
                nominal_horizon_seconds=8,
                model_time_alignment_verified=False,
                prefix_path_length_m=5,
                goal_image_used_for_scoring_only=True,
                observation_age_at_selection_seconds=selection[
                    "observation_age_seconds"
                ],
                observation_age_at_dispatch_seconds=next(
                    e for e in events if e["event"] == "urban_model_selection_consumed"
                )["observation_age_seconds"],
                input_frame_simulation_time_ns=request["source_timing"][
                    "simulation_time_ns"
                ],
            )
        runs.append(run)
    result = {
        "schema_version": "missionos_urban_public_report.v1",
        "scope": "bounded_engineering_trials_not_randomized_benchmark",
        "physical_hardware_executed": False,
        "learned_navigation_benefit_established": False,
        "collision_prediction_validated": False,
        "receding_horizon_replanning_implemented": False,
        "runs": runs,
    }
    if gpu_cleanup is not None:
        cleanup = json.loads(gpu_cleanup.read_text())
        result["resources"] = {
            key: cleanup[key]
            for key in (
                "created_vm_absent",
                "created_boot_disk_absent",
                "duration_upper_bound_minutes",
                "current_run_estimate_upper_bound_usd",
                "total_task_conservative_estimate_upper_bound_usd",
                "user_total_cap_usd",
                "invoice_reconciled",
            )
        }
        result["resources"]["retained_source_sha256"] = hashlib.sha256(
            gpu_cleanup.read_bytes()
        ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(result, indent=2, allow_nan=False)
    # Keep each numeric sample on one line so the reviewed trace is readable.
    rendered = re.sub(
        r"\[\s*[-0-9eE+.,\s]+\]", lambda m: json.dumps(json.loads(m.group())), rendered
    )
    output.write_text(rendered + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu-cleanup", type=Path)
    args = parser.parse_args()
    export(args.root, args.output, args.gpu_cleanup)


if __name__ == "__main__":
    main()
