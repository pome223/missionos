from __future__ import annotations

import math

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import scenario


def test_legacy_route_entrypoint_uses_packaged_scenario_helpers() -> None:
    bindings = {
        "_form2a_wind_compensation_xy_offset": scenario.wind_compensation_xy_offset,
        "_form2a_wind_feed_forward_xy_mps": scenario.wind_feed_forward_xy_mps,
        "_thermal_battery_drain_factor_from_temperature": (
            scenario.thermal_battery_drain_factor_from_temperature
        ),
        "_thermal_motor_derate_factor_from_temperature": (
            scenario.thermal_motor_derate_factor_from_temperature
        ),
        "_wind_vector": scenario.wind_vector,
    }
    for legacy_name, packaged_function in bindings.items():
        assert getattr(route_entrypoint, legacy_name) is packaged_function


def test_wind_compensation_request_is_bounded_and_has_no_authority() -> None:
    request = scenario.build_wind_compensation_request(
        selected_response_kind="reroute",
        compensated_route_requested=True,
        compensation_method="mid_route_velocity_feed_forward",
        preemptive_offset_m=4.0,
        preemptive_offset_direction_deg=90.0,
        feed_forward_mps=1.2,
        feed_forward_ramp_start_fraction=1.4,
        feed_forward_ramp_end_fraction=-2.0,
        source_response_kind="operator_revision",
    )

    assert request["feed_forward_ramp_start_fraction"] == 1.0
    assert request["feed_forward_ramp_end_fraction"] == 1.0
    assert request["route_geometry_compensation_applied"] is False
    assert request["velocity_feed_forward_applied"] is True
    assert request["automatic_dispatch_executed"] is False
    assert request["physical_execution_invoked"] is False
    assert request["hardware_target_allowed"] is False
    assert request["delivery_completion_claimed"] is False


def test_requested_environment_profiles_never_create_execution_authority() -> None:
    wind = scenario.build_wind_requested_profile(
        wind_mean_mps=4.5,
        wind_direction_deg=90.0,
        wind_gust_mps=None,
        wind_variance=None,
    )
    thermal = scenario.build_thermal_weather_requested_profile(
        temperature_c=45.0,
        pressure_hpa=1500.0,
        thermal_battery_drain_factor=99.0,
        thermal_motor_derate_factor=0.01,
    )

    assert wind["requested_present"] is True
    assert wind["requested"]["wind_mean_mps"] == 4.5
    assert thermal["requested"] == {
        "temperature_c": 45.0,
        "pressure_hpa": None,
        "thermal_battery_drain_factor": 10.0,
        "thermal_motor_derate_factor": 0.1,
    }
    for profile in (wind, thermal):
        assert profile["physical_execution_invoked"] is False
        assert profile["hardware_target_allowed"] is False
        assert profile["delivery_completion_claimed"] is False


@pytest.mark.parametrize(
    ("battery_scenario", "remaining", "expected_scenario", "warning_level"),
    [
        (None, 9.0, "battery_critical", 2),
        (None, 40.0, "battery_low", 1),
        ("battery_low", 200.0, "battery_low", 1),
        ("unexpected", 50.0, "unsupported", None),
    ],
)
def test_battery_profile_normalizes_bounded_scenarios(
    battery_scenario: str | None,
    remaining: float | None,
    expected_scenario: str,
    warning_level: int | None,
) -> None:
    profile = scenario.build_battery_requested_profile(
        battery_scenario=battery_scenario,
        requested_remaining_percent=remaining,
    )

    assert profile["requested"]["battery_scenario"] == expected_scenario
    assert profile["requested"]["requested_warning_level"] == warning_level
    assert profile["requested_remaining_does_not_spoof_px4_battery_status"] is True
    assert profile["physical_execution_invoked"] is False


def test_sensor_profile_defaults_failure_to_gps_and_reports_unsupported_values() -> None:
    gps = scenario.build_sensor_failure_requested_profile(
        sensor_component=None,
        failure_type="off",
    )
    unsupported = scenario.build_sensor_failure_requested_profile(
        sensor_component="camera",
        failure_type="stuck",
    )

    assert gps["requested"] == {
        "sensor_component": "gps",
        "failure_type": "off",
        "reset_failure_type": "ok",
    }
    assert gps["validation_reasons"] == []
    assert unsupported["validation_reasons"] == [
        "sensor_component_not_in_this_vertical_slice",
        "sensor_failure_type_not_in_this_vertical_slice",
    ]
    assert unsupported["physical_execution_invoked"] is False


def test_entrypoint_profile_wrappers_read_environment_only_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(route_entrypoint.WIND_MEAN_MPS_ENV, "3.25")
    monkeypatch.setenv(route_entrypoint.TEMPERATURE_C_ENV, "42")
    monkeypatch.setenv(route_entrypoint.BATTERY_REMAINING_PERCENT_ENV, "8")
    monkeypatch.setenv(route_entrypoint.SENSOR_FAILURE_TYPE_ENV, "off")

    assert route_entrypoint._wind_requested_profile()["requested"][
        "wind_mean_mps"
    ] == 3.25
    assert route_entrypoint._thermal_weather_requested_profile()["requested"][
        "temperature_c"
    ] == 42.0
    assert route_entrypoint._battery_requested_profile()["requested"][
        "battery_scenario"
    ] == "battery_critical"
    assert route_entrypoint._sensor_failure_requested_profile()["requested"][
        "sensor_component"
    ] == "gps"


