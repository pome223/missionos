"""Runtime smoke for the minimal Gazebo delivery world fixture."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from src.runtime.gazebo_delivery_world import (
    DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION,
    build_gazebo_delivery_world_fixture,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def main() -> int:
    fixture = build_gazebo_delivery_world_fixture(now=NOW)
    result = {
        "schema_version": fixture.schema_version,
        "world_ref": fixture.world_ref,
        "world_name": fixture.world_name,
        "sdf_version": fixture.sdf_version,
        "pickup_model_ref": fixture.pickup_model_ref,
        "dropoff_model_ref": fixture.dropoff_model_ref,
        "safe_corridor_model_ref": fixture.safe_corridor_model_ref,
        "model_names": list(fixture.model_names),
        "headless_compatible": fixture.headless_compatible,
        "server_only_compatible": fixture.server_only_compatible,
        "requires_gui": fixture.requires_gui,
        "plugin_count": fixture.plugin_count,
        "include_count": fixture.include_count,
        "command_surface_present": fixture.command_surface_present,
        "ros_surface_present": fixture.ros_surface_present,
        "mavlink_surface_present": fixture.mavlink_surface_present,
        "gazebo_entity_mutation_allowed": fixture.gazebo_entity_mutation_allowed,
        "live_execution_allowed": fixture.live_execution_allowed,
        "physical_execution_invoked": fixture.physical_execution_invoked,
        "actuator_execution_allowed": fixture.actuator_execution_allowed,
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    assert result["schema_version"] == GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION
    assert result["world_ref"] == DEFAULT_GAZEBO_DELIVERY_WORLD_PATH
    assert result["world_name"] == "delivery_minimal"
    assert result["pickup_model_ref"] == "model://pickup_pad_a"
    assert result["dropoff_model_ref"] == "model://dropoff_pad_b"
    assert result["safe_corridor_model_ref"] == "model://safe_corridor_pickup_to_dropoff"
    assert result["headless_compatible"] is True
    assert result["server_only_compatible"] is True
    assert result["requires_gui"] is False
    assert result["plugin_count"] == 0
    assert result["include_count"] == 0
    assert result["command_surface_present"] is False
    assert result["ros_surface_present"] is False
    assert result["mavlink_surface_present"] is False
    assert result["gazebo_entity_mutation_allowed"] is False
    assert result["live_execution_allowed"] is False
    assert result["physical_execution_invoked"] is False
    assert result["actuator_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
