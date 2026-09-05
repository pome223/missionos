"""Deterministic multi-hazard feasibility verification for bounded actions."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.runtime.px4_gazebo_route.hazard_state import (
    HAZARD_STATE_SCHEMA_VERSION,
    recovery_policy_sha256,
)


ACTION_FEASIBILITY_SCHEMA_VERSION = (
    "missionos_runtime_recovery_action_feasibility.v1"
)
ACTION_FEASIBILITY_SET_SCHEMA_VERSION = (
    "missionos_runtime_recovery_action_feasibility_set.v1"
)
SUPPORTED_FEASIBILITY_ACTIONS = frozenset(
    {
        "avoid_obstacle",
        "reroute",
        "adjust_altitude",
        "return_to_launch",
        "land",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _fact_value(hazard_state: Mapping[str, Any], name: str) -> Any:
    observed = _mapping(hazard_state.get("observed_facts"))
    derived = _mapping(hazard_state.get("derived_facts"))
    fact = _mapping(observed.get(name)) or _mapping(derived.get(name))
    return fact.get("value")


def _fact_status(hazard_state: Mapping[str, Any], name: str) -> str:
    observed = _mapping(hazard_state.get("observed_facts"))
    derived = _mapping(hazard_state.get("derived_facts"))
    fact = _mapping(observed.get(name)) or _mapping(derived.get(name))
    return str(fact.get("fact_status") or "").strip()


def _fact_frame(hazard_state: Mapping[str, Any], name: str) -> str:
    observed = _mapping(hazard_state.get("observed_facts"))
    return str(_mapping(observed.get(name)).get("frame") or "").strip()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def action_feasibility_hash_matches(feasibility: Mapping[str, Any]) -> bool:
    expected = str(feasibility.get("action_feasibility_sha256") or "")
    unhashed = {
        key: value
        for key, value in feasibility.items()
        if key
        not in {
            "action_feasibility_id",
            "action_feasibility_sha256",
        }
    }
    return bool(expected and expected == _canonical_sha256(unhashed))


def _candidate_action(candidate: Mapping[str, Any]) -> str:
    return str(
        candidate.get("selected_bounded_action")
        or candidate.get("action")
        or candidate.get("compiled_action")
        or ""
    ).strip()


def _candidate_parameters(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return (
        _mapping(candidate.get("proposed_parameters"))
        or _mapping(candidate.get("compiled_parameters"))
        or _mapping(candidate.get("parameters"))
    )


def _point_aabb_distance(
    point: tuple[float, float, float],
    bounds: tuple[float, float, float, float, float, float],
) -> float:
    minimum_x, maximum_x, minimum_y, maximum_y, minimum_z, maximum_z = bounds
    squared = 0.0
    for value, minimum, maximum in (
        (point[0], minimum_x, maximum_x),
        (point[1], minimum_y, maximum_y),
        (point[2], minimum_z, maximum_z),
    ):
        delta = minimum - value if value < minimum else value - maximum if value > maximum else 0.0
        squared += delta * delta
    return math.sqrt(squared)


def _segment_aabb_minimum_distance(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    bounds: tuple[float, float, float, float, float, float],
) -> float:
    """Return the exact Euclidean minimum between one segment and an AABB."""

    delta = tuple(end[index] - start[index] for index in range(3))
    axis_bounds = (
        (bounds[0], bounds[1]),
        (bounds[2], bounds[3]),
        (bounds[4], bounds[5]),
    )
    breakpoints = {0.0, 1.0}
    for axis, (minimum, maximum) in enumerate(axis_bounds):
        if abs(delta[axis]) <= 1e-12:
            continue
        for boundary in (minimum, maximum):
            crossing = (boundary - start[axis]) / delta[axis]
            if 0.0 < crossing < 1.0:
                breakpoints.add(crossing)
    ordered = sorted(breakpoints)
    minimum_distance = min(
        _point_aabb_distance(
            tuple(start[axis] + delta[axis] * value for axis in range(3)),
            bounds,
        )
        for value in ordered
    )
    for left, right in zip(ordered, ordered[1:]):
        midpoint = (left + right) / 2.0
        quadratic_a = 0.0
        quadratic_b = 0.0
        quadratic_c = 0.0
        for axis, (minimum, maximum) in enumerate(axis_bounds):
            midpoint_value = start[axis] + delta[axis] * midpoint
            if midpoint_value < minimum:
                offset_a = minimum - start[axis]
                offset_b = -delta[axis]
            elif midpoint_value > maximum:
                offset_a = start[axis] - maximum
                offset_b = delta[axis]
            else:
                continue
            quadratic_a += offset_b * offset_b
            quadratic_b += 2.0 * offset_a * offset_b
            quadratic_c += offset_a * offset_a
        candidates = [left, right]
        if quadratic_a > 1e-12:
            candidates.append(
                max(left, min(right, -quadratic_b / (2.0 * quadratic_a)))
            )
        for value in candidates:
            squared = (
                quadratic_a * value * value
                + quadratic_b * value
                + quadratic_c
            )
            minimum_distance = min(
                minimum_distance,
                math.sqrt(max(0.0, squared)),
            )
    return minimum_distance


def _verify_obstacle_path_clearance(
    *,
    candidate: Mapping[str, Any],
    hazard_state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    blocked: list[str] = []
    unverified: list[str] = []
    geometry = _mapping(hazard_state.get("obstacle_geometry"))
    if geometry.get("geometry_status") != "verified":
        unverified.extend(
            str(item) for item in geometry.get("blocking_reasons") or []
        )
        unverified.append("action_feasibility_obstacle_geometry_unverified")
    path = _mapping(candidate.get("recovery_path"))
    frame_id = str(path.get("frame_id") or "").strip()
    geometry_frame = str(geometry.get("frame_id") or "").strip()
    if not frame_id:
        unverified.append("action_feasibility_recovery_path_frame_missing")
    elif geometry_frame and frame_id != geometry_frame:
        unverified.append("action_feasibility_recovery_path_frame_mismatch")
    origin_frames = {
        value
        for value in (
            _fact_frame(hazard_state, "local_x_m"),
            _fact_frame(hazard_state, "local_y_m"),
            _fact_frame(hazard_state, "altitude_above_home_m"),
        )
        if value
    }
    if len(origin_frames) != 1 or (frame_id and frame_id not in origin_frames):
        unverified.append("action_feasibility_recovery_path_origin_frame_mismatch")
    raw_waypoints = path.get("waypoints")
    raw_waypoints = raw_waypoints if isinstance(raw_waypoints, list) else []
    current = (
        _number(_fact_value(hazard_state, "local_x_m")),
        _number(_fact_value(hazard_state, "local_y_m")),
        _number(_fact_value(hazard_state, "altitude_above_home_m")),
    )
    waypoints: list[tuple[float, float, float]] = []
    for raw in raw_waypoints:
        waypoint = _mapping(raw)
        values = (
            _number(waypoint.get("x_m")),
            _number(waypoint.get("y_m")),
            _number(waypoint.get("z_m"), waypoint.get("altitude_m")),
        )
        if any(value is None for value in values):
            unverified.append("action_feasibility_recovery_path_waypoint_invalid")
            break
        waypoints.append(tuple(float(value) for value in values))
    if any(value is None for value in current):
        unverified.append("action_feasibility_recovery_path_origin_missing")
    if not waypoints:
        unverified.append("action_feasibility_recovery_path_missing")
    elif len(waypoints) != 1:
        unverified.append(
            "action_feasibility_recovery_path_not_executor_equivalent"
        )
    parameters = _candidate_parameters(candidate)
    parameter_endpoint = (
        _number(parameters.get("target_x_m"), parameters.get("x_m")),
        _number(parameters.get("target_y_m"), parameters.get("y_m")),
        _number(
            parameters.get("target_altitude_m"),
            parameters.get("altitude_m"),
        ),
    )
    if any(value is None for value in parameter_endpoint):
        unverified.append(
            "action_feasibility_recovery_path_parameter_endpoint_missing"
        )
    elif waypoints and any(
        abs(waypoints[-1][axis] - float(parameter_endpoint[axis])) > 1e-6
        for axis in range(3)
    ):
        blocked.append("action_feasibility_recovery_path_endpoint_mismatch")

    raw_bounds = _mapping(geometry.get("bounds_local_xyz_m"))
    bound_values = tuple(
        _number(raw_bounds.get(key))
        for key in (
            "min_x_m",
            "max_x_m",
            "min_y_m",
            "max_y_m",
            "min_z_m",
            "max_z_m",
        )
    )
    required_clearance = _number(geometry.get("required_clearance_m"))
    if any(value is None for value in bound_values):
        unverified.append("action_feasibility_obstacle_bounds_missing")
    if required_clearance is None or required_clearance < 0:
        unverified.append("action_feasibility_required_obstacle_clearance_missing")

    segment_clearances: list[float] = []
    comparable = not unverified and not blocked
    if comparable:
        bounds = tuple(float(value) for value in bound_values)
        points = [
            tuple(float(value) for value in current),
            *waypoints,
        ]
        for start, end in zip(points, points[1:]):
            segment_clearances.append(
                _segment_aabb_minimum_distance(start, end, bounds)
            )
        minimum_clearance = min(segment_clearances)
        if minimum_clearance <= float(required_clearance) + 1e-9:
            blocked.append("action_feasibility_obstacle_clearance_not_met")

        start = points[0]
        end = points[-1]
        center_x = (bounds[0] + bounds[1]) / 2.0
        center_y = (bounds[2] + bounds[3]) / 2.0
        vector_x = center_x - start[0]
        vector_y = center_y - start[1]
        vector_length = math.hypot(vector_x, vector_y)
        if vector_length <= 1e-9:
            unverified.append(
                "action_feasibility_obstacle_pass_direction_unavailable"
            )
        else:
            unit_x = vector_x / vector_length
            unit_y = vector_y / vector_length
            half_x = (bounds[1] - bounds[0]) / 2.0
            half_y = (bounds[3] - bounds[2]) / 2.0
            far_face = (
                center_x * unit_x
                + center_y * unit_y
                + abs(unit_x) * half_x
                + abs(unit_y) * half_y
            )
            endpoint_along = end[0] * unit_x + end[1] * unit_y
            if endpoint_along <= far_face + float(required_clearance) + 1e-9:
                blocked.append(
                    "action_feasibility_recovery_path_not_beyond_obstacle"
                )
    else:
        minimum_clearance = None
    return (
        {
            "verification_status": (
                "blocked"
                if blocked
                else "unverified"
                if unverified
                else "verified"
            ),
            "frame_id": frame_id or None,
            "obstacle_frame_id": geometry_frame or None,
            "required_clearance_m": required_clearance,
            "minimum_clearance_m": (
                round(minimum_clearance, 6)
                if minimum_clearance is not None
                else None
            ),
            "segment_clearance_m": [
                round(value, 6) for value in segment_clearances
            ],
        },
        blocked,
        unverified,
    )


def _maneuver_geometry(
    *,
    action: str,
    parameters: Mapping[str, Any],
    hazard_state: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> tuple[
    float | None,
    float | None,
    float | None,
    dict[str, Any],
    list[str],
]:
    reasons: list[str] = []
    current_x = _number(_fact_value(hazard_state, "local_x_m"))
    current_y = _number(_fact_value(hazard_state, "local_y_m"))
    current_altitude = _number(_fact_value(hazard_state, "altitude_above_home_m"))
    target_x = _number(parameters.get("target_x_m"), parameters.get("x_m"))
    target_y = _number(parameters.get("target_y_m"), parameters.get("y_m"))
    target_altitude = _number(
        parameters.get("target_altitude_m"),
        parameters.get("altitude_m"),
    )
    horizontal_distance: float | None = 0.0
    if action in {"avoid_obstacle", "reroute", "calibrate_offboard"}:
        if None in {current_x, current_y, target_x, target_y}:
            horizontal_distance = None
            reasons.append("action_feasibility_horizontal_geometry_missing")
        else:
            horizontal_distance = math.hypot(
                float(target_x) - float(current_x),
                float(target_y) - float(current_y),
            )
    elif action == "return_to_launch":
        horizontal_distance = _number(
            parameters.get("distance_to_home_m"),
            _fact_value(hazard_state, "distance_to_home_m"),
        )
        if horizontal_distance is None:
            reasons.append("action_feasibility_distance_to_home_missing")

    vertical_distance: float | None = 0.0
    if action == "land":
        if current_altitude is None:
            vertical_distance = None
            reasons.append("action_feasibility_current_altitude_missing")
        else:
            vertical_distance = max(0.0, current_altitude)
    elif target_altitude is not None:
        if current_altitude is None:
            vertical_distance = None
            reasons.append("action_feasibility_current_altitude_missing")
        else:
            vertical_distance = abs(target_altitude - current_altitude)
    elif action == "adjust_altitude":
        vertical_distance = None
        reasons.append("action_feasibility_target_altitude_missing")

    max_horizontal_speed = _number(
        recovery_policy.get("max_recovery_horizontal_speed_mps")
    )
    max_vertical_speed = _number(
        recovery_policy.get("max_recovery_vertical_speed_mps")
    )
    wind_margin = _number(_fact_value(hazard_state, "wind_control_margin_mps"))
    performance_envelope = _mapping(hazard_state.get("performance_envelope"))
    performance_required = bool(
        recovery_policy.get("offboard_performance_envelope_required") is True
        and action in {"avoid_obstacle", "reroute"}
    )
    calibration_speed = _number(
        recovery_policy.get("offboard_performance_calibration_speed_mps")
    )
    performance_speed = _number(
        performance_envelope.get("conservative_horizontal_speed_mps")
    )
    if performance_required:
        if performance_envelope.get("envelope_status") != "verified":
            reasons.extend(
                str(item)
                for item in performance_envelope.get("blocking_reasons") or []
            )
            reasons.append(
                "action_feasibility_offboard_performance_envelope_unverified"
            )
        if performance_speed is None or performance_speed <= 0:
            reasons.append(
                "action_feasibility_offboard_performance_speed_missing"
            )
    if horizontal_distance and wind_margin is None:
        reasons.append("action_feasibility_wind_control_margin_missing")
    bounded_horizontal_speed = (
        calibration_speed
        if action == "calibrate_offboard"
        else performance_speed
        if performance_required
        else wind_margin
    )
    if action == "calibrate_offboard" and (
        calibration_speed is None or calibration_speed <= 0
    ):
        reasons.append(
            "action_feasibility_offboard_calibration_speed_missing"
        )
    horizontal_speed = (
        min(
            value
            for value in (
                bounded_horizontal_speed,
                max_horizontal_speed,
                wind_margin,
            )
            if value is not None and value > 0
        )
        if (
            (
                action != "calibrate_offboard"
                or calibration_speed is not None
            )
            and (not performance_required or performance_speed is not None)
            and any(
                value is not None and value > 0
                for value in (
                    bounded_horizontal_speed,
                    max_horizontal_speed,
                    wind_margin,
                )
            )
        )
        else None
    )
    horizontal_duration = (
        horizontal_distance / horizontal_speed
        if horizontal_distance is not None
        and horizontal_distance > 0
        and horizontal_speed is not None
        and horizontal_speed > 0
        else 0.0
        if horizontal_distance == 0
        else None
    )
    if vertical_distance and max_vertical_speed is None:
        reasons.append("action_feasibility_vertical_speed_limit_missing")
    vertical_duration = (
        vertical_distance / max_vertical_speed
        if vertical_distance is not None
        and max_vertical_speed is not None
        and max_vertical_speed > 0
        else 0.0
        if vertical_distance == 0
        else None
    )
    setup_seconds = _number(recovery_policy.get("reachability_setup_seconds"))
    duration_margin = _number(
        recovery_policy.get("reachability_duration_margin_factor")
    )
    if setup_seconds is None:
        reasons.append("action_feasibility_setup_duration_missing")
    if duration_margin is None:
        reasons.append("action_feasibility_duration_margin_missing")
    # Separating setup/resume waits from measured travel must not erase them
    # from the duration budget. Keep the policy floor and reserve observed
    # same-task non-movement time with the existing uncertainty margin.
    observed_non_movement_seconds = (
        _number(performance_envelope.get("non_movement_duration_seconds"))
        if performance_required else None
    )
    effective_setup_seconds = setup_seconds
    if (
        observed_non_movement_seconds is not None
        and setup_seconds is not None and duration_margin is not None
    ):
        effective_setup_seconds = max(
            setup_seconds, observed_non_movement_seconds * duration_margin
        )
    duration = (
        max(horizontal_duration, vertical_duration) * duration_margin
        + effective_setup_seconds
        if horizontal_duration is not None
        and vertical_duration is not None
        and duration_margin is not None
        and setup_seconds is not None
        else None
    )
    duration_model = {
        "model_id": (
            "observed_same_task_offboard_performance.v1"
            if performance_required
            else "bounded_sitl_offboard_calibration.v1"
            if action == "calibrate_offboard"
            else "bounded_policy_speed_with_wind_margin.v1"
        ),
        "performance_envelope_required": performance_required,
        "performance_envelope_status": performance_envelope.get(
            "envelope_status"
        ),
        "observed_horizontal_speed_mps": performance_envelope.get(
            "observed_horizontal_speed_mps"
        ),
        "conservative_horizontal_speed_mps": performance_speed,
        "effective_horizontal_speed_mps": horizontal_speed,
        "wind_control_margin_mps": wind_margin,
        "maximum_horizontal_speed_mps": max_horizontal_speed,
        "maximum_vertical_speed_mps": max_vertical_speed,
        "calibration_speed_mps": (
            calibration_speed if action == "calibrate_offboard" else None
        ),
        "duration_margin_factor": duration_margin,
        "setup_seconds": setup_seconds,
        "observed_non_movement_duration_seconds": observed_non_movement_seconds,
        "effective_setup_seconds": effective_setup_seconds,
        "source_refs": list(performance_envelope.get("source_refs") or []),
    }
    return (
        horizontal_distance,
        vertical_distance,
        duration,
        duration_model,
        reasons,
    )


def _projected_battery_after_action(
    *,
    horizontal_distance_m: float | None,
    vertical_distance_m: float | None,
    hazard_state: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> tuple[float | None, dict[str, Any], list[str]]:
    model = _mapping(recovery_policy.get("battery_action_energy_model"))
    effective_battery = _number(
        _fact_value(hazard_state, "effective_battery_remaining_percent")
    )
    reasons: list[str] = []
    model_id = str(model.get("model_id") or "").strip()
    model_version = str(model.get("model_version") or "").strip()
    source_refs = [
        str(item).strip()
        for item in model.get("source_refs") or []
        if str(item).strip()
    ]
    uncertainty = _number(model.get("uncertainty_percent"))
    horizontal_rate = _number(model.get("percent_per_meter_horizontal"))
    climb_rate = _number(model.get("percent_per_meter_climb"))
    headwind_rate = _number(model.get("headwind_multiplier_per_mps"))
    payload_multiplier_per_kg = _number(
        model.get("payload_energy_multiplier_per_kg")
    )
    payload_mass_kg = _number(_fact_value(hazard_state, "payload_mass_kg"))
    payload_status = _fact_status(hazard_state, "payload_mass_kg")
    payload_requested_mass_kg = _number(
        _fact_value(hazard_state, "payload_requested_mass_kg")
    )
    payload_requested_status = _fact_status(
        hazard_state,
        "payload_requested_mass_kg",
    )
    if not model_id:
        reasons.append("battery_action_energy_model_id_missing")
    if not model_version:
        reasons.append("battery_action_energy_model_version_missing")
    if not source_refs:
        reasons.append("battery_action_energy_model_source_refs_missing")
    if uncertainty is None:
        reasons.append("battery_action_energy_model_uncertainty_missing")
    if horizontal_rate is None:
        reasons.append("battery_action_energy_horizontal_rate_missing")
    if climb_rate is None:
        reasons.append("battery_action_energy_climb_rate_missing")
    if headwind_rate is None:
        reasons.append("battery_action_energy_headwind_rate_missing")
    payload_request_is_applicable = payload_requested_status != "not_applicable"
    payload_is_requested = payload_requested_mass_kg is not None
    if payload_request_is_applicable and payload_requested_mass_kg is None:
        reasons.append("action_feasibility_payload_requested_mass_missing")
    if payload_is_requested:
        if payload_requested_mass_kg < 0:
            reasons.append("action_feasibility_payload_requested_mass_invalid")
        if payload_mass_kg is None:
            reasons.append("action_feasibility_payload_applied_mass_missing")
        elif payload_mass_kg < 0:
            reasons.append("action_feasibility_payload_applied_mass_invalid")
        elif not math.isclose(
            payload_mass_kg,
            payload_requested_mass_kg,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            reasons.append("action_feasibility_payload_mass_mismatch")
        elif payload_status not in {"observed", "configured_applied"}:
            reasons.append("action_feasibility_payload_mass_unverified")
        if payload_requested_status in {"missing", "not_applicable", ""}:
            reasons.append("action_feasibility_payload_request_unverified")
    elif payload_mass_kg is not None:
        if payload_mass_kg < 0:
            reasons.append("action_feasibility_payload_applied_mass_invalid")
        if payload_status not in {"observed", "configured_applied"}:
            reasons.append("action_feasibility_payload_mass_unverified")
    if payload_is_requested or payload_mass_kg is not None:
        if payload_multiplier_per_kg is None:
            reasons.append("battery_action_energy_payload_multiplier_missing")
        elif payload_multiplier_per_kg < 0:
            reasons.append("battery_action_energy_payload_multiplier_invalid")
    if effective_battery is None:
        reasons.append("effective_battery_remaining_missing")
    if horizontal_distance_m is None or vertical_distance_m is None:
        reasons.append("battery_action_energy_geometry_missing")
    wind_speed = _number(_fact_value(hazard_state, "wind_speed_mps"))
    if wind_speed is None:
        reasons.append("battery_action_energy_wind_missing")
    projected = None
    if not reasons:
        base_cost = (
            float(horizontal_distance_m) * float(horizontal_rate)
            + float(vertical_distance_m) * float(climb_rate)
        )
        wind_multiplier = 1.0 + float(wind_speed) * float(headwind_rate)
        payload_multiplier = (
            1.0 + float(payload_mass_kg) * float(payload_multiplier_per_kg)
            if payload_mass_kg is not None and payload_multiplier_per_kg is not None
            else 1.0
        )
        conservative_cost = (
            base_cost
            * wind_multiplier
            * payload_multiplier
            * (1.0 + float(uncertainty) / 100.0)
        )
        projected = float(effective_battery) - conservative_cost
    return (
        projected,
        {
            "model_id": model_id or None,
            "model_version": model_version or None,
            "source_refs": source_refs,
            "uncertainty_percent": uncertainty,
            "percent_per_meter_horizontal": horizontal_rate,
            "percent_per_meter_climb": climb_rate,
            "headwind_multiplier_per_mps": headwind_rate,
            "payload_energy_multiplier_per_kg": payload_multiplier_per_kg,
            "payload_requested_mass_kg": payload_requested_mass_kg,
            "payload_requested_fact_status": payload_requested_status or None,
            "payload_mass_kg": payload_mass_kg,
            "payload_fact_status": payload_status or None,
        },
        reasons,
    )


def verify_runtime_recovery_action_feasibility(
    *,
    candidate: Mapping[str, Any],
    hazard_state: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one candidate without selecting, approving, or dispatching it."""

    action = _candidate_action(candidate)
    parameters = _candidate_parameters(candidate)
    blocked: list[str] = []
    unverified: list[str] = []
    source_refs = [
        str(item).strip()
        for item in candidate.get("source_refs") or []
        if str(item).strip()
    ]
    if action not in SUPPORTED_FEASIBILITY_ACTIONS | {
        "calibrate_offboard"
    }:
        blocked.append(f"action_feasibility_action_not_supported:{action or '<missing>'}")
    if hazard_state.get("schema_version") != HAZARD_STATE_SCHEMA_VERSION:
        unverified.append("action_feasibility_hazard_state_schema_invalid")
    if hazard_state.get("hazard_state_status") != "verified":
        freshness = _mapping(hazard_state.get("freshness"))
        unverified.extend(
            str(item) for item in freshness.get("blocking_reasons") or []
        )
        unverified.append("action_feasibility_hazard_state_unverified")
    current_policy_sha256 = recovery_policy_sha256(recovery_policy)
    if hazard_state.get("policy_sha256") != current_policy_sha256:
        unverified.append("action_feasibility_policy_drift")

    for fact_name in (
        "battery_remaining_percent",
        "wind_speed_mps",
        "terrain_clearance_m",
        "altitude_above_home_m",
    ):
        if _fact_value(hazard_state, fact_name) is None:
            unverified.append(f"action_feasibility_fact_missing:{fact_name}")
    temperature_model = _mapping(hazard_state.get("temperature_model"))
    if temperature_model.get("model_status") == "unverified":
        unverified.extend(
            str(item) for item in temperature_model.get("blocking_reasons") or []
        )
        unverified.append("action_feasibility_temperature_model_unverified")

    wind_margin = _number(_fact_value(hazard_state, "wind_control_margin_mps"))
    if wind_margin is None:
        unverified.append("action_feasibility_wind_control_margin_missing")
    elif wind_margin <= 0:
        blocked.append("action_feasibility_wind_control_margin_not_positive")
    terrain_margin = _number(
        _fact_value(hazard_state, "terrain_clearance_margin_m")
    )
    current_altitude = _number(
        _fact_value(hazard_state, "altitude_above_home_m")
    )
    target_altitude = _number(
        parameters.get("target_altitude_m"),
        parameters.get("altitude_m"),
    )
    projected_terrain_margin = terrain_margin
    if (
        projected_terrain_margin is not None
        and current_altitude is not None
        and target_altitude is not None
    ):
        # Both ascent and descent change clearance relative to the same
        # altitude-up local frame.  Ignoring a negative delta made a descent
        # appear to retain its pre-maneuver terrain margin.
        projected_terrain_margin += target_altitude - current_altitude
    if terrain_margin is None:
        unverified.append("action_feasibility_terrain_margin_missing")
    elif projected_terrain_margin is not None and projected_terrain_margin < 0:
        blocked.append("action_feasibility_terrain_clearance_below_policy")
    motor_factor = _number(_fact_value(hazard_state, "motor_thrust_factor"))
    minimum_motor_factor = _number(
        recovery_policy.get("minimum_motor_thrust_factor")
    )
    if motor_factor is not None:
        if minimum_motor_factor is None:
            unverified.append("action_feasibility_minimum_motor_factor_missing")
        elif motor_factor < minimum_motor_factor:
            blocked.append("action_feasibility_motor_thrust_derating_exceeded")

    (
        horizontal_distance,
        vertical_distance,
        duration,
        maneuver_duration_model,
        geometry_reasons,
    ) = (
        _maneuver_geometry(
            action=action,
            parameters=parameters,
            hazard_state=hazard_state,
            recovery_policy=recovery_policy,
        )
    )
    unverified.extend(geometry_reasons)
    candidate_maximum_duration = _mapping(
        candidate.get("intent_constraints")
    ).get("maximum_duration_s")
    maximum_duration = (
        _number(
            candidate_maximum_duration,
            recovery_policy.get("max_obstacle_avoidance_duration_s"),
            recovery_policy.get("max_recovery_duration_s"),
        )
        if action == "avoid_obstacle"
        else _number(
            candidate_maximum_duration,
            recovery_policy.get("max_recovery_duration_s"),
        )
    )
    if maximum_duration is None:
        unverified.append("action_feasibility_maximum_duration_missing")
    elif duration is None:
        unverified.append("action_feasibility_duration_bound_missing")
    elif duration > maximum_duration:
        blocked.append("action_feasibility_duration_bound_exceeded")

    obstacle_clearance = {
        "verification_status": "not_applicable",
        "frame_id": None,
        "obstacle_frame_id": None,
        "required_clearance_m": None,
        "minimum_clearance_m": None,
        "segment_clearance_m": [],
    }
    if action == "avoid_obstacle":
        local_avoidance = _fact_value(hazard_state, "local_avoidance_required")
        safety_hold_observed = _fact_value(
            hazard_state,
            "safety_hold_observed",
        )
        obstacle_name = str(
            _fact_value(hazard_state, "nearest_obstacle_name") or ""
        ).strip()
        candidate_source = str(parameters.get("source_obstacle_name") or "").strip()
        if local_avoidance is not True:
            blocked.append("action_feasibility_local_avoidance_not_required")
        if not obstacle_name:
            unverified.append("action_feasibility_nearest_obstacle_missing")
        if not candidate_source:
            unverified.append("action_feasibility_candidate_obstacle_source_missing")
        elif obstacle_name and candidate_source != obstacle_name:
            blocked.append("action_feasibility_obstacle_source_mismatch")
        time_to_conflict = _number(
            _fact_value(hazard_state, "obstacle_time_to_conflict_s")
        )
        if safety_hold_observed is True:
            # The source-backed preauthorized HOLD stops progress along the
            # colliding route before human approval. The original-route TTC is
            # therefore no longer a running maneuver deadline. Geometry,
            # source identity, reachability, and every other hazard remain
            # mandatory and are revalidated again at dispatch.
            pass
        elif time_to_conflict is None:
            unverified.append("action_feasibility_time_to_conflict_missing")
        elif duration is not None and duration >= time_to_conflict:
            blocked.append(
                "action_feasibility_maneuver_not_complete_before_conflict"
            )
        (
            obstacle_clearance,
            obstacle_clearance_blocked,
            obstacle_clearance_unverified,
        ) = _verify_obstacle_path_clearance(
            candidate=candidate,
            hazard_state=hazard_state,
        )
        blocked.extend(obstacle_clearance_blocked)
        unverified.extend(obstacle_clearance_unverified)
    if action == "calibrate_offboard":
        if (
            recovery_policy.get(
                "offboard_performance_calibration_enabled"
            )
            is not True
        ):
            blocked.append(
                "action_feasibility_offboard_calibration_disabled"
            )
        local_avoidance = _fact_value(
            hazard_state,
            "local_avoidance_required",
        )
        if local_avoidance is None:
            unverified.append(
                "action_feasibility_offboard_calibration_conflict_state_missing"
            )
        elif local_avoidance is True:
            blocked.append(
                "action_feasibility_offboard_calibration_conflict_active"
            )
        performance_envelope = _mapping(
            hazard_state.get("performance_envelope")
        )
        if performance_envelope.get("envelope_status") == "verified":
            blocked.append(
                "action_feasibility_offboard_calibration_already_observed"
            )
        max_calibration_distance = _number(
            recovery_policy.get(
                "offboard_performance_calibration_max_distance_m"
            )
        )
        if max_calibration_distance is None or max_calibration_distance <= 0:
            unverified.append(
                "action_feasibility_offboard_calibration_distance_limit_missing"
            )
        elif (
            horizontal_distance is not None
            and horizontal_distance > max_calibration_distance
        ):
            blocked.append(
                "action_feasibility_offboard_calibration_distance_exceeded"
            )
        max_altitude_delta = _number(
            recovery_policy.get(
                "offboard_performance_calibration_max_altitude_delta_m"
            )
        )
        if max_altitude_delta is None or max_altitude_delta < 0:
            unverified.append(
                "action_feasibility_offboard_calibration_altitude_limit_missing"
            )
        elif (
            vertical_distance is not None
            and vertical_distance > max_altitude_delta
        ):
            blocked.append(
                "action_feasibility_offboard_calibration_altitude_delta_exceeded"
            )
        if parameters.get("calibration_only") is not True:
            blocked.append(
                "action_feasibility_offboard_calibration_flag_required"
            )
        if parameters.get("resume_original_route") is not True:
            blocked.append(
                "action_feasibility_offboard_calibration_route_resume_required"
            )
    if action == "land":
        landing_zone_safe = _fact_value(hazard_state, "landing_zone_safe")
        if landing_zone_safe is None:
            unverified.append("action_feasibility_landing_zone_safety_missing")
        elif landing_zone_safe is not True:
            blocked.append("action_feasibility_landing_zone_not_safe")

    projected_battery, battery_model, battery_reasons = (
        _projected_battery_after_action(
            horizontal_distance_m=horizontal_distance,
            vertical_distance_m=vertical_distance,
            hazard_state=hazard_state,
            recovery_policy=recovery_policy,
        )
    )
    unverified.extend(battery_reasons)
    battery_threshold = _number(
        recovery_policy.get("battery_return_threshold_percent")
    )
    if battery_threshold is None:
        unverified.append("action_feasibility_battery_threshold_missing")
    elif projected_battery is not None and projected_battery < battery_threshold:
        blocked.append("action_feasibility_projected_battery_reserve_negative")

    blocked = list(dict.fromkeys(blocked))
    unverified = [
        item for item in dict.fromkeys(unverified) if item not in blocked
    ]
    status = (
        "blocked"
        if blocked
        else "unverified"
        if unverified
        else "verified_feasible"
    )
    payload = {
        "schema_version": ACTION_FEASIBILITY_SCHEMA_VERSION,
        "feasibility_status": status,
        "action": action,
        "candidate_parameters": parameters,
        "source_hazard_state_id": hazard_state.get("hazard_state_id"),
        "source_hazard_state_sha256": hazard_state.get("hazard_state_sha256"),
        "telemetry_cursor": dict(
            _mapping(hazard_state.get("telemetry_cursor"))
        ),
        "policy_ref": hazard_state.get("policy_ref"),
        "policy_sha256": current_policy_sha256,
        "model_refs": {
            "temperature": temperature_model.get("model_id"),
            "battery_action_energy": battery_model.get("model_id"),
        },
        "source_refs": list(dict.fromkeys(source_refs)),
        "obstacle_clearance_verification": obstacle_clearance,
        "horizontal_distance_m": (
            round(horizontal_distance, 3)
            if horizontal_distance is not None
            else None
        ),
        "vertical_distance_m": (
            round(vertical_distance, 3) if vertical_distance is not None else None
        ),
        "projected_terrain_clearance_margin_m": (
            round(projected_terrain_margin, 3)
            if projected_terrain_margin is not None
            else None
        ),
        "maneuver_completion_upper_bound_s": (
            round(duration, 3) if duration is not None else None
        ),
        "maneuver_duration_model": maneuver_duration_model,
        "time_to_conflict_constraint_status": (
            "suspended_by_observed_safety_hold"
            if action == "avoid_obstacle"
            and _fact_value(hazard_state, "safety_hold_observed") is True
            else "evaluated"
            if action == "avoid_obstacle"
            else "not_applicable"
        ),
        "projected_battery_after_action_percent": (
            round(projected_battery, 3)
            if projected_battery is not None
            else None
        ),
        "battery_action_energy_model": battery_model,
        "blocking_reasons": blocked,
        "unverified_reasons": unverified,
        "assumptions": [
            "headwind_cost_uses_the_source_backed_policy_model",
            "verification_does_not_claim_future_maneuver_success",
            (
                "calibration_dispatch_is_sitl_only_and_requires_separate_"
                "operator_approval"
                if action == "calibrate_offboard"
                else "normal_recovery_action"
            ),
        ],
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }
    digest = _canonical_sha256(payload)
    return {
        **payload,
        "action_feasibility_sha256": digest,
        "action_feasibility_id": f"action_feasibility_{digest[:12]}",
    }


