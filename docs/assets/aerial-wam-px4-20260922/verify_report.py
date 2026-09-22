"""Verify curated report arithmetic and file integrity, without model/API/flight calls."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
ARTIFACTS = {
    "README.md",
    "summary.json",
    "trajectory.json",
    "flight-verification.png",
    "render_report.py",
    "verify_report.py",
    "test_verify_report.py",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def close(actual, expected):
    require(
        math.isfinite(actual) and math.isclose(actual, expected, rel_tol=0, abs_tol=1e-8),
        f"inconsistent quantity: {actual} != {expected}",
    )


def verify_data(summary, trajectory):
    require(
        summary["schema_version"] == "missionos_aerial_flight_public_summary.v1", "summary schema"
    )
    scope = summary["scope"]
    require(scope["completed_selected_simulator_action"] is True, "selected action not completed")
    require(scope["goal_direction_selection_correct"] is False, "wrong goal promoted to success")
    require(
        scope["physical_hardware_execution"] is False
        and scope["mission_completion_claimed"] is False,
        "unsupported hardware or mission claim",
    )
    require(scope["live_turtlebot3_motion_demonstrated"] is False, "unobserved live TB3 claim")
    model = summary["model"]
    require(
        model["actual_calls"] == 2
        and model["context_size"] == 16
        and model["diffusion_steps"] == 250,
        "actual model contract",
    )
    require(
        model["risk_score"] is None and model["model_time_alignment_verified"] is False,
        "goal cost is not calibrated risk or verified model time",
    )
    require(model["nominal_horizon_seconds"] == 1.0, "nominal model horizon changed")
    observation = summary["observation"]
    stamps = observation["frame_simulation_times_ns"]
    require(
        len(stamps) == observation["context_frames"] == 16 and len(set(stamps)) == 16,
        "distinct frames",
    )
    require(
        all(abs((b - a) / 1e9 - 0.25) <= 0.004 for a, b in zip(stamps, stamps[1:])),
        "measured frame cadence",
    )
    require(
        0
        <= observation["original_image_age_at_evaluation_s"]
        <= observation["original_image_age_at_dispatch_s"]
        <= 180,
        "original image age or ordering",
    )
    require(0 <= observation["command_age_at_dispatch_s"] < 5, "command expiration")
    close(
        observation["original_image_age_at_dispatch_s"]
        - observation["original_image_age_at_evaluation_s"],
        observation["command_age_at_dispatch_s"],
    )
    candidates = {c["candidate_id"]: c for c in summary["candidates"]}
    require(set(candidates) == {"left_5m", "right_5m"}, "candidate set")
    for candidate in candidates.values():
        close(
            candidate["distance_to_goal_camera_m"],
            math.dist(
                candidate["planned_camera_position_ned_m"], summary["goal_camera_position_ned_m"]
            ),
        )
        require(
            math.isfinite(candidate["goal_image_mse"]) and candidate["goal_image_mse"] >= 0,
            "invalid cost",
        )
    selected = min(candidates, key=lambda k: candidates[k]["goal_image_mse"])
    geometric_best = min(candidates, key=lambda k: candidates[k]["distance_to_goal_camera_m"])
    require(
        selected == "right_5m" and geometric_best == "left_5m",
        "negative choice result not preserved",
    )
    jev = summary["jev"]
    require(
        jev["actual_api_call"] is True
        and jev["response"] == "replan"
        and jev["review_signal"] == "bounded",
        "Jev outcome",
    )
    require(
        jev["selected_candidate"] == selected and jev["approval_generated"] is False,
        "Jev selection or approval",
    )
    flight = summary["flight"]
    checkpoints = flight["checkpoints"]
    require(
        flight["selected_candidate"] == selected and flight["terminal_error"] is None,
        "flight result",
    )
    require(
        all(
            flight[k] is True
            for k in (
                "takeoff_observed",
                "selected_candidate_reached",
                "landing_observed",
                "disarm_observed",
            )
        ),
        "terminal observations",
    )
    close(
        flight["measured_displacement_m"],
        math.dist(
            checkpoints["dispatch"]["position_ned_m"], checkpoints["arrival"]["position_ned_m"]
        ),
    )
    close(
        flight["target_error_m"],
        math.dist(checkpoints["arrival"]["position_ned_m"], flight["commanded_target_ned_m"]),
    )
    close(
        flight["candidate_duration_simulation_s"],
        checkpoints["arrival"]["elapsed_simulation_s"]
        - checkpoints["dispatch"]["elapsed_simulation_s"],
    )
    require(
        flight["target_error_m"] <= 0.18
        and flight["candidate_duration_simulation_s"] > model["nominal_horizon_seconds"],
        "arrival or unequal horizon",
    )
    times = [
        checkpoints[k]["elapsed_simulation_s"] for k in ("dispatch", "arrival", "landing", "disarm")
    ]
    require(all(a < b for a, b in zip(times, times[1:])), "observed event order")
    require(
        all(0 <= c["telemetry_age_s"] < 2 for c in checkpoints.values()),
        "fresh checkpoint telemetry",
    )
    land, disarm = checkpoints["landing"], checkpoints["disarm"]
    require(
        land["landed_state"] == disarm["landed_state"] == 1 and disarm["armed"] is False,
        "landed/disarmed observation",
    )
    require(
        abs(land["position_ned_m"][2]) < 0.25
        and math.dist(land["velocity_ned_mps"], [0, 0, 0]) < 0.2,
        "ground pose and speed",
    )
    require(
        flight["max_height_m"] <= 5 and flight["max_absolute_horizontal_coordinate_m"] <= 6,
        "recorded simulation bounds",
    )
    points = trajectory["points"]
    require(
        trajectory["interpolated"] is False and len(points) > 100, "sampled observed trajectory"
    )
    require(
        all(
            a["elapsed_simulation_s"] <= b["elapsed_simulation_s"]
            for a, b in zip(points, points[1:])
        ),
        "trace time order",
    )
    require(
        all(
            len(p["position_ned_m"]) == 3 and all(math.isfinite(x) for x in p["position_ned_m"])
            for p in points
        ),
        "trace coordinates",
    )
    require(
        all(
            math.isfinite(p["elapsed_simulation_s"]) and p["elapsed_simulation_s"] >= 0
            for p in points
        ),
        "trace timestamps",
    )
    require(
        all(
            -flight["max_height_m"] - 1e-6 <= p["position_ned_m"][2] <= 0.25
            and max(abs(v) for v in p["position_ned_m"][:2])
            <= flight["max_absolute_horizontal_coordinate_m"] + 1e-6
            for p in points
        ),
        "trace exceeds recorded bounds",
    )
    event_points = [p for p in points if "checkpoint" in p]
    require(
        len(event_points) == 4 and {p["checkpoint"] for p in event_points} == set(checkpoints),
        "trace checkpoint set",
    )
    for point in event_points:
        checkpoint = checkpoints[point["checkpoint"]]
        close(point["elapsed_simulation_s"], checkpoint["elapsed_simulation_s"])
        require(
            all(
                abs(a - b) <= 1e-6
                for a, b in zip(point["position_ned_m"], checkpoint["position_ned_m"])
            ),
            "trace checkpoint position",
        )
    require(
        points[-1].get("checkpoint") == "disarm" and points[-1]["phase"] == "landed",
        "trace terminal observation",
    )
    close(points[0]["elapsed_simulation_s"], 0)
    require(math.dist(points[0]["position_ned_m"], [0, 0, 0]) < 0.25, "trace initial ground pose")
    budget = summary["budget"]
    close(
        budget["cumulative_compute_storage_ip_estimate_usd"],
        sum(
            budget[k]
            for k in (
                "prior_total_estimate_usd",
                "current_vm_estimate_usd",
                "current_100gb_pd_balanced_estimate_usd",
                "ephemeral_ipv4_estimate_usd",
            )
        ),
    )
    require(
        budget["cumulative_compute_storage_ip_estimate_usd"] <= budget["user_total_cap_usd"] == 10,
        "budget",
    )
    require(
        budget["remaining_instances"] == budget["remaining_disks"] == 0
        and all(summary["cleanup"].values()),
        "resource cleanup",
    )
    require(
        budget["billing_statement_verified"] is False
        and budget["jev_api_billing_verified"] is False,
        "estimate promoted to invoice",
    )
    for digest in summary["provenance"]["retained_source_sha256"].values():
        require(re.fullmatch("[0-9a-f]{64}", digest) is not None, "source digest")
    text = json.dumps([summary, trajectory])
    require(
        not any(
            token in text
            for token in (
                "/Users/",
                "/home/",
                "api_key",
                "instruction_ref",
                "session_id",
                "private_key",
            )
        ),
        "non-public source metadata",
    )
    return {
        "artifact_consistency": "passed",
        "selected_simulator_action": "completed",
        "goal_direction_selection": "failed",
        "public_trajectory_samples": len(points),
        "model_rerun": False,
        "flight_rerun": False,
    }


def verify(root=ROOT):
    manifest = json.loads((root / "manifest.json").read_text())
    require(isinstance(manifest, dict) and set(manifest) == ARTIFACTS, "manifest artifact set")
    require(
        {p.name for p in root.iterdir() if p.name not in {"__pycache__", ".pytest_cache"}}
        == ARTIFACTS | {"manifest.json"},
        "unexpected or missing artifact",
    )
    for name, digest in manifest.items():
        require(Path(name).name == name, "manifest paths must be direct children")
        require(
            hashlib.sha256((root / name).read_bytes()).hexdigest() == digest,
            "artifact digest: " + name,
        )
    png = (root / "flight-verification.png").read_bytes()
    require(png.startswith(b"\x89PNG\r\n\x1a\n"), "PNG signature")
    return verify_data(
        json.loads((root / "summary.json").read_text()),
        json.loads((root / "trajectory.json").read_text()),
    )


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
