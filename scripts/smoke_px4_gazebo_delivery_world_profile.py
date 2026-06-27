#!/usr/bin/env python3
"""Runtime smoke for the PX4 SITL + Gazebo delivery world profile."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.px4_gazebo_delivery_world_profile import (
    PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION,
    attach_px4_gazebo_delivery_world_profile,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_gazebo_delivery_world_profile",
            title="PX4 Gazebo delivery world profile smoke",
            status="running",
            artifacts={"existing": {"case_id": "profile", "kept": True}},
        )
        artifacts = attach_px4_gazebo_delivery_world_profile(
            task["task_id"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        reloaded = store.get(task["task_id"])

    assert reloaded is not None
    profile = artifacts["px4_gazebo_delivery_world_profile"]
    summary = {
        "task_status": reloaded["status"],
        "schema_version": profile["schema_version"],
        "profile_name": profile["profile_name"],
        "px4_sitl_image": profile["px4_sitl_image"],
        "px4_sitl_model": profile["px4_sitl_model"],
        "gazebo_world_ref": profile["gazebo_world_ref"],
        "gazebo_world_name": profile["gazebo_world_name"],
        "required_compose_profiles": profile["required_compose_profiles"],
        "required_observation_modes": profile["required_observation_modes"],
        "startup_sequence": profile["startup_sequence"],
        "existing_artifacts_retained": reloaded["artifacts"]["existing"]["kept"],
        "profile_attached": "px4_gazebo_delivery_world_profile"
        in reloaded["artifacts"],
        "simulation_only": profile["simulation_only"],
        "telemetry_first": profile["telemetry_first"],
        "profile_descriptor_only": profile["profile_descriptor_only"],
        "operator_approval_required_for_commands": profile[
            "operator_approval_required_for_commands"
        ],
        "command_surface_present": profile["command_surface_present"],
        "command_payload_allowed": profile["command_payload_allowed"],
        "dispatch_implementation_present": profile["dispatch_implementation_present"],
        "ros_dispatch_allowed": profile["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": profile["mavlink_dispatch_allowed"],
        "px4_mission_upload_allowed": profile["px4_mission_upload_allowed"],
        "gazebo_entity_mutation_allowed": profile["gazebo_entity_mutation_allowed"],
        "hardware_target_allowed": profile["hardware_target_allowed"],
        "live_execution_allowed": profile["live_execution_allowed"],
        "physical_execution_invoked": profile["physical_execution_invoked"],
        "actuator_execution_allowed": profile["actuator_execution_allowed"],
        "network_ports_exposed": profile["network_ports_exposed"],
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["schema_version"] == PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION
    assert summary["task_status"] == "running"
    assert summary["profile_attached"] is True
    assert summary["existing_artifacts_retained"] is True
    assert summary["simulation_only"] is True
    assert summary["telemetry_first"] is True
    assert summary["profile_descriptor_only"] is True
    assert summary["operator_approval_required_for_commands"] is True
    assert summary["command_surface_present"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["dispatch_implementation_present"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["actuator_execution_allowed"] is False
    assert summary["network_ports_exposed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
