#!/usr/bin/env python3
"""Screen frozen PX4 camera geometry before spending on a model comparison.

This is a camera-reference diagnostic, not an airframe collision checker. The
independent oracle concerns only explicitly declared static boxes. Depth with no
return, missing registration coverage, and space outside the camera frustum
remain unknown. This CLI invokes no simulator, model, judge, or aircraft action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.prediction.px4_camera import (  # noqa: E402
    ALGORITHM,
    LOCAL_NED_FROM_ENU,
    SCHEMA,
    audit_camera_sdf,
    optical_pose_local_ned,
    pose_matrix,
    register_depth_to_rgb,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def _read_child(directory: Path, filename: str, expected: str | None = None) -> bytes:
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or filename in ("", ".", "..")
    ):
        raise ValueError("artifact must be a direct child filename")
    path = directory / filename
    if path.is_symlink():
        raise ValueError("artifact symlinks are unsupported")
    data = path.read_bytes()
    if expected is not None and _sha(data) != expected:
        raise ValueError(f"artifact hash mismatch: {filename}")
    return data


def _vector(value: Any) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.shape != (3,) or not np.isfinite(result).all():
        raise ValueError("expected a finite three-vector")
    return result


def segment_intersects_box(start: Any, end: Any, lower: Any, upper: Any) -> bool:
    """Exact slab intersection for a segment and a closed axis-aligned box."""
    start, end, lower, upper = map(_vector, (start, end, lower, upper))
    if np.any(lower > upper):
        raise ValueError("box lower bounds exceed upper bounds")
    lo, hi = 0.0, 1.0
    for axis, delta in enumerate(end - start):
        if abs(delta) < 1e-12:
            if start[axis] < lower[axis] or start[axis] > upper[axis]:
                return False
        else:
            bounds = sorted(
                ((lower[axis] - start[axis]) / delta, (upper[axis] - start[axis]) / delta)
            )
            lo, hi = max(lo, bounds[0]), min(hi, bounds[1])
            if lo > hi:
                return False
    return True


def _point_hit_mask(
    points: np.ndarray, start: np.ndarray, end: np.ndarray, margin: np.ndarray
) -> np.ndarray:
    """Whether a segment intersects each observed point's margin box."""
    lo, hi = np.zeros(len(points)), np.ones(len(points))
    valid = np.ones(len(points), dtype=bool)
    for axis, delta in enumerate(end - start):
        if abs(delta) < 1e-12:
            valid &= np.abs(points[:, axis] - start[axis]) <= margin[axis]
        else:
            a = (points[:, axis] - margin[axis] - start[axis]) / delta
            b = (points[:, axis] + margin[axis] - start[axis]) / delta
            lo, hi = np.maximum(lo, np.minimum(a, b)), np.minimum(hi, np.maximum(a, b))
    return valid & (lo <= hi)


