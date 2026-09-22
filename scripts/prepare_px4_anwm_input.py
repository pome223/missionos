#!/usr/bin/env python3
"""Prepare a typed ANWM input from real frozen PX4 history and a declared goal.

This performs CPU validation only. Goal references, historical observations, and
future flight outcomes have distinct roles. Unknown registered depth is retained
in a mask and converted to zero only for upstream projection's positive-Z test.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.aerial_anwm_runtime import (  # noqa: E402
    CONTEXT_SIZE,
    digest_array,
    digest_file,
    digest_json,
    target_pose_from_delta,
    validate_request,
)
from scripts.screen_px4_aerial_candidates import verify_scene  # noqa: E402
from src.prediction.px4_camera import LOCAL_NED_FROM_ENU, pose_matrix, register_capture  # noqa: E402


def last_frame_observation_time(capture):
    """The last historical frame's earliest source receipt, never processing end."""
    received = capture["frames"][-1]["received_at"]
    if set(received) != {"rgb", "depth", "rgb_info", "depth_info", "pose"}:
        raise ValueError("last historical frame must retain every actual source receipt time")
    times = []
    for value in received.values():
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("historical source receipt timezone required")
        times.append(observed.timestamp())
    timestamp = min(times)
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), timestamp


def read_bound(directory: Path, filename: str, expected: str) -> bytes:
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or filename in ("", ".", "..")
    ):
        raise ValueError("source asset must be a direct child filename")
    path = directory / filename
    if path.is_symlink():
        raise ValueError("source asset symlinks are unsupported")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"source asset hash mismatch: {filename}")
    return data


def load_registered_history(capture_dir: Path, registration_dir: Path):
    """Recreate registration from bound raw sensor inputs before admitting it."""
    capture = json.loads((capture_dir / "capture.json").read_text())
    manifest = json.loads((registration_dir / "registration.json").read_text())
    if capture.get("capture_scope") != "px4_sitl_airborne_observation":
        raise ValueError("PX4 ANWM requires an explicitly airborne SITL observation")
    if len(capture.get("frames", [])) != CONTEXT_SIZE:
        raise ValueError("PX4 ANWM requires sixteen independently recorded historical frames")
    with tempfile.TemporaryDirectory(prefix="missionos-px4-registration-") as temp:
        derived_dir = Path(temp) / "registered"
        reproduced = register_capture(
            capture_dir / "capture.json",
            airframe_sdf=capture_dir / capture["model_sdf_files"]["airframe"],
            camera_sdf=capture_dir / capture["model_sdf_files"]["camera"],
            output_dir=derived_dir,
            output_size=tuple(manifest["frames"][0]["calibration"]["output_size"]),
        )
        if {k: v for k, v in manifest.items() if k != "frames"} != {
            k: v for k, v in reproduced.items() if k != "frames"
        }:
            raise ValueError("registration manifest does not reproduce its captured source")
        if len(manifest["frames"]) != CONTEXT_SIZE:
            raise ValueError("registered historical frame count differs")
        frames = []
        for record, reproduced_record in zip(manifest["frames"], reproduced["frames"]):
            if {k: v for k, v in record.items() if k != "sha256"} != {
                k: v for k, v in reproduced_record.items() if k != "sha256"
            }:
                raise ValueError("registered frame provenance does not reproduce")
            raw = read_bound(registration_dir, record["file"], record["sha256"])
            with np.load(io.BytesIO(raw), allow_pickle=False) as data:
                frame = {key: data[key] for key in data.files}
            with np.load(derived_dir / reproduced_record["file"], allow_pickle=False) as expected:
                if set(frame) != set(expected.files) or any(
                    not np.array_equal(frame[key], expected[key], equal_nan=True) for key in frame
                ):
                    raise ValueError("registered arrays differ from recomputed raw sensor geometry")
            frames.append(frame)
    first_K = frames[0]["intrinsics"]
    if any(not np.array_equal(frame["intrinsics"], first_K) for frame in frames):
        raise ValueError("ANWM requires a fixed intrinsics matrix throughout the history")
    native_width = capture["frames"][0]["sensors"]["rgb"]["width"]
    native_height = capture["frames"][0]["sensors"]["rgb"]["height"]
    if frames[0]["rgb"].shape != (native_height, native_width, 3):
        raise ValueError("this source adapter preserves the native RGB grid before model cropping")
    positions = np.stack(
        [pose_matrix(frame["vehicle_model_pose"])[:3, 3] for frame in capture["frames"]]
    )
    if (
        np.max(np.abs(positions[:, 2] - 3.0)) > 0.35
        or np.max(np.linalg.norm(positions - positions[-1], axis=1)) > 0.35
    ):
        raise ValueError("historical observations must belong to the bounded three-metre hover")
    return capture, manifest, frames


