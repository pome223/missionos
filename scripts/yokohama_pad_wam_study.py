"""Crops, lead readout and metrics for the ANWM lead-forecast study.

The readout finds the scripted lead's orange body by colour. It is validated on
real frames before any native forecast is read with it; it is not a general
aircraft detector.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

# Original 640x360 pixel boxes, resized to the native 224x224 input.
CROPS = {
    # The earlier regional study's crop (lead body ~16x7 px after resizing).
    "wide": (256, 48, 584, 328),
    # Covers the lead within ~8 m of the pad from the ground to 7 m (~32x13 px).
    "tight": (310, 80, 480, 250),
}
OFFSETS = (4, 12)  # 4 Hz: one and three seconds ahead
HISTORY = 16


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def crop(rgb, name):
    box = CROPS[name]
    return np.asarray(Image.fromarray(rgb).crop(box).resize((224, 224), Image.Resampling.BILINEAR))


def to_original(xy, name):
    """Crop-space (224 px) point back to original 640x360 pixels."""
    x0, y0, x1, y1 = CROPS[name]
    return [x0 + xy[0] * (x1 - x0) / 224, y0 + xy[1] * (y1 - y0) / 224]


def lead_mask(rgb):
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    return (r > 150) & (g > 60) & (g < 200) & (b < 90) & (r - b > 110)


def detect_lead(rgb, name, min_pixels=6):
    """Largest orange blob in a crop, reported in original-image pixels."""
    mask = lead_mask(np.asarray(rgb))
    if int(mask.sum()) < min_pixels:
        return dict(present=False, pixels=int(mask.sum()))
    ys, xs = np.nonzero(mask)
    # Keep the blob around the densest column band; stray orange texture is spread out.
    cx, cy = float(np.median(xs)), float(np.median(ys))
    near = (np.abs(xs - cx) <= 24) & (np.abs(ys - cy) <= 16)
    xs, ys = xs[near], ys[near]
    if len(xs) < min_pixels:
        return dict(present=False, pixels=int(mask.sum()))
    center = to_original([float(xs.mean()), float(ys.mean())], name)
    return dict(
        present=True,
        pixels=int(len(xs)),
        stray_pixels=int(mask.sum()) - int(len(xs)),
        center_px=[round(center[0], 2), round(center[1], 2)],
    )


def displacement(a, b):
    if not (a["present"] and b["present"]):
        return None
    return float(
        np.hypot(b["center_px"][0] - a["center_px"][0], b["center_px"][1] - a["center_px"][1])
    )