def depth_corridor_screen(
    depth: Any,
    valid_mask: Any,
    intrinsics: Any,
    optical_to_world: Any,
    start: Any,
    end: Any,
    margin: Any,
    *,
    sample_spacing_m: float = 0.2,
) -> dict[str, Any]:
    """Classify measured blockage and explicitly sampled free-space coverage.

    A no-hit result cannot establish clear space. All sampled corridor points
    must project onto valid observed rays in front of measured surfaces. The
    camera's near blind region is not silently assumed free.
    """
    depth, mask = np.asarray(depth), np.asarray(valid_mask)
    K, transform = np.asarray(intrinsics, dtype=float), np.asarray(optical_to_world, dtype=float)
    start, end, margin = map(_vector, (start, end, margin))
    if depth.ndim != 2 or mask.shape != depth.shape or mask.dtype != bool or np.any(margin <= 0):
        raise ValueError("invalid depth, coverage mask, or corridor margin")
    if not np.array_equal(mask, np.isfinite(depth) & (depth > 0)):
        raise ValueError("validity mask must exactly identify finite positive depth")
    if (
        K.shape != (3, 3)
        or transform.shape != (4, 4)
        or not np.isfinite(K).all()
        or not np.isfinite(transform).all()
    ):
        raise ValueError("invalid camera geometry")
    if not 0 < sample_spacing_m <= 1:
        raise ValueError("invalid sample spacing")
    vv, uu = np.indices(depth.shape)
    rays = np.stack(
        ((uu[mask] - K[0, 2]) / K[0, 0], (vv[mask] - K[1, 2]) / K[1, 1], np.ones(mask.sum())),
        axis=1,
    )
    points = rays * depth[mask, None]
    points = points @ transform[:3, :3].T + transform[:3, 3]
    hit_count = int(_point_hit_mask(points, start, end, margin).sum())

    centers = np.linspace(
        start, end, int(np.ceil(np.linalg.norm(end - start) / sample_spacing_m)) + 1
    )
    offsets = np.stack(
        np.meshgrid(*[[-value, 0, value] for value in margin], indexing="ij"), axis=-1
    ).reshape(-1, 3)
    samples = (centers[:, None, :] + offsets[None, :, :]).reshape(-1, 3)
    optical = (samples - transform[:3, 3]) @ transform[:3, :3]
    front = optical[:, 2] > 1e-6
    uv = np.full((len(samples), 2), np.nan)
    uv[front] = (optical[front] @ K.T)[:, :2] / optical[front, 2, None]
    height, width = depth.shape
    inside = (
        front
        & (uv[:, 0] >= 0)
        & (uv[:, 0] <= width - 1)
        & (uv[:, 1] >= 0)
        & (uv[:, 1] <= height - 1)
    )
    covered = np.zeros(len(samples), dtype=bool)
    free = np.zeros(len(samples), dtype=bool)
    indices = np.flatnonzero(inside)
    pixels = np.floor(uv[inside] + 0.5).astype(int)
    covered[indices] = mask[pixels[:, 1], pixels[:, 0]]
    free[indices] = covered[indices] & (
        depth[pixels[:, 1], pixels[:, 0]] > optical[indices, 2] + 0.02
    )
    classification = (
        "observed_blocked" if hit_count else "observed_clear" if free.all() else "unobserved"
    )
    return {
        "classification": classification,
        "observed_surface_points_in_corridor": hit_count,
        "sample_count": len(samples),
        "observed_free_samples": int(free.sum()),
        "unobserved_samples": int((~covered).sum()),
        "outside_frustum_or_behind_camera_samples": int((~inside).sum()),
        "samples_on_or_behind_observed_surface": int((covered & ~free).sum()),
        "observed_free_fraction": float(free.mean()),
        "sample_spacing_m": sample_spacing_m,
        "coverage_is_finite_sampling_not_continuous_certification": True,
        "absence_of_surface_hits_implies_clear": False,
    }


