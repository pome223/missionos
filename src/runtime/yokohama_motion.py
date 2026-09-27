"""Reopen real AP motion records; proposed image targets confer no dispatch authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zlib

import numpy as np
from scripts.ship_anwm import action_pose, camera_pose


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rotation_error(a, b):
    return float(np.arccos(np.clip((np.trace(a[:3, :3].T @ b[:3, :3]) - 1) / 2, -1, 1)))


def load_frames(root):
    root = Path(root)
    m = json.loads((root / "motion/manifest.json").read_text())
    raw = (root / "motion/frames.jsonl").read_bytes()
    if m["schema_version"] != "yokohama_ap_motion_capture.v1" or sha(raw) != m["frames_sha256"]:
        raise ValueError("Motion manifest binding mismatch")
    frames = [json.loads(line) for line in raw.splitlines()]
    if len(frames) != m["frames"] or not frames:
        raise ValueError("Empty or incomplete motion capture")
    info = m["camera_info"]
    fx = 640 / (2 * np.tan(np.pi / 6))
    if (info.get("width"), info.get("height")) != (640, 360) or not np.allclose(
        np.asarray(info["k"]).reshape(3, 3), [[fx, 0, 320], [0, fx, 180], [0, 0, 1]], atol=1e-5
    ):
        raise ValueError("Unqualified onboard camera intrinsics")
    total_bytes = 0
    for i, f in enumerate(frames):
        if f["index"] != i or (i and f["stamp_ns"] <= frames[i - 1]["stamp_ns"]):
            raise ValueError("Unordered or duplicate sensor frame")
        if abs(f["pose"]["sensor_sim_s"] - f["stamp_ns"] / 1e9) > 0.012:
            raise ValueError("Motion pose timestamp mismatch")
        if f["phase"] not in m["config"]["phases"]:
            raise ValueError("Unexpected capture phase")
        for key, size in [("onboard_rgb", 3), ("onboard_depth", 4)]:
            a = f["assets"][key]
            if a["file"] != f"{i:05d}-{key}.z" or a["codec"] != (
                "zlib-byteplanes4" if size == 4 else "zlib"
            ):
                raise ValueError("Motion asset role mismatch")
            p = root / "motion" / a["file"]
            if p.is_symlink():
                raise ValueError("Symlinked motion asset")
            packed = p.read_bytes()
            if sha(packed) != a["sha256"] or len(packed) != a["compressed_bytes"]:
                raise ValueError("Motion compressed asset hash mismatch")
            data = zlib.decompress(packed)
            if size == 4:
                if len(data) != 640 * 360 * 4:
                    raise ValueError("Invalid byteplane depth length")
                decoded = bytearray(len(data))
                for j in range(4):
                    decoded[j::4] = data[j * 640 * 360 : (j + 1) * 640 * 360]
                data = bytes(decoded)
            if (
                len(data) != 640 * 360 * size
                or len(data) != a["raw_bytes"]
                or sha(data) != a["raw_sha256"]
            ):
                raise ValueError("Motion raw asset hash mismatch")
            if key == "onboard_depth":
                depth = np.frombuffer(data, "<f4")
                if np.isnan(depth).any() or np.isneginf(depth).any() or (depth < 0).any():
                    raise ValueError("Invalid raw depth values")
            total_bytes += len(packed)
        f["camera_pose"] = camera_pose(
            dict(
                vehicle_position_enu_m=f["pose"]["xyz"],
                vehicle_quaternion_wxyz=f["pose"]["quat_wxyz"],
            )
        )
    if total_bytes != m["compressed_bytes"]:
        raise ValueError("Motion byte inventory mismatch")
    return m, frames


def candidate_windows(frames):
    """Past-only constant-speed/hold candidates; inspect exactly +1 s, never later.

    The fixed AP route was dispatched. These local image prediction candidates
    were not separate AP commands and are not learned-control evidence.
    """
    rows = []
    # Evaluate a fixed 1 Hz grid. Retain every evaluated candidate and rejection.
    for i in range(15, len(frames) - 4, 4):
        history, outcome = frames[i - 15 : i + 1], frames[i + 4]
        all_frames = frames[i - 15 : i + 5]
        stamps = np.array([f["stamp_ns"] for f in all_frames]) / 1e9
        record = dict(
            cutoff_index=i,
            target_index=i + 4,
            phase=frames[i]["phase"],
            input_indices=list(range(i - 15, i + 1)),
            candidate_dispatched=False,
        )
        reason = None
        if (
            len({f["phase"] for f in all_frames}) != 1
            or np.max(np.abs(np.diff(stamps) - 0.25)) > 0.004000001
        ):
            reason = "noncontiguous_or_cross_phase"
        else:
            poses = [f["camera_pose"] for f in history]
            recent = poses[-1][:3, 3] - poses[-4][:3, 3]
            forward = np.r_[poses[-1][:2, 2], 0.0]
            forward /= np.linalg.norm(forward)
            right = np.array([-forward[1], forward[0], 0.0])
            motion = float(np.linalg.norm(poses[-1][:3, 3] - poses[0][:3, 3]))
            record["history_displacement_m"] = motion
            if np.linalg.norm(recent) <= 0.15 and rotation_error(poses[-1], poses[-4]) <= 0.03:
                delta = [0.0, 0.0, 0.0, 0.0]
                action = "hold"
            elif (
                1.95 <= recent @ forward <= 2.55
                and abs(recent @ right) <= 0.15
                and abs(recent[2]) <= 0.10
                and rotation_error(poses[-1], poses[-4]) <= 0.03
            ):
                delta = [3.0, 0.0, 0.0, 0.0]
                action = "forward"
            else:
                reason = "past_motion_outside_fixed_candidate_envelope"
            if reason is None:
                target = action_pose(poses[-1], delta)
                error = float(np.linalg.norm(target[:3, 3] - outcome["camera_pose"][:3, 3]))
                angle = rotation_error(target, outcome["camera_pose"])
                record.update(
                    action=action,
                    delta=delta,
                    requested_camera_pose=target.tolist(),
                    endpoint_position_error_m=error,
                    endpoint_rotation_error_rad=angle,
                    observed_elapsed_sim_s=float(stamps[-1] - stamps[15]),
                )
                if error > 0.30 or angle > 0.05:
                    reason = "requested_view_not_reached_at_fixed_time"
        rows.append(dict(record, qualified=reason is None, reason=reason))
    return rows


def verify(root):
    root = Path(root)
    result = json.loads((root / "result.json").read_text())
    if (
        result["status"] != "passed"
        or not result.get("cleanup")
        or any(
            result.get(k)
            for k in ["vla_invoked", "wam_invoked", "gpu_requested", "physical_execution_invoked"]
        )
    ):
        raise ValueError("Motion run did not finish as a CPU-only AP flight")
    m, frames = load_frames(root)
    candidates = candidate_windows(frames)
    passed = [c for c in candidates if c["qualified"]]
    counts = {
        action: sum(c.get("action") == action for c in passed) for action in ["hold", "forward"]
    }
    if counts["forward"] < 4 or counts["hold"] < 1:
        raise ValueError("Insufficient qualified hold/forward records")
    return dict(
        schema_version="yokohama_motion_verification.v1",
        status="passed",
        run_id=result["run_id"],
        frames=len(frames),
        compressed_bytes=m["compressed_bytes"],
        frames_sha256=m["frames_sha256"],
        sensor_rate_hz=4,
        max_pose_join_s=0.012,
        qualified_counts=counts,
        candidates=candidates,
        interpretation="Data acquisition only; separate SITL verification required; no model inference, learning or dispatch",
        motion_model_flight_qualified=False,
    )