def verify_runtime_recovery_action_candidates(
    *,
    candidates: Sequence[Mapping[str, Any]],
    hazard_state: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_candidates = [
        dict(candidate)
        for candidate in candidates
        if isinstance(candidate, Mapping)
    ]
    evaluations = [
        verify_runtime_recovery_action_feasibility(
            candidate=candidate,
            hazard_state=hazard_state,
            recovery_policy=recovery_policy,
        )
        for candidate in normalized_candidates
    ]
    verified = [
        item for item in evaluations if item["feasibility_status"] == "verified_feasible"
    ]
    return {
        "schema_version": ACTION_FEASIBILITY_SET_SCHEMA_VERSION,
        "source_hazard_state_id": hazard_state.get("hazard_state_id"),
        "source_hazard_state_sha256": hazard_state.get("hazard_state_sha256"),
        "telemetry_cursor": dict(_mapping(hazard_state.get("telemetry_cursor"))),
        "policy_ref": hazard_state.get("policy_ref"),
        "policy_sha256": hazard_state.get("policy_sha256"),
        "evaluations": evaluations,
        "verified_feasible_actions": [item["action"] for item in verified],
        "verified_feasible_candidates": [
            dict(candidate)
            for candidate, evaluation in zip(
                normalized_candidates,
                evaluations,
                strict=True,
            )
            if evaluation["feasibility_status"] == "verified_feasible"
        ],
        "feasible_candidate_count": len(verified),
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }


__all__ = [
    "ACTION_FEASIBILITY_SCHEMA_VERSION",
    "ACTION_FEASIBILITY_SET_SCHEMA_VERSION",
    "SUPPORTED_FEASIBILITY_ACTIONS",
    "action_feasibility_hash_matches",
    "verify_runtime_recovery_action_candidates",
    "verify_runtime_recovery_action_feasibility",
]
