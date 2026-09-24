"""Small, deterministic 3D scene/route contract for isolated PX4 research.

All coordinates are Gazebo ENU metres. This is scene truth for feasibility and
verification, not learned perception. It uses a conservative enclosing sphere
and mesh AABBs, never a camera-center or endpoint-only clearance claim.
"""

from __future__ import annotations

import hashlib
import json
import math

ASSET_REVISION = "8163eb4b5e7e21985c6591d1c0bfb56468c0093f"
ASSET_FILES = {
    "LICENSE": "776eee6c1a315c1acca92898edb797a9549c71da0e41dcb0eb6864df6301678e",
    "apartment/model.config": "67278bc623a0e133655acbbb7d8bf47d662d27d57425969cea71b9a9635f5cb5",
    "apartment/model.sdf": "b85af2c0c6b143d6960e98821a8d9bff21c843bd1a3cad002747cd16d169d08b",
    "apartment/meshes/apartment.dae": "1bad8646026a29dcef112850278badbf60ade2b2dc479a054d78a72b4715e32b",
    "apartment/materials/textures/apartment_diffuse.jpg": "a013d0272d7437d64a3466ada08a59618aaf875b54fa2b7b10d3be21cf7b3158",
    "apartment/materials/textures/apartment_spec.jpg": "5dff7b507c86b6155b59e0477e34d2d8c86feb01cc114647c2bfa999d84168e1",
}
AIRFRAME_RADIUS_M = 0.6
# All mesh position vertices, COLLADA inch -> metre. No node transform is present
# on Apartment. The renderer and collision shape use the same mesh and scale.
APARTMENT_LOWER = [-398.7645 * 0.0254, -438.3875 * 0.0254, -0.4456792 * 0.0254]
APARTMENT_UPPER = [398.7645 * 0.0254, 438.3875 * 0.0254, 582.4122 * 0.0254]


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def vector(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(type(x) not in (float, int) or not math.isfinite(x) for x in value)
    ):
        raise ValueError("finite three-vector required")
    return list(map(float, value))


def building(name, east, north, scale):
    translation = [east, north, -APARTMENT_LOWER[2] * scale]
    return {
        "name": name,
        "asset": "apartment",
        "scale": scale,
        "translation_enu_m": translation,
        "lower_enu_m": [a * scale + b for a, b in zip(APARTMENT_LOWER, translation)],
        "upper_enu_m": [a * scale + b for a, b in zip(APARTMENT_UPPER, translation)],
    }


def scene_spec(family):
    if family == "gap":
        buildings = [
            building("urban_left", 7.5, 5.4, 0.35),
            building("urban_right", 7.5, -5.4, 0.35),
        ]
        routes = {
            "forward": [[3, 0, 3], [7.5, 0, 3], [12, 0, 3], [14, 0, 3]],
            "left_detour": [[1.5, 11, 3], [14, 11, 3], [14, 0, 3]],
        }
        preferred, goal = "forward", [14, 0, 3]
    elif family == "climb":
        buildings = [
            building("urban_low_block", 7, 0, 0.25),
            building("urban_left", 10, 8, 0.35),
            building("urban_right", 10, -8, 0.35),
        ]
        routes = {
            "forward": [[14, 0, 3]],
            "climb": [[2.5, 0, 3], [2.5, 0, 5.5], [11, 0, 5.5], [14, 0, 3]],
            "left_detour": [[1.5, 3.6, 3], [14, 3.6, 3], [14, 0, 3]],
        }
        preferred, goal = "climb", [14, 0, 3]
    elif family == "detour":
        buildings = [building("urban_block", 8, 0, 0.5)]
        routes = {
            "forward": [[16, 0, 3]],
            "left_detour": [[1.5, 7, 3], [15, 7, 3], [16, 0, 3]],
            "right_detour": [[1.5, -7, 3], [15, -7, 3], [16, 0, 3]],
        }
        preferred, goal = "left_detour", [16, 0, 3]
    else:
        raise ValueError("unknown urban scene family")
    buildings += [
        building("urban_background_a", 26, 4, 0.6),
        building("urban_background_b", 29, -12, 0.65),
    ]
    result = {
        "schema_version": "missionos_urban_scene.v1",
        "family": family,
        "asset_revision": ASSET_REVISION,
        "buildings": buildings,
        "start_enu_m": [0, 0, 3],
        "goal_enu_m": goal,
        "routes": routes,
        "feasibility_route": preferred,
        "geofence_lower_enu_m": [-2, -12, -0.5],
        "geofence_upper_enu_m": [19, 12, 8],
        "airframe_radius_m": AIRFRAME_RADIUS_M,
        "required_clearance_m": 0.25,
        "goal_radius_m": 0.3,
        "goal_dwell_sim_seconds": 1.0,
        "reference_speed_m_s": 1.0,
        "selector": "declared_geometric_feasibility_route",
        "wam_invoked": False,
    }
    result["scene_sha256"] = digest(result)
    return result


