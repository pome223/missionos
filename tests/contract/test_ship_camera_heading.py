"""Qualify mapped-marker yaw correction independently of the route selector."""

import hashlib
import itertools
import math

import pytest
from PIL import Image, ImageDraw

from src.runtime.ship_onboard import FX, observe_image


def render_observation(root, yaw, estimator_bias, ego_east, obstacle_east):
    # Independent pinhole rendering at surveyed points, not obstacle truth
    # passed into observe_image. The decoder receives RGB, ego and map only.
    (root / "rgb").mkdir(exist_ok=True)
    im = Image.new("RGB", (640, 360), (80, 80, 80))

    def project(east, north):
        bearing = (
            math.atan2(east - ego_east - 0.25 * math.sin(yaw), north - 70 - 0.25 * math.cos(yaw))
            - yaw
        )
        return 319.5 + FX * math.tan(bearing)

    obstacle = project(obstacle_east, 166)
    marker = project(-14, 165)
    draw = ImageDraw.Draw(im)
    draw.rectangle((round(obstacle - 45), 0, round(obstacle + 45), 359), fill=(220, 40, 10))
    draw.rectangle((round(marker - 5), 168, round(marker + 5), 191), fill=(10, 210, 230))
    path = root / "rgb/frame.png"
    im.save(path)
    estimated_yaw = yaw + estimator_bias
    return {
        "elapsed_s": 10.0,
        "local_ned": [70, ego_east, -30],
        "attitude_q": [math.cos(estimated_yaw / 2), 0, 0, math.sin(estimated_yaw / 2)],
        "onboard_frame": {
            "file": "rgb/frame.png",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "received_age_s": 0.05,
            "sensor_stamp_s": 20.0,
        },
    }


def test_rendered_heading_grid_stays_inside_declared_position_error(tmp_path):
    errors = []
    for yaw, bias, ego, obstacle in itertools.product(
        (-0.1, -0.05, 0, 0.05, 0.1),
        (-0.14, -0.10, -0.06, 0, 0.06, 0.1, 0.14),
        (-2, 0, 2),
        (-2, 0, 6, 12, 16),
    ):
        row = render_observation(tmp_path, yaw, bias, ego, obstacle)
        observed = observe_image(tmp_path, row, 166)
        errors.append(abs(observed["obstacle_x_m"] - obstacle))
    assert len(errors) == 525
    assert max(errors) < 0.3


@pytest.mark.parametrize("bias", [-0.2, 0.2])
def test_heading_outside_qualified_envelope_still_stops(tmp_path, bias):
    with pytest.raises(ValueError, match="calibration envelope"):
        observe_image(tmp_path, render_observation(tmp_path, 0, bias, 0, 5), 166)


@pytest.mark.parametrize("fault", ["stale", "bad_hash", "no_marker"])
def test_wider_heading_envelope_does_not_relax_sensor_checks(tmp_path, fault):
    row = render_observation(tmp_path, 0, 0.1, 0, 5)
    if fault == "stale":
        row["onboard_frame"]["received_age_s"] = 1
    elif fault == "bad_hash":
        row["onboard_frame"]["sha256"] = "0" * 64
    else:
        path = tmp_path / "rgb/frame.png"
        with Image.open(path) as im:
            draw = ImageDraw.Draw(im)
            draw.rectangle((0, 168, 260, 191), fill=(80, 80, 80))
            im.save(path)
        row["onboard_frame"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        observe_image(tmp_path, row, 166)
