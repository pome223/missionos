"""Runtime smoke for the inert delivery mission contract artifact."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from pydantic import ValidationError

from src.runtime.delivery_mission_contract import build_delivery_mission_contract


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _valid_contract():
    return build_delivery_mission_contract(
        mission_id="delivery-smoke-001",
        pickup_location={
            "location_id": "pickup-pad-a",
            "label": "Warehouse pad A",
            "latitude": 35.681236,
            "longitude": 139.767125,
            "altitude_m": 16.0,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "label": "Customer pad B",
            "latitude": 35.689487,
            "longitude": 139.691706,
            "altitude_m": 41.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={
            "package_id": "pkg-smoke",
            "max_weight_kg": 1.2,
            "max_length_m": 0.3,
            "max_width_m": 0.2,
            "max_height_m": 0.15,
        },
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad", "clear_rooftop"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
        now=NOW,
    )


def main() -> int:
    contract = _valid_contract()
    second = _valid_contract()
    command_like_rejected = False
    safety_override_rejected = False

    try:
        build_delivery_mission_contract(
            mission_id="delivery-smoke-001",
            pickup_location={**contract.pickup_location.model_dump(mode="json")},
            dropoff_location={**contract.dropoff_location.model_dump(mode="json")},
            delivery_window=contract.delivery_window.model_dump(mode="json"),
            package_constraints=contract.package_constraints.model_dump(mode="json"),
            weather_constraints=contract.weather_constraints.model_dump(mode="json"),
            battery_policy=contract.battery_policy.model_dump(mode="json"),
            landing_zone_policy=contract.landing_zone_policy.model_dump(mode="json"),
            telemetry_requirements=contract.telemetry_requirements.model_dump(mode="json"),
            metadata={"nested": [{"RosTopic": "/cmd_vel"}]},
            now=NOW,
        )
    except (ValueError, ValidationError):
        command_like_rejected = True

    payload = contract.model_dump(mode="json")
    payload["live_execution_allowed"] = True
    try:
        type(contract).model_validate(payload)
    except ValidationError:
        safety_override_rejected = True

    result = {
        "schema_version": contract.schema_version,
        "contract_id": contract.contract_id,
        "deterministic_contract_id": contract.contract_id == second.contract_id,
        "pickup_location": contract.pickup_location.location_id,
        "dropoff_location": contract.dropoff_location.location_id,
        "battery_minimum_takeoff_percent": contract.battery_policy.minimum_takeoff_percent,
        "landing_zone_required": contract.landing_zone_policy.require_verified_landing_zone,
        "required_evidence_includes_hil": "hil_telemetry_evidence" in contract.required_evidence,
        "command_like_rejected": command_like_rejected,
        "safety_override_rejected": safety_override_rejected,
        "operator_approval_required": contract.operator_approval_required,
        "operator_approval_performed": contract.operator_approval_performed,
        "live_execution_allowed": contract.live_execution_allowed,
        "physical_execution_invoked": contract.physical_execution_invoked,
        "command_payload_allowed": contract.command_payload_allowed,
        "ros_dispatch_allowed": contract.ros_dispatch_allowed,
        "mavlink_dispatch_allowed": contract.mavlink_dispatch_allowed,
        "actuator_execution_allowed": contract.actuator_execution_allowed,
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    assert result["deterministic_contract_id"] is True
    assert result["required_evidence_includes_hil"] is True
    assert result["command_like_rejected"] is True
    assert result["safety_override_rejected"] is True
    assert result["operator_approval_required"] is True
    assert result["operator_approval_performed"] is False
    assert result["live_execution_allowed"] is False
    assert result["physical_execution_invoked"] is False
    assert result["command_payload_allowed"] is False
    assert result["ros_dispatch_allowed"] is False
    assert result["mavlink_dispatch_allowed"] is False
    assert result["actuator_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
