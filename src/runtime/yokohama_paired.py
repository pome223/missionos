"""Offline image diagnostics; these functions confer no flight authority."""

from __future__ import annotations

import json
import numpy as np

from src.runtime.yokohama_native import read_asset


def evaluation_frames(path, after_sim_s):
    """Reopen hashed measurements with a schema rejected by load_capture."""
    record = json.loads(path.read_text())
    if (
        record.get("schema_version") != "yokohama_rgbd_evaluation.v1"
        or record.get("evaluation_only") is not True
        or record.get("future_frames_included") is not True
        or record.get("history_indices") != []
        or record.get("startup_indices") != []
        or len(record.get("frames", [])) != 24
    ):
        raise ValueError("Expected withheld evaluation capture")
    stamps = np.array([f["stamp_ns"] for f in record["frames"]], dtype=np.int64)
    if (
        not 0 < stamps[0] / 1e9 - after_sim_s <= 0.254000001
        or np.max(np.abs(np.diff(stamps) / 1e9 - 0.25)) > 0.004000001
    ):
        raise ValueError("Evaluation timestamps do not follow the declared cutoff")
    for frame in record["frames"]:
        if abs(frame["pose"]["sensor_sim_s"] - frame["stamp_ns"] / 1e9) > 0.012:
            raise ValueError("Evaluation RGBD and pose are not synchronized")
        assets = {key: read_asset(path.parent, entry) for key, entry in frame["assets"].items()}
        if (
            len(assets.get("onboard_rgb_raw", b"")) != 360 * 640 * 3
            or len(assets.get("onboard_depth_raw", b"")) != 360 * 640 * 4
        ):
            raise ValueError("Evaluation RGBD payload is incomplete")
    return record


def fill_infinite_appearance(
    projected, geometry_known, rgb, raw_depth, intrinsics, source_pose, target_pose
):
    """Test an infinite-distance appearance hypothesis, without adding depth.

    Poses use world NED. Only positive-infinite depth on upward world rays
    (negative world Z) may supply appearance.
    Reverse rotation maps destination rays into the last historical image.
    Known geometry is immutable; disocclusion is not certified free space.
    The returned mask means appearance substituted, never geometry observed.
    """
    h, w = raw_depth.shape
    if (
        projected.shape != (h, w, 3)
        or rgb.shape != projected.shape
        or geometry_known.shape != (h, w)
        or geometry_known.dtype != bool
    ):
        raise ValueError("Appearance projection dimensions/mask mismatch")
    y, x = np.indices((h, w))
    rays = np.stack((x, y, np.ones_like(x)), axis=-1) @ np.linalg.inv(intrinsics).T
    world = rays @ target_pose[:3, :3].T
    source = world @ source_pose[:3, :3]
    image = source @ intrinsics.T
    positive = source[..., 2] > 1e-6
    z = np.maximum(image[..., 2], 1e-6)
    u = np.rint(image[..., 0] / z).astype(int)
    v = np.rint(image[..., 1] / z).astype(int)
    inside = positive & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    u, v = np.clip(u, 0, w - 1), np.clip(v, 0, h - 1)
    replace = inside & (world[..., 2] < 0) & np.isposinf(raw_depth[v, u]) & ~geometry_known
    result = projected.copy()
    result[replace] = rgb[v[replace], u[replace]]
    return result, replace


def paired_metrics(predicted, actual):
    """Absolute appearance errors. Thresholds belong to a frozen protocol."""
    if (
        predicted.shape != (224, 224, 3)
        or actual.shape != predicted.shape
        or predicted.dtype != np.uint8
        or actual.dtype != np.uint8
    ):
        raise ValueError("Expected matched 224 square RGB views")
    error = np.abs(predicted.astype(float) - actual.astype(float))
    return dict(
        rgb_mae=float(error.mean()),
        fraction_pixels_max_channel_error_over_40=float((error.max(axis=2) > 40).mean()),
    )
