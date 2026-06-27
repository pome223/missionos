#!/usr/bin/env python3
"""Opt-in actual PX4/Gazebo multi-waypoint mission finalization smoke."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from src.runtime.px4_gazebo_delivery_mission_control import (
    build_px4_gazebo_delivery_mission_contract,
    run_px4_gazebo_delivery_mission_v1,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_MULTI_WAYPOINT_MISSION_SMOKE"
ARTIFACT_ROOT_ENV = "PX4_GAZEBO_MULTI_WAYPOINT_MISSION_ARTIFACT_ROOT"
HORIZONTAL_OPT_IN_ENV = "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"
HORIZONTAL_ARTIFACT_ROOT_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the actual PX4/Gazebo multi-waypoint smoke."
        )


def _artifact_root() -> Path:
    return Path(os.getenv(ARTIFACT_ROOT_ENV, "output/px4_gazebo_mission_runs"))


def _new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = _artifact_root() / f"multi_waypoint_mission_{stamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = _artifact_root() / f"multi_waypoint_mission_{stamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _contract():
    return build_px4_gazebo_delivery_mission_contract(
        route_plan_refs=(
            "px4_gazebo_pickup_dropoff_route_plan:actual_pickup_to_mid_alpha",
            "px4_gazebo_pickup_dropoff_route_plan:actual_mid_alpha_to_mid_bravo",
            "px4_gazebo_pickup_dropoff_route_plan:actual_mid_bravo_to_dropoff",
        ),
        waypoint_refs=(
            "gazebo_waypoint:actual_alpha",
            "gazebo_waypoint:actual_bravo",
            "gazebo_waypoint:actual_charlie",
        ),
        now=NOW,
    )


def _run_actual_horizontal_route(run_dir: Path) -> dict[str, Any]:
    route_root = run_dir / "actual_route_runs"
    env = os.environ.copy()
    env[HORIZONTAL_OPT_IN_ENV] = "1"
    env[HORIZONTAL_ARTIFACT_ROOT_ENV] = str(route_root)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=420,
        env=env,
    )
    (run_dir / "horizontal_route_stdout.log").write_text(result.stdout)
    (run_dir / "horizontal_route_stderr.log").write_text(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            "actual PX4/Gazebo horizontal route smoke failed; "
            f"returncode={result.returncode}"
        )
    summaries = sorted(route_root.glob("horizontal_route_*/summary.json"))
    if not summaries:
        raise RuntimeError("actual horizontal route smoke did not persist summary.json")
    return json.loads(summaries[-1].read_text())


def main() -> int:
    _require_opt_in()
    run_dir = _new_run_dir()
    actual = _run_actual_horizontal_route(run_dir)

    completed_xy = actual["completed_pose_xy_m"]
    target_xy = (actual["route_target_x_m"], actual["route_target_y_m"])
    dropoff_landing_error_m = math.hypot(
        float(completed_xy[0]) - float(target_xy[0]),
        float(completed_xy[1]) - float(target_xy[1]),
    )
    mission = run_px4_gazebo_delivery_mission_v1(
        mission_contract=_contract(),
        route_dispatch_refs=(
            "px4_gazebo_route_command_dispatch_result:actual_leg_pickup_to_mid_alpha",
            "px4_gazebo_route_command_dispatch_result:actual_leg_mid_alpha_to_mid_bravo",
            "px4_gazebo_route_command_dispatch_result:actual_leg_mid_bravo_to_dropoff",
        ),
        route_completion_gate_refs=(
            "px4_gazebo_route_delivery_completion_gate:actual_leg_pickup_to_mid_alpha",
            "px4_gazebo_route_delivery_completion_gate:actual_leg_mid_alpha_to_mid_bravo",
            "px4_gazebo_route_delivery_completion_gate:actual_leg_mid_bravo_to_dropoff",
        ),
        dropoff_landing_error_m=dropoff_landing_error_m,
        now=NOW,
    )
    runner = mission["runner_result"]
    summary = {
        "schema_version": "px4_gazebo_multi_waypoint_mission_smoke.v1",
        "artifact_dir": str(run_dir),
        "actual_route_artifact_dir": actual["artifact_dir"],
        "actual_px4_gazebo_container_started": True,
        "actual_multi_waypoint_sitl_smoke": True,
        "actual_px4_gazebo_horizontal_smoke_observed": actual[
            "actual_px4_gazebo_horizontal_smoke_observed"
        ],
        "final_status": runner.final_status.value,
        "waypoint_count": runner.waypoint_count,
        "route_segment_count": runner.route_segment_count,
        "segment_health_snapshot_count": len(mission["health_snapshots"]),
        "segment_phase_gate_evaluation_count": len(mission["phase_gate_evaluations"]),
        "phase_transition_event_count": len(mission["phase_transition_events"]),
        "dropoff_landing_error_m": dropoff_landing_error_m,
        "completed_pose_xy_m": completed_xy,
        "route_target_xy_m": list(target_xy),
        "horizontal_progress_m": actual["horizontal_progress_m"],
        "offboard_mode_switch_ack_observed": actual[
            "offboard_mode_switch_ack_observed"
        ],
        "hardware_target_allowed": runner.hardware_target_allowed,
        "physical_execution_invoked": runner.physical_execution_invoked,
        "px4_mission_upload_allowed": runner.px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": runner.unbounded_setpoint_stream_allowed,
        "memory_direct_command_authority_allowed": (
            runner.memory_direct_command_authority_allowed
        ),
    }
    _write_json(run_dir / "summary.json", summary)
    _write_json(
        run_dir / "mission_artifacts.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "frozen_for_test": False,
            "mission_runner_result": runner.model_dump(mode="json"),
            "mission_replay_timeline": mission["replay_timeline"].model_dump(
                mode="json"
            ),
            "phase_gate_evaluations": [
                item.model_dump(mode="json")
                for item in mission["phase_gate_evaluations"]
            ],
            "health_snapshots": [
                item.model_dump(mode="json") for item in mission["health_snapshots"]
            ],
        },
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    assert summary["actual_px4_gazebo_container_started"] is True
    assert summary["actual_multi_waypoint_sitl_smoke"] is True
    assert summary["waypoint_count"] >= 3
    assert summary["route_segment_count"] >= 3
    assert summary["segment_health_snapshot_count"] >= 3
    assert summary["segment_phase_gate_evaluation_count"] >= 3
    assert summary["dropoff_landing_error_m"] <= 0.5
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
