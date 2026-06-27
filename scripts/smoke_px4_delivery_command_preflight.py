#!/usr/bin/env python3
"""Runtime smoke for PX4/Gazebo delivery command preflight artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.px4_delivery_command_preflight import (
    attach_px4_simulation_command_preflight_artifacts,
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


def main() -> int:
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_simulation_command_preflight",
            title="PX4 command preflight smoke",
            status="running",
            artifacts={"existing": {"case_id": "preflight", "kept": True}},
        )
        artifacts = attach_px4_simulation_command_preflight_artifacts(
            task["task_id"],
            profile=profile,
            observation=observation,
            operator_approval_performed=True,
            now=NOW,
            task_store_factory=lambda: store,
        )
        reloaded = store.get(task["task_id"])

    assert reloaded is not None
    connection = artifacts["px4_simulation_mavlink_connection_contract"]
    adapter = artifacts["px4_simulation_mavlink_telemetry_adapter"]
    proposal = artifacts["px4_simulation_delivery_command_proposal"]
    approval = artifacts["px4_simulation_command_approval"]
    allowlist = artifacts["px4_simulation_command_allowlist"]
    summary = {
        "task_status": reloaded["status"],
        "existing_artifacts_retained": reloaded["artifacts"]["existing"]["kept"],
        "connection_schema": connection["schema_version"],
        "adapter_schema": adapter["schema_version"],
        "proposal_schema": proposal["schema_version"],
        "approval_schema": approval["schema_version"],
        "allowlist_schema": allowlist["schema_version"],
        "connection_opened": connection["connection_opened"],
        "telemetry_observation_only": connection["telemetry_observation_only"],
        "adapter_mode": adapter["adapter_mode"],
        "command_frames_observed": adapter["command_frames_observed"],
        "proposed_command_kinds": proposal["proposed_command_kinds"],
        "operator_approval_performed": approval["operator_approval_performed"],
        "allowed_command_kinds": allowlist["allowed_command_kinds"],
        "allowed_protocols": allowlist["allowed_protocols"],
        "denied_command_families": allowlist["denied_command_families"],
        "raw_command_payload_allowed": allowlist["raw_command_payload_allowed"],
        "all_command_payload_allowed_false": all(
            item["command_payload_allowed"] is False
            for item in (connection, adapter, proposal, approval, allowlist)
        ),
        "all_dispatch_implementation_present_false": all(
            item["dispatch_implementation_present"] is False
            for item in (connection, adapter, proposal, approval, allowlist)
        ),
        "all_mavlink_dispatch_allowed_false": all(
            item["mavlink_dispatch_allowed"] is False
            for item in (connection, adapter, proposal, approval, allowlist)
        ),
        "all_ros_dispatch_allowed_false": all(
            item["ros_dispatch_allowed"] is False
            for item in (connection, adapter, proposal, approval, allowlist)
        ),
        "all_hardware_target_allowed_false": all(
            item["hardware_target_allowed"] is False
            for item in (connection, adapter, proposal, approval, allowlist)
        ),
        "all_physical_execution_invoked_false": all(
            item["physical_execution_invoked"] is False
            for item in (connection, adapter, proposal, approval, allowlist)
        ),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["task_status"] == "running"
    assert summary["existing_artifacts_retained"] is True
    assert summary["connection_opened"] is False
    assert summary["telemetry_observation_only"] is True
    assert summary["adapter_mode"] == "telemetry_observation_only"
    assert summary["command_frames_observed"] == 0
    assert summary["operator_approval_performed"] is True
    assert summary["raw_command_payload_allowed"] is False
    assert summary["all_command_payload_allowed_false"] is True
    assert summary["all_dispatch_implementation_present_false"] is True
    assert summary["all_mavlink_dispatch_allowed_false"] is True
    assert summary["all_ros_dispatch_allowed_false"] is True
    assert summary["all_hardware_target_allowed_false"] is True
    assert summary["all_physical_execution_invoked_false"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