def verify_scene(capture: dict[str, Any], directory: Path) -> tuple[list[dict[str, Any]], str]:
    """Match declared static boxes to their SDF and every observed scene pose."""
    scene_bytes = _read_child(directory, "scene.json")
    scene = json.loads(scene_bytes)
    if (
        scene.get("schema_version") != "missionos_px4_calibration_scene.v1"
        or scene.get("flight_outcomes_observed") is not False
    ):
        raise ValueError("unsupported static calibration scene")
    boxes = scene.get("boxes", [])
    if not 1 <= len(boxes) <= 8:
        raise ValueError("one through eight declared boxes are required")
    names = set()
    records = []
    for box in boxes:
        name = box["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name) or name in names:
            raise ValueError("invalid or duplicate scene entity")
        names.add(name)
        center, size = _vector(box["center_xyz_m"]), _vector(box["size_xyz_m"])
        if np.any(size <= 0):
            raise ValueError("invalid box size")
        sdf_bytes = _read_child(directory, f"{name}.sdf")
        model = ET.fromstring(sdf_bytes).find("model")
        if model is None or model.get("name") != name or model.findtext("static") != "true":
            raise ValueError("box is not a declared static SDF model")
        pose = np.asarray([float(v) for v in model.findtext("pose", "").split()])
        declared_size = np.asarray(
            [float(v) for v in model.findtext("link/collision/geometry/box/size", "").split()]
        )
        if (
            pose.shape != (6,)
            or not np.allclose(pose[:3], center, atol=1e-8)
            or not np.allclose(pose[3:], 0, atol=1e-8)
            or not np.array_equal(declared_size, size)
        ):
            raise ValueError("declared box and SDF geometry disagree")
        for frame in capture["frames"]:
            matches = [p for p in frame.get("scene_model_poses", []) if p.get("name") == name]
            if len(matches) != 1:
                raise ValueError("declared box lacks exactly one observed scene pose")
            observed = pose_matrix(matches[0])
            if not np.allclose(observed[:3, 3], center, atol=1e-6, rtol=0) or not np.allclose(
                observed[:3, :3], np.eye(3), atol=1e-6, rtol=0
            ):
                raise ValueError("observed scene pose differs from declared static box")
        records.append(
            {
                "name": name,
                "center_xyz_m": center.tolist(),
                "size_xyz_m": size.tolist(),
                "sdf_sha256": _sha(sdf_bytes),
            }
        )
    return records, _sha(scene_bytes)


def verify_registration(
    capture_dir: Path, registration_dir: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, np.ndarray]]:
    """Recheck bound source assets and the registered pixels used by this sink."""
    capture_bytes = _read_child(capture_dir, "capture.json")
    capture = json.loads(capture_bytes)
    registration = json.loads(_read_child(registration_dir, "registration.json"))
    if (
        capture.get("schema_version") != "missionos_px4_aerial_camera_probe.v1"
        or capture.get("source_kind") != "actual_px4_gazebo_sensor_capture"
        or registration.get("schema_version") != SCHEMA
        or registration.get("source_kind") != "px4_gazebo_frozen_capture"
        or registration.get("frozen_replay_only") is not True
        or registration.get("algorithm") != ALGORITHM
        or registration.get("capture_sha256") != _sha(capture_bytes)
    ):
        raise ValueError("registration/capture binding mismatch")
    if (
        registration.get("world_frame") != "local_ned_from_gazebo_enu"
        or registration.get("camera_frame") != "optical_right_down_forward"
        or registration.get("invalid_depth_policy") != "nan_with_explicit_mask_no_fill"
    ):
        raise ValueError("unsupported registered frame or invalid-depth contract")
    for key in (
        "live_freshness_established",
        "model_inference_invoked",
        "dispatch_authority_created",
        "physical_execution_invoked",
    ):
        if registration.get(key) is not False:
            raise ValueError("registration exceeds the frozen diagnostic scope")
    for key in (
        "hardware_target_allowed",
        "arm_command_sent",
        "flight_command_sent",
        "model_inference_invoked",
        "dispatch_authority_created",
        "physical_execution_invoked",
    ):
        if capture.get(key) is not False:
            raise ValueError("capture exceeds the diagnostic scope")
    sdf = {
        key: _read_child(
            capture_dir, capture["model_sdf_files"][key], capture["model_sdf_sha256"][key]
        )
        for key in ("airframe", "camera")
    }
    if registration["source_sdf_sha256"] != capture["model_sdf_sha256"]:
        raise ValueError("registration SDF mismatch")
    geometry = audit_camera_sdf(sdf["airframe"], sdf["camera"])
    if len(registration["frames"]) != len(capture["frames"]) or not capture["frames"]:
        raise ValueError("capture and registered frame counts disagree")
    last = None
    previous_stamp = -1
    for index, (source, record) in enumerate(zip(capture["frames"], registration["frames"])):
        stamp = source["simulation_time_ns"]
        if (
            type(stamp) is not int
            or stamp <= previous_stamp
            or record["simulation_time_ns"] != stamp
        ):
            raise ValueError("invalid registered source timestamps")
        previous_stamp = stamp
        stamps = source["source_simulation_timestamps_ns"]
        if (
            source["source_timestamp_match"] != "exact"
            or set(stamps) != {"rgb", "depth", "rgb_info", "depth_info", "pose"}
            or any(type(v) is not int or v != stamp for v in stamps.values())
        ):
            raise ValueError("source messages are not exactly synchronized")
        calibration = record["calibration"]
        if _digest(calibration) != record["calibration_sha256"]:
            raise ValueError("calibration hash mismatch")
        if calibration["source_sdf_sha256"] != capture["model_sdf_sha256"] or not np.allclose(
            calibration["model_from_camera_link"], geometry.model_from_link, atol=1e-10, rtol=0
        ):
            raise ValueError("calibration source geometry mismatch")
        _read_child(capture_dir, f"frame_{index:02d}-pose.pb", source["pose_message_sha256"])
        if record["source_pose_message_sha256"] != source["pose_message_sha256"]:
            raise ValueError("registered source pose binding mismatch")
        native = {}
        poses = {}
        for key in ("rgb", "depth"):
            sensor = source["sensors"][key]
            raw = _read_child(capture_dir, sensor["raw_file"], sensor["raw_sha256"])
            if record["source_raw_sha256"][key] != sensor["raw_sha256"]:
                raise ValueError("registered raw sensor binding mismatch")
            if (
                record["source_camera_info_message_sha256"][key]
                != sensor["camera_info_message_sha256"]
                or not np.allclose(
                    calibration["camera_link_from_sensors"][key],
                    geometry.link_from_sensor[key],
                    atol=1e-10,
                    rtol=0,
                )
            ):
                raise ValueError("registered sensor calibration binding mismatch")
            shape = (sensor["height"], sensor["width"])
            native[key] = np.frombuffer(raw, dtype=np.uint8 if key == "rgb" else "<f4").reshape(
                (*shape, 3) if key == "rgb" else shape
            )
            K = np.asarray(sensor["camera_info"]["intrinsics"]["k"]).reshape(3, 3)
            if not np.array_equal(K, calibration["source_intrinsics"][key]):
                raise ValueError("source intrinsics mismatch")
            poses[key] = optical_pose_local_ned(
                source["vehicle_model_pose"],
                source["reported_camera_link_pose"],
                geometry.link_from_sensor[key],
                expected_model_from_link=geometry.model_from_link,
            )
        _read_child(registration_dir, record["file"], record["sha256"])
        with np.load(registration_dir / record["file"], allow_pickle=False) as archive:
            arrays = {key: archive[key] for key in archive.files}
        width, height = calibration["output_size"]
        native_height, native_width = native["rgb"].shape[:2]
        scale = np.diag([width / native_width, height / native_height, 1.0])
        scale[0, 2], scale[1, 2] = (width / native_width - 1) / 2, (height / native_height - 1) / 2
        expected_K = scale @ np.asarray(calibration["source_intrinsics"]["rgb"])
        expected_transform = np.linalg.inv(poses["rgb"]) @ poses["depth"]
        if not np.allclose(
            calibration["rgb_from_depth_optical"], expected_transform, atol=1e-10, rtol=0
        ):
            raise ValueError("registered relative optical transform mismatch")
        if not np.array_equal(arrays["intrinsics"], expected_K) or not np.array_equal(
            arrays["intrinsics"], calibration["output_intrinsics"]
        ):
            raise ValueError("registered intrinsics mismatch")
        if (
            not np.allclose(arrays["optical_to_local_ned"], poses["rgb"], atol=1e-10, rtol=0)
            or not np.allclose(record["optical_to_local_ned"], poses["rgb"], atol=1e-10, rtol=0)
            or int(arrays["simulation_time_ns"]) != stamp
        ):
            raise ValueError("registered pose or timestamp mismatch")
        depth, mask, native_mask = register_depth_to_rgb(
            native["depth"],
            calibration["source_intrinsics"]["depth"],
            expected_K,
            expected_transform,
            (height, width),
            near_m=geometry.clip["depth"][0],
            far_m=geometry.clip["depth"][1],
        )
        columns = np.floor((np.arange(width) + 0.5) * native_width / width).astype(int)
        rows = np.floor((np.arange(height) + 0.5) * native_height / height).astype(int)
        if (
            not np.array_equal(arrays["rgb"], native["rgb"][rows[:, None], columns])
            or not np.array_equal(arrays["depth_m"], depth, equal_nan=True)
            or not np.array_equal(arrays["valid_mask"], mask)
            or not np.array_equal(arrays["source_depth_m"], native["depth"], equal_nan=True)
            or not np.array_equal(arrays["source_valid_mask"], native_mask)
        ):
            raise ValueError("registered arrays do not reproduce the bound raw source")
        last = arrays
    assert last is not None
    return capture, registration, last


