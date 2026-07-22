from __future__ import annotations

from datetime import datetime, timezone

from src.runtime.gazebo_delivery_world import (
    DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION,
    build_gazebo_delivery_world_fixture,
)


def test_minimal_world_fixture_is_headless_and_has_no_command_surface() -> None:
    fixture = build_gazebo_delivery_world_fixture(
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    )

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
