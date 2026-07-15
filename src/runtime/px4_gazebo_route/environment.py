"""Environment boundary for PX4/Gazebo route scenarios.

This module translates process environment values into explicit scenario
requests.  It does not satisfy external-execution opt-in, mint approval or
dispatch authority, execute an action, or claim completion.
"""

from __future__ import annotations

import math
import os
from typing import Any, Mapping

from src.runtime.missionos_sitl_dispatch_runtime import (
    MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV,
    WIND_COMPENSATED_ROUTE_ENV,
    WIND_COMPENSATION_METHOD_ENV,
    WIND_COMPENSATION_SOURCE_RESPONSE_ENV,
    WIND_FEED_FORWARD_MPS_ENV,
    WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV,
    WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV,
    WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
    WIND_PREEMPTIVE_OFFSET_M_ENV,
)
from src.runtime.px4_gazebo_route import scenario


PAYLOAD_RELEASE_MODEL_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL"
WIND_MEAN_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS"
WIND_DIRECTION_DEG_ENV = "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG"
WIND_GUST_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_GUST_MPS"
WIND_VARIANCE_ENV = "MISSION_DESIGNER_REALISM_WIND_VARIANCE"
TEMPERATURE_C_ENV = "MISSION_DESIGNER_REALISM_TEMPERATURE_C"
PRESSURE_HPA_ENV = "MISSION_DESIGNER_REALISM_PRESSURE_HPA"
THERMAL_BATTERY_DRAIN_FACTOR_ENV = "MISSION_DESIGNER_REALISM_THERMAL_BATTERY_DRAIN_FACTOR"
THERMAL_MOTOR_DERATE_FACTOR_ENV = "MISSION_DESIGNER_REALISM_THERMAL_MOTOR_DERATE_FACTOR"
PAYLOAD_MASS_KG_ENV = "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG"
BATTERY_SCENARIO_ENV = "MISSION_DESIGNER_REALISM_BATTERY_SCENARIO"
BATTERY_REMAINING_PERCENT_ENV = "MISSION_DESIGNER_REALISM_BATTERY_REMAINING_PERCENT"
SENSOR_FAILURE_COMPONENT_ENV = "MISSION_DESIGNER_REALISM_SENSOR_FAILURE_COMPONENT"
SENSOR_FAILURE_TYPE_ENV = "MISSION_DESIGNER_REALISM_SENSOR_FAILURE_TYPE"
LANDING_ZONE_BLOCKED_ENV = "MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED"
VISIBILITY_MODE_ENV = "MISSION_DESIGNER_REALISM_VISIBILITY_MODE"
NO_FLY_ZONE_MARKER_ENV = "MISSION_DESIGNER_REALISM_NO_FLY_ZONE_MARKER"
TRAFFIC_CONFLICT_MARKER_ENV = "MISSION_DESIGNER_REALISM_TRAFFIC_CONFLICT_MARKER"
ALTERNATE_LANDING_MARKER_ENV = "MISSION_DESIGNER_REALISM_ALTERNATE_LANDING_MARKER"
RTH_BEHAVIOR_ENV = "MISSION_DESIGNER_REALISM_RTH_BEHAVIOR"
MOVING_ACTOR_MARKER_ENV = "MISSION_DESIGNER_REALISM_MOVING_ACTOR_MARKER"
COLLISION_OBSTACLE_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE"
COLLISION_OBSTACLE_CONTACT_TOPIC_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC"
COLLISION_OBSTACLE_START_X_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_X_M"
COLLISION_OBSTACLE_START_Y_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_Y_M"
COLLISION_OBSTACLE_END_X_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_X_M"
COLLISION_OBSTACLE_END_Y_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_Y_M"
MULTI_DRONE_CONFLICT_PROBE_ENV = "MISSION_DESIGNER_REALISM_MULTI_DRONE_CONFLICT_PROBE"
TELEMETRY_DROPOUT_MODE_ENV = "MISSION_DESIGNER_REALISM_TELEMETRY_DROPOUT_MODE"
MAVLINK_LINK_DEGRADATION_MODE_ENV = "MISSION_DESIGNER_REALISM_MAVLINK_LINK_DEGRADATION_MODE"


