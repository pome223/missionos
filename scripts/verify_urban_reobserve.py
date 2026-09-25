#!/usr/bin/env python3
"""Independently recheck raw stop/observe/resume records; never dispatch."""

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

from scripts.px4_urban_reobserve_trial import barrier_sdf, source_hashes  # noqa: E402
from scripts.select_urban_depth_route import depth_clouds, rank_routes  # noqa: E402
from scripts.urban_navigation_contract import digest, scene_spec, route_check, segment_box_clearance  # noqa: E402
from scripts.urban_reobserve_contract import (  # noqa: E402
    IMAGE,
    PROTOCOL,
    BARRIER,
    CHECKPOINT,
    routes,
    planner,
    truth_scene,
    validate,
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def verify(root):
    session = root / "session"

    def read(name):
        return json.loads((session / name).read_text())

    frozen = json.loads((root.parent / "protocol.json").read_text())
    config, result, scene = read("config.json"), read("flight-result.json"), read("scene.json")
    require(
        frozen["protocol"] == PROTOCOL and frozen["source_sha256"] == source_hashes(),
        "frozen implementation differs",
    )
    require(config["runtime_source_sha256"] == frozen["source_sha256"], "runtime source not frozen")
    policy = {"protocol": PROTOCOL, "mode": config["mode"], "source_sha256": source_hashes()}
    require(
        read("reobserve-policy.json") == policy
        and digest(policy) == config["reobserve_policy_sha256"],
        "approval policy differs",
    )
    require(
        scene == scene_spec("gap") and config["scene_sha256"] == scene["scene_sha256"],
        "initial scene differs",
    )
    require(
        result["session_id"] == config["session_id"] and result["mode"] == config["mode"],
        "session differs",
    )
    require(
        result["reobserve_verification_complete"] and result["error"] is None,
        "runtime error or incomplete result",
    )
    require(
        result["model_forecast_used_for_dispatch"] is False
        and result["jev_judgment_invoked"] is False,
        "unexpected model",
    )
    require(
        json.loads((root / "container.json").read_text())["image_id"] == IMAGE, "unpinned image"
    )
    require(
        json.loads((root / "cleanup.json").read_text())["created_container_removed"] is True,
        "cleanup not observed",
    )
    require(
        json.loads((root / "probe-process.json").read_text())["returncode"] == 0,
        "contact probe failed",
    )
    probe = read("contact-positive-control.json")
    require(
        probe["building_sensor_positive_control"]
        and probe["probe_removed_observed"]
        and probe["subscriptions_released"],
        "contact positive control absent",
    )
    require((session / "barrier.sdf").read_text() == barrier_sdf(), "barrier geometry differs")
    for name, expected in read("source-files.json").items():
        require(Path(name).name == name and sha(session / name) == expected, "initial SDF changed")
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]

    def one(name):
        found = [e for e in events if e["event"] == name]
        require(len(found) == 1, "exactly one event required: " + name)
        return found[0]

    start = one("reobserve_approach_dispatch")["observed"]
    checkpoint = one("reobserve_checkpoint_stopped")
    barrier_stamp = checkpoint["barrier_first_seen_sim_ns"]
    stopped = checkpoint["observed"]
    end = one("disarm_observed")["observed"]
    landed = one("landing_observed")["observed"]
    require(
        start["pose_simulation_time_ns"]
        < barrier_stamp
        < stopped["pose_simulation_time_ns"]
        < end["pose_simulation_time_ns"],
        "change/stop/terminal order differs",
    )
    require(result["barrier_first_seen_sim_ns"] == barrier_stamp, "barrier receipt differs")
    require(
        math.dist(stopped["gazebo_pose_enu_m"], CHECKPOINT) <= 0.15
        and math.sqrt(sum(v * v for v in stopped["local_ned_velocity_mps"])) <= 0.1,
        "checkpoint not stationary",
    )
    require(
        landed["landed_state"] == 1
        and abs(landed["gazebo_pose_enu_m"][2]) < 0.25
        and end["armed"] is False,
        "landing/disarm not observed",
    )
    choices, ages = {}, {}
    for stage in ("approach", "resume"):
        history = session / ("history-" + stage)
        latest, _, receipt = depth_clouds(history)
        geometry = planner(stage, receipt["observation_enu_m"])
        choice = rank_routes(geometry, latest)
        selection = read(stage + "-selection.json")
        require(read(stage + "-planner-input.json") == geometry, "planner input changed")
        require(
            selection["latest_depth_choice"] == choice
            and selection["planner_input_sha256"] == digest(geometry),
            "depth choice not reproducible",
        )
        for k, v in receipt.items():
            require(selection[k] == v, "observation binding differs: " + k)
        consumed = [
            e for e in events if e["event"] == "reobserve_decision_consumed" and e["stage"] == stage
        ]
        require(
            len(consumed) == 1 and consumed[0]["selection"] == selection,
            "decision missing/replayed",
        )
        event = consumed[0]
        now = datetime.fromisoformat(event["at"]).timestamp()
        age = validate(
            read(stage + "-go-accepted.json"),
            config,
            (session / ".dispatch-key").read_bytes(),
            selection,
            event["observed"],
            now,
            stage,
        )
        expected = (
            choice["route_id"]
            if stage == "approach" or config["mode"] == "reobserve"
            else choices["approach"]["selected"]
        )
        require(selection["route_id"] == expected, "unexplained route selection")
        stamps = receipt["input_frame_simulation_time_ns"]
        if stage == "approach":
            require(max(stamps) < start["pose_simulation_time_ns"], "initial capture from future")
        else:
            require(
                min(stamps) > stopped["pose_simulation_time_ns"] and min(stamps) > barrier_stamp,
                "old checkpoint capture",
            )
        choices[stage] = {
            "selected": selection["route_id"],
            "latest_depth": choice["route_id"],
            "scores": choice["scores"],
        }
        ages[stage] = age
    gate = one("reobserve_safety_gate")
    route_id = choices["resume"]["selected"]
    route = routes("resume").get(route_id)
    expected_gate = (
        route_check(truth_scene(), route, start=gate["observed"]["gazebo_pose_enu_m"])
        if route
        else {"admissible": False}
    )
    require(gate["result"] == expected_gate and gate["route_id"] == route_id, "safety gate differs")
    goal = None
    if expected_gate["admissible"]:
        dispatch = one("urban_route_dispatch")
        require(
            dispatch["route_enu_m"] == route and dispatch["route_id"] == route_id,
            "executed route differs",
        )
        points = [e for e in events if e["event"] == "urban_waypoint_observed"]
        require(len(points) == len(route), "waypoint evidence incomplete")
        for i, (event, point) in enumerate(zip(points, route)):
            require(
                event["index"] == i
                and event["target_enu_m"] == point
                and math.dist(event["observed"]["gazebo_pose_enu_m"], point) <= 0.3,
                "waypoint not observed",
            )
        goal = one("urban_goal_observed")["observed"]
        require(
            math.dist(goal["gazebo_pose_enu_m"], scene["goal_enu_m"]) <= 0.3
            and result["complete"]
            and result["goal_reached"],
            "goal not observed",
        )
    else:
        one("reobserve_safe_abort")
        require(
            not any(e["event"] == "urban_route_dispatch" for e in events), "unsafe route dispatched"
        )
        require(
            result["safe_abort_observed"]
            and result["complete"] is False
            and result["goal_reached"] is False,
            "abort incorrectly claimed completion",
        )
    trace = [json.loads(line) for line in (session / "telemetry.jsonl").read_text().splitlines()]
    samples = (
        [start]
        + [
            s
            for s in trace
            if s["pose_simulation_time_ns"] is not None
            and start["pose_simulation_time_ns"]
            < s["pose_simulation_time_ns"]
            < end["pose_simulation_time_ns"]
        ]
        + [end]
    )
    require(len(samples) > 20, "insufficient telemetry")
    clearance, distance, max_gap = math.inf, 0.0, 0.0
    for a, b in zip(samples, samples[1:]):
        gap = (b["pose_simulation_time_ns"] - a["pose_simulation_time_ns"]) / 1e9
        require(
            0 <= gap <= 0.25 and b["telemetry_age_seconds"] < 2 and b["scene_static_verified"],
            "trajectory stale/gap/scene change",
        )
        max_gap = max(max_gap, gap)
        distance += math.dist(a["gazebo_pose_enu_m"], b["gazebo_pose_enu_m"])
        obstacles = scene["buildings"] + (
            [BARRIER] if b["pose_simulation_time_ns"] >= barrier_stamp else []
        )
        for obstacle in obstacles:
            clearance = min(
                clearance,
                segment_box_clearance(
                    a["gazebo_pose_enu_m"],
                    b["gazebo_pose_enu_m"],
                    obstacle["lower_enu_m"],
                    obstacle["upper_enu_m"],
                ),
            )
        require(
            all(
                lo <= v <= hi
                for lo, v, hi in zip(
                    scene["geofence_lower_enu_m"],
                    b["gazebo_pose_enu_m"],
                    scene["geofence_upper_enu_m"],
                )
            ),
            "geofence breached",
        )
    require(
        clearance >= scene["required_clearance_m"] and result["building_contact_messages"] == 0,
        "clearance/contact failure",
    )
    # Dwell is rechecked from terminal telemetry rather than accepting the event name.
    for point, finish, speed, radius in (
        (CHECKPOINT, stopped, 0.1, 0.15),
        *([(scene["goal_enu_m"], goal, 0.18, 0.3)] if goal else []),
    ):
        terminal = finish["pose_simulation_time_ns"]
        dwell = [
            s
            for s in samples
            if terminal - 1_000_000_000 <= s["pose_simulation_time_ns"] <= terminal
        ]
        require(
            len(dwell) >= 4 and (terminal - dwell[0]["pose_simulation_time_ns"]) / 1e9 >= 0.8,
            "missing dwell",
        )
        require(
            all(
                math.dist(s["gazebo_pose_enu_m"], point) <= radius
                and math.sqrt(sum(v * v for v in s["local_ned_velocity_mps"])) <= speed
                for s in dwell
            ),
            "unstable dwell",
        )
    frames = read("route-images/frames.json")["frames"]
    require(
        len(frames) > 10
        and all(
            a["simulation_time_ns"] < b["simulation_time_ns"] for a, b in zip(frames, frames[1:])
        ),
        "camera order/coverage differs",
    )
    for frame in frames:
        require(
            Path(frame["file"]).name == frame["file"]
            and sha(session / "route-images" / frame["file"]) == frame["image_sha256"],
            "camera frame changed",
        )
    return {
        "schema_version": "urban_reobserve_verification.v1",
        "mode": config["mode"],
        "destination_reached": goal is not None,
        "safe_abort": result["safe_abort_observed"],
        "landing_and_disarm_observed": True,
        "choices": choices,
        "input_age_wall_s": ages,
        "checkpoint_to_resume_sim_s": (
            gate["observed"]["pose_simulation_time_ns"] - stopped["pose_simulation_time_ns"]
        )
        / 1e9,
        "checkpoint_to_resume_wall_s": (
            datetime.fromisoformat(gate["at"]) - datetime.fromisoformat(checkpoint["at"])
        ).total_seconds(),
        "minimum_observed_envelope_clearance_m": clearance,
        "building_contact_messages": result["building_contact_messages"],
        "path_from_departure_through_disarm_m": distance,
        "elapsed_departure_through_disarm_sim_s": (
            end["pose_simulation_time_ns"] - start["pose_simulation_time_ns"]
        )
        / 1e9,
        "maximum_telemetry_gap_sim_s": max_gap,
        "barrier_appearance_sim_ns": barrier_stamp,
        "checkpoint_sim_ns": stopped["pose_simulation_time_ns"],
        "departure_sim_ns": start["pose_simulation_time_ns"],
        "terminal_sim_ns": end["pose_simulation_time_ns"],
        "replan_count": result["replan_count"],
        "model_invoked": False,
        "learned_navigation_benefit_established": False,
        "physical_execution_invoked": False,
        "simulator_removed": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.root), indent=2, allow_nan=False))
