"""Stationary city-view experiment contracts; no flight or cloud authority.

The image gate checks consistency of visible, previously observed structure.
It is not semantic building detection, inferred depth, or free-space proof.
Independent mapped geometry must constrain every subsequently executed segment.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from scripts import ship_anwm
from src.runtime.ship_vla_adapter import decode_action
from src.runtime.yokohama_scene import to_source


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read_asset(root, entry):
    name = entry["file"]
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Invalid capture asset path")
    path = root / name
    if path.is_symlink() or ship_anwm.digest(path) != entry["sha256"]:
        raise ValueError("Capture asset hash mismatch")
    return path.read_bytes()


def load_capture(path, *, appearance=False):
    record = json.loads(path.read_text())
    if (
        record.get("schema_version") != "yokohama_rgbd_history.v1"
        or record.get("startup_indices") != list(range(8))
        or record.get("history_indices") != list(range(8, 24))
        or record.get("future_frames_included") is not False
        or len(record.get("frames", [])) != 24
    ):
        raise ValueError("Unsupported city sensor history")
    stamps = np.array([f["stamp_ns"] for f in record["frames"]], dtype=np.int64)
    if np.max(np.abs(np.diff(stamps) / 1e9 - 0.25)) > 0.004000001:
        raise ValueError("History has gaps or duplicate timestamps")
    rgb, depth, poses = [], [], []
    for frame in record["frames"]:
        assets = {k: read_asset(path.parent, v) for k, v in frame["assets"].items()}
        pose = frame["pose"]
        if abs(pose["sensor_sim_s"] - frame["stamp_ns"] / 1e9) > 0.012:
            raise ValueError("Camera and pose timestamps disagree")
        rgb.append(np.frombuffer(assets["onboard_rgb_raw"], np.uint8).reshape(360, 640, 3))
        d = np.frombuffer(assets["onboard_depth_raw"], "<f4").reshape(360, 640)
        depth.append(np.where(np.isfinite(d) & (d > 0) & (d <= 500), d, 0))
        poses.append(
            ship_anwm.camera_pose(
                {
                    "vehicle_position_enu_m": pose["xyz"],
                    "vehicle_quaternion_wxyz": pose["quat_wxyz"],
                }
            )
        )
    positions = np.array([p[:3, 3] for p in poses])
    if np.max(np.linalg.norm(positions - positions[-1], axis=1)) > 0.5:
        raise ValueError("Vehicle did not hold during camera history")
    if any(
        np.arccos(np.clip((np.trace(p[:3, :3].T @ poses[-1][:3, :3]) - 1) / 2, -1, 1)) > 0.03
        for p in poses
    ):
        raise ValueError("Camera rotated during stationary history")
    fx = 640 / (2 * math.tan(math.pi / 6))
    arrays = dict(
        rgb=np.array(rgb[8:]),
        depth=np.array(depth[8:]),
        poses=np.array(poses[8:]),
        intrinsics=np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]]),
        stamps_ns=stamps[8:],
    )
    if appearance:
        arrays["last_depth_infinite"] = np.isposinf(d)
    return record, arrays


def vla_candidate(text, row):
    bins, (forward, down, yaw) = decode_action(text)
    # Preserve upstream yaw-first semantics. A rotation-only proposal does not
    # meet this experiment's translation requirement; it is never rewritten.
    if abs(yaw) >= 0.25 or not 0.5 <= forward <= 5 or abs(down) > 0.205:
        raise ValueError("VLA did not propose an admissible short level translation")
    heading = row["heading_ned_rad"] + yaw
    # Gazebo's magnetic field / EKF yaw can differ from true ENU. Model pixels
    # must follow their measured camera frame, while PX4 commands retain EKF yaw.
    physical_heading = camera_heading(row) + yaw
    world_delta = [
        forward * math.sin(physical_heading),
        forward * math.cos(physical_heading),
        -down,
    ]
    return dict(
        bins=bins,
        target_world_xyz_m=[round(a + b, 9) for a, b in zip(row["vehicle"]["xyz"], world_delta)],
        target_heading_ned_rad=round(math.remainder(heading, 2 * math.pi), 12),
        target_heading_world_ned_rad=round(math.remainder(physical_heading, 2 * math.pi), 12),
        heading_source="Gazebo_camera_pose_for_geometry_PX4_EKF_for_yaw_command",
        delta_body_frd=[forward * math.cos(yaw), forward * math.sin(yaw), down, yaw],
    )


def camera_heading(row):
    rotation = ship_anwm.rotation(row["vehicle"]["quat_wxyz"])
    return math.atan2(rotation[0, 0], rotation[1, 0])


def geometry_rules(start, target, next_target, config, bundle):
    """Constrain the proposed leg AND its connection to the authored AP route."""
    from shapely.geometry import LineString, shape

    if (
        not np.isfinite([start, target, next_target]).all()
        or not 0.5 <= math.dist(start, target) <= 5.01
        or abs(target[2] - start[2]) > 0.205
    ):
        raise ValueError("Unbounded candidate")
    source = to_source(np.array([start, target, next_target]), config["world"]["frame"])
    features = json.loads((bundle / "collision-footprints.geojson").read_text())["features"]
    distances = []
    for a, b in zip(source, source[1:]):
        relevant = [
            shape(f["geometry"])
            for f in features
            if f["properties"]["zmax"] >= min(a[2], b[2]) - 2
            and f["properties"]["zmin"] <= max(a[2], b[2]) + 2
        ]
        if not relevant:
            raise ValueError("Mapped city envelope missing")
        distances.append(min(LineString([a[:2], b[:2]]).distance(g) for g in relevant))
    if min(distances) <= 2:
        raise ValueError("Mapped building clearance below 2 m")
    return dict(
        allowed=True,
        source="frozen_collision_prisms_not_model_perception",
        margin_m=2,
        start_world_xyz_m=start,
        target_world_xyz_m=target,
        next_target_world_xyz_m=next_target,
        minimum_centerline_clearances_m=distances,
    )


def past_view(arrays, target_pose):
    """Independent z-buffer of past RGBD into the native 480x360/224 crop.

    No service-returned projection, scene mesh or future observation is read.
    Unknown and newly exposed pixels stay unknown, never certified free.
    """
    depth = arrays["depth"][-1]
    k = arrays["intrinsics"]
    v, u = np.indices(depth.shape)
    points = np.stack(
        ((u + 0.5 - k[0, 2]) / k[0, 0] * depth, (v + 0.5 - k[1, 2]) / k[1, 1] * depth, depth),
        axis=-1,
    )
    transform = np.linalg.inv(target_pose) @ arrays["poses"][-1]
    q = points @ transform[:3, :3].T + transform[:3, 3]
    z = q[..., 2]
    valid = (depth > 0) & (z > 0.1)
    x = np.floor(((q[..., 0] / np.maximum(z, 0.1) * k[0, 0] + 320) - 80) * 224 / 480).astype(int)
    y = np.floor((q[..., 1] / np.maximum(z, 0.1) * k[1, 1] + 180) * 224 / 360).astype(int)
    valid &= (x >= 0) & (x < 224) & (y >= 0) & (y < 224)
    ids = np.flatnonzero(valid)
    order = ids[np.argsort(z.flat[ids])[::-1]]
    pixels = y.flat[order] * 224 + x.flat[order]
    # Last (nearest) assignment wins. Unique explicitly avoids duplicate-index
    # assignment semantics varying across NumPy versions.
    _, reverse_indices = np.unique(pixels[::-1], return_index=True)
    chosen = order[::-1][reverse_indices]
    out = np.zeros((224, 224, 3), np.uint8)
    mask = np.zeros((224, 224), bool)
    out[y.flat[chosen], x.flat[chosen]] = arrays["rgb"][-1].reshape(-1, 3)[chosen]
    mask[y.flat[chosen], x.flat[chosen]] = True
    return out, mask


def forecast_consistency(predicted, reference, mask):
    """Frozen absolute image consistency bounds, not generic hazard detection."""
    p = np.asarray(predicted)
    r = np.asarray(reference)
    if (
        p.shape != (224, 224, 3)
        or p.dtype != np.uint8
        or r.shape != p.shape
        or mask.shape != (224, 224)
    ):
        raise ValueError("Unexpected forecast/crop shape")
    # Exclude borders and holes; measure luminance structure regardless of hue.
    valid = mask.copy()
    for dy, dx in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        valid &= np.roll(mask, (dy, dx), (0, 1))
    valid[:4] = valid[-4:] = False
    valid[:, :4] = valid[:, -4:] = False
    pg, rg = p.astype(float).mean(axis=2), r.astype(float).mean(axis=2)

    def edges(gray):
        return (
            np.hypot(
                np.roll(gray, 1, 0) - np.roll(gray, -1, 0),
                np.roll(gray, 1, 1) - np.roll(gray, -1, 1),
            )
            >= 18
        )

    re, pe = edges(rg) & valid, edges(pg) & valid
    near = pe.copy()
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            near |= np.roll(pe, (dy, dx), (0, 1))
    count = int(re.sum())
    coverage = float(valid.mean())
    matched = float((re & near).sum() / max(count, 1))
    mae = float(np.abs(pg[valid] - rg[valid]).mean()) if valid.any() else 255.0
    density = float(pe.sum() / max(int(valid.sum()), 1))
    return dict(
        passed=coverage >= 0.6
        and count >= 100
        and matched >= 0.55
        and mae <= 45
        and density <= 0.35,
        known_pixel_fraction=coverage,
        reference_edge_pixels=count,
        matched_edge_fraction=matched,
        luminance_mae=mae,
        predicted_edge_density=density,
        interpretation="visible_structure_consistency_only_not_free_space_or_collision_prediction",
    )


def write_request(folder, arrays, config, candidate, vla_response_sha256):
    from scripts.yokohama_wam_profile import (
        APPEARANCE_POLICY,
        MOTION_ADAPTER_SHA256,
        MOTION_CONTRACT,
    )

    profile = config.get("decisions", {}).get("wam_profile", "legacy")
    if profile not in {"legacy", "motion-v4"}:
        raise ValueError("Unknown city WAM profile")
    motion = profile == "motion-v4"
    folder.mkdir()
    np.savez_compressed(folder / "history.npz", **arrays)
    request = dict(
        schema_version=MOTION_CONTRACT if motion else "yokohama_anwm_request.v1",
        run_id=config["run_id"],
        world_sha256=config["world"]["world_sha256"],
        plan_sha256=digest(config),
        source_kind="yokohama_rgbd_hold",
        ego_source="Gazebo_model_pose_simulator_ground_truth",
        history_sha256=ship_anwm.digest(folder / "history.npz"),
        delta_frame="body_frd_at_observation",
        num_timesteps=1 if motion else 4,
        nominal_horizon_s=1,
        model_time_alignment_verified=False,
        future_ground_truth_used_for_forecast=False,
        dispatch_allowed=False,
        seed=42,
        diffusion_steps=250,
        candidates=[
            {"id": "hold", "delta": [0, 0, 0, 0]},
            {"id": "vla", "delta": candidate["delta_body_frd"]},
        ],
        vla_response_sha256=vla_response_sha256,
    )
    if motion:
        request.update(adapter_sha256=MOTION_ADAPTER_SHA256, appearance_policy=APPEARANCE_POLICY)
    ship_anwm.write_json(folder / "request.json", request)
    ship_anwm.validate(folder / "request.json")
    return request
