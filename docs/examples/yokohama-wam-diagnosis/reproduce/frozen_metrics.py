"""Verbatim metrics from source fa0d0d2; no changed thresholds."""
import numpy as np

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
