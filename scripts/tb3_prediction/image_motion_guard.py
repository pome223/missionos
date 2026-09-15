"""Veto a learned wait proposal that conflicts with observed obstacle edges.

This is a single-red-obstacle, constant-image-translation consistency check,
not a calibrated collision predictor. It never creates a wait proposal.
"""

import math
import statistics


def check_edges(bounds, timestamps, horizon=4.0):
    if (
        len(bounds) != 4
        or len(timestamps) != 4
        or not all(math.isfinite(t) for t in timestamps)
        or any(b <= a for a, b in zip(timestamps, timestamps[1:]))
        or not math.isfinite(horizon)
        or horizon != 4.0
    ):
        raise ValueError("four ordered frames and four-second horizon required")
    unknown = dict(wait_consistent=None, reason="image_motion_not_supported")
    if any(b is None for b in bounds):
        return unknown
    if any(len(b) != 2 or not (0 <= b[0] <= b[1] <= 223) for b in bounds):
        raise ValueError("invalid image edge bounds")
    velocities = []
    for edge in (0, 1):
        # A clipped edge has unknown extent and must not look stationary.
        samples = [(t, b[edge]) for t, b in zip(timestamps, bounds) if 0 < b[edge] < 223]
        if len(samples) < 3 or samples[-1][0] - samples[0][0] < 1.0:
            continue
        mt, mx = (statistics.mean(s[k] for s in samples) for k in (0, 1))
        v = sum((t - mt) * (x - mx) for t, x in samples) / sum((t - mt) ** 2 for t, _ in samples)
        span = max(x for _, x in samples) - min(x for _, x in samples)
        if max(abs(x - (mx + v * (t - mt))) for t, x in samples) > max(4, 0.15 * span):
            return unknown
        velocities.append(v)
    if not velocities or max(velocities) - min(velocities) > max(
        3, 0.3 * max(abs(v) for v in velocities)
    ):
        return unknown
    velocity = statistics.mean(velocities)
    # Both sides of the currently observed silhouette are moved together.
    # The trailing edge must be visible, including for a partially clipped actor.
    if (velocity < 0 and bounds[-1][1] == 223) or (velocity > 0 and bounds[-1][0] == 0):
        return unknown
    left, right = [x + velocity * horizon for x in bounds[-1]]
    overlap = max(0, min(146, right + 3) - max(78, left - 3)) / 68
    return dict(
        wait_consistent=overlap <= 0.06,
        reason="observed_edges_clear" if overlap <= 0.06 else "observed_edges_still_block",
        velocity_px_s=velocity,
        projected_interval_px=[left, right],
        projected_horizontal_overlap=overlap,
        pixel_margin=3,
        model="constant image translation; single red obstacle only",
    )


def check_images(images, timestamps):
    import numpy as np
    from PIL import Image

    bounds = []
    for image in images:
        rgb = np.asarray(
            image.convert("RGB").resize((224, 224), Image.Resampling.NEAREST), dtype=float
        )
        red = (
            (rgb[:, :, 0] > 51)
            & (rgb[:, :, 0] > 1.5 * rgb[:, :, 1])
            & (rgb[:, :, 0] > 1.5 * rgb[:, :, 2])
        )
        columns = np.flatnonzero(red[56:190].mean(axis=0) > 0.25)
        bounds.append([int(columns[0]), int(columns[-1])] if len(columns) else None)
    return dict(check_edges(bounds, timestamps), observed_bounds_px=bounds)
