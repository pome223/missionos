"""Backend-neutral, source-backed hazard-state normalization.

The normalized state is evidence for deterministic recovery verification.  It
does not select an action and cannot create approval, dispatch, execution, or
completion authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from src.runtime.px4_gazebo_route.compound_hazard_transition import (
    telemetry_cursor,
)


HAZARD_STATE_SCHEMA_VERSION = "missionos_runtime_recovery_hazard_state.v1"
TEMPERATURE_MODEL_SCHEMA_VERSION = (
    "missionos_runtime_recovery_temperature_model.v1"
)
PERFORMANCE_ENVELOPE_SCHEMA_VERSION = (
    "missionos_runtime_recovery_performance_envelope.v1"
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


def recovery_policy_snapshot(recovery_policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable policy material used by feasibility decisions."""

    excluded = {
        "approval_created",
        "dispatch_authority_created",
        "physical_execution_invoked",
        "progress_counted",
    }
    return {
        str(key): value
        for key, value in recovery_policy.items()
        if str(key) not in excluded
    }


def recovery_policy_sha256(recovery_policy: Mapping[str, Any]) -> str:
    return _canonical_sha256(recovery_policy_snapshot(recovery_policy))


def hazard_state_hash_matches(hazard_state: Mapping[str, Any]) -> bool:
    expected = str(hazard_state.get("hazard_state_sha256") or "")
    material = {
        "telemetry_cursor": _mapping(hazard_state.get("telemetry_cursor")),
        "policy_sha256": hazard_state.get("policy_sha256"),
        "observed_facts": _mapping(hazard_state.get("observed_facts")),
        "derived_facts": _mapping(hazard_state.get("derived_facts")),
        "temperature_model": _mapping(hazard_state.get("temperature_model")),
        "obstacle_geometry": _mapping(hazard_state.get("obstacle_geometry")),
        "performance_envelope": _mapping(
            hazard_state.get("performance_envelope")
        ),
    }
    return bool(expected and expected == _canonical_sha256(material))


