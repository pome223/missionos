"""Validate a simulator Nav2 planning witness, not a collision-free guarantee."""

import hashlib
import json
import math


def validate_detour_plan(plan, waypoints, now_sim_s):
    if (
        plan.get("status") != "succeeded"
        or plan.get("frame_id") != "map"
        or plan.get("waypoints") != waypoints
        or not 0 <= now_sim_s - plan.get("computed_at_sim_s", float("-inf")) < 2
    ):
        raise ValueError("fresh_matching_Nav2_plan_required")
    points = plan.get("path_xy_m", [])
    if len(points) < 2 or any(
        not isinstance(p, list)
        or len(p) != 2
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in p)
        or abs(p[0]) > 1.5
        or abs(p[1]) > 1.0
        for p in points
    ):
        raise ValueError("Nav2_plan_outside_bounded_scene")
    if any(min(math.dist(p, goal) for p in points) > 0.3 for goal in waypoints):
        raise ValueError("Nav2_plan_does_not_cover_goals")
    ref = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {
        "source": "actual_Nav2_ComputePathThroughPoses",
        "plan_ref": ref,
        "computed_at_sim_s": plan["computed_at_sim_s"],
        "waypoints": waypoints,
        "path_points": len(points),
        "bounded_route_computed": True,
        "dispatch_requires_fresh_replanning": True,
        "collision_free_claimed": False,
    }
