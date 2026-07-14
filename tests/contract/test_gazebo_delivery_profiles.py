from datetime import datetime, timezone
from pathlib import Path

from src.runtime.gazebo_delivery_world import (
    DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION,
    build_gazebo_delivery_world_fixture,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION,
    attach_px4_gazebo_delivery_world_profile,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_gazebo_delivery_world_fixture_is_read_only_and_headless() -> None:
    fixture = build_gazebo_delivery_world_fixture(now=NOW)

    assert fixture.schema_version == GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION
    assert fixture.world_ref == DEFAULT_GAZEBO_DELIVERY_WORLD_PATH
    assert fixture.world_name == "delivery_minimal"
    assert fixture.pickup_model_ref == "model://pickup_pad_a"
    assert fixture.dropoff_model_ref == "model://dropoff_pad_b"
    assert fixture.safe_corridor_model_ref == "model://safe_corridor_pickup_to_dropoff"
    assert fixture.headless_compatible is True
    assert fixture.server_only_compatible is True
    assert fixture.requires_gui is False
    assert fixture.plugin_count == 0
    assert fixture.include_count == 0
    assert fixture.command_surface_present is False
    assert fixture.ros_surface_present is False
    assert fixture.mavlink_surface_present is False
    assert fixture.gazebo_entity_mutation_allowed is False
    assert fixture.live_execution_allowed is False
    assert fixture.physical_execution_invoked is False
    assert fixture.actuator_execution_allowed is False


def test_px4_gazebo_delivery_profile_attach_preserves_task_and_authority_boundary(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_gazebo_delivery_world_profile",
        title="PX4 Gazebo delivery world profile contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )

    artifacts = attach_px4_gazebo_delivery_world_profile(
        task["task_id"],
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    profile = artifacts["px4_gazebo_delivery_world_profile"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert profile["schema_version"] == PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION
    assert profile["simulation_only"] is True
    assert profile["telemetry_first"] is True
    assert profile["profile_descriptor_only"] is True
    assert profile["operator_approval_required_for_commands"] is True
    assert profile["command_surface_present"] is False
    assert profile["command_payload_allowed"] is False
    assert profile["dispatch_implementation_present"] is False
    assert profile["ros_dispatch_allowed"] is False
    assert profile["mavlink_dispatch_allowed"] is False
    assert profile["px4_mission_upload_allowed"] is False
    assert profile["gazebo_entity_mutation_allowed"] is False
    assert profile["hardware_target_allowed"] is False
    assert profile["live_execution_allowed"] is False
    assert profile["physical_execution_invoked"] is False
    assert profile["actuator_execution_allowed"] is False
    assert profile["network_ports_exposed"] is False
