#!/usr/bin/env python3
"""Verify saved urban PX4 route observations without dispatch or model calls."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.urban_navigation_contract import (  # noqa: E402
    digest,
    scene_spec,
    segment_box_clearance,
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root):
    session = root / "session"
    scene = json.loads((session / "scene.json").read_text())
    config = json.loads((session / "config.json").read_text())
    outcome = json.loads((session / "flight-result.json").read_text())
    require(
        scene == scene_spec(scene["family"]), "scene differs from declared geometry"
    )
    require(
        config["scene_sha256"] == scene["scene_sha256"]
        and config["session_id"] == outcome["session_id"],
        "scene/session mismatch",
    )
    require(
        outcome.get("complete") is True
        and outcome.get("goal_reached") is True
        and outcome.get("disarm_observed") is True,
        "complete arrival and disarm required",
    )
    model_used = outcome.get("model_forecast_used_for_dispatch")
    require(
        type(model_used) is bool and outcome.get("jev_judgment_invoked") is False,
        "unsupported urban selection mode",
    )
    require(
        model_used == (config.get("selection_mode", "geometric") == "anwm"),
        "configured and observed selector differ",
    )
    route_id = outcome["route_id"]
    route = scene["routes"][route_id]
    require(
        model_used
        or config.get("selection_mode") == "headroom"
        or (route_id == config["route_id"] and digest(route) == config["route_sha256"]),
        "route digest mismatch",
    )
    files = json.loads((session / "source-files.json").read_text())
    require(
        all(
            Path(name).name == name and sha(session / name) == expected
            for name, expected in files.items()
        ),
        "source SDF changed",
    )
    events = [
        json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()
    ]

    def one(name):
        found = [event for event in events if event["event"] == name]
        require(len(found) == 1, "exactly one " + name + " required")
        return found[0]

    dispatch, goal = one("urban_route_dispatch"), one("urban_goal_observed")
    one("landing_observed")
    one("disarm_observed")
    require(
        dispatch["route_enu_m"] == route
        and dispatch["route_id"] == route_id
        and dispatch["model_forecast_used_for_dispatch"] is model_used,
        "dispatch route differs",
    )
    depth_details = None
    if model_used:
        from scripts.px4_urban_wam_trial import validate_trigger
        from scripts.select_urban_wam_route import select_route

        selection = json.loads((session / "urban-selection.json").read_text())
        # The telemetry receipt can precede the command by one update interval.
        # Expiry is checked at dispatch event time, preserving observation age.
        dispatched_at = datetime.fromisoformat(dispatch["at"])
        require(dispatched_at.tzinfo is not None, "dispatch event timezone missing")
        dispatch_time = dispatched_at.timestamp()
        envelope = json.loads((session / "urban-go-accepted.json").read_text())
        require(
            sha(session / "urban-selection.json")
            == envelope["command"]["selection_sha256"]
            == outcome["selection_sha256"],
            "selection receipt changed",
        )
        validate_trigger(
            envelope,
            config,
            (session / ".dispatch-key").read_bytes(),
            dispatch_time,
        )
        recomputed = select_route(
            root / "input",
            root / "forecast/result.json",
            scene,
            dispatch["observed_start"],
            dispatch_time,
        )
        for key in (
            "route_id",
            "route_sha256",
            "input_manifest_sha256",
            "model_result_sha256",
            "scores",
            "projection_choice",
            "shortest_geometry_choice",
            "unconstrained_model_choice",
        ):
            require(
                selection[key] == recomputed[key],
                "model selection does not reproduce: " + key,
            )
        require(
            one("urban_model_selection_consumed")["selection"] == selection,
            "model selection was not consumed unchanged",
        )
        require(
            selection["route_id"] == route_id,
            "model selection did not control dispatched route",
        )
    elif config.get("selection_mode") == "headroom":
        from scripts.verify_urban_headroom import verify_choice

        depth_details = verify_choice(root, scene, config, outcome, dispatch, events)
    waypoints = [
        event for event in events if event["event"] == "urban_waypoint_observed"
    ]
    require(
        len(waypoints) == len(route) == outcome["completed_waypoints"],
        "missing route waypoint",
    )
    errors = []
    for index, (event, target) in enumerate(zip(waypoints, route)):
        require(
            event["index"] == index and event["target_enu_m"] == target,
            "waypoint order/target differs",
        )
        error = math.dist(event["observed"]["gazebo_pose_enu_m"], target)
        require(error <= 0.3, "waypoint not reached")
        errors.append(error)
    start_ns = dispatch["observed_start"]["pose_simulation_time_ns"]
    end_ns = goal["observed"]["pose_simulation_time_ns"]
    require(end_ns > start_ns, "invalid route time")
    trace = [
        json.loads(line)
        for line in (session / "telemetry.jsonl").read_text().splitlines()
    ]
    samples = (
        [dispatch["observed_start"]]
        + [
            s
            for s in trace
            if s["pose_simulation_time_ns"] is not None
            and start_ns < s["pose_simulation_time_ns"] < end_ns
        ]
        + [goal["observed"]]
    )
    require(len(samples) >= 10, "insufficient observed trajectory")
    minimum, distance, max_gap = math.inf, 0.0, 0.0
    for previous, current in zip(samples, samples[1:]):
        require(
            current["scene_static_verified"] is True
            and current["telemetry_age_seconds"] < 2,
            "stale observation or moved scene",
        )
        gap = (
            current["pose_simulation_time_ns"] - previous["pose_simulation_time_ns"]
        ) / 1e9
        require(0 <= gap <= 0.25, "missing trajectory interval")
        max_gap = max(max_gap, gap)
        a, b = previous["gazebo_pose_enu_m"], current["gazebo_pose_enu_m"]
        require(
            all(
                lo <= v <= hi
                for lo, v, hi in zip(
                    scene["geofence_lower_enu_m"], b, scene["geofence_upper_enu_m"]
                )
            ),
            "observed geofence breach",
        )
        distance += math.dist(a, b)
        for obstacle in scene["buildings"]:
            minimum = min(
                minimum,
                segment_box_clearance(
                    a,
                    b,
                    obstacle["lower_enu_m"],
                    obstacle["upper_enu_m"],
                    scene["airframe_radius_m"],
                ),
            )
    require(
        minimum >= scene["required_clearance_m"],
        "observed swept envelope clearance below bound",
    )
    require(
        math.dist(goal["observed"]["gazebo_pose_enu_m"], scene["goal_enu_m"])
        <= scene["goal_radius_m"],
        "goal not observed",
    )
    require(outcome["building_contact_messages"] == 0, "building contact was observed")
    images = json.loads((session / "route-images/frames.json").read_text())
    require(
        images["exact_rgb_pose_timestamps"] is True and len(images["frames"]) >= 16,
        "missing camera/pose samples",
    )
    for frame in images["frames"]:
        require(
            Path(frame["file"]).name == frame["file"]
            and sha(session / "route-images" / frame["file"]) == frame["image_sha256"],
            "route image digest mismatch",
        )
    cleanup = json.loads((root / "cleanup.json").read_text())
    require(cleanup["created_container_removed"] is True, "created simulator remains")
    return {
        "schema_version": "missionos_urban_route_verification.v1",
        "family": scene["family"],
        "route_id": route_id,
        "route_sha256": digest(route),
        "selector": outcome["selector"],
        "model_invoked": model_used,
        "model_forecast_used_for_dispatch": model_used,
        **({"depth_selection": depth_details} if depth_details is not None else {}),
        **(
            {
                "model_selection": {
                    key: selection[key]
                    for key in (
                        "scores",
                        "projection_choice",
                        "shortest_geometry_choice",
                        "unconstrained_model_choice",
                        "multiple_admissible_routes",
                        "model_sha256",
                        "runtime_script_sha256",
                        "input_manifest_sha256",
                        "model_result_sha256",
                        "remote_call_wall_seconds",
                    )
                }
            }
            if model_used
            else {}
        ),
        "jev_invoked": False,
        "physical_hardware_executed": False,
        "destination_reached": True,
        "landing_and_disarm_observed": True,
        "minimum_observed_envelope_clearance_m": minimum,
        "clearance_scope": "piecewise_linear_observed_trajectory_against_mesh_AABBs_with_0.6m_sphere",
        "building_contact_messages": outcome["building_contact_messages"],
        "contact_sensor_streams_observed": outcome["contact_sensor_streams_observed"],
        "ground_contact_positive_control_messages": outcome[
            "ground_contact_positive_control_messages"
        ],
        "route_simulation_seconds": (end_ns - start_ns) / 1e9,
        "route_wall_seconds": goal["elapsed_seconds"] - dispatch["elapsed_seconds"],
        "observed_distance_m": distance,
        "maximum_altitude_m": max(s["gazebo_pose_enu_m"][2] for s in samples),
        "waypoint_errors_m": errors,
        "trajectory_samples": len(samples),
        "maximum_trajectory_sim_gap_seconds": max_gap,
        "captured_rgb_frames": len(images["frames"]),
        "simulator_removed": True,
        "source_sha256": {
            name: sha(session / name)
            for name in (
                "scene.json",
                "flight-result.json",
                "events.jsonl",
                "telemetry.jsonl",
                "route-images/frames.json",
            )
        },
        "learned_navigation_benefit_established": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
