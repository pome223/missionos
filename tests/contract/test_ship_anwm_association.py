"""Past-only target association, original native regressions and refusal cases."""

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from scripts import ship_anwm as native
from src.runtime.ship_anwm_static import (
    obstacle_interval,
    projected_target_interval,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/ship-anwm-association"
CASES = json.loads((FIXTURES / "manifest.json").read_text())["cases"]


def inputs(case):
    root = FIXTURES / case["case"]
    for name, expected in case["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    with np.load(root / "geometry.npz", allow_pickle=False) as data:
        arrays = dict(data)
    arrays["rgb"] = np.asarray(Image.open(root / "observed.png"))[None]
    return root, arrays


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case"])
@pytest.mark.parametrize("candidate,delta", [("hold", 0), ("right_5m", 5)])
def test_original_native_images_associate_using_only_past_input(case, candidate, delta):
    root, arrays = inputs(case)
    reference = projected_target_interval(arrays, native.lateral_pose(arrays["poses"][-1], delta))
    image = Image.open(root / (candidate + ".png"))
    interval = obstacle_interval(image, reference)
    if case["case"] == "04-full-route-static_center" and candidate == "right_5m":
        with pytest.raises(ValueError, match="ambiguous"):
            obstacle_interval(image)
        assert interval == [104, 147]
    else:
        assert interval == obstacle_interval(image)


def picture(*spans):
    image = Image.new("RGB", (224, 224), (100, 100, 100))
    draw = ImageDraw.Draw(image)
    for lo, hi in spans:
        draw.rectangle((lo, 20, hi, 220), fill=(220, 130, 95))
    return image


def test_unrelated_warm_background_does_not_replace_missing_target():
    assert obstacle_interval(picture((0, 70), (100, 140)), [100, 140]) == [100, 140]
    with pytest.raises(ValueError, match="missing"):
        obstacle_interval(picture((0, 70)), [100, 140])


@pytest.mark.parametrize(
    "spans",
    [
        [],
        [(100, 117), (123, 140)],
        [(0, 140)],
        [(100, 223)],
        [(125, 165)],
        [(110, 120)],
        [(60, 180)],
    ],
)
def test_missing_split_clipped_shifted_shrunken_merged_targets_reject(spans):
    with pytest.raises(ValueError):
        obstacle_interval(picture(*spans), [100, 140])


@pytest.mark.parametrize("reference", [[0, 40], [180, 223], [140, 100], [100, float("nan")]])
def test_unqualified_reference_cannot_rescue_prediction(reference):
    with pytest.raises(ValueError):
        obstacle_interval(picture((100, 140)), reference)


def test_past_depth_is_required_and_candidate_motion_changes_reference():
    _, arrays = inputs(CASES[-1])
    pose = arrays["poses"][-1]
    hold = projected_target_interval(arrays, pose)
    right = projected_target_interval(arrays, native.lateral_pose(pose, 5))
    assert right[0] < hold[0] and right[1] < hold[1]
    arrays["depth"][:] = 0
    with pytest.raises(ValueError, match="depth"):
        projected_target_interval(arrays, pose)
