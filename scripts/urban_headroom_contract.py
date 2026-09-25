"""Frozen CPU-only development cohort; scene truth is not a planner input."""

from __future__ import annotations

import copy

FAMILIES = ("gap", "climb", "detour")
EAST_OFFSETS_M = (-0.45, -0.15, 0.15, 0.45)
CASES = tuple(f"headroom_{family}_{i}" for family in FAMILIES for i in range(4))
PROTOCOL = {
    "schema_version": "urban_headroom_protocol.v1",
    "cases": list(CASES),
    "development_cases": 12,
    "minimum_oracle_additional_arrivals": 3,
    "holdout_cases_not_executed": 24,
    "model_calls_permitted": 0,
    "static_world_only": True,
    "selector": "history_depth",
    "comparators": ["stale_map", "latest_depth", "history_depth"],
    "geometry_only_comparator_is_diagnostic": True,
    "unknown_space_is_not_certified_free": True,
    "depth_pixel_stride": 4,
    "occupied_voxel_m": 0.15,
    "surface_discretization_margin_m": 0.13,
    "history_frames": 16,
    "input_maximum_age_wall_s": 60,
    "initial_position_tolerance_m": 0.15,
    "initial_yaw_tolerance_rad": 0.03,
    "initial_speed_limit_m_s": 0.1,
    "observation_translation_drift_m": 0.25,
    "observation_yaw_drift_rad": 0.1,
    "route_deadline_sim_s": 90,
    "route_deadline_wall_s": 450,
    "infrastructure_failures": "retain_and_stop_no_cohort_replacement",
    "futility_rule": "oracle_arrivals_at_most_12; if verified_baseline_arrivals>=10, gain<3",
    "futility_is_not_complete_candidate_matrix": True,
    "exact_physics_state_cloning_claimed": False,
    "contact_free_from_silent_topics_claimed": False,
}


def case_scene(case_id):
    from scripts.urban_navigation_contract import digest, scene_spec

    if case_id not in CASES:
        raise ValueError("case outside frozen development cohort")
    _, family, index = case_id.split("_")
    scene = copy.deepcopy(scene_spec(family))
    count = 2 if family == "gap" else 1
    for obstacle in scene["buildings"][:count]:
        for key in ("translation_enu_m", "lower_enu_m", "upper_enu_m"):
            obstacle[key][0] += EAST_OFFSETS_M[int(index)]
    scene.update(
        family=case_id,
        base_family=family,
        selector="history_depth_before_independent_safety_filter",
        protocol_sha256=digest(PROTOCOL),
        omitted_map_entities=[b["name"] for b in scene["buildings"][:count]],
    )
    scene.pop("scene_sha256")
    scene["scene_sha256"] = digest(scene)
    return scene


def planner_geometry(scene):
    """Explicit whitelist: never expose truth, admissible masks, or case identity."""
    omitted = set(scene["omitted_map_entities"])
    return {
        "schema_version": "urban_partial_map_planner_input.v1",
        "routes": copy.deepcopy(scene["routes"]),
        "goal_enu_m": list(scene["goal_enu_m"]),
        "map_boxes": [
            {k: list(b[k]) for k in ("lower_enu_m", "upper_enu_m")}
            for b in scene["buildings"]
            if b["name"] not in omitted
        ],
        "airframe_radius_m": scene["airframe_radius_m"],
        "required_clearance_m": scene["required_clearance_m"],
        "geofence_lower_enu_m": list(scene["geofence_lower_enu_m"]),
        "geofence_upper_enu_m": list(scene["geofence_upper_enu_m"]),
    }
