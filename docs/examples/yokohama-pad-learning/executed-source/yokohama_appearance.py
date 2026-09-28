"""Past-only appearance conditioning; substituted pixels are never metric geometry."""

import numpy as np


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