def load_goal_reference(
    path: Path, expected_shape: tuple[int, ...], expected_K: np.ndarray, scene_hash: str
):
    """Read an independently captured static camera goal, never an outcome tape."""
    reference = json.loads(path.read_text())
    if (
        reference.get("schema_version") != "missionos_aerial_goal_reference.v1"
        or reference.get("role") != "declared_goal_reference"
        or reference.get("source_kind") != "actual_gazebo_static_camera"
        or reference.get("scene_geometry_sha256") != scene_hash
    ):
        raise ValueError(
            "goal reference requires its explicit role and the same bound static scene"
        )
    for key in (
        "future_outcome_image",
        "candidate_outcome_observed",
        "flight_outcome_observed",
        "future_ground_truth_used_for_forecast",
        "flight_command_sent",
        "physical_execution_observed",
        "physical_execution_invoked",
    ):
        if key in reference and reference[key] is not False:
            raise ValueError(f"goal reference contradicts its reference-only role: {key}")
    image_bytes = read_bound(path.parent, reference["rgb_file"], reference["rgb_sha256"])
    image = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
    if image.shape != expected_shape or image.shape[:2] != (
        reference["height"],
        reference["width"],
    ):
        raise ValueError("goal image and historical image geometry differ")
    if not np.allclose(reference["intrinsics"], expected_K, rtol=2e-6, atol=1e-5):
        raise ValueError("goal camera must match the historical RGB intrinsics")
    sdf = read_bound(path.parent, reference["camera_sdf_file"], reference["camera_sdf_sha256"])
    camera = ET.fromstring(sdf).find(".//sensor[@type='camera']/camera")
    if camera is None:
        raise ValueError("goal SDF does not declare an RGB camera")
    width, height = (
        int(camera.findtext("image/width", "0")),
        int(camera.findtext("image/height", "0")),
    )
    hfov = float(camera.findtext("horizontal_fov", "nan"))
    focal = width / (2 * np.tan(hfov / 2))
    sdf_K = [[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]]
    if (width, height) != (reference["width"], reference["height"]) or not np.allclose(
        sdf_K, expected_K, rtol=2e-6, atol=1e-5
    ):
        raise ValueError("goal SDF camera calibration does not match its declared intrinsics")
    read_bound(
        path.parent,
        reference.get("source_message_file", "goal-rgb.pb"),
        reference["source_message_sha256"],
    )
    clean = {
        key: reference[key]
        for key in (
            "schema_version",
            "role",
            "source_kind",
            "rgb_sha256",
            "source_message_sha256",
            "camera_sdf_sha256",
            "width",
            "height",
            "intrinsics",
            "optical_to_local_ned",
            "simulation_time_ns",
            "scene_geometry_sha256",
        )
    }
    clean.update(
        {
            "reference_manifest_sha256": digest_file(path),
            "flight_outcome_observed": False,
            "future_ground_truth_used_for_forecast": False,
        }
    )
    return image, clean


