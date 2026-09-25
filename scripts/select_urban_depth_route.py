"""CPU occupied-surface baseline with a strict partial-map input whitelist.

Unknown depth/space is not certified free. Routes are hypotheses from the prior
map; a separate, common safety filter may reject the frozen choice.
"""

from __future__ import annotations

import hashlib
import json
import math

from scripts.urban_navigation_contract import segment_box_clearance, digest

INPUT_KEYS = {
    "schema_version",
    "routes",
    "goal_enu_m",
    "map_boxes",
    "airframe_radius_m",
    "required_clearance_m",
    "geofence_lower_enu_m",
    "geofence_upper_enu_m",
    "observation_enu_m",
}


def depth_clouds(history):
    """Validate native calibration/pose/raw depth; emit observed surfaces only."""
    import numpy as np
    from src.prediction.px4_camera import (
        LOCAL_NED_FROM_ENU,
        pose_matrix,
        audit_camera_sdf,
        _camera_K,
        optical_pose_local_ned,
    )
    from scripts.prepare_px4_anwm_input import read_bound, last_frame_observation_time
    from scripts.urban_headroom_contract import PROTOCOL

    capture = json.loads((history / "capture.json").read_text())
    if capture["capture_scope"] != "px4_sitl_airborne_observation":
        raise ValueError("airborne observation required")
    sdf = {
        k: read_bound(
            history, capture["model_sdf_files"][k], capture["model_sdf_sha256"][k]
        )
        for k in ("airframe", "camera")
    }
    geometry = audit_camera_sdf(sdf["airframe"], sdf["camera"])
    frames = capture["frames"]
    if len(frames) != PROTOCOL["history_frames"]:
        raise ValueError("sixteen independent frames required")
    stamps = [f["simulation_time_ns"] for f in frames]
    if any(abs(b - a - 250000000) > 4000000 for a, b in zip(stamps, stamps[1:])):
        raise ValueError("history cadence differs")
    clouds = []
    stride = PROTOCOL["depth_pixel_stride"]
    voxel = PROTOCOL["occupied_voxel_m"]
    for index, f in enumerate(frames):
        if set(f["source_simulation_timestamps_ns"].values()) != {
            f["simulation_time_ns"]
        }:
            raise ValueError("unaligned depth/pose source")
        read_bound(history, f"frame_{index:02d}-pose.pb", f["pose_message_sha256"])
        sensor = f["sensors"]["depth"]
        raw = read_bound(history, sensor["raw_file"], sensor["raw_sha256"])
        if sensor["pixel_format"] != "R_FLOAT32" or sensor["depth_units"] != "metres":
            raise ValueError("unexpected depth contract")
        depth = np.frombuffer(raw, dtype="<f4").reshape(
            sensor["height"], sensor["width"]
        )
        K = _camera_K(sensor, "depth", geometry, f["simulation_time_ns"])
        z = depth[::stride, ::stride].ravel()
        v, u = np.mgrid[0 : sensor["height"] : stride, 0 : sensor["width"] : stride]
        near, far = geometry.clip["depth"]
        valid = np.isfinite(z) & (z >= near) & (z < far)
        z = z[valid]
        optical = np.stack(
            (
                (u.ravel()[valid] - K[0, 2]) * z / K[0, 0],
                (v.ravel()[valid] - K[1, 2]) * z / K[1, 1],
                z,
            ),
            axis=1,
        )
        pose = optical_pose_local_ned(
            f["vehicle_model_pose"],
            f["reported_camera_link_pose"],
            geometry.link_from_sensor["depth"],
            expected_model_from_link=geometry.model_from_link,
        )
        world = (optical @ pose[:3, :3].T + pose[:3, 3]) @ LOCAL_NED_FROM_ENU
        # Occupied voxel centres with half-diagonal padding in the scorer.
        cells = np.unique(np.floor(world / voxel).astype("int64"), axis=0)
        clouds.append((cells + 0.5) * voxel)
    merged = np.unique(np.concatenate(clouds), axis=0)
    _, timestamp = last_frame_observation_time(capture)
    pose = pose_matrix(frames[-1]["vehicle_model_pose"])
    q = frames[-1]["vehicle_model_pose"]["orientation"]
    # ENU yaw -> NED heading, matching the flight controller.
    yaw_enu = math.atan2(
        2 * (q["w"] * q.get("z", 0) + q.get("x", 0) * q.get("y", 0)),
        1 - 2 * (q.get("y", 0) ** 2 + q.get("z", 0) ** 2),
    )
    receipt = {
        "observation_unix_s": timestamp,
        "observation_enu_m": pose[:3, 3].tolist(),
        "observation_yaw_ned_rad": math.pi / 2 - yaw_enu,
        "capture_sha256": hashlib.sha256(
            (history / "capture.json").read_bytes()
        ).hexdigest(),
        "input_frame_simulation_time_ns": stamps,
        "latest_occupied_voxels": len(clouds[-1]),
        "history_occupied_voxels": len(merged),
        "uses_scene_truth_for_selection": False,
        "unknown_space_certified_free": False,
    }
    return clouds[-1], merged, receipt


