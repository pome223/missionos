#!/usr/bin/env python3
"""Curate a PX4/Gazebo horizontal route smoke run into golden fixtures."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

DEFAULT_GOLDEN_DIR = Path("tests/golden/px4_gazebo_route/horizontal_route_v1")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _pose_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sample_xy(row: dict[str, Any]) -> tuple[float, float]:
    sample = row["sample"]
    return (float(sample["x"]), float(sample["y"]))


def _pose_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    phase_counts = Counter(str(row["phase"]) for row in rows)
    pickup = next(row for row in rows if row["phase"] == "pickup")
    route = next(row for row in rows if row["phase"] == "route")
    completed = next(row for row in rows if row["phase"] == "completed")
    z_values = [float(row["sample"]["z"]) for row in rows]
    pickup_xy = _sample_xy(pickup)
    completed_xy = _sample_xy(completed)
    horizontal_progress_m = (
        (completed_xy[0] - pickup_xy[0]) ** 2 + (completed_xy[1] - pickup_xy[1]) ** 2
    ) ** 0.5
    return {
        "phase_counts": dict(sorted(phase_counts.items())),
        "pickup_pose_xy_m": list(pickup_xy),
        "route_pose_xy_m": list(_sample_xy(route)),
        "completed_pose_xy_m": list(completed_xy),
        "completed_pose_z_m": float(completed["sample"]["z"]),
        "max_pose_z_m": max(z_values),
        "min_pose_z_m": min(z_values),
        "horizontal_progress_m": horizontal_progress_m,
    }


def _summary_golden(summary: dict[str, Any], *, source_run_id: str) -> dict[str, Any]:
    curated = dict(summary)
    curated.pop("artifact_dir", None)
    curated["recorded_at"] = "<recorded_at>"
    curated["frozen_for_test"] = True
    curated["source_run_id"] = source_run_id
    return curated


def _mission_artifacts_golden(
    payload: dict[str, Any], *, source_run_id: str
) -> dict[str, Any]:
    curated = dict(payload)
    curated["recorded_at"] = "<recorded_at>"
    curated["frozen_for_test"] = True
    curated["source_run_id"] = source_run_id
    return curated


def _expected_invariants(
    summary: dict[str, Any], pose_summary: dict[str, Any]
) -> dict[str, Any]:
    return {
        "exact": {
            "final_status": "completed",
            "task_status": "completed",
            "dropoff_region_reached": True,
            "actual_px4_gazebo_horizontal_smoke_observed": True,
            "route_geofence_violation": False,
            "blocked_reasons": [],
            "pose_deviation_gate_active": True,
            "pose_deviation_aborted": False,
            "deviation_samples": [],
            "route_plan_schema_version": summary["route_plan_schema_version"],
            "route_allowlist_schema_version": summary["route_allowlist_schema_version"],
            "dispatch_schema_version": summary["dispatch_schema_version"],
            "progress_schema_version": summary["progress_schema_version"],
            "completion_gate_schema_version": summary["completion_gate_schema_version"],
            "runner_schema_version": summary["runner_schema_version"],
            "bounded_setpoint_stream_allowed": True,
            "unbounded_setpoint_stream_allowed": False,
            "offboard_mode_switch_ack_required": True,
            "offboard_mode_switch_ack_command_id": 176,
            "offboard_mode_switch_ack_observed": True,
            "offboard_mode_switch_ack_result_code": 0,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "px4_mission_upload_allowed": False,
        },
        "expected_numeric": {
            "setpoint_frames_sent": int(summary["setpoint_frames_sent"]),
            "setpoint_stream_duration_seconds": float(
                summary["setpoint_stream_duration_seconds"]
            ),
            "horizontal_progress_m": float(summary["horizontal_progress_m"]),
            "completed_pose_xy_m": [
                float(item) for item in summary["completed_pose_xy_m"]
            ],
            "route_pose_xy_m": [float(item) for item in summary["route_pose_xy_m"]],
            "completed_pose_z_m": float(summary["completed_pose_z_m"]),
            "pose_summary_horizontal_progress_m": float(
                pose_summary["horizontal_progress_m"]
            ),
            "pose_summary_completed_pose_xy_m": [
                float(item) for item in pose_summary["completed_pose_xy_m"]
            ],
            "pose_summary_route_pose_xy_m": [
                float(item) for item in pose_summary["route_pose_xy_m"]
            ],
            "pose_summary_completed_pose_z_m": float(
                pose_summary["completed_pose_z_m"]
            ),
        },
        "tolerances": {
            "setpoint_frames_sent_pct": 0.10,
            "setpoint_stream_duration_seconds_pct": 0.10,
            "horizontal_progress_m_pct": 0.10,
            "completed_pose_xy_m_abs": 0.5,
            "route_pose_xy_m_abs": 0.5,
            "completed_pose_z_m_abs": 0.2,
        },
    }


def curate(run_dir: Path, output_dir: Path) -> None:
    summary = _read_json(run_dir / "summary.json")
    mission_artifacts = _read_json(run_dir / "mission_artifacts.json")
    pose_rows = _pose_rows(run_dir / "pose_samples.jsonl")
    pose_summary = _pose_summary(pose_rows)
    source_run_id = run_dir.name

    _write_json(
        output_dir / "summary.golden.json",
        _summary_golden(summary, source_run_id=source_run_id),
    )
    _write_json(
        output_dir / "mission_artifacts.golden.json",
        _mission_artifacts_golden(mission_artifacts, source_run_id=source_run_id),
    )
    _write_json(output_dir / "pose_samples.summary.json", pose_summary)
    _write_json(
        output_dir / "expected_invariants.json",
        _expected_invariants(summary, pose_summary),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_GOLDEN_DIR)
    args = parser.parse_args()
    curate(args.run_dir, args.output_dir)
    print(json.dumps({"golden_dir": str(args.output_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
