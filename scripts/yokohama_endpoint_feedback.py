"""Opt-in two-segment native feedback bounds; no model or execution authority."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

SCHEMA = "yokohama_native_endpoint_feedback.v1"
FIXED = {
    "schema_version": SCHEMA,
    "entry_phase": "00-D1",
    "exit_phase": "01-FEEDBACK-EXIT",
    "corridor_half_width_m": 0.5,
    "tracking_tube_m": 1.0,
    "clearance_m": 2.0,
    "goal_tolerance_m": 0.5,
    "minimum_progress_m": 0.1,
    "max_segment_m": 3.0,
    "max_cycles": 2,
    "max_vla_requests": 2,
    "max_wam_requests": 2,
    "total_timeout_s": 600,
}
KEYS = set(FIXED) | {
    "entry_world_xyz_m", "goal_world_xyz_m", "exit_world_xyz_m", "map_sha256",
}


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def vector(value):
    return isinstance(value, (list, tuple)) and len(value) == 3 and all(map(finite, value))


def feedback_policy(config):
    """Validate the entire narrow policy, or preserve the legacy path if absent."""
    if not isinstance(config, dict) or not isinstance(config.get("decisions", {}), dict):
        raise ValueError("Invalid feedback configuration")
    if config.get("delivery_trial") is not None:
        if __package__:
            from .yokohama_delivery_contract import validate_config
        else:
            from yokohama_delivery_contract import validate_config
        return validate_config(config)
    decisions = config.get("decisions", {})
    if decisions.get("goal_distance_adapter") is not None:
        if __package__:
            from .yokohama_goal_distance_adapter import POLICY
        else:
            from yokohama_goal_distance_adapter import POLICY
        if decisions["goal_distance_adapter"] != POLICY:
            raise ValueError("Unsupported explicit vehicle distance adapter")
        if "endpoint_feedback" not in decisions:
            raise ValueError("Vehicle distance adapter requires the bounded endpoint policy")
        if config.get("candidate_recovery") is not None:
            if __package__:
                from .yokohama_native_endpoint_contract import validate_config
            else:
                from yokohama_native_endpoint_contract import validate_config
            validate_config(config)
    if "endpoint_feedback" not in decisions:
        return None
    policy = decisions["endpoint_feedback"]
    if not isinstance(policy, dict) or set(policy) != KEYS:
        raise ValueError("Invalid endpoint feedback policy fields")
    if any(
        policy[k] != expected
        or (isinstance(expected, (int, float)) and not finite(policy[k]))
        or (type(expected) is int and type(policy[k]) is not int)
        for k, expected in FIXED.items()
    ):
        raise ValueError("Unsupported endpoint feedback limits")
    entry, goal, exit_point = (policy[k] for k in (
        "entry_world_xyz_m", "goal_world_xyz_m", "exit_world_xyz_m",
    ))
    if (
        not all(vector(p) for p in (entry, goal, exit_point))
        or not math.isclose(math.dist(entry, goal), 4.0, abs_tol=1e-8)
        or abs(goal[2] - entry[2]) > 1e-8 or entry[2] <= 0
        or list(exit_point) != list(entry)
    ):
        raise ValueError("Feedback requires a fixed four-metre inland goal and return")
    bound = policy["map_sha256"]
    world = config.get("world", {})
    if (
        not isinstance(bound, str) or len(bound) != 64
        or any(c not in "0123456789abcdef" for c in bound)
        or not isinstance(world, dict)
        or not isinstance(world.get("source_sha256", {}), dict)
        or not isinstance(world.get("world_sha256"), str)
        or len(world["world_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in world["world_sha256"])
        or world.get("source_sha256", {}).get(
            "collision-footprints.geojson"
        ) != bound
    ):
        raise ValueError("Feedback map is not frozen")
    stages = config.get("flight_stages", [])
    if (
        not isinstance(stages, list) or not all(isinstance(s, dict) for s in stages)
        or [s.get("name") for s in stages] != [policy["entry_phase"], policy["exit_phase"]]
        or stages[0].get("target_world_xyz_m") != list(entry)
        or stages[1].get("target_world_xyz_m") != list(exit_point)
    ):
        raise ValueError("Feedback requires exactly the approved entry and exit")
    if (
        decisions.get("backend") not in {"fixture", "native"}
        or decisions.get("wam_profile") != "motion-v4"
        or decisions.get("points") != ["D1"]
        or decisions.get("sea_leg_present") is not False
        or decisions.get("payload_release_present") is not False
        or any(decisions.get(k) for k in (
            "hold_recovery", "pad_approach", "capture_paired_views",
        ))
        or config.get("world", {}).get("sea_extension")
        or config.get("world", {}).get("wind")
        or any(world.get(k) for k in ("payload_delivery", "pad_queue", "goal_plan", "dynamic_actors", "actors"))
        or config.get("motion_capture") or config.get("wind_mps")
    ):
        raise ValueError("Feedback admits only the stationary inland experiment")
    for key, limit in (("startup_timeout_s", 300), ("shutdown_timeout_s", 65), ("inference_timeout_s", 75)):
        value = decisions.get(key, limit)
        if not finite(value) or not 0 < value <= limit:
            raise ValueError("Unbounded feedback lifecycle/response deadline")
    return policy


def corridor_distance(policy, point):
    a, b = policy["entry_world_xyz_m"], policy["goal_world_xyz_m"]
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy)
                       / (dx * dx + dy * dy)))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def validate_feedback_map(config, bundle):
    policy = feedback_policy(config)
    if policy is None:
        raise ValueError("Feedback map check requires explicit policy")
    path = Path(bundle) / "collision-footprints.geojson"
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != policy["map_sha256"]:
        raise ValueError("Feedback collision map changed")


def validate_feedback_observation(config, row, *, held=False):
    policy = feedback_policy(config)
    if policy is None:
        raise ValueError("Feedback observation requires explicit policy")
    if not isinstance(row, dict) or not isinstance(row.get("vehicle"), dict):
        raise ValueError("Malformed feedback observation")
    vehicle = row["vehicle"]
    point, velocity = vehicle.get("xyz"), row.get("velocity_ned")
    quat = vehicle.get("quat_wxyz")
    resets = row.get("reset_counters")
    scalars = [row.get(k) for k in ("sim_s", "wall_s", "heading_ned_rad", "battery_fraction")]
    if (
        not vector(point) or not vector(velocity) or not all(map(finite, scalars))
        or not isinstance(quat, (list, tuple)) or len(quat) != 4
        or not all(map(finite, quat)) or abs(sum(q * q for q in quat) - 1) > 1e-3
        or not finite(vehicle.get("age_s")) or not 0 <= vehicle["age_s"] <= 2
        or row["sim_s"] < 0 or row["wall_s"] < 0
        or row.get("run_id") != config["run_id"]
        or row.get("world_sha256") != config["world"]["world_sha256"]
        or not 0.2 <= row["battery_fraction"] <= 1
        or row.get("position_valid") is not True
        or row.get("arming_state") != 2 or row.get("landed") is not False
        or not isinstance(resets, (list, tuple)) or len(resets) != 3
        or any(not finite(v) or not 0 <= v <= 255 or int(v) != v for v in resets)
        or abs(point[2] - policy["entry_world_xyz_m"][2]) > 0.5
        or corridor_distance(policy, point) > policy["corridor_half_width_m"] + policy["tracking_tube_m"]
        or (held and (row.get("nav_state") != 4 or math.hypot(*velocity) > 0.3))
    ):
        raise ValueError("Feedback observation lost its bounded corridor/state")
    return row


def validate_feedback_candidate(config, start, target, origin=None):
    policy = feedback_policy(config)
    if policy is None:
        raise ValueError("Feedback candidate requires explicit policy")
    original = start if origin is None else origin
    if not all(vector(p) for p in (start, target, original)):
        raise ValueError("Invalid feedback candidate coordinates")
    before = math.dist(original, policy["goal_world_xyz_m"])
    actual_before = math.dist(start, policy["goal_world_xyz_m"])
    after = math.dist(target, policy["goal_world_xyz_m"])
    if (
        not 0.5 <= math.dist(original, target) <= policy["max_segment_m"]
        or not 0.5 <= math.dist(start, target) <= policy["max_segment_m"]
        or abs(target[2] - original[2]) > 0.205
        or abs(target[2] - policy["entry_world_xyz_m"][2]) > 0.205
        or corridor_distance(policy, target) > policy["corridor_half_width_m"]
        or corridor_distance(policy, start) > policy["corridor_half_width_m"] + policy["tracking_tube_m"]
        or before - after < policy["minimum_progress_m"]
        or actual_before - after < policy["minimum_progress_m"]
    ):
        raise ValueError("Feedback candidate violates goal progress or corridor")
    return dict(
        allowed=True, policy_sha256=digest(policy), goal_distance_before_m=before,
        actual_goal_distance_before_m=actual_before,
        goal_distance_after_m=after, minimum_progress_m=policy["minimum_progress_m"],
        candidate_corridor_half_width_m=policy["corridor_half_width_m"],
    )


def validate_feedback_request(config, message):
    policy = feedback_policy(config)
    if policy is None:
        raise ValueError("Feedback request requires explicit policy")
    operation = message.get("operation")
    if operation == "stop":
        return
    cycle = message.get("cycle")
    row = message.get("observation", {})
    validate_feedback_observation(config, row, held=True)
    if (
        type(cycle) is not int or cycle not in (1, 2)
        or operation not in {"start", "vla", "wam", "authorize", "activate"}
        or (operation == "start" and cycle != 1)
        or row.get("phase") != policy["entry_phase"]
        or message.get("attempt", 0) != 0
        or message.get("run_id", config["run_id"]) != config["run_id"]
        or message.get("config_sha256", digest(config)) != digest(config)
        or ("next_target_world_xyz_m" in message
            and message["next_target_world_xyz_m"] != policy["goal_world_xyz_m"])
    ):
        raise ValueError("Unapproved feedback cycle/goal/phase")
    previous = message.get("previous_segment")
    if cycle == 1:
        if previous is not None or math.dist(row["vehicle"]["xyz"], policy["entry_world_xyz_m"]) > 0.25:
            raise ValueError("First feedback request is outside the approved entry hold")
        return
    if not isinstance(previous, dict) or set(previous) != {
        "permit", "arrival", "stable_since_sim_s", "stable_samples",
    }:
        raise ValueError("Second feedback request needs the observed prior segment")
    permit, arrival = previous["permit"], previous["arrival"]
    if not isinstance(permit, dict) or not isinstance(arrival, dict):
        raise ValueError("Malformed prior feedback evidence")
    validate_feedback_observation(config, arrival, held=True)
    target = permit.get("candidate", {}).get("target_world_xyz_m")
    since, count = previous["stable_since_sim_s"], previous["stable_samples"]
    if (
        not vector(target) or permit.get("cycle") != 1
        or permit.get("run_id") != config["run_id"]
        or permit.get("config_sha256") != digest(config)
        or not finite(since) or since < 0 or arrival["sim_s"] - since < 2
        or type(count) is not int or count < 3
        or arrival.get("phase") != policy["entry_phase"]
        or math.dist(arrival["vehicle"]["xyz"], target) > 0.25
        or row["sim_s"] < arrival["sim_s"] or row["wall_s"] < arrival["wall_s"]
        or math.dist(row["vehicle"]["xyz"], arrival["vehicle"]["xyz"]) > 0.5
        or row["reset_counters"] != arrival["reset_counters"]
        or abs(math.remainder(row["heading_ned_rad"] - arrival["heading_ned_rad"], 2 * math.pi)) > 0.03
    ):
        raise ValueError("Second feedback request lacks a stable bound endpoint")


def goal_reached(config, row):
    policy = feedback_policy(config)
    validate_feedback_observation(config, row, held=True)
    return math.dist(row["vehicle"]["xyz"], policy["goal_world_xyz_m"]) <= policy["goal_tolerance_m"]