def prepare(
    capture_dir: Path,
    registration_dir: Path,
    goal_reference: Path,
    output_dir: Path,
    *,
    upstream_root: str,
    checkpoint_path: str,
) -> dict:
    if output_dir.exists():
        raise ValueError("prepared output directory must not already exist")
    capture, registration, frames = load_registered_history(capture_dir, registration_dir)
    boxes, _ = verify_scene(capture, capture_dir)
    scene_hash = digest_json(boxes)
    goal, goal_provenance = load_goal_reference(
        goal_reference, frames[0]["rgb"].shape, frames[0]["intrinsics"], scene_hash
    )
    session = capture.get("flight_session", {})
    if session.get("control_hold_kind") != "px4_offboard_hover":
        raise ValueError("capture requires a bound PX4 offboard hover session")
    status_bytes = read_bound(
        capture_dir,
        session.get("status_file", "flight-session-status.json"),
        session["status_sha256"],
    )
    status = json.loads(status_bytes)
    for key in (
        "session_id",
        "control_hold_kind",
        "current_vehicle_local_ned_m",
        "yaw_ned_rad",
        "observed_at_unix_s",
    ):
        if session.get(key) != status.get(key):
            raise ValueError("captured hover session metadata differs from the bound status file")
    last_vehicle = pose_matrix(capture["frames"][-1]["vehicle_model_pose"])
    source_ned = LOCAL_NED_FROM_ENU @ last_vehicle[:3, 3]
    source_forward = LOCAL_NED_FROM_ENU @ last_vehicle[:3, 0]
    source_yaw = float(np.arctan2(source_forward[1], source_forward[0]))
    status_pose = np.asarray(status["current_vehicle_local_ned_m"], dtype=float)
    if status_pose.shape != (3,) or not np.isfinite(status_pose).all() or np.linalg.norm(status_pose - source_ned) > .2:
        raise ValueError("post-capture session pose moved from the last historical observation")
    observed_at, observed_timestamp = last_frame_observation_time(capture)
    depth = np.stack([frame["depth_m"] for frame in frames])
    mask = np.stack([frame["valid_mask"] for frame in frames])
    arrays = {
        "context_rgb": np.stack([frame["rgb"] for frame in frames]),
        "context_depth": np.where(mask, depth, np.float32(0)),
        "context_valid_mask": mask,
        "context_camera_poses": np.stack([frame["optical_to_local_ned"] for frame in frames]),
        "context_simulation_time_ns": np.array(
            [frame["simulation_time_ns"] for frame in frames], dtype=np.int64
        ),
        "camera_intrinsics": frames[0]["intrinsics"],
        "goal_rgb": goal,
    }
    candidates = []
    for name, delta in (("left_5m", [0, -5, 0, 0]), ("right_5m", [0, 5, 0, 0])):
        candidate = {
            "candidate_id": name,
            "delta_local_m_rad": [float(value) for value in delta],
            "target_camera_pose": target_pose_from_delta(
                np, arrays["context_camera_poses"][-1], np.array(delta)
            ).tolist(),
            "horizon_seconds": 1.0,
        }
        candidate["candidate_sha256"] = digest_json(candidate)
        candidates.append(candidate)
    history = {
        "role": "historical_observation",
        "capture_scope": capture["capture_scope"],
        "capture_sha256": digest_file(capture_dir / "capture.json"),
        "registration_sha256": digest_file(registration_dir / "registration.json"),
        "capture_completed_at": capture["capture_completed_at"],
        "observed_at": observed_at,
        "observed_at_unix_s": observed_timestamp,
        "flight_session_status_sha256": session["status_sha256"],
        "frame_npz_sha256": [frame["sha256"] for frame in registration["frames"]],
        "frame_rgb_raw_sha256": [
            frame["source_raw_sha256"]["rgb"] for frame in registration["frames"]
        ],
        "frame_depth_raw_sha256": [
            frame["source_raw_sha256"]["depth"] for frame in registration["frames"]
        ],
        "frame_calibration_sha256": [
            frame["calibration_sha256"] for frame in registration["frames"]
        ],
        "source_sdf_sha256": registration["source_sdf_sha256"],
        "last_camera_optical_to_local_ned": arrays["context_camera_poses"][-1].tolist(),
    }
    provenance = {
        "history": history,
        "goal_reference": goal_provenance,
        "scene_geometry_sha256": scene_hash,
        "session_id": session["session_id"],
        "control_hold_kind": session["control_hold_kind"],
        "current_vehicle_local_ned_m": source_ned.tolist(),
        "current_vehicle_gazebo_enu_m": last_vehicle[:3, 3].tolist(),
        "yaw_ned_rad": source_yaw,
        "prepared_array_sha256": {key: digest_array(value) for key, value in arrays.items()},
        "registered_depth_sha256": digest_array(depth),
        "source_assets_reverified": True,
        "transport_field_decode_independently_verified": False,
    }
    request = {
        "schema_version": "aerial_anwm_request.v1",
        "source_kind": "px4_gazebo_frozen_capture",
        "request_id": f"px4-{session['session_id']}",
        "assets_npz": "assets.npz",
        "delta_frame": "body_frd_at_observation",
        "num_timesteps": 4,
        "frame_interval_seconds": 0.25,
        "frame_interval_source": "measured_Gazebo_simulation_timestamps",
        "physical_frame_timing_verified": False,
        "simulation_frame_timing_verified": True,
        "horizon_seconds_nominal": True,
        "model_time_alignment_verified": False,
        "source_timing": {"simulation_time_ns": arrays["context_simulation_time_ns"].tolist()},
        "candidates": candidates,
        "candidate_plans_sha256": digest_json(candidates),
        "px4_provenance": provenance,
        "seed": 42,
        "diffusion_steps": 250,
        "upstream_root": upstream_root,
        "checkpoint_path": checkpoint_path,
    }
    output_dir.mkdir(parents=True)
    np.savez_compressed(output_dir / "assets.npz", **arrays)
    request["asset_npz_sha256"] = digest_file(output_dir / "assets.npz")
    _, manifest = validate_request(request, output_dir)
    (output_dir / "request.json").write_text(json.dumps(request, indent=2, allow_nan=False) + "\n")
    (output_dir / "input-manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--registration-dir", type=Path, required=True)
    parser.add_argument("--goal-reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--upstream-root", required=True)
    parser.add_argument("--checkpoint-path", required=True)
    args = parser.parse_args()
    manifest = prepare(
        args.capture_dir,
        args.registration_dir,
        args.goal_reference,
        args.output_dir,
        upstream_root=args.upstream_root,
        checkpoint_path=args.checkpoint_path,
    )
    print(
        json.dumps(
            {
                "prepared": True,
                "source_kind": manifest["source_kind"],
                "input_manifest_sha256": digest_json(manifest),
            }
        )
    )


if __name__ == "__main__":
    main()
