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
