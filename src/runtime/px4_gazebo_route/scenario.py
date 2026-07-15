"""Pure scenario decisions for the PX4/Gazebo route runtime.

The entrypoint owns environment reads.  These helpers receive explicit values
and derive bounded configuration or geometry only; they do not mint authority,
dispatch commands, mutate task state, or claim physical execution.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


def build_wind_requested_profile(
    *,
    wind_mean_mps: float | None,
    wind_direction_deg: float | None,
    wind_gust_mps: float | None,
    wind_variance: float | None,
) -> dict[str, Any]:
    requested = {
        "wind_mean_mps": wind_mean_mps,
        "wind_direction_deg": wind_direction_deg,
        "wind_gust_mps": wind_gust_mps,
        "wind_variance": wind_variance,
    }
    return {
        "schema_version": "environment_condition_profile.v1",
        "condition_id": "environment_condition_profile:mission_designer_wind_gust",
        "condition_kind": "wind_gust",
        "requested": requested,
        "requested_present": any(value is not None for value in requested.values()),
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def build_thermal_weather_requested_profile(
    *,
    temperature_c: float | None,
    pressure_hpa: float | None,
    thermal_battery_drain_factor: float | None,
    thermal_motor_derate_factor: float | None,
) -> dict[str, Any]:
    if pressure_hpa is not None and (pressure_hpa < 500.0 or pressure_hpa > 1100.0):
        pressure_hpa = None
    if thermal_battery_drain_factor is not None:
        thermal_battery_drain_factor = max(
            0.1,
            min(10.0, float(thermal_battery_drain_factor)),
        )
    if thermal_motor_derate_factor is not None:
        thermal_motor_derate_factor = max(
            0.1,
            min(1.0, float(thermal_motor_derate_factor)),
        )
    requested = {
        "temperature_c": temperature_c,
        "pressure_hpa": pressure_hpa,
        "thermal_battery_drain_factor": thermal_battery_drain_factor,
        "thermal_motor_derate_factor": thermal_motor_derate_factor,
    }
    return {
        "schema_version": "thermal_weather_condition_profile.v1",
        "condition_id": "thermal_weather_condition_profile:mission_designer_temperature",
        "condition_kind": "thermal_weather",
        "requested": requested,
        "requested_present": any(value is not None for value in requested.values()),
        "source": "mission_designer_coordinate_route",
        "thermal_air_physics_claimed": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def build_battery_requested_profile(
    *,
    battery_scenario: str | None,
    requested_remaining_percent: float | None,
) -> dict[str, Any]:
    scenario = (battery_scenario or "").strip().lower()
    remaining = requested_remaining_percent
    if remaining is not None and (remaining < 0.0 or remaining > 100.0):
        remaining = None
    if not scenario and remaining is not None:
        scenario = "battery_critical" if remaining <= 10.0 else "battery_low"
    if scenario not in ("", "battery_low", "battery_critical"):
        scenario = "unsupported"
    return {
        "schema_version": "battery_condition_profile.v1",
        "condition_id": "battery_condition_profile:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold",
        "requested": {
            "battery_scenario": scenario or None,
            "requested_remaining_percent": remaining,
            "requested_warning_level": (
                2
                if scenario == "battery_critical"
                else 1 if scenario == "battery_low" else None
            ),
        },
        "requested_present": bool(scenario or remaining is not None),
        "source": "mission_designer_coordinate_route",
        "requested_remaining_does_not_spoof_px4_battery_status": True,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def build_sensor_failure_requested_profile(
    *,
    sensor_component: str | None,
    failure_type: str | None,
) -> dict[str, Any]:
    component = (sensor_component or "").strip().lower()
    failure = (failure_type or "").strip().lower()
    if not component and failure:
        component = "gps"
    requested_present = bool(component or failure)
    validation_reasons: list[str] = []
    if component and component not in {"gps"}:
        validation_reasons.append("sensor_component_not_in_this_vertical_slice")
    if failure and failure not in {"off"}:
        validation_reasons.append("sensor_failure_type_not_in_this_vertical_slice")
    return {
        "schema_version": "sensor_condition_profile.v1",
        "condition_id": "sensor_condition_profile:mission_designer_sensor_failure",
        "condition_kind": "sensor_failure",
        "requested": {
            "sensor_component": component or None,
            "failure_type": failure or None,
            "reset_failure_type": "ok" if component else None,
        },
        "requested_present": requested_present,
        "validation_reasons": validation_reasons,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def build_wind_compensation_request(
    *,
    selected_response_kind: str,
    compensated_route_requested: bool,
    compensation_method: str,
    preemptive_offset_m: float,
    preemptive_offset_direction_deg: float,
    feed_forward_mps: float,
    feed_forward_ramp_start_fraction: float,
    feed_forward_ramp_end_fraction: float,
    source_response_kind: str,
) -> dict[str, Any]:
    ramp_start_fraction = min(
        max(float(feed_forward_ramp_start_fraction), 0.0),
        1.0,
    )
    ramp_end_fraction = min(
        max(float(feed_forward_ramp_end_fraction), ramp_start_fraction),
        1.0,
    )
    offset_enabled = bool(
        compensated_route_requested
        and compensation_method == "static_target_offset"
        and preemptive_offset_m > 0.0
    )
    feed_forward_enabled = bool(
        compensated_route_requested
        and compensation_method == "mid_route_velocity_feed_forward"
        and feed_forward_mps > 0.0
    )
    return {
        "schema_version": "missionos_form2a_wind_compensation_request.v1",
        "selected_response_kind": selected_response_kind,
        "compensation_method": compensation_method,
        "compensated_route_requested": compensated_route_requested,
        "preemptive_offset_m": preemptive_offset_m,
        "preemptive_offset_direction_deg": preemptive_offset_direction_deg,
        "preemptive_offset_direction_convention": "opposite_wind_vector_xy",
        "feed_forward_mps": feed_forward_mps,
        "feed_forward_direction_deg": preemptive_offset_direction_deg,
        "feed_forward_direction_convention": "opposite_wind_vector_xy",
        "feed_forward_phase_schedule": "full_then_linear_ramp_down",
        "feed_forward_ramp_start_fraction": ramp_start_fraction,
        "feed_forward_ramp_end_fraction": ramp_end_fraction,
        "source_response_kind": source_response_kind,
        "route_geometry_compensation_applied": offset_enabled,
        "velocity_feed_forward_applied": feed_forward_enabled,
        "progress_counted": False,
        "drone_physics_affected": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
    }


def wind_compensation_xy_offset(
    request: Mapping[str, Any],
) -> tuple[float, float]:
    if request.get("route_geometry_compensation_applied") is not True:
        return (0.0, 0.0)
    offset_m = float(request.get("preemptive_offset_m") or 0.0)
    direction_rad = math.radians(
        float(request.get("preemptive_offset_direction_deg") or 0.0)
    )
    wind_unit_x = math.sin(direction_rad)
    wind_unit_y = math.cos(direction_rad)
    return (-offset_m * wind_unit_x, -offset_m * wind_unit_y)


def wind_feed_forward_xy_mps(
    request: Mapping[str, Any],
) -> tuple[float, float]:
    if request.get("velocity_feed_forward_applied") is not True:
        return (0.0, 0.0)
    feed_forward_mps = float(request.get("feed_forward_mps") or 0.0)
    direction_rad = math.radians(
        float(request.get("feed_forward_direction_deg") or 0.0)
    )
    wind_unit_x = math.sin(direction_rad)
    wind_unit_y = math.cos(direction_rad)
    return (-feed_forward_mps * wind_unit_x, -feed_forward_mps * wind_unit_y)


def wind_feed_forward_scale(
    *,
    elapsed_seconds: float,
    duration_seconds: float,
    ramp_start_fraction: float,
    ramp_end_fraction: float,
) -> float:
    if duration_seconds <= 0.0:
        return 0.0
    progress = min(max(elapsed_seconds / duration_seconds, 0.0), 1.0)
    ramp_start_fraction = min(max(ramp_start_fraction, 0.0), 1.0)
    ramp_end_fraction = min(max(ramp_end_fraction, ramp_start_fraction), 1.0)
    if progress <= ramp_start_fraction:
        return 1.0
    if progress >= ramp_end_fraction:
        return 0.0
    ramp_span = ramp_end_fraction - ramp_start_fraction
    if ramp_span <= 0.0:
        return 0.0
    return 1.0 - ((progress - ramp_start_fraction) / ramp_span)


def normalize_telemetry_dropout_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    aliases = {
        "": "",
        "none": "",
        "off": "",
        "observer": "observer_sample_pause",
        "observer_side_dropout": "observer_sample_pause",
        "observer_pose_gap": "observer_sample_pause",
        "pose_gap": "observer_sample_pause",
        "observer_sample_pause": "observer_sample_pause",
        "sample_pause": "observer_sample_pause",
    }
    return aliases.get(normalized, normalized)


def normalize_mavlink_link_degradation_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    aliases = {
        "": "",
        "none": "",
        "off": "",
        "heartbeat": "heartbeat_observer",
        "heartbeat_observer": "heartbeat_observer",
        "heartbeat_gap_observer": "heartbeat_observer",
        "mavlink_heartbeat_observer": "heartbeat_observer",
        "bounded_link_loss": "bounded_link_loss",
        "bounded_mavlink_link_loss": "bounded_link_loss",
        "link_loss": "bounded_link_loss",
        "mavlink_link_loss": "bounded_link_loss",
        "link_loss_applicator": "bounded_link_loss",
        "mavlink_link_loss_applicator": "bounded_link_loss",
        "mavlink_link_loss_probe": "link_loss_probe",
        "link_loss_probe": "link_loss_probe",
    }
    return aliases.get(normalized, normalized)


def terrain_relative_xy_origin(
    pickup_pose: Mapping[str, float],
    *,
    terrain_world_loaded: bool,
) -> tuple[float, float]:
    if not terrain_world_loaded:
        return (0.0, 0.0)
    return (float(pickup_pose["x"]), float(pickup_pose["y"]))


def landing_z_threshold(
    pickup_pose: Mapping[str, float],
    *,
    terrain_world_loaded: bool,
) -> float:
    if not terrain_world_loaded:
        return 0.15
    return float(pickup_pose["z"]) + 0.15


def wind_vector(*, mean_mps: float, direction_deg: float) -> tuple[float, float]:
    radians = math.radians(direction_deg)
    return (
        round(mean_mps * math.sin(radians), 6),
        round(mean_mps * math.cos(radians), 6),
    )


def thermal_battery_drain_factor_from_temperature(
    temperature_c: float | None,
) -> float | None:
    if temperature_c is None:
        return None
    if temperature_c >= 35.0:
        return round(min(2.5, 1.0 + (temperature_c - 25.0) * 0.04), 3)
    if temperature_c <= 0.0:
        return round(min(2.2, 1.0 + abs(temperature_c) * 0.03), 3)
    return 1.0


def thermal_motor_derate_factor_from_temperature(
    temperature_c: float | None,
) -> float | None:
    if temperature_c is None:
        return None
    if temperature_c >= 40.0:
        return round(max(0.55, 1.0 - (temperature_c - 35.0) * 0.015), 3)
    return 1.0


__all__ = [
    "build_battery_requested_profile",
    "build_sensor_failure_requested_profile",
    "build_thermal_weather_requested_profile",
    "build_wind_requested_profile",
    "build_wind_compensation_request",
    "landing_z_threshold",
    "normalize_mavlink_link_degradation_mode",
    "normalize_telemetry_dropout_mode",
    "terrain_relative_xy_origin",
    "thermal_battery_drain_factor_from_temperature",
    "thermal_motor_derate_factor_from_temperature",
    "wind_compensation_xy_offset",
    "wind_feed_forward_scale",
    "wind_feed_forward_xy_mps",
    "wind_vector",
]