def rank_routes(planner, occupied):
    import numpy as np
    from scripts.urban_headroom_contract import PROTOCOL

    if (
        set(planner) != INPUT_KEYS
        or planner["schema_version"] != "urban_partial_map_planner_input.v1"
    ):
        raise ValueError("planner input whitelist differs")
    cloud = np.asarray(occupied, dtype=float).reshape(-1, 3)
    if not np.isfinite(cloud).all():
        raise ValueError("nonfinite observed surface")
    scores = {}
    for key, route in sorted(planner["routes"].items()):
        current = np.asarray(planner["observation_enu_m"])
        length, nearest, map_clear = 0.0, math.inf, math.inf
        for target in route:
            target = np.asarray(target, dtype=float)
            if not np.isfinite(target).all() or any(
                x < lo or x > hi
                for x, lo, hi in zip(
                    target,
                    planner["geofence_lower_enu_m"],
                    planner["geofence_upper_enu_m"],
                )
            ):
                raise ValueError("invalid route waypoint")
            delta = target - current
            denom = float(delta @ delta)
            if len(cloud):
                fraction = np.clip((cloud - current) @ delta / max(denom, 1e-12), 0, 1)
                nearest = min(
                    nearest,
                    float(
                        np.linalg.norm(
                            cloud - current - fraction[:, None] * delta, axis=1
                        ).min()
                    ),
                )
            for box in planner["map_boxes"]:
                map_clear = min(
                    map_clear,
                    segment_box_clearance(
                        current.tolist(),
                        target.tolist(),
                        box["lower_enu_m"],
                        box["upper_enu_m"],
                        planner["airframe_radius_m"],
                    ),
                )
            length += math.sqrt(denom)
            current = target
        threshold = (
            planner["airframe_radius_m"]
            + planner["required_clearance_m"]
            + PROTOCOL["surface_discretization_margin_m"]
        )
        scores[key] = {
            "route_length_m": length,
            "minimum_observed_surface_distance_m": (
                nearest if math.isfinite(nearest) else None
            ),
            "prior_map_envelope_clearance_m": (
                map_clear if math.isfinite(map_clear) else None
            ),
            "rejected_by_observed_surface": nearest < threshold,
            "rejected_by_available_map": map_clear < planner["required_clearance_m"],
        }
    eligible = [
        k
        for k, s in scores.items()
        if not s["rejected_by_observed_surface"] and not s["rejected_by_available_map"]
    ]
    return {
        "route_id": (
            min(eligible, key=lambda k: (scores[k]["route_length_m"], k))
            if eligible
            else None
        ),
        "scores": scores,
    }


def select(root, geometry):
    import numpy as np

    latest, history, receipt = depth_clouds(root / "session/history")
    planner = {**geometry, "observation_enu_m": receipt["observation_enu_m"]}
    choices = {
        name: rank_routes(planner, cloud)
        for name, cloud in (
            ("stale_map", []),
            ("latest_depth", latest),
            ("history_depth", history),
        )
    }
    input_dir = root / "planner-input"
    input_dir.mkdir()
    (input_dir / "input.json").write_text(
        json.dumps(planner, indent=2, allow_nan=False) + "\n"
    )
    np.savez_compressed(
        input_dir / "observed-surfaces.npz", latest=latest, history=history
    )
    return {
        **receipt,
        "schema_version": "urban_depth_route_selection.v1",
        "selector": "history_depth",
        "route_id": choices["history_depth"]["route_id"],
        "choices": choices,
        "planner_input_sha256": digest(planner),
        "surface_file_sha256": hashlib.sha256(
            (input_dir / "observed-surfaces.npz").read_bytes()
        ).hexdigest(),
        "model_forecast_used_for_dispatch": False,
        "model_invoked": False,
    }