def _get(environ: Mapping[str, str] | None, name: str) -> str | None:
    return (os.environ if environ is None else environ).get(name)


def float_value(
    name: str,
    default: float = 0.0,
    *,
    environ: Mapping[str, str] | None = None,
) -> float:
    try:
        return float(_get(environ, name) or str(default))
    except ValueError:
        return float(default)


def optional_float_value(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> float | None:
    raw = _get(environ, name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def bounded_float_value(
    name: str,
    *,
    default: float,
    minimum: float = -10.0,
    maximum: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> float:
    raw = _get(environ, name)
    if raw is None:
        return default
    if raw == "":
        raise ValueError(f"{name} must be a finite float")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite float") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def form2a_wind_compensation_request(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return scenario.build_wind_compensation_request(
        selected_response_kind=_get(environ, MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV) or "",
        compensated_route_requested=_get(environ, WIND_COMPENSATED_ROUTE_ENV) == "1",
        compensation_method=_get(environ, WIND_COMPENSATION_METHOD_ENV) or "static_target_offset",
        preemptive_offset_m=float_value(WIND_PREEMPTIVE_OFFSET_M_ENV, 0.0, environ=environ),
        preemptive_offset_direction_deg=float_value(
            WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV, 90.0, environ=environ
        ),
        feed_forward_mps=float_value(WIND_FEED_FORWARD_MPS_ENV, 0.0, environ=environ),
        feed_forward_ramp_start_fraction=float_value(
            WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV, 0.65, environ=environ
        ),
        feed_forward_ramp_end_fraction=float_value(
            WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV, 0.9, environ=environ
        ),
        source_response_kind=_get(environ, WIND_COMPENSATION_SOURCE_RESPONSE_ENV) or "",
    )


def payload_mass_request(*, environ: Mapping[str, str] | None = None) -> float | None:
    value = optional_float_value(PAYLOAD_MASS_KG_ENV, environ=environ)
    if value is None or value < 0.0 or value > 100.0:
        return None
    return value


def payload_model_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    return (
        _get(environ, PAYLOAD_RELEASE_MODEL_ENV) == "1"
        or payload_mass_request(environ=environ) is not None
    )


def landing_zone_blocked_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return scenario.landing_zone_blocked_requested(_get(environ, LANDING_ZONE_BLOCKED_ENV))


def visibility_mode_request(*, environ: Mapping[str, str] | None = None) -> str | None:
    return scenario.normalize_visibility_mode(_get(environ, VISIBILITY_MODE_ENV))


def _selector(
    selector: Any,
    name: str,
    environ: Mapping[str, str] | None,
) -> bool:
    return bool(selector(_get(environ, name)))


def no_fly_zone_marker_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(scenario.no_fly_zone_marker_requested, NO_FLY_ZONE_MARKER_ENV, environ)


def traffic_conflict_marker_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(
        scenario.traffic_conflict_marker_requested,
        TRAFFIC_CONFLICT_MARKER_ENV,
        environ,
    )


def alternate_landing_marker_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(
        scenario.alternate_landing_marker_requested,
        ALTERNATE_LANDING_MARKER_ENV,
        environ,
    )


def rth_behavior_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(scenario.rth_behavior_requested, RTH_BEHAVIOR_ENV, environ)


def moving_actor_marker_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(
        scenario.moving_actor_marker_requested,
        MOVING_ACTOR_MARKER_ENV,
        environ,
    )


def collision_obstacle_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(
        scenario.collision_obstacle_requested,
        COLLISION_OBSTACLE_ENV,
        environ,
    )


def collision_obstacle_contact_topic_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(
        scenario.collision_obstacle_contact_topic_requested,
        COLLISION_OBSTACLE_CONTACT_TOPIC_ENV,
        environ,
    )


def multi_drone_conflict_probe_requested(*, environ: Mapping[str, str] | None = None) -> bool:
    return _selector(
        scenario.multi_drone_conflict_probe_requested,
        MULTI_DRONE_CONFLICT_PROBE_ENV,
        environ,
    )


def telemetry_dropout_mode_request(*, environ: Mapping[str, str] | None = None) -> str:
    return scenario.normalize_telemetry_dropout_mode(_get(environ, TELEMETRY_DROPOUT_MODE_ENV))


def mavlink_link_degradation_mode_request(*, environ: Mapping[str, str] | None = None) -> str:
    return scenario.normalize_mavlink_link_degradation_mode(
        _get(environ, MAVLINK_LINK_DEGRADATION_MODE_ENV)
    )


def collision_obstacle_motion_spec(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return scenario.build_collision_obstacle_motion_spec(
        start_x_m=bounded_float_value(COLLISION_OBSTACLE_START_X_ENV, default=2.1, environ=environ),
        start_y_m=bounded_float_value(COLLISION_OBSTACLE_START_Y_ENV, default=2.1, environ=environ),
        end_x_m=bounded_float_value(COLLISION_OBSTACLE_END_X_ENV, default=3.7, environ=environ),
        end_y_m=bounded_float_value(COLLISION_OBSTACLE_END_Y_ENV, default=3.7, environ=environ),
    )


def wind_requested_profile(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return scenario.build_wind_requested_profile(
        wind_mean_mps=optional_float_value(WIND_MEAN_MPS_ENV, environ=environ),
        wind_direction_deg=optional_float_value(WIND_DIRECTION_DEG_ENV, environ=environ),
        wind_gust_mps=optional_float_value(WIND_GUST_MPS_ENV, environ=environ),
        wind_variance=optional_float_value(WIND_VARIANCE_ENV, environ=environ),
    )


def thermal_weather_requested_profile(
    *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    return scenario.build_thermal_weather_requested_profile(
        temperature_c=optional_float_value(TEMPERATURE_C_ENV, environ=environ),
        pressure_hpa=optional_float_value(PRESSURE_HPA_ENV, environ=environ),
        thermal_battery_drain_factor=optional_float_value(
            THERMAL_BATTERY_DRAIN_FACTOR_ENV, environ=environ
        ),
        thermal_motor_derate_factor=optional_float_value(
            THERMAL_MOTOR_DERATE_FACTOR_ENV, environ=environ
        ),
    )


def battery_requested_profile(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return scenario.build_battery_requested_profile(
        battery_scenario=_get(environ, BATTERY_SCENARIO_ENV),
        requested_remaining_percent=optional_float_value(
            BATTERY_REMAINING_PERCENT_ENV, environ=environ
        ),
    )


def sensor_failure_requested_profile(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return scenario.build_sensor_failure_requested_profile(
        sensor_component=_get(environ, SENSOR_FAILURE_COMPONENT_ENV),
        failure_type=_get(environ, SENSOR_FAILURE_TYPE_ENV),
    )


__all__ = [name for name in globals() if name.endswith("_ENV")]
__all__ += [
    "battery_requested_profile",
    "bounded_float_value",
    "collision_obstacle_contact_topic_requested",
    "collision_obstacle_motion_spec",
    "collision_obstacle_requested",
    "float_value",
    "form2a_wind_compensation_request",
    "landing_zone_blocked_requested",
    "mavlink_link_degradation_mode_request",
    "moving_actor_marker_requested",
    "multi_drone_conflict_probe_requested",
    "no_fly_zone_marker_requested",
    "optional_float_value",
    "payload_mass_request",
    "payload_model_enabled",
    "rth_behavior_requested",
    "sensor_failure_requested_profile",
    "telemetry_dropout_mode_request",
    "thermal_weather_requested_profile",
    "traffic_conflict_marker_requested",
    "visibility_mode_request",
    "wind_requested_profile",
]
