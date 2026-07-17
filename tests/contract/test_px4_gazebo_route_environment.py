from __future__ import annotations

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import environment


def test_legacy_entrypoint_delegates_environment_reads_to_package() -> None:
    assert (
        route_entrypoint._form2a_wind_compensation_request
        is environment.form2a_wind_compensation_request
    )
    assert route_entrypoint._payload_mass_request is environment.payload_mass_request
    assert (
        route_entrypoint._collision_obstacle_motion_spec
        is environment.collision_obstacle_motion_spec
    )
    assert route_entrypoint._wind_requested_profile is environment.wind_requested_profile


def test_wind_compensation_request_reads_explicit_mapping_without_authority() -> None:
    request = environment.form2a_wind_compensation_request(
        environ={
            environment.MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV: "adjust_route",
            environment.WIND_COMPENSATED_ROUTE_ENV: "1",
            environment.WIND_COMPENSATION_METHOD_ENV: "static_target_offset",
            environment.WIND_PREEMPTIVE_OFFSET_M_ENV: "1.25",
            environment.WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV: "180",
        }
    )

    assert request["route_geometry_compensation_applied"] is True
    assert request["preemptive_offset_m"] == 1.25
    assert request["automatic_dispatch_executed"] is False
    assert request["physical_execution_invoked"] is False
    assert request["delivery_completion_claimed"] is False


@pytest.mark.parametrize("value", ["", "nan", "inf", "11", "-11"])
def test_bounded_environment_float_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="must"):
        environment.bounded_float_value(
            "FIXTURE_VALUE",
            default=0.0,
            environ={"FIXTURE_VALUE": value},
        )


def test_payload_mass_and_model_enablement_are_bounded() -> None:
    assert environment.payload_mass_request(environ={}) is None
    assert environment.payload_mass_request(environ={environment.PAYLOAD_MASS_KG_ENV: "5.5"}) == 5.5
    assert (
        environment.payload_mass_request(environ={environment.PAYLOAD_MASS_KG_ENV: "101"}) is None
    )
    assert (
        environment.payload_model_enabled(environ={environment.PAYLOAD_RELEASE_MODEL_ENV: "1"})
        is True
    )


def test_environment_selectors_normalize_only_allowlisted_requests() -> None:
    requested = {
        environment.LANDING_ZONE_BLOCKED_ENV: "blocked",
        environment.VISIBILITY_MODE_ENV: " fog ",
        environment.RTH_BEHAVIOR_ENV: "return_to_launch",
        environment.TELEMETRY_DROPOUT_MODE_ENV: "observer",
        environment.MAVLINK_LINK_DEGRADATION_MODE_ENV: "heartbeat",
    }

    assert environment.landing_zone_blocked_requested(environ=requested) is True
    assert environment.visibility_mode_request(environ=requested) == "fog"
    assert environment.rth_behavior_requested(environ=requested) is True
    assert environment.telemetry_dropout_mode_request(environ=requested) == "observer_sample_pause"
    assert (
        environment.mavlink_link_degradation_mode_request(environ=requested) == "heartbeat_observer"
    )


def test_environment_profiles_preserve_false_claim_boundaries() -> None:
    environ = {
        environment.WIND_MEAN_MPS_ENV: "4.0",
        environment.TEMPERATURE_C_ENV: "38",
        environment.BATTERY_REMAINING_PERCENT_ENV: "8",
        environment.SENSOR_FAILURE_TYPE_ENV: "off",
    }

    profiles = [
        environment.wind_requested_profile(environ=environ),
        environment.thermal_weather_requested_profile(environ=environ),
        environment.battery_requested_profile(environ=environ),
        environment.sensor_failure_requested_profile(environ=environ),
    ]
    assert all(profile["requested_present"] is True for profile in profiles)
    assert all(profile["hardware_target_allowed"] is False for profile in profiles)
    assert all(profile["physical_execution_invoked"] is False for profile in profiles)
    assert all(profile["delivery_completion_claimed"] is False for profile in profiles)
