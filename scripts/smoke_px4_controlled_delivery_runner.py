#!/usr/bin/env python3
"""Runtime smoke for PX4-controlled Gazebo delivery runner v0."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.px4_controlled_delivery_runner import (
    run_px4_controlled_gazebo_delivery_mission_v0_task,
)
from src.runtime.px4_delivery_command_preflight import (
    build_px4_simulation_command_preflight_artifacts,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PX4_SITL_LOGS = "\n".join(
    [
        "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
        "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
        "INFO  [init] SIH simulator",
        "INFO  [simulator_sih] Simulation loop with 250 Hz",
        "INFO  [logger] logger started (mode=all)",
        "INFO  [px4] Startup script returned successfully",
    ]
)


def _preflight_artifacts():
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    return build_px4_simulation_command_preflight_artifacts(
        profile=profile,
        observation=observation,
        operator_approval_performed=True,
        now=NOW,
    )


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_controlled_gazebo_delivery_runner_v0",
            title="PX4 controlled delivery runner smoke",
            status="running",
            artifacts={"existing": {"case_id": "px4-controlled", "kept": True}},
        )
        updated = run_px4_controlled_gazebo_delivery_mission_v0_task(
            task["task_id"],
            preflight_artifacts=_preflight_artifacts(),
            now=NOW,
            task_store_factory=lambda: store,
        )

    dispatch_results = updated["artifacts"]["px4_simulation_mavlink_dispatch_results"]
    runner = updated["artifacts"]["px4_controlled_gazebo_delivery_runner_result"]
    summary = {
        "task_status": updated["status"],
        "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
        "dispatch_result_count": len(dispatch_results),
        "dispatch_schema": dispatch_results[0]["schema_version"],
        "all_dispatches_artifact_stub": all(
            item["dispatch_mode"] == "artifact_stub" for item in dispatch_results
        ),
        "runner_schema": runner["schema_version"],
        "final_status": runner["final_status"],
        "observed_delivery_phases": runner["observed_delivery_phases"],
        "pickup_reached": runner["pickup_reached"],
        "enroute_reached": runner["enroute_reached"],
        "dropoff_reached": runner["dropoff_reached"],
        "completed_reached": runner["completed_reached"],
        "all_dispatches_simulation_only": all(
            item["simulation_only"] is True for item in dispatch_results
        ),
        "all_dispatches_bounded_allowlist_enforced": all(
            item["bounded_allowlist_enforced"] is True for item in dispatch_results
        ),
        "all_dispatches_operator_approved": all(
            item["operator_approval_performed"] is True for item in dispatch_results
        ),
        "all_dispatches_no_raw_payload": all(
            item["raw_mavlink_payload_present"] is False for item in dispatch_results
        ),
        "all_dispatches_no_mavlink_socket": all(
            item["mavlink_socket_opened"] is False for item in dispatch_results
        ),
        "all_dispatches_no_mavlink_frame": all(
            item["mavlink_frame_sent"] is False for item in dispatch_results
        ),
        "all_dispatches_no_hardware_target": all(
            item["hardware_target_allowed"] is False for item in dispatch_results
        ),
        "runner_hardware_target_allowed": runner["hardware_target_allowed"],
        "runner_physical_execution_invoked": runner["physical_execution_invoked"],
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["task_status"] == "completed"
    assert summary["existing_artifacts_retained"] is True
    assert summary["dispatch_result_count"] == 4
    assert summary["all_dispatches_artifact_stub"] is True
    assert summary["final_status"] == "completed"
    assert summary["observed_delivery_phases"] == [
        "pickup",
        "enroute",
        "dropoff",
        "completed",
    ]
    assert summary["pickup_reached"] is True
    assert summary["enroute_reached"] is True
    assert summary["dropoff_reached"] is True
    assert summary["completed_reached"] is True
    assert summary["all_dispatches_simulation_only"] is True
    assert summary["all_dispatches_bounded_allowlist_enforced"] is True
    assert summary["all_dispatches_operator_approved"] is True
    assert summary["all_dispatches_no_raw_payload"] is True
    assert summary["all_dispatches_no_mavlink_socket"] is True
    assert summary["all_dispatches_no_mavlink_frame"] is True
    assert summary["all_dispatches_no_hardware_target"] is True
    assert summary["runner_hardware_target_allowed"] is False
    assert summary["runner_physical_execution_invoked"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