def _performance_envelope(
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize observed bounded-OFFBOARD performance without authority.

    AUTO cruise velocity is not interchangeable with lateral OFFBOARD recovery
    performance.  Only a completed, same-task bounded maneuver observation is
    accepted here.  Missing evidence remains ``unverified`` so the LLM may
    reason about it, but neither proposal nor dispatch can treat it as a
    verified execution envelope.
    """

    recovery = _mapping(telemetry_snapshot.get("recovery"))
    observation = _mapping(recovery.get("performance_observation"))
    action = str(observation.get("action") or recovery.get("action") or "").strip()
    sample_count = _number(observation.get("sample_count"))
    duration_seconds = _number(observation.get("duration_seconds"))
    horizontal_distance_m = _number(
        observation.get("horizontal_distance_m")
    )
    observed_speed_mps = _number(
        observation.get("observed_horizontal_speed_mps")
    )
    if (
        observed_speed_mps is None
        and horizontal_distance_m is not None
        and duration_seconds is not None
        and duration_seconds > 0
    ):
        observed_speed_mps = horizontal_distance_m / duration_seconds
    minimum_samples = _number(
        recovery_policy.get("offboard_performance_min_samples")
    )
    uncertainty_fraction = _number(
        recovery_policy.get("offboard_performance_uncertainty_fraction")
    )
    reasons: list[str] = []
    if action not in {
        "avoid_obstacle",
        "reroute",
        "calibrate_offboard",
    }:
        reasons.append("performance_envelope_action_observation_missing")
    if sample_count is None:
        reasons.append("performance_envelope_sample_count_missing")
    elif minimum_samples is None:
        reasons.append("performance_envelope_minimum_samples_missing")
    elif sample_count < minimum_samples:
        reasons.append("performance_envelope_sample_count_insufficient")
    if duration_seconds is None or duration_seconds <= 0:
        reasons.append("performance_envelope_duration_missing")
    if horizontal_distance_m is None or horizontal_distance_m <= 0:
        reasons.append("performance_envelope_horizontal_distance_missing")
    if observed_speed_mps is None or observed_speed_mps <= 0:
        reasons.append("performance_envelope_observed_speed_missing")
    if uncertainty_fraction is None:
        reasons.append("performance_envelope_uncertainty_missing")
    elif not 0 <= uncertainty_fraction < 1:
        reasons.append("performance_envelope_uncertainty_invalid")
    source_refs = [
        str(item).strip()
        for item in observation.get("source_refs") or []
        if str(item).strip()
    ]
    if not source_refs:
        reasons.append("performance_envelope_source_refs_missing")
    conservative_speed_mps = (
        observed_speed_mps * (1.0 - uncertainty_fraction)
        if observed_speed_mps is not None
        and uncertainty_fraction is not None
        and 0 <= uncertainty_fraction < 1
        else None
    )
    if conservative_speed_mps is not None and conservative_speed_mps <= 0:
        reasons.append("performance_envelope_conservative_speed_not_positive")
    return {
        "schema_version": PERFORMANCE_ENVELOPE_SCHEMA_VERSION,
        "envelope_status": "verified" if not reasons else "unverified",
        "evidence_kind": (
            "same_task_bounded_offboard_maneuver"
            if observation
            else "missing"
        ),
        "action": action or None,
        "sample_count": (
            int(sample_count) if sample_count is not None else None
        ),
        "minimum_sample_count": (
            int(minimum_samples) if minimum_samples is not None else None
        ),
        "duration_seconds": duration_seconds,
        "horizontal_distance_m": horizontal_distance_m,
        "observed_horizontal_speed_mps": observed_speed_mps,
        "uncertainty_fraction": uncertainty_fraction,
        "conservative_horizontal_speed_mps": (
            round(conservative_speed_mps, 6)
            if conservative_speed_mps is not None
            else None
        ),
        "source_refs": source_refs,
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
    }


def _obstacle_geometry(
    *,
    obstacle: Mapping[str, Any],
    nearest: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    obstacle_name = str(nearest.get("obstacle_name") or "").strip()
    manifest = _mapping(obstacle.get("obstacle_manifest"))
    raw_obstacles = manifest.get("obstacles")
    raw_obstacles = raw_obstacles if isinstance(raw_obstacles, list) else []
    selected: dict[str, Any] = {}
    for item in raw_obstacles:
        item = _mapping(item)
        if obstacle_name and str(item.get("name") or "").strip() == obstacle_name:
            selected = item
            break
    bounds = _mapping(selected.get("bounds_local_xyz_m"))
    frame_id = str(obstacle.get("frame_id") or "").strip()
    required_clearance = _number(
        recovery_policy.get("obstacle_minimum_clearance_m")
    )
    normalized_bounds = {
        key: _number(bounds.get(key))
        for key in (
            "min_x_m",
            "max_x_m",
            "min_y_m",
            "max_y_m",
            "min_z_m",
            "max_z_m",
        )
    }
    reasons: list[str] = []
    if not obstacle_name:
        reasons.append("obstacle_geometry_name_missing")
    if not selected:
        reasons.append("obstacle_geometry_manifest_entry_missing")
    if not frame_id:
        reasons.append("obstacle_geometry_frame_missing")
    if any(value is None for value in normalized_bounds.values()):
        reasons.append("obstacle_geometry_bounds_missing")
    else:
        for minimum, maximum in (
            ("min_x_m", "max_x_m"),
            ("min_y_m", "max_y_m"),
            ("min_z_m", "max_z_m"),
        ):
            if float(normalized_bounds[minimum]) >= float(
                normalized_bounds[maximum]
            ):
                reasons.append("obstacle_geometry_bounds_invalid")
                break
    if required_clearance is None or required_clearance < 0:
        reasons.append("obstacle_geometry_required_clearance_missing")
    return {
        "geometry_status": "verified" if not reasons else "unverified",
        "obstacle_name": obstacle_name or None,
        "frame_id": frame_id or None,
        "bounds_local_xyz_m": normalized_bounds,
        "required_clearance_m": required_clearance,
        "source_refs": list(
            dict.fromkeys(
                [
                    "telemetry_snapshot.obstacle.obstacle_manifest",
                    *[
                        str(item).strip()
                        for item in selected.get("source_refs") or []
                        if str(item).strip()
                    ],
                ]
            )
        ),
        "blocking_reasons": reasons,
    }


def _source_refs(value: Mapping[str, Any], fallback: str) -> list[str]:
    refs = [
        str(item).strip()
        for item in value.get("source_refs") or []
        if str(item).strip()
    ]
    return list(dict.fromkeys(refs or [fallback]))


def _fact(
    *,
    name: str,
    value: Any,
    unit: str,
    source: Mapping[str, Any],
    fallback_source_ref: str,
    cursor: Mapping[str, Any],
    observed_at: str,
    frame: str = "",
    applicable: bool = True,
) -> dict[str, Any]:
    present = value is not None and value != ""
    configured_status = str(source.get("observation_status") or "").strip()
    fact_status = (
        configured_status
        if present and configured_status in {"configured_applied", "configured_unverified"}
        else "observed" if present else "missing" if applicable else "not_applicable"
    )
    return {
        "fact_name": name,
        "fact_status": fact_status,
        "value": value if present else None,
        "unit": unit,
        "source_refs": _source_refs(source, fallback_source_ref),
        "observed_at": observed_at or None,
        "telemetry_cursor": dict(cursor),
        "frame": frame or None,
    }


def _temperature_model(
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    temperature_c: float | None,
) -> dict[str, Any]:
    temperature = _mapping(telemetry_snapshot.get("temperature"))
    environment = _mapping(telemetry_snapshot.get("environment"))
    candidate = _mapping(temperature.get("model"))
    if not candidate:
        candidate = _mapping(environment.get("temperature_model"))
    policy_model = _mapping(recovery_policy.get("temperature_derating_model"))
    if not candidate:
        candidate = policy_model
    if temperature_c is None:
        return {
            "schema_version": TEMPERATURE_MODEL_SCHEMA_VERSION,
            "model_status": "not_applicable",
            "model_id": None,
            "model_version": None,
            "source_refs": [],
            "uncertainty": None,
            "battery_capacity_factor": None,
            "motor_thrust_factor": None,
            "blocking_reasons": [],
        }

    model_id = str(candidate.get("model_id") or "").strip()
    model_version = str(candidate.get("model_version") or "").strip()
    source_refs = [
        str(item).strip()
        for item in candidate.get("source_refs") or []
        if str(item).strip()
    ]
    uncertainty = _number(
        candidate.get("uncertainty_percent"),
        candidate.get("uncertainty"),
    )
    battery_factor = _number(
        temperature.get("battery_capacity_factor"),
        environment.get("battery_capacity_factor"),
        candidate.get("battery_capacity_factor"),
    )
    motor_factor = _number(
        temperature.get("motor_thrust_factor"),
        environment.get("motor_thrust_factor"),
        candidate.get("motor_thrust_factor"),
    )
    reasons: list[str] = []
    if not model_id:
        reasons.append("temperature_model_id_missing")
    if not model_version:
        reasons.append("temperature_model_version_missing")
    if not source_refs:
        reasons.append("temperature_model_source_refs_missing")
    expected_model_id = str(policy_model.get("model_id") or "").strip()
    expected_model_version = str(
        policy_model.get("model_version") or ""
    ).strip()
    if (
        policy_model
        and (
            model_id != expected_model_id
            or model_version != expected_model_version
        )
    ):
        reasons.append("temperature_model_policy_mismatch")
    if uncertainty is None:
        reasons.append("temperature_model_uncertainty_missing")
    if battery_factor is None:
        reasons.append("temperature_battery_capacity_factor_missing")
    elif not 0.0 < battery_factor <= 1.0:
        reasons.append("temperature_battery_capacity_factor_invalid")
    if motor_factor is None:
        reasons.append("temperature_motor_thrust_factor_missing")
    elif not 0.0 < motor_factor <= 1.0:
        reasons.append("temperature_motor_thrust_factor_invalid")
    return {
        "schema_version": TEMPERATURE_MODEL_SCHEMA_VERSION,
        "model_status": "verified" if not reasons else "unverified",
        "model_id": model_id or None,
        "model_version": model_version or None,
        "source_refs": source_refs,
        "uncertainty": uncertainty,
        "battery_capacity_factor": battery_factor,
        "motor_thrust_factor": motor_factor,
        "blocking_reasons": reasons,
    }


def build_runtime_recovery_hazard_state(
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    observed_at: str = "",
    prior_telemetry_cursor: Mapping[str, Any] | None = None,
    expected_policy_sha256: str = "",
) -> dict[str, Any]:
    """Normalize observed and derived facts without granting authority."""

    telemetry = _mapping(telemetry_snapshot.get("telemetry"))
    battery = _mapping(telemetry_snapshot.get("battery"))
    wind = _mapping(telemetry_snapshot.get("wind"))
    terrain = _mapping(telemetry_snapshot.get("terrain"))
    obstacle = _mapping(telemetry_snapshot.get("obstacle"))
    conflict = _mapping(obstacle.get("conflict_assessment"))
    nearest = _mapping(conflict.get("nearest_obstacle"))
    position = _mapping(telemetry_snapshot.get("position"))
    temperature = _mapping(telemetry_snapshot.get("temperature"))
    environment = _mapping(telemetry_snapshot.get("environment"))
    payload = _mapping(telemetry_snapshot.get("payload"))
    landing = _mapping(telemetry_snapshot.get("landing_zone"))
    recovery = _mapping(telemetry_snapshot.get("recovery"))
    cursor = telemetry_cursor(telemetry_snapshot)
    policy_ref = str(
        recovery_policy.get("policy_ref")
        or recovery_policy.get("recovery_policy_ref")
        or ""
    ).strip()
    policy_snapshot = recovery_policy_snapshot(recovery_policy)
    policy_sha256 = recovery_policy_sha256(recovery_policy)

    battery_remaining = _number(
        battery.get("remaining_percent"),
        telemetry_snapshot.get("battery_remaining_percent"),
    )
    wind_speed = _number(
        wind.get("speed_mps"),
        wind.get("observed_speed_mps"),
        telemetry_snapshot.get("wind_speed_mps"),
    )
    wind_gust = _number(
        wind.get("gust_mps"),
        wind.get("observed_gust_mps"),
        telemetry_snapshot.get("wind_gust_mps"),
    )
    terrain_clearance = _number(
        terrain.get("terrain_clearance_m"),
        terrain.get("clearance_m"),
    )
    terrain_margin = _number(
        terrain.get("terrain_clearance_margin_m"),
        terrain.get("clearance_margin_m"),
    )
    terrain_target = _number(
        terrain.get("terrain_clearance_target_m"),
        terrain.get("target_clearance_m"),
        recovery_policy.get("min_terrain_clearance_m"),
    )
    if (
        terrain_margin is None
        and terrain_clearance is not None
        and terrain_target is not None
    ):
        terrain_margin = terrain_clearance - terrain_target
    temperature_c = _number(
        temperature.get("temperature_c"),
        environment.get("temperature_c"),
        telemetry_snapshot.get("temperature_c"),
    )
    time_to_conflict = _number(
        nearest.get("time_to_conflict_s"),
        conflict.get("time_to_conflict_s"),
        telemetry_snapshot.get("obstacle_time_to_conflict_s"),
    )
    distance_to_home = _number(
        position.get("distance_to_home_m"),
        telemetry_snapshot.get("distance_to_home_m"),
    )
    altitude = _number(
        position.get("altitude_above_home_m"),
        telemetry_snapshot.get("altitude_above_home_m"),
    )
    temperature_model = _temperature_model(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
        temperature_c=temperature_c,
    )
    battery_factor = _number(temperature_model.get("battery_capacity_factor"))
    effective_battery = (
        battery_remaining * battery_factor
        if battery_remaining is not None and battery_factor is not None
        else battery_remaining
        if temperature_c is None
        else None
    )
    battery_threshold = _number(
        recovery_policy.get("battery_return_threshold_percent")
    )
    max_horizontal_speed = _number(
        recovery_policy.get("max_recovery_horizontal_speed_mps")
    )
    worst_wind = max(
        value for value in (wind_speed, wind_gust) if value is not None
    ) if wind_speed is not None or wind_gust is not None else None
    wind_control_margin = (
        max_horizontal_speed - worst_wind
        if max_horizontal_speed is not None and worst_wind is not None
        else None
    )
    obstacle_geometry = _obstacle_geometry(
        obstacle=obstacle,
        nearest=nearest,
        recovery_policy=recovery_policy,
    )
    performance_envelope = _performance_envelope(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
    )
    observed_facts = {
        "battery_remaining_percent": _fact(
            name="battery_remaining_percent",
            value=battery_remaining,
            unit="percent",
            source=battery,
            fallback_source_ref="telemetry_snapshot.battery.remaining_percent",
            cursor=cursor,
            observed_at=observed_at,
        ),
        "wind_speed_mps": _fact(
            name="wind_speed_mps",
            value=wind_speed,
            unit="m/s",
            source=wind,
            fallback_source_ref="telemetry_snapshot.wind.speed_mps",
            cursor=cursor,
            observed_at=observed_at,
        ),
        "wind_gust_mps": _fact(
            name="wind_gust_mps",
            value=wind_gust,
            unit="m/s",
            source=wind,
            fallback_source_ref="telemetry_snapshot.wind.gust_mps",
            cursor=cursor,
            observed_at=observed_at,
            applicable=wind_gust is not None,
        ),
        "terrain_clearance_m": _fact(
            name="terrain_clearance_m",
            value=terrain_clearance,
            unit="m",
            source=terrain,
            fallback_source_ref="telemetry_snapshot.terrain.terrain_clearance_m",
            cursor=cursor,
            observed_at=observed_at,
            frame=str(terrain.get("frame_id") or ""),
        ),
        "local_avoidance_required": _fact(
            name="local_avoidance_required",
            value=(
                conflict.get("local_avoidance_required")
                if "local_avoidance_required" in conflict
                else None
            ),
            unit="boolean",
            source=conflict,
            fallback_source_ref=(
                "telemetry_snapshot.obstacle.conflict_assessment."
                "local_avoidance_required"
            ),
            cursor=cursor,
            observed_at=observed_at,
            frame=str(obstacle.get("frame_id") or ""),
        ),
        "nearest_obstacle_name": _fact(
            name="nearest_obstacle_name",
            value=str(nearest.get("obstacle_name") or "").strip() or None,
            unit="identifier",
            source=nearest,
            fallback_source_ref=(
                "telemetry_snapshot.obstacle.conflict_assessment."
                "nearest_obstacle.obstacle_name"
            ),
            cursor=cursor,
            observed_at=observed_at,
            frame=str(obstacle.get("frame_id") or ""),
        ),
        "obstacle_time_to_conflict_s": _fact(
            name="obstacle_time_to_conflict_s",
            value=time_to_conflict,
            unit="s",
            source=nearest or conflict,
            fallback_source_ref=(
                "telemetry_snapshot.obstacle.conflict_assessment."
                "nearest_obstacle.time_to_conflict_s"
            ),
            cursor=cursor,
            observed_at=observed_at,
            applicable=conflict.get("local_avoidance_required") is True,
        ),
        "safety_hold_observed": _fact(
            name="safety_hold_observed",
            value=bool(
                recovery.get("request_observed") is True
                and str(recovery.get("action") or "").strip().lower()
                == "safety_hold"
                and recovery.get("command_ack_observed") is True
                and str(recovery.get("assist_status") or "").strip().lower()
                == "safety_hold_observed"
                and str(recovery.get("resume_status") or "").strip().lower()
                == "held_awaiting_operator_recovery_approval"
            )
            if recovery
            else None,
            unit="boolean",
            source=recovery,
            fallback_source_ref="telemetry_snapshot.recovery",
            cursor=cursor,
            observed_at=observed_at,
            applicable=bool(recovery),
        ),
        "temperature_c": _fact(
            name="temperature_c",
            value=temperature_c,
            unit="degC",
            source=temperature or environment,
            fallback_source_ref="telemetry_snapshot.temperature.temperature_c",
            cursor=cursor,
            observed_at=observed_at,
            applicable=temperature_c is not None,
        ),
        "payload_mass_kg": _fact(
            name="payload_mass_kg",
            value=_number(payload.get("mass_kg")),
            unit="kg",
            source=payload,
            fallback_source_ref="telemetry_snapshot.payload.mass_kg",
            cursor=cursor,
            observed_at=observed_at,
            applicable=bool(payload),
        ),
        "payload_requested_mass_kg": _fact(
            name="payload_requested_mass_kg",
            value=_number(payload.get("requested_mass_kg")),
            unit="kg",
            source=payload,
            fallback_source_ref=(
                "telemetry_snapshot.payload.requested_mass_kg"
            ),
            cursor=cursor,
            observed_at=observed_at,
            applicable="requested_mass_kg" in payload,
        ),
        "distance_to_home_m": _fact(
            name="distance_to_home_m",
            value=distance_to_home,
            unit="m",
            source=position,
            fallback_source_ref="telemetry_snapshot.position.distance_to_home_m",
            cursor=cursor,
            observed_at=observed_at,
        ),
        "local_x_m": _fact(
            name="local_x_m",
            value=_number(position.get("local_x_m")),
            unit="m",
            source=position,
            fallback_source_ref="telemetry_snapshot.position.local_x_m",
            cursor=cursor,
            observed_at=observed_at,
            frame=str(position.get("frame_id") or "local_ned"),
        ),
        "local_y_m": _fact(
            name="local_y_m",
            value=_number(position.get("local_y_m")),
            unit="m",
            source=position,
            fallback_source_ref="telemetry_snapshot.position.local_y_m",
            cursor=cursor,
            observed_at=observed_at,
            frame=str(position.get("frame_id") or "local_ned"),
        ),
        "altitude_above_home_m": _fact(
            name="altitude_above_home_m",
            value=altitude,
            unit="m",
            source=position,
            fallback_source_ref=(
                "telemetry_snapshot.position.altitude_above_home_m"
            ),
            cursor=cursor,
            observed_at=observed_at,
            frame=str(position.get("frame_id") or "local_ned_xy_altitude_up"),
        ),
        "landing_zone_safe": _fact(
            name="landing_zone_safe",
            value=(
                landing.get("safe")
                if "safe" in landing
                else telemetry_snapshot.get("landing_zone_safe")
            ),
            unit="boolean",
            source=landing,
            fallback_source_ref="telemetry_snapshot.landing_zone.safe",
            cursor=cursor,
            observed_at=observed_at,
            applicable=bool(landing)
            or "landing_zone_safe" in telemetry_snapshot,
        ),
    }
    derived_facts = {
        "battery_return_margin_percent": {
            "fact_status": (
                "derived"
                if effective_battery is not None and battery_threshold is not None
                else "unverified"
            ),
            "value": (
                round(effective_battery - battery_threshold, 3)
                if effective_battery is not None and battery_threshold is not None
                else None
            ),
            "unit": "percentage_points",
            "source_refs": [
                "observed_facts.battery_remaining_percent",
                "temperature_model.battery_capacity_factor",
                "policy_snapshot.battery_return_threshold_percent",
            ],
            "model_ref": temperature_model.get("model_id"),
        },
        "wind_control_margin_mps": {
            "fact_status": (
                "derived" if wind_control_margin is not None else "unverified"
            ),
            "value": (
                round(wind_control_margin, 3)
                if wind_control_margin is not None
                else None
            ),
            "unit": "m/s",
            "source_refs": [
                "observed_facts.wind_speed_mps",
                "observed_facts.wind_gust_mps",
                "policy_snapshot.max_recovery_horizontal_speed_mps",
            ],
            "model_ref": "conservative_worst_observed_wind.v1",
        },
        "terrain_clearance_margin_m": {
            "fact_status": "derived" if terrain_margin is not None else "unverified",
            "value": round(terrain_margin, 3) if terrain_margin is not None else None,
            "unit": "m",
            "source_refs": [
                "observed_facts.terrain_clearance_m",
                "policy_snapshot.min_terrain_clearance_m",
            ],
            "model_ref": "clearance_minus_policy_minimum.v1",
        },
        "obstacle_time_to_conflict_s": {
            "fact_status": (
                "derived" if time_to_conflict is not None else "unverified"
            ),
            "value": round(time_to_conflict, 3) if time_to_conflict is not None else None,
            "unit": "s",
            "source_refs": [
                "observed_facts.obstacle_time_to_conflict_s",
            ],
            "model_ref": str(
                conflict.get("time_to_conflict_model_ref") or ""
            )
            or None,
        },
        "effective_battery_remaining_percent": {
            "fact_status": (
                "derived" if effective_battery is not None else "unverified"
            ),
            "value": round(effective_battery, 3) if effective_battery is not None else None,
            "unit": "percent",
            "source_refs": [
                "observed_facts.battery_remaining_percent",
                "temperature_model.battery_capacity_factor",
            ],
            "model_ref": temperature_model.get("model_id"),
        },
        "motor_thrust_factor": {
            "fact_status": (
                "derived"
                if temperature_model.get("motor_thrust_factor") is not None
                else "not_applicable"
                if temperature_c is None
                else "unverified"
            ),
            "value": temperature_model.get("motor_thrust_factor"),
            "unit": "ratio",
            "source_refs": list(temperature_model.get("source_refs") or []),
            "model_ref": temperature_model.get("model_id"),
        },
    }

    freshness_reasons: list[str] = []
    if cursor.get("cursor_status") != "complete":
        freshness_reasons.append("hazard_state_telemetry_cursor_incomplete")
    if telemetry.get("stale") is True:
        freshness_reasons.append("hazard_state_telemetry_stale")
    if telemetry.get("dropout") is True:
        freshness_reasons.append("hazard_state_telemetry_dropout")
    prior_cursor = dict(prior_telemetry_cursor or {})
    if prior_cursor:
        current_index = cursor.get("sample_index")
        current_elapsed = cursor.get("elapsed_seconds")
        prior_index = prior_cursor.get("sample_index")
        prior_elapsed = prior_cursor.get("elapsed_seconds")
        if None in {current_index, current_elapsed, prior_index, prior_elapsed}:
            freshness_reasons.append("hazard_state_prior_cursor_incomparable")
        elif int(current_index) < int(prior_index) or float(current_elapsed) < float(
            prior_elapsed
        ):
            freshness_reasons.append("hazard_state_telemetry_cursor_regression")
    if not policy_ref:
        freshness_reasons.append("hazard_state_policy_ref_missing")
    if expected_policy_sha256 and expected_policy_sha256 != policy_sha256:
        freshness_reasons.append("hazard_state_policy_drift")

    state_material = {
        "telemetry_cursor": cursor,
        "policy_sha256": policy_sha256,
        "observed_facts": observed_facts,
        "derived_facts": derived_facts,
        "temperature_model": temperature_model,
        "obstacle_geometry": obstacle_geometry,
        "performance_envelope": performance_envelope,
    }
    state_sha256 = _canonical_sha256(state_material)
    return {
        "schema_version": HAZARD_STATE_SCHEMA_VERSION,
        "hazard_state_id": f"hazard_state_{state_sha256[:12]}",
        "hazard_state_sha256": state_sha256,
        "hazard_state_status": (
            "verified" if not freshness_reasons else "unverified"
        ),
        "source": str(telemetry_snapshot.get("source") or "telemetry_snapshot"),
        "observed_at": observed_at or None,
        "telemetry_cursor": cursor,
        "freshness": {
            "freshness_status": (
                "verified" if not freshness_reasons else "unverified"
            ),
            "blocking_reasons": freshness_reasons,
        },
        "policy_ref": policy_ref or None,
        "policy_sha256": policy_sha256,
        "policy_snapshot": policy_snapshot,
        "observed_facts": observed_facts,
        "derived_facts": derived_facts,
        "temperature_model": temperature_model,
        "obstacle_geometry": obstacle_geometry,
        "performance_envelope": performance_envelope,
        "model_assumptions": [
            "temperature_derating_applies_only_when_source_backed_model_is_complete",
            "missing_or_incomparable_evidence_is_not_treated_as_safe",
            "auto_cruise_speed_is_not_offboard_recovery_performance",
        ],
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }


__all__ = [
    "HAZARD_STATE_SCHEMA_VERSION",
    "PERFORMANCE_ENVELOPE_SCHEMA_VERSION",
    "TEMPERATURE_MODEL_SCHEMA_VERSION",
    "build_runtime_recovery_hazard_state",
    "hazard_state_hash_matches",
    "recovery_policy_sha256",
    "recovery_policy_snapshot",
]
