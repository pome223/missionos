"""Deterministic telemetry window summaries for runtime recovery.

This module deliberately returns numeric facts only. Situation labels such as
``worsening`` or action choices such as ``return_to_launch`` belong to the LLM
advisory layer; hard threshold booleans and soft non-nominal booleans remain
deterministic guardrail facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


RECOVERY_WINDOW_SUMMARY_SCHEMA_VERSION = "missionos_recovery_window_summary.v1"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(sample: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(sample.get(key))
        if value is not None:
            return value
    return None


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def _minmax(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return min(values), max(values)


def _delta(first: float | None, last: float | None) -> float | None:
    if first is None or last is None:
        return None
    return last - first


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "stale", "lost"}
    if isinstance(value, int | float):
        return bool(value)
    return False


def _telemetry_stale(sample: Mapping[str, Any]) -> bool:
    if sample.get("heartbeat_observed") is False:
        return True
    if _truthy(sample.get("telemetry_stale")):
        return True
    telemetry = sample.get("telemetry")
    if isinstance(telemetry, Mapping) and _truthy(telemetry.get("stale")):
        return True
    return False


def _obstacle_or_building_risk(sample: Mapping[str, Any]) -> bool:
    obstacle = sample.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    manifest = obstacle.get("obstacle_manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    top_level_manifest = sample.get("obstacle_manifest")
    top_level_manifest = (
        top_level_manifest if isinstance(top_level_manifest, Mapping) else {}
    )
    projection_status = str(obstacle.get("projection_status") or "").strip().lower()
    source_backed = projection_status == "source_backed" or bool(
        manifest or top_level_manifest
    )
    detected = any(
        _truthy(value)
        for value in (
            sample.get("obstacle_or_building_risk"),
            sample.get("obstacle_detected"),
            sample.get("building_risk_detected"),
            sample.get("landing_zone_blocked"),
            sample.get("gazebo_obstacle_model_spawned"),
            obstacle.get("obstacle_detected"),
            obstacle.get("building_risk_detected"),
            obstacle.get("landing_zone_blocked"),
            obstacle.get("gazebo_obstacle_model_spawned"),
            manifest.get("building_risk_detected"),
            manifest.get("landing_zone_blocked"),
            manifest.get("gazebo_obstacle_model_spawned"),
            top_level_manifest.get("building_risk_detected"),
            top_level_manifest.get("landing_zone_blocked"),
            top_level_manifest.get("gazebo_obstacle_model_spawned"),
        )
    )
    return source_backed and detected


def _elapsed_seconds(sample: Mapping[str, Any], fallback: int) -> float | None:
    value = _first_number(
        sample,
        "elapsed_seconds",
        "elapsed_s",
        "sample_time_s",
        "timestamp_s",
        "t",
    )
    if value is not None:
        return value
    return _first_number(sample, "sample_index", "index") or float(fallback)


def _sample_index(sample: Mapping[str, Any]) -> int | None:
    value = _first_number(sample, "sample_index", "index")
    if value is None:
        return None
    return int(value)


def _terrain_clearance(sample: Mapping[str, Any]) -> float | None:
    terrain = sample.get("terrain")
    if isinstance(terrain, Mapping):
        value = _first_number(terrain, "terrain_clearance_m", "clearance_m")
        if value is not None:
            return value
    return _first_number(
        sample,
        "terrain_clearance_m",
        "terrain_clearance_agl_m",
        "clearance_m",
    )


def _terrain_margin(sample: Mapping[str, Any]) -> float | None:
    terrain = sample.get("terrain")
    if isinstance(terrain, Mapping):
        value = _first_number(terrain, "terrain_clearance_margin_m", "clearance_margin_m")
        if value is not None:
            return value
    return _first_number(
        sample,
        "terrain_clearance_margin_m",
        "clearance_margin_m",
    )


def _battery_percent(sample: Mapping[str, Any]) -> float | None:
    battery = sample.get("battery")
    if isinstance(battery, Mapping):
        value = _first_number(battery, "remaining_percent", "battery_remaining_percent")
        if value is not None:
            return value
    return _first_number(
        sample,
        "battery_remaining_percent",
        "remaining_percent",
        "battery_percent",
    )


def _distance_to_home(sample: Mapping[str, Any]) -> float | None:
    position = sample.get("position")
    if isinstance(position, Mapping):
        value = _first_number(position, "distance_to_home_m")
        if value is not None:
            return value
    return _first_number(sample, "distance_to_home_m", "home_distance_m")


def _progress(sample: Mapping[str, Any]) -> float | None:
    route = sample.get("route")
    if isinstance(route, Mapping):
        value = _first_number(route, "progress_m")
        if value is not None:
            return value
    return _first_number(sample, "progress_m", "route_progress_m")


def _cross_track(sample: Mapping[str, Any]) -> float | None:
    route = sample.get("route")
    if isinstance(route, Mapping):
        drift = route.get("drift_projection")
        if isinstance(drift, Mapping):
            value = _first_number(drift, "cross_track_m", "deviation_xy_m")
            if value is not None:
                return abs(value)
        value = _first_number(route, "cross_track_m", "deviation_xy_m")
        if value is not None:
            return abs(value)
    value = _first_number(
        sample,
        "cross_track_m",
        "deviation_xy_m",
        "wind_drift_deviation_xy_m",
    )
    if value is None:
        return None
    return abs(value)


def _wind_speed(sample: Mapping[str, Any]) -> float | None:
    wind = sample.get("wind")
    if isinstance(wind, Mapping):
        value = _first_number(wind, "speed_mps", "wind_speed_mps")
        if value is not None:
            return value
    return _first_number(sample, "wind_speed_mps", "weather_wind_speed_mps")


def _nav_state(sample: Mapping[str, Any]) -> str | None:
    value = sample.get("nav_state")
    if value in (None, ""):
        flight_state = sample.get("flight_state")
        if isinstance(flight_state, Mapping):
            value = flight_state.get("nav_state")
    if value in (None, ""):
        return None
    return str(value)


def _bucket_summary(
    bucket_samples: Sequence[Mapping[str, Any]],
    *,
    bucket_start_s: float,
    bucket_end_s: float,
    window_end_s: float,
    min_terrain_clearance_m: float,
    battery_critical_percent: float,
    terrain_soft_margin_m: float,
    cross_track_soft_limit_m: float,
    battery_drop_soft_percent: float,
    wind_soft_limit_mps: float,
) -> dict[str, Any]:
    ordered = list(bucket_samples)
    progresses = [_progress(sample) for sample in ordered]
    progresses = [value for value in progresses if value is not None]
    clearances = [_terrain_clearance(sample) for sample in ordered]
    clearances = [value for value in clearances if value is not None]
    margins = [_terrain_margin(sample) for sample in ordered]
    margins = [value for value in margins if value is not None]
    batteries = [_battery_percent(sample) for sample in ordered]
    batteries = [value for value in batteries if value is not None]
    home_distances = [_distance_to_home(sample) for sample in ordered]
    home_distances = [value for value in home_distances if value is not None]
    cross_tracks = [_cross_track(sample) for sample in ordered]
    cross_tracks = [value for value in cross_tracks if value is not None]
    wind_speeds = [_wind_speed(sample) for sample in ordered]
    wind_speeds = [value for value in wind_speeds if value is not None]
    nav_states = [_nav_state(sample) for sample in ordered]
    nav_states = [value for value in nav_states if value is not None]
    telemetry_stale_count = sum(1 for sample in ordered if _telemetry_stale(sample))
    obstacle_risk_count = sum(
        1 for sample in ordered if _obstacle_or_building_risk(sample)
    )
    sample_indices = [
        index for sample in ordered if (index := _sample_index(sample)) is not None
    ]
    progress_delta = _delta(progresses[0], progresses[-1]) if progresses else None
    battery_delta = _delta(batteries[0], batteries[-1]) if batteries else None
    clearance_min, clearance_max = _minmax(clearances)
    margin_min, margin_max = _minmax(margins)
    battery_min, battery_max = _minmax(batteries)
    home_min, home_max = _minmax(home_distances)
    cross_min, cross_max = _minmax(cross_tracks)
    wind_min, wind_max = _minmax(wind_speeds)
    terrain_breach = any(
        _truthy(sample.get("terrain_clearance_below_minimum")) for sample in ordered
    ) or (
        clearance_min is not None
        and float(clearance_min) < float(min_terrain_clearance_m)
    )
    battery_breach = (
        battery_min is not None and float(battery_min) <= float(battery_critical_percent)
    )
    terrain_near_by_margin = (
        margin_min is not None and 0 <= float(margin_min) <= float(terrain_soft_margin_m)
    )
    terrain_near_by_clearance = (
        margin_min is None
        and clearance_min is not None
        and float(min_terrain_clearance_m)
        <= float(clearance_min)
        <= float(min_terrain_clearance_m) + float(terrain_soft_margin_m)
    )
    terrain_near_minimum = (
        not terrain_breach and (terrain_near_by_margin or terrain_near_by_clearance)
    )
    progress_non_positive = (
        len(progresses) > 1
        and progress_delta is not None
        and float(progress_delta) <= 0
    )
    battery_drop_high = (
        len(batteries) > 1
        and battery_delta is not None
        and float(battery_delta) <= -abs(float(battery_drop_soft_percent))
    )
    nav_state_changed = len(set(nav_states)) > 1
    cross_track_high = (
        cross_max is not None and float(cross_max) >= float(cross_track_soft_limit_m)
    )
    wind_high = wind_max is not None and float(wind_max) >= float(wind_soft_limit_mps)
    soft_signals = {
        "terrain_clearance_near_minimum": terrain_near_minimum,
        "cross_track_above_soft_limit": cross_track_high,
        "progress_non_positive": progress_non_positive,
        "battery_drop_above_soft_limit": battery_drop_high,
        "nav_state_changed": nav_state_changed,
        "wind_speed_above_soft_limit": wind_high,
    }
    return {
        "offset_start_s": _round(bucket_start_s - window_end_s),
        "offset_end_s": _round(bucket_end_s - window_end_s),
        "elapsed_start_s": _round(bucket_start_s),
        "elapsed_end_s": _round(bucket_end_s),
        "sample_count": len(ordered),
        "sample_index_min": min(sample_indices) if sample_indices else None,
        "sample_index_max": max(sample_indices) if sample_indices else None,
        "progress_start_m": _round(progresses[0] if progresses else None),
        "progress_end_m": _round(progresses[-1] if progresses else None),
        "progress_delta_m": _round(progress_delta),
        "terrain_clearance_min_m": _round(clearance_min),
        "terrain_clearance_max_m": _round(clearance_max),
        "terrain_clearance_margin_min_m": _round(margin_min),
        "terrain_clearance_margin_max_m": _round(margin_max),
        "battery_min_percent": _round(battery_min),
        "battery_max_percent": _round(battery_max),
        "battery_delta_percent": _round(battery_delta),
        "distance_to_home_min_m": _round(home_min),
        "distance_to_home_max_m": _round(home_max),
        "cross_track_min_m": _round(cross_min),
        "cross_track_max_m": _round(cross_max),
        "wind_speed_min_mps": _round(wind_min),
        "wind_speed_max_mps": _round(wind_max),
        "nav_state_values": sorted(set(nav_states)),
        "telemetry_stale_count": telemetry_stale_count,
        "obstacle_or_building_risk_count": obstacle_risk_count,
        "hard_breaches": {
            "terrain_clearance_below_minimum": terrain_breach,
            "battery_critical": battery_breach,
            "telemetry_lost": telemetry_stale_count > 0,
            "obstacle_or_building_risk": obstacle_risk_count > 0,
        },
        "soft_signals": soft_signals,
    }


def build_recovery_window_summary(
    samples: Sequence[Mapping[str, Any]],
    *,
    window_s: float = 30.0,
    cadence_s: float = 10.0,
    bucket_s: float = 5.0,
    min_terrain_clearance_m: float = 30.0,
    battery_critical_percent: float = 20.0,
    terrain_soft_margin_m: float = 5.0,
    cross_track_soft_limit_m: float = 25.0,
    battery_drop_soft_percent: float = 5.0,
    wind_soft_limit_mps: float = 6.0,
) -> dict[str, Any]:
    """Summarize recent telemetry into fact-only buckets for recovery advice.

    The function is intentionally pure: it performs no I/O, calls no LLM, and
    returns no situation labels or action recommendations.
    """
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    if cadence_s <= 0:
        raise ValueError("cadence_s must be positive")
    if bucket_s <= 0:
        raise ValueError("bucket_s must be positive")
    normalized: list[tuple[float, int, Mapping[str, Any]]] = []
    for order, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            continue
        elapsed = _elapsed_seconds(sample, order)
        if elapsed is None:
            continue
        normalized.append((float(elapsed), order, sample))
    normalized.sort(key=lambda item: (item[0], item[1]))
    if not normalized:
        return {
            "schema_version": RECOVERY_WINDOW_SUMMARY_SCHEMA_VERSION,
            "source": "missionos_auto_mission_runtime_samples",
            "summary_status": "no_samples",
            "window_s": float(window_s),
            "cadence_s": float(cadence_s),
            "bucket_s": float(bucket_s),
            "sample_count": 0,
            "buckets": [],
            "hard_breaches": {
                "terrain_clearance_below_minimum": False,
                "battery_critical": False,
                "telemetry_lost": False,
                "obstacle_or_building_risk": False,
                "any": False,
            },
            "soft_signals": {
                "terrain_clearance_near_minimum": False,
                "cross_track_above_soft_limit": False,
                "progress_non_positive": False,
                "battery_drop_above_soft_limit": False,
                "nav_state_changed": False,
                "wind_speed_above_soft_limit": False,
                "any": False,
            },
            "latest": {},
        }

    window_end_s = normalized[-1][0]
    window_start_s = window_end_s - float(window_s)
    window_items = [
        item for item in normalized if window_start_s <= item[0] <= window_end_s
    ]
    window_samples = [item[2] for item in window_items]
    buckets: list[dict[str, Any]] = []
    bucket_start = window_start_s
    while bucket_start < window_end_s:
        bucket_end = min(bucket_start + float(bucket_s), window_end_s)
        is_last = bucket_end >= window_end_s
        bucket_samples = [
            sample
            for elapsed, _, sample in window_items
            if bucket_start <= elapsed
            and (elapsed <= bucket_end if is_last else elapsed < bucket_end)
        ]
        buckets.append(
            _bucket_summary(
                bucket_samples,
                bucket_start_s=bucket_start,
                bucket_end_s=bucket_end,
                window_end_s=window_end_s,
                min_terrain_clearance_m=min_terrain_clearance_m,
                battery_critical_percent=battery_critical_percent,
                terrain_soft_margin_m=terrain_soft_margin_m,
                cross_track_soft_limit_m=cross_track_soft_limit_m,
                battery_drop_soft_percent=battery_drop_soft_percent,
                wind_soft_limit_mps=wind_soft_limit_mps,
            )
        )
        bucket_start = bucket_end

    overall = _bucket_summary(
        window_samples,
        bucket_start_s=window_start_s,
        bucket_end_s=window_end_s,
        window_end_s=window_end_s,
        min_terrain_clearance_m=min_terrain_clearance_m,
        battery_critical_percent=battery_critical_percent,
        terrain_soft_margin_m=terrain_soft_margin_m,
        cross_track_soft_limit_m=cross_track_soft_limit_m,
        battery_drop_soft_percent=battery_drop_soft_percent,
        wind_soft_limit_mps=wind_soft_limit_mps,
    )
    hard_breaches = dict(overall["hard_breaches"])
    hard_breaches["any"] = any(hard_breaches.values())
    soft_signals = dict(overall["soft_signals"])
    soft_signals["any"] = any(soft_signals.values())
    latest = window_samples[-1] if window_samples else normalized[-1][2]
    latest_facts = {
        "sample_index": _sample_index(latest),
        "elapsed_seconds": _round(_elapsed_seconds(latest, len(window_samples) - 1)),
        "progress_m": _round(_progress(latest)),
        "local_x_m": _round(_first_number(latest, "local_x_m", "x_m", "x")),
        "local_y_m": _round(_first_number(latest, "local_y_m", "y_m", "y")),
        "local_z_m": _round(_first_number(latest, "local_z_m", "z_m", "z")),
        "altitude_above_home_m": _round(
            _first_number(latest, "altitude_above_home_m", "relative_alt_m")
        ),
        "distance_to_home_m": _round(_distance_to_home(latest)),
        "terrain_clearance_m": _round(_terrain_clearance(latest)),
        "battery_remaining_percent": _round(_battery_percent(latest)),
        "cross_track_m": _round(_cross_track(latest)),
        "wind_speed_mps": _round(_wind_speed(latest)),
        "telemetry_stale": _telemetry_stale(latest),
        "obstacle_or_building_risk": _obstacle_or_building_risk(latest),
        "nav_state": latest.get("nav_state"),
        "arming_state": latest.get("arming_state"),
        "landed": latest.get("landed"),
        "heartbeat_observed": latest.get("heartbeat_observed"),
    }
    return {
        "schema_version": RECOVERY_WINDOW_SUMMARY_SCHEMA_VERSION,
        "source": "missionos_auto_mission_runtime_samples",
        "summary_status": "computed",
        "window_s": float(window_s),
        "cadence_s": float(cadence_s),
        "bucket_s": float(bucket_s),
        "window_start_elapsed_s": _round(window_start_s),
        "window_end_elapsed_s": _round(window_end_s),
        "sample_count": len(window_samples),
        "bucket_count": len(buckets),
        "thresholds": {
            "min_terrain_clearance_m": float(min_terrain_clearance_m),
            "battery_critical_percent": float(battery_critical_percent),
            "terrain_soft_margin_m": float(terrain_soft_margin_m),
            "cross_track_soft_limit_m": float(cross_track_soft_limit_m),
            "battery_drop_soft_percent": float(battery_drop_soft_percent),
            "wind_soft_limit_mps": float(wind_soft_limit_mps),
        },
        "hard_breaches": hard_breaches,
        "soft_signals": soft_signals,
        "overall": overall,
        "latest": latest_facts,
        "buckets": buckets,
    }