def segment_box_clearance(start, end, lower, upper, radius=AIRFRAME_RADIUS_M):
    """Minimum Euclidean segment-to-AABB distance minus enclosing-sphere radius.

    Exact piecewise quadratic minimization; thin boxes cannot fall between samples.
    Negative means envelope overlap, not penetration depth of the actual mesh.
    """
    start, end, lower, upper = map(vector, (start, end, lower, upper))
    if (
        any(a > b for a, b in zip(lower, upper))
        or not math.isfinite(radius)
        or radius < 0
    ):
        raise ValueError("invalid box or radius")
    delta = [b - a for a, b in zip(start, end)]
    cuts = {0.0, 1.0}
    for a, d, lo, hi in zip(start, delta, lower, upper):
        if abs(d) > 1e-12:
            cuts.update(t for t in ((lo - a) / d, (hi - a) / d) if 0 < t < 1)
    cuts = sorted(cuts)

    def distance2(t):
        return sum(
            max(lo - (a + t * d), 0, (a + t * d) - hi) ** 2
            for a, d, lo, hi in zip(start, delta, lower, upper)
        )

    best = min(distance2(t) for t in cuts)
    for lo_t, hi_t in zip(cuts, cuts[1:]):
        midpoint = (lo_t + hi_t) / 2
        linear, quadratic = 0.0, 0.0
        for a, d, lo, hi in zip(start, delta, lower, upper):
            p = a + midpoint * d
            if p < lo or p > hi:
                boundary = lo if p < lo else hi
                linear += d * (a - boundary)
                quadratic += d * d
        if quadratic:
            t = max(lo_t, min(hi_t, -linear / quadratic))
            best = min(best, distance2(t))
    return math.sqrt(best) - radius


def route_check(scene, route, *, start=None):
    start = vector(scene["start_enu_m"] if start is None else start)
    if not isinstance(route, list) or not 1 <= len(route) <= 16:
        raise ValueError("one through sixteen route waypoints required")
    minimum, length = math.inf, 0.0
    for target in route:
        target = vector(target)
        if not all(
            lo <= x <= hi
            for x, lo, hi in zip(
                target, scene["geofence_lower_enu_m"], scene["geofence_upper_enu_m"]
            )
        ):
            raise ValueError("waypoint outside urban geofence")
        for obstacle in scene["buildings"]:
            minimum = min(
                minimum,
                segment_box_clearance(
                    start,
                    target,
                    obstacle["lower_enu_m"],
                    obstacle["upper_enu_m"],
                    scene["airframe_radius_m"],
                ),
            )
        length += math.dist(start, target)
        start = target
    return {
        "admissible": minimum >= scene["required_clearance_m"],
        "minimum_envelope_clearance_m": minimum,
        "route_length_m": length,
        "uses_scene_truth": True,
        "flight_observed": False,
    }