@pytest.mark.parametrize(
    ("selector", "accepted"),
    [
        (scenario.landing_zone_blocked_requested, "blocked"),
        (scenario.no_fly_zone_marker_requested, "visual"),
        (scenario.traffic_conflict_marker_requested, "vehicle"),
        (scenario.alternate_landing_marker_requested, "alternate"),
        (scenario.rth_behavior_requested, "return_to_launch"),
        (scenario.moving_actor_marker_requested, "actor"),
        (scenario.collision_obstacle_requested, "obstacle"),
        (scenario.collision_obstacle_contact_topic_requested, "contact"),
        (scenario.multi_drone_conflict_probe_requested, "multi_drone"),
    ],
)
def test_scenario_selectors_use_explicit_alias_allowlists(
    selector: object,
    accepted: str,
) -> None:
    assert callable(selector)
    assert selector(f"  {accepted.upper()}  ") is True
    assert selector("true") is True
    assert selector("unexpected") is False
    assert selector(None) is False


def test_visibility_mode_normalization_is_value_only() -> None:
    assert scenario.normalize_visibility_mode(None) is None
    assert scenario.normalize_visibility_mode("  FOG  ") == "fog"


def test_entrypoint_scenario_selector_wrappers_only_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(route_entrypoint.LANDING_ZONE_BLOCKED_ENV, "blocked")
    monkeypatch.setenv(route_entrypoint.VISIBILITY_MODE_ENV, " fog ")
    monkeypatch.setenv(route_entrypoint.RTH_BEHAVIOR_ENV, "rtl")
    monkeypatch.setenv(route_entrypoint.COLLISION_OBSTACLE_ENV, "obstacle")

    assert route_entrypoint._landing_zone_blocked_requested() is True
    assert route_entrypoint._visibility_mode_request() == "fog"
    assert route_entrypoint._rth_behavior_requested() is True
    assert route_entrypoint._collision_obstacle_requested() is True


def test_wind_geometry_uses_opposite_wind_vector() -> None:
    offset = scenario.wind_compensation_xy_offset(
        {
            "route_geometry_compensation_applied": True,
            "preemptive_offset_m": 2.0,
            "preemptive_offset_direction_deg": 90.0,
        }
    )
    feed_forward = scenario.wind_feed_forward_xy_mps(
        {
            "velocity_feed_forward_applied": True,
            "feed_forward_mps": 0.5,
            "feed_forward_direction_deg": 180.0,
        }
    )

    assert offset == pytest.approx((-2.0, 0.0), abs=1e-9)
    assert feed_forward == pytest.approx((0.0, 0.5), abs=1e-9)
    assert scenario.wind_feed_forward_scale(
        elapsed_seconds=7.5,
        duration_seconds=10.0,
        ramp_start_fraction=0.5,
        ramp_end_fraction=1.0,
    ) == pytest.approx(0.5)
    assert scenario.wind_feed_forward_scale(
        elapsed_seconds=1.0,
        duration_seconds=0.0,
        ramp_start_fraction=0.5,
        ramp_end_fraction=1.0,
    ) == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (" OFF ", ""),
        ("observer_pose_gap", "observer_sample_pause"),
        ("custom_mode", "custom_mode"),
    ],
)
def test_telemetry_dropout_mode_normalization(
    value: str | None,
    expected: str,
) -> None:
    assert scenario.normalize_telemetry_dropout_mode(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("HEARTBEAT", "heartbeat_observer"),
        ("mavlink_link_loss_applicator", "bounded_link_loss"),
        ("mavlink_link_loss_probe", "link_loss_probe"),
        ("custom_mode", "custom_mode"),
    ],
)
def test_mavlink_link_degradation_mode_normalization(
    value: str | None,
    expected: str,
) -> None:
    assert scenario.normalize_mavlink_link_degradation_mode(value) == expected


def test_terrain_geometry_requires_materialized_world() -> None:
    pose = {"x": 3.5, "y": -2.0, "z": 1.25}

    assert scenario.terrain_relative_xy_origin(
        pose,
        terrain_world_loaded=False,
    ) == (0.0, 0.0)
    assert scenario.landing_z_threshold(
        pose,
        terrain_world_loaded=False,
    ) == 0.15
    assert scenario.terrain_relative_xy_origin(
        pose,
        terrain_world_loaded=True,
    ) == (3.5, -2.0)
    assert scenario.landing_z_threshold(
        pose,
        terrain_world_loaded=True,
    ) == 1.4


def test_wind_and_thermal_derivations_preserve_bounds() -> None:
    assert scenario.wind_vector(mean_mps=3.0, direction_deg=90.0) == (3.0, 0.0)
    assert math.isclose(
        scenario.thermal_battery_drain_factor_from_temperature(100.0) or 0.0,
        2.5,
    )
    assert scenario.thermal_battery_drain_factor_from_temperature(-100.0) == 2.2
    assert scenario.thermal_battery_drain_factor_from_temperature(20.0) == 1.0
    assert scenario.thermal_motor_derate_factor_from_temperature(100.0) == 0.55
    assert scenario.thermal_motor_derate_factor_from_temperature(20.0) == 1.0
    assert scenario.thermal_motor_derate_factor_from_temperature(None) is None
