#!/usr/bin/env python3
"""Compare matched-horizon ANWM forecasts with two measured PX4 SITL images.

This offline audit reads local captures but emits only scores and hashes. It
does not approve or dispatch a flight, call Jev, or publish the source images.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.aerial_anwm_runtime import (  # noqa: E402
    MODEL_SHA256, UPSTREAM_REVISION, digest_file, digest_json,
)

HORIZON_SECONDS = 6.75
ARMS = (("left_5m", "left"), ("right_5m", "right"))


def image_pixels(path: Path) -> np.ndarray:
    """Mirror pinned ANWM CenterCropAR -> bilinear Resize -> ToTensor."""
    image = Image.open(path).convert("RGB")
    width, height = image.size
    if width > height:
        cropped_width = int(height * 4 / 3)
        left = round((width - cropped_width) / 2)
        image = image.crop((left, 0, left + cropped_width, height))
    else:
        cropped_height = int(width * 3 / 4)
        top = round((height - cropped_height) / 2)
        image = image.crop((0, top, width, top + cropped_height))
    return np.asarray(image.resize((224, 224), Image.Resampling.BILINEAR), dtype=np.float32) / 255


def mse(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean((a - b) ** 2))


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def arm_result(root: Path, expected_id: str) -> tuple[dict, np.ndarray, np.ndarray]:
    session = root / "session"
    local_input = root / "input-27"
    forecast_dir = root / "model-output-27"
    flight = json.loads((session / "flight-result.json").read_text())
    require(
        flight.get("complete") is True
        and flight.get("disarm_observed") is True
        and flight.get("selected_candidate_reached") is True
        and flight.get("candidate_id") == expected_id
        and flight.get("experimental_measurement_only") is True
        and flight.get("model_forecast_used_for_dispatch") is False
        and flight.get("jev_judgment_invoked") is False,
        "only complete measurement-only PX4 flights are comparable",
    )
    frames = json.loads((session / "diagnostic-images/frames.json").read_text())
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    dispatches = [event for event in events if event.get("event") == "candidate_dispatch"]
    targets = [event for event in events if event.get("event") == "candidate_target_observed"]
    require(len(dispatches) == len(targets) == 1, "one observed dispatch and target required")
    dispatch, target = dispatches[0], targets[0]
    require(dispatch["command"]["candidate_id"] == expected_id, "dispatch candidate mismatch")
    start_ns = dispatch["observed_start"]["pose_simulation_time_ns"]
    require(frames["dispatch_simulation_time_ns"] == start_ns, "image clock disagrees with dispatch")
    require(frames["target_observed_simulation_time_ns"] == target["observed"]["pose_simulation_time_ns"],
            "image clock disagrees with target observation")
    require(bool(frames["frames"]), "no actual future RGB images")
    observed_frame = min(
        frames["frames"], key=lambda frame: abs(frame["simulation_time_ns"] -
                                                     (start_ns + int(HORIZON_SECONDS * 1e9)))
    )
    seconds = (observed_frame["simulation_time_ns"] - start_ns) / 1e9
    require(abs(seconds - HORIZON_SECONDS) <= 0.2, "no actual image near nominal horizon")
    actual_file = session / "diagnostic-images" / observed_frame["file"]
    require(digest_file(actual_file) == observed_frame["image_sha256"], "actual image hash mismatch")
    result = json.loads((forecast_dir / "result.json").read_text())
    manifest = result["input_manifest"]
    require(
        result.get("schema_version") == "aerial_anwm_result.v1"
        and manifest.get("source_kind") == "px4_gazebo_frozen_capture"
        and manifest.get("num_timesteps") == 27
        and manifest.get("frame_interval_seconds") == 0.25
        and manifest.get("outcome_evaluation_only") is True
        and manifest.get("model_time_alignment_verified") is False
        and result.get("input_manifest_sha256") == digest_json(manifest)
        and manifest == json.loads((local_input / "input-manifest.json").read_text())
        and manifest["asset_npz_sha256"] == digest_file(local_input / "assets.npz"),
        "forecast is not bound to the local evaluation-only input",
    )
    require(
        manifest["px4_provenance"]["session_id"] == flight["session_id"]
        and manifest["px4_provenance"]["goal_reference"]["rgb_sha256"]
        == digest_file(session / "goal/goal.png")
        and manifest["px4_provenance"]["scene_geometry_sha256"]
        == json.loads((session / "scene.json").read_text())["scene_geometry_sha256"]
        and result["model"]["checkpoint_sha256"] == MODEL_SHA256
        and result["model"]["upstream_revision"] == UPSTREAM_REVISION
        and manifest["diffusion_steps"] == 250,
        "session, goal, scene, or released ANWM model identity differs",
    )
    evidence = result["runtime_invocation_evidence"]
    require(evidence.get("model_execution_verified") is True
            and evidence.get("actual_model_calls") == 2
            and evidence.get("input_manifest_sha256") == result["input_manifest_sha256"],
            "two real model calls required")
    source_start = np.asarray(manifest["px4_provenance"]["current_vehicle_gazebo_enu_m"], dtype=float)
    flight_start = np.asarray(dispatch["observed_start"]["gazebo_pose_enu_m"], dtype=float)
    actual_pose = np.asarray(observed_frame["pose_enu_m"], dtype=float)
    goal = image_pixels(session / "goal/goal.png")
    actual = image_pixels(actual_file)
    forecasts = {}
    goal_scores = {}
    projection_scores = {}
    for candidate in result["candidates"]:
        identifier = candidate["candidate_id"]
        require(all(Path(candidate[key]).name == candidate[key]
                    for key in ("predicted_image", "projection_image")),
                "forecast images must be direct child files")
        predicted_file = forecast_dir / candidate["predicted_image"]
        projection_file = forecast_dir / candidate["projection_image"]
        require(digest_file(predicted_file) == candidate["predicted_image_sha256"]
                and digest_file(projection_file) == candidate["projection_image_sha256"],
                "forecast image hash mismatch")
        predicted = image_pixels(predicted_file)
        projection = image_pixels(projection_file)
        forecasts[identifier] = {
            "model_goal_mse_float": candidate["goal_mse"],
            "projection_goal_mse_float": candidate["projection_goal_mse"],
        }
        if identifier == expected_id:
            forecasts[identifier]["model_vs_actual_rgb_mse_quantized"] = mse(predicted, actual)
            forecasts[identifier]["projection_vs_actual_rgb_mse_quantized"] = mse(projection, actual)
        goal_scores[identifier] = candidate["goal_mse"]
        projection_scores[identifier] = candidate["projection_goal_mse"]
    require(set(forecasts) == {name for name, _ in ARMS}, "both candidate forecasts required")
    reference = np.asarray(manifest["px4_provenance"]["goal_reference"]["optical_to_local_ned"])
    distances = {candidate["candidate_id"]: float(np.linalg.norm(
        np.asarray(candidate["target_camera_pose"])[:3, 3] - reference[:3, 3]))
        for candidate in manifest["candidates"]}
    data = {
        "flight_complete_and_disarmed": True,
        "forecast_used_to_dispatch": False,
        "jev_invoked_for_measurement": False,
        "scene_sha256": manifest["px4_provenance"]["scene_geometry_sha256"],
        "goal_image_sha256": digest_file(session / "goal/goal.png"),
        "input_manifest_sha256": result["input_manifest_sha256"],
        "model_result_sha256": digest_file(forecast_dir / "result.json"),
        "start_pose_enu_m": flight_start.tolist(),
        "start_yaw_ned_rad": dispatch["observed_start"]["yaw_ned_rad"],
        "input_to_dispatch_start_gap_m": float(np.linalg.norm(source_start - flight_start)),
        "target_observed_sim_seconds": (target["observed"]["pose_simulation_time_ns"] - start_ns) / 1e9,
        "sample_sim_seconds": seconds,
        "sample_offset_from_nominal_seconds": seconds - HORIZON_SECONDS,
        "sample_displacement_m": float(np.linalg.norm(actual_pose - flight_start)),
        "measured_target_displacement_m": flight["measured_displacement_m"],
        "actual_image_sha256": observed_frame["image_sha256"],
        "actual_goal_rgb_mse": mse(actual, goal),
        "current_goal_rgb_mse": mse(image_pixels(session / "history/frame_15-rgb.png"), goal),
        "forecast_scores": forecasts,
        "model_selected": min(goal_scores, key=goal_scores.get),
        "projection_selected": min(projection_scores, key=projection_scores.get),
        "geometry_target_goal_distance_m": distances,
        "geometry_selected": min(distances, key=distances.get),
    }
    return data, image_pixels(session / "history/frame_15-rgb.png"), goal


def compare(left_root: Path, right_root: Path) -> dict:
    left, left_start, left_goal = arm_result(left_root, "left_5m")
    right, right_start, right_goal = arm_result(right_root, "right_5m")
    require(left["scene_sha256"] == right["scene_sha256"]
            and left["goal_image_sha256"] == right["goal_image_sha256"],
            "different scene or declared goal reference")
    require(left["model_result_sha256"] != right["model_result_sha256"],
            "each arm must have its own actual model inference")
    require(np.array_equal(left_goal, right_goal), "declared goal pixels differ")
    pose_gap = float(np.linalg.norm(np.asarray(left["start_pose_enu_m"]) -
                                    np.asarray(right["start_pose_enu_m"])))
    heading_gap = abs(math.remainder(left["start_yaw_ned_rad"] - right["start_yaw_ned_rad"],
                                     2 * math.pi))
    actual = {"left_5m": left["actual_goal_rgb_mse"],
              "right_5m": right["actual_goal_rgb_mse"]}
    return {
        "schema_version": "missionos_px4_anwm_matched_outcome_comparison.v1",
        "evaluation_scope": "two_separate_matched_start_px4_sitl_measurements",
        "actual_hardware_executed": False,
        "model_or_jev_dispatched_measurement": False,
        "model_time_alignment_verified": False,
        "nominal_forecast_horizon_sim_seconds": HORIZON_SECONDS,
        "rgb_mse_space": "ANWM_center_crop_4_to_3_resize_224_bilinear_RGB_0_to_1",
        "start_pose_gap_m": pose_gap,
        "start_heading_gap_rad": heading_gap,
        "start_rgb_mse": mse(left_start, right_start),
        "actual_goal_rgb_mse_by_arm": actual,
        "actual_lower_goal_mse_arm": min(actual, key=actual.get),
        "arms": {"left_5m": left, "right_5m": right},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-root", type=Path, required=True)
    parser.add_argument("--right-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.left_root, args.right_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"compared": True, "actual_lower_goal_mse_arm":
                      result["actual_lower_goal_mse_arm"]}))


if __name__ == "__main__":
    main()
