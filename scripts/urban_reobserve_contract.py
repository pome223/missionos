"""Bounded stop/observe/resume development case; no learned-model value claim."""

from __future__ import annotations

import copy
import hmac
import math

from scripts.px4_aerial_flight_session import sign_command, wrap_angle
from scripts.urban_navigation_contract import digest, scene_spec

IMAGE = "sha256:79968fe25aa19d51c49fbd4a863ea9380f4efe6d9afabd1c579ddeffeb8b8c93"
MODES = ("frozen_route", "reobserve")
CHECKPOINT = [3, 0, 3]
BARRIER = {
    "name": "reobserve_barrier",
    "lower_enu_m": [7.5, -1.5, 0],
    "upper_enu_m": [8.5, 1.5, 5],
    "translation_enu_m": [8, 0, 2.5],
}
PROTOCOL = {
    "schema_version": "urban_reobserve_protocol.v1",
    "cases": list(MODES),
    "development_comparison_only": True,
    "checkpoint_enu_m": CHECKPOINT,
    "barrier_spawn_after_observed_east_m": 1.0,
    "barrier": BARRIER,
    "primary_endpoint": "observed_goal_dwell_then_landing_and_disarm",
    "common_safety_filter": "independent_scene_geometry_before_each_segment",
    "baseline": "initial_depth_choice_frozen_then_common_safety_gate",
    "simple_comparator": "latest_native_depth_at_planned_stop",
    "candidate_generation": "fixed_human_supplied_forward_and_left_detour",
    "expected_candidate_divergence": "forward_rejected_after_change; left_detour_admissible",
    "oracle_additional_arrivals_upper_bound": 1,
    "wam_headroom_over_simple_comparator": "unknown_until_observed; zero_if_simple_arrives",
    "model_gpu_training_calls_permitted": 0,
    "maximum_flights_in_frozen_pair": 2,
    "stop_rule": "retain_failure_and_stop_on_infrastructure_or_safety_failure",
    "input_max_age_wall_s": 5.0,
    "dispatch_max_age_wall_s": 4.0,
    "observation_translation_drift_m": 0.25,
    "observation_yaw_drift_rad": 0.1,
    "checkpoint_speed_limit_m_s": 0.1,
    "checkpoint_dwell_sim_s": 1.0,
    "continuous_emergency_avoidance_claimed": False,
    "exact_physics_state_cloning_claimed": False,
    "physical_execution_claimed": False,
}


def routes(stage):
    if stage not in ("approach", "resume"):
        raise ValueError("unknown decision stage")
    return {
        "forward": ([[3, 0, 3]] if stage == "approach" else [])
        + [[7.5, 0, 3], [12, 0, 3], [14, 0, 3]],
        "left_detour": [[1.5, 0, 3], [1.5, 11, 3], [14, 11, 3], [14, 0, 3]],
    }


def planner(stage, position):
    scene = scene_spec("gap")
    return {
        "schema_version": "urban_partial_map_planner_input.v1",
        "routes": routes(stage),
        "goal_enu_m": scene["goal_enu_m"],
        "map_boxes": [
            {key: list(b[key]) for key in ("lower_enu_m", "upper_enu_m")}
            for b in scene["buildings"]
        ],
        **{
            key: scene[key]
            for key in (
                "airframe_radius_m",
                "required_clearance_m",
                "geofence_lower_enu_m",
                "geofence_upper_enu_m",
            )
        },
        "observation_enu_m": list(position),
    }


def truth_scene():
    result = copy.deepcopy(scene_spec("gap"))
    result["buildings"].append(copy.deepcopy(BARRIER))
    return result


def validate(envelope, config, key, selection, current, now, stage):
    if set(envelope) != {"command", "hmac_sha256"}:
        raise ValueError("invalid reobservation envelope")
    command = envelope["command"]
    if not isinstance(envelope["hmac_sha256"], str) or not hmac.compare_digest(
        sign_command(command, key)["hmac_sha256"], envelope["hmac_sha256"]
    ):
        raise ValueError("reobservation signature rejected")
    if (
        any(
            command.get(k) != config[k]
            for k in (
                "session_id",
                "scene_sha256",
                "approved_instruction_ref",
                "reobserve_policy_sha256",
            )
        )
        or command.get("stage") != stage
    ):
        raise ValueError("reobservation approval/stage binding differs")
    if command.get("selection_sha256") != digest(selection):
        raise ValueError("selection binding differs")
    issued, expiry = command.get("issued_at_unix_s"), command.get("expires_at_unix_s")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (issued, expiry)) or not (
        issued <= now < expiry <= issued + PROTOCOL["dispatch_max_age_wall_s"]
    ):
        raise ValueError("dispatch expired")
    if (
        selection.get("schema_version") != "urban_reobserve_selection.v1"
        or any(selection.get(k) != config[k] for k in ("session_id", "scene_sha256", "mode"))
        or selection.get("stage") != stage
        or selection.get("model_invoked") is not False
    ):
        raise ValueError("selection scope differs")
    selected = selection.get("route_id")
    if selected not in (*routes(stage), None):
        raise ValueError("route outside approved candidate family")
    if selection.get("route_sha256") != (digest(routes(stage)[selected]) if selected else None):
        raise ValueError("route binding differs")
    numeric = [
        selection.get("observation_unix_s"),
        selection.get("observation_yaw_ned_rad"),
        current.get("yaw_ned_rad"),
        current.get("telemetry_age_seconds"),
    ]
    for obj, key in (
        (selection, "observation_enu_m"),
        (current, "gazebo_pose_enu_m"),
        (current, "local_ned_velocity_mps"),
    ):
        value = obj.get(key)
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("finite observation vectors required")
        numeric.extend(value)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in numeric):
        raise ValueError("finite observation required")
    age = now - selection["observation_unix_s"]
    if (
        not 0 <= age <= PROTOCOL["input_max_age_wall_s"]
        or (
            math.dist(current["gazebo_pose_enu_m"], selection["observation_enu_m"])
            > PROTOCOL["observation_translation_drift_m"]
        )
        or abs(wrap_angle(current["yaw_ned_rad"] - selection["observation_yaw_ned_rad"]))
        > PROTOCOL["observation_yaw_drift_rad"]
    ):
        raise ValueError("observation stale or moved")
    expected = [0, 0, 3] if stage == "approach" else CHECKPOINT
    if (
        math.dist(current["gazebo_pose_enu_m"], expected) > 0.2
        or (math.sqrt(sum(v * v for v in current["local_ned_velocity_mps"])) > 0.1)
        or current["telemetry_age_seconds"] >= 2
    ):
        raise ValueError("stationary decision boundary not observed")
    return age