def screen(capture_dir: Path, registration_dir: Path) -> dict[str, Any]:
    capture, registration, arrays = verify_registration(capture_dir, registration_dir)
    boxes, scene_hash = verify_scene(capture, capture_dir)
    transform = arrays["optical_to_local_ned"].copy()
    transform[:3, :] = LOCAL_NED_FROM_ENU.T @ transform[:3, :]
    start = transform[:3, 3]
    vehicle = pose_matrix(capture["frames"][-1]["vehicle_model_pose"])
    yaw = np.arctan2(vehicle[1, 0], vehicle[0, 0])
    rotation = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    margin = np.array([0.35, 0.35, 0.10])
    candidates = []
    for identifier, delta_flu in (
        ("continue_forward", [5.0, 0.0, 0.0]),
        ("replan_left", [0.0, 5.0, 0.0]),
    ):
        end = start + rotation @ np.asarray(delta_flu)
        collisions = []
        for box in boxes:
            center, half = np.asarray(box["center_xyz_m"]), np.asarray(box["size_xyz_m"]) / 2
            if segment_intersects_box(start, end, center - half - margin, center + half + margin):
                collisions.append(box["name"])
        candidates.append(
            {
                "candidate_id": identifier,
                "camera_reference_start_enu_m": start.tolist(),
                "camera_reference_end_enu_m": end.tolist(),
                "delta_body_frd_m": [delta_flu[0], -delta_flu[1], -delta_flu[2]],
                "oracle": {
                    "declared_box_intersection": bool(collisions),
                    "intersected_boxes": collisions,
                    "scope": "declared_static_boxes_only",
                    "flight_outcome_observed": False,
                },
                "depth_baseline": depth_corridor_screen(
                    arrays["depth_m"],
                    arrays["valid_mask"],
                    arrays["intrinsics"],
                    transform,
                    start,
                    end,
                    margin,
                ),
            }
        )
    divergence = (
        len({candidate["oracle"]["declared_box_intersection"] for candidate in candidates}) > 1
    )
    unknown = [
        candidate["candidate_id"]
        for candidate in candidates
        if candidate["depth_baseline"]["classification"] == "unobserved"
    ]
    correctly_detected = [
        candidate["candidate_id"]
        for candidate in candidates
        if candidate["oracle"]["declared_box_intersection"]
        and candidate["depth_baseline"]["classification"] == "observed_blocked"
    ]
    reasons = []
    if not divergence:
        reasons.append("declared_candidates_lack_geometric_outcome_divergence")
    if unknown:
        reasons.append("alternative_space_not_observed_by_the_available_depth")
    if correctly_detected:
        reasons.append("simple_depth_baseline_already_detects_the_declared_obstacle")
    reasons.extend(
        [
            "camera_reference_geometry_is_not_an_airframe_flight_outcome",
            "no_goal_image_or_learned_collision_risk_contract_declared",
        ]
    )
    return {
        "schema_version": "missionos_px4_aerial_candidate_screen.v1",
        "capture_sha256": registration["capture_sha256"],
        "registration_sha256": _sha((registration_dir / "registration.json").read_bytes()),
        "scene_sha256": scene_hash,
        "source_assets_reverified": True,
        "registered_arrays_recomputed_from_raw": True,
        "observed_scene_poses_match_declared_boxes": True,
        "diagnostic_scope": "frozen_camera_reference_static_geometry",
        "camera_corridor_margin_xyz_m": margin.tolist(),
        "airframe_collision_claimed": False,
        "boxes": boxes,
        "candidates": candidates,
        "geometric_oracle_has_candidate_divergence": divergence,
        "unobserved_candidates": unknown,
        "obstacle_candidates_already_detected_by_depth": correctly_detected,
        "gpu_comparison_eligible": False,
        "decision": "no_additional_gpu_test_justified_by_this_screen",
        "decision_reasons": reasons,
        "simulation_invoked": False,
        "model_inference_invoked": False,
        "jev_invoked": False,
        "flight_command_sent": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--registration-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = screen(args.capture_dir, args.registration_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "decision": result["decision"],
                "gpu_comparison_eligible": False,
                "unobserved_candidates": result["unobserved_candidates"],
            }
        )
    )


if __name__ == "__main__":
    main()
