"""Image-only observation and bounded motion baselines for a fixed entry camera.

This perception screen is not an aircraft controller. No case identity, actor
pose, future frame or clearance time is accepted by the policy interface.
"""

from __future__ import annotations

import math
from pathlib import Path


CAMERA = {"width": 640, "height": 360, "horizontal_fov": 1.0471975512}
POLICIES = ("always_wait", "always_detour", "constant_velocity", "stopping_aware")
EXTRA_DETOUR_S = 170 / 12 + 8
# Camera at y=70; the near face of the 8m-deep obstacle is y=166.
# This is a known static calibration, not an observed or estimated depth.
FOCAL_PX = CAMERA["width"] / (2 * math.tan(CAMERA["horizontal_fov"] / 2))
CLEARANCE_PX = CAMERA["width"] / 2 + FOCAL_PX * 13 / 96


def obstacle_pixels(path: Path) -> dict:
    """Segment the one red synthetic obstruction; reject missing/ambiguous blobs.

    A deliberately simple color baseline, not general object recognition.
    Image content is the only position input.
    """
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.mode != "RGB" or image.size != (CAMERA["width"], CAMERA["height"]):
            raise ValueError("Expected a calibrated 640x360 RGB image")
        rgb = np.asarray(image, dtype=np.int16)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (red > 80) & (red > green * 1.6) & (red > blue * 1.6)
    ys, xs = np.where(mask)
    if len(xs) < 200:
        raise ValueError("Obstacle not visible")
    left, right, top, bottom = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    if left == 0 or right == CAMERA["width"] - 1 or top == 0 or bottom == CAMERA["height"] - 1:
        raise ValueError("Clipped obstacle cannot establish a center")
    if len(xs) / ((right - left + 1) * (bottom - top + 1)) < 0.9:
        raise ValueError("Ambiguous or occluded red obstacle")
    return {"center_x_px": (left + right) / 2, "bbox_px": [left, top, right, bottom]}


def choose_camera_action(history, policy):
    """Compare matched image histories, keeping the stronger simple baseline.

    Stopping-aware uses three observed secant knots, a conservative
    two-pixel deadband and the predicted stopping distance. It never resumes
    motion using hidden knowledge of the scripted obstacle.
    """
    if policy not in POLICIES:
        raise ValueError("Unsupported camera policy")
    if not isinstance(history, list) or not 5 <= len(history) <= 8:
        raise ValueError("Five to eight observed image positions required")
    for row in history:
        if set(row) != {"observed_at_s", "center_x_px"} or not all(
            type(v) in (int, float) and math.isfinite(v) for v in row.values()
        ):
            raise ValueError("Only finite observed image time and center belong in policy input")
        if row["observed_at_s"] < 0 or not 0 < row["center_x_px"] < CAMERA["width"] - 1:
            raise ValueError("Observation outside calibrated image or clock")
    gaps = [b["observed_at_s"] - a["observed_at_s"] for a, b in zip(history, history[1:])]
    span = history[-1]["observed_at_s"] - history[0]["observed_at_s"]
    if any(not 0.3 <= dt <= 0.7 for dt in gaps) or not 1.8 <= span <= 3.6:
        raise ValueError("Missing, duplicated or discontinuous camera frames")
    first, middle, last = history[0], history[len(history) // 2], history[-1]
    early_dt = middle["observed_at_s"] - first["observed_at_s"]
    late_dt = last["observed_at_s"] - middle["observed_at_s"]
    early = (middle["center_x_px"] - first["center_x_px"]) / early_dt
    late = (last["center_x_px"] - middle["center_x_px"]) / late_dt
    acceleration = (late - early) / ((early_dt + late_dt) / 2)
    velocity = (last["center_x_px"] - first["center_x_px"]) / span
    remaining = max(0.0, CLEARANCE_PX - last["center_x_px"])
    wait_s = remaining / velocity if velocity > 0.5 else (0.0 if remaining == 0 else None)
    # Require curvature greater than two pixels over each half-window.
    braking = acceleration < -2 / min(early_dt, late_dt) ** 2
    final_velocity = max(0.0, late + acceleration * late_dt / 2)
    stop_px = last["center_x_px"] + final_velocity**2 / (-2 * acceleration) if braking else None
    action = "wait" if wait_s is not None and wait_s <= EXTRA_DETOUR_S else "detour"
    if (
        policy == "stopping_aware"
        and remaining > 0
        and stop_px is not None
        and stop_px < CLEARANCE_PX + 2
    ):
        action = "detour"
    if policy in ("always_wait", "always_detour"):
        action = policy.removeprefix("always_")
    return {
        "policy": policy,
        "action_proposal": action,
        "estimated_velocity_px_s": velocity,
        "estimated_acceleration_px_s2": acceleration,
        "estimated_stop_center_px": stop_px,
        "estimated_remaining_wait_s": wait_s,
        "estimated_extra_detour_s": EXTRA_DETOUR_S,
        "observation_source": "rgb_color_track_fixed_camera",
        "vla_invoked": False,
        "wam_invoked": False,
        "dispatch_authorized": False,
    }
