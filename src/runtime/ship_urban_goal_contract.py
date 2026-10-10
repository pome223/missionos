"""Observed geometry and candidate forecasts for the opt-in CPU goal fixture.

These contracts describe explicit synthetic observations and forecasts. They
do not turn a native model image into a collision prediction or grant dispatch
authority. The caller still binds the complete request/response and consumes a
fresh, independently checked segment permit.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import re

from .ship_urban_world import segment_intersects_box

CONTEXT_SCHEMA = "ship_urban_observed_geometry.v1"
FORECAST_SCHEMA = "ship_urban_goal_forecast.fixture.v1"
MAX_OBSTACLES = 32
OBSERVATION_FIELDS = frozenset(
    {
        "run_id", "sequence", "observed_at_s", "image_observed_at_s", "image_sha256",
        "position_valid", "position_ned_m", "velocity_ned_mps", "phase", "ap_mode",
        "battery_fraction", "goal_context",
    }
)


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _vector(value):
    return isinstance(value, (list, tuple)) and len(value) == 3 and all(map(_finite, value))


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value)


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("goal_invalid_fields:" + label)


def _bounds(lower, upper):
    if not _vector(lower) or not _vector(upper) or any(a >= b for a, b in zip(lower, upper)):
        raise ValueError("goal_invalid_coverage_bounds")


def _inside(point, lower, upper, margin=0):
    return all(
        _finite(x - margin) and _finite(x + margin) and lo <= x - margin <= x + margin <= hi
        for x, lo, hi in zip(point, lower, upper)
    )


def _context(value):
    if isinstance(value, dict) and "goal_context" in value:
        _keys(value, OBSERVATION_FIELDS, "observation")
        value = value["goal_context"]
    _keys(value, {"schema_version", "observed_at_s", "coverage", "obstacles"}, "context")
    if value["schema_version"] != CONTEXT_SCHEMA or not _finite(value["observed_at_s"]):
        raise ValueError("goal_invalid_context_schema_or_time")
    if value["observed_at_s"] < 0:
        raise ValueError("goal_invalid_context_time")
    coverage = value["coverage"]
    _keys(coverage, {"lower_ned_m", "upper_ned_m"}, "coverage")
    lower, upper = coverage["lower_ned_m"], coverage["upper_ned_m"]
    _bounds(lower, upper)
    obstacles = value["obstacles"]
    if not isinstance(obstacles, list) or len(obstacles) > MAX_OBSTACLES:
        raise ValueError("goal_invalid_obstacle_count")
    seen = set()
    for box in obstacles:
        _keys(box, {"id", "center_ned_m", "half_size_m"}, "obstacle")
        identity, center, half = box["id"], box["center_ned_m"], box["half_size_m"]
        if not _identifier(identity) or identity in seen:
            raise ValueError("goal_invalid_obstacle_id")
        seen.add(identity)
        if not _vector(center) or not _vector(half) or any(h <= 0 for h in half):
            raise ValueError("goal_invalid_obstacle_geometry")
        if any(
            not _finite(c - h) or not _finite(c + h) or not lo <= c - h < c + h <= hi
            for c, h, lo, hi in zip(center, half, lower, upper)
        ):
            raise ValueError("goal_obstacle_outside_observed_coverage")
    return value


def validate_goal_observation(
    observation, *, now_s, max_age_s, lower_ned_m, upper_ned_m
):
    """Reject hidden scenario fields and require fresh, corridor-wide coverage."""
    _keys(observation, OBSERVATION_FIELDS, "observation")
    _bounds(lower_ned_m, upper_ned_m)
    if not _finite(now_s) or now_s < 0 or not _finite(max_age_s) or max_age_s <= 0:
        raise ValueError("goal_invalid_freshness_limit")
    context = _context(observation)
    observed = observation["observed_at_s"]
    image_time = observation["image_observed_at_s"]
    context_time = context["observed_at_s"]
    if (
        not _finite(observed)
        or not _finite(image_time)
        or observed < 0
        or image_time < 0
        or not 0 <= now_s - observed <= max_age_s
        or not 0 <= now_s - image_time <= max_age_s
        or not 0 <= now_s - context_time <= max_age_s
        or image_time > observed
        or context_time > observed
    ):
        raise ValueError("goal_stale_or_future_observation")
    if (
        not _identifier(observation["run_id"])
        or type(observation["sequence"]) is not int
        or observation["sequence"] <= 0
        or observation["position_valid"] is not True
        or not _vector(observation["position_ned_m"])
        or not _vector(observation["velocity_ned_mps"])
        or not _inside(observation["position_ned_m"], lower_ned_m, upper_ned_m)
        or observation["phase"] != "urban"
        or not isinstance(observation["ap_mode"], str)
        or observation["ap_mode"] not in {"hold", "mission", "return"}
        or not _finite(observation["battery_fraction"])
        or not 0 <= observation["battery_fraction"] <= 1
        or not isinstance(observation["image_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", observation["image_sha256"]) is None
    ):
        raise ValueError("goal_invalid_observed_sensor_fields")
    coverage = context["coverage"]
    if any(
        covered_lo > required_lo or covered_hi < required_hi
        for covered_lo, covered_hi, required_lo, required_hi in zip(
            coverage["lower_ned_m"], coverage["upper_ned_m"], lower_ned_m, upper_ned_m
        )
    ):
        raise ValueError("goal_incomplete_corridor_coverage")
    return context


def context_digest(observation_or_context):
    """Bind observed boxes and coverage; timestamp refresh and list order are immaterial."""
    context = _context(observation_or_context)
    return digest(
        {
            "schema_version": context["schema_version"],
            "coverage": context["coverage"],
            "obstacles": sorted(context["obstacles"], key=lambda box: box["id"]),
        }
    )


def segment_clearance(start, target, context, clearance_m):
    """Check a swept axis-aligned clearance envelope against observed boxes.

    Tangency blocks. Convex coverage contains the entire swept segment when
    both expanded endpoints are inside it. This is fixture geometry, not a
    claim about unobserved objects or native model perception.
    """
    context = _context(context)
    if not _vector(start) or not _vector(target) or not _finite(clearance_m) or clearance_m <= 0:
        raise ValueError("goal_invalid_clearance_segment")
    coverage = context["coverage"]
    if not all(
        _inside(point, coverage["lower_ned_m"], coverage["upper_ned_m"], clearance_m)
        for point in (start, target)
    ):
        raise ValueError("goal_segment_outside_observed_coverage")
    blocked = []
    for box in context["obstacles"]:
        relative_start = [a - c for a, c in zip(start, box["center_ned_m"])]
        relative_target = [b - c for b, c in zip(target, box["center_ned_m"])]
        expanded = [h + clearance_m for h in box["half_size_m"]]
        if not all(_vector(v) for v in (relative_start, relative_target, expanded)):
            raise ValueError("goal_unrepresentable_clearance_geometry")
        if any(not _finite(b - a) for a, b in zip(relative_start, relative_target)):
            raise ValueError("goal_unrepresentable_clearance_geometry")
        if segment_intersects_box(relative_start, relative_target, expanded):
            blocked.append(box["id"])
    return {"allowed": not blocked, "blocking_obstacle_ids": sorted(blocked)}


def validate_goal_forecast(vla, wam, observation):
    """Require one explicit fixture forecast for every exact VLA candidate.

    The runtime separately verifies the full response's request/session hash.
    A passing forecast is only a proposal: independent Rules must still check
    fresh observed geometry immediately before any dispatch.
    """
    if not isinstance(vla, dict) or not isinstance(wam, dict):
        raise ValueError("goal_invalid_model_response")
    candidates = vla.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4:
        raise ValueError("goal_invalid_candidate_count")
    by_id = {}
    for candidate in candidates:
        _keys(candidate, {"id", "target_ned_m"}, "candidate")
        identity = candidate["id"]
        if not _identifier(identity) or identity in by_id or not _vector(candidate["target_ned_m"]):
            raise ValueError("goal_invalid_candidate")
        by_id[identity] = candidate
    if (
        vla.get("fixture") is not True
        or vla.get("dispatch_allowed") is not False
        or wam.get("fixture") is not True
        or wam.get("dispatch_allowed") is not False
        or wam.get("forecast_schema") != FORECAST_SCHEMA
        or wam.get("proposal_sha256") != digest(vla)
        or wam.get("observation_context_sha256") != context_digest(observation)
    ):
        raise ValueError("goal_unbound_fixture_forecast")
    forecasts = wam.get("forecasts")
    if not isinstance(forecasts, list) or len(forecasts) != len(candidates):
        raise ValueError("goal_incomplete_forecast_set")
    by_forecast = {}
    for forecast in forecasts:
        _keys(forecast, {"candidate_id", "candidate_sha256", "predicted_clear"}, "forecast")
        identity = forecast["candidate_id"]
        if (
            not _identifier(identity)
            or identity not in by_id
            or identity in by_forecast
            or forecast["candidate_sha256"] != digest(by_id[identity])
            or type(forecast["predicted_clear"]) is not bool
        ):
            raise ValueError("goal_invalid_candidate_forecast")
        by_forecast[identity] = forecast
    selected = wam.get("selected_candidate_id")
    if not _identifier(selected) or selected not in by_forecast:
        raise ValueError("goal_no_selected_forecast")
    if by_forecast[selected]["predicted_clear"] is not True:
        raise ValueError("goal_selected_forecast_blocked")
    return [by_forecast[candidate["id"]] for candidate in candidates]
