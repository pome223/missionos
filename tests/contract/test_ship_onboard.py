import hashlib
import math

import pytest
from PIL import Image, ImageDraw

from src.runtime.ship_onboard import FX, choose_onboard_action, observe_image


def frame(tmp_path, *, east=5, yaw=0.0, ego_east=0):
    (tmp_path / "rgb").mkdir(exist_ok=True)
    image = Image.new("RGB", (640, 360), (80, 80, 80))
    # Project a mapped face with a yawing camera at a translated aircraft.
    depth = 166 - 70 - 0.25 * math.cos(yaw)
    relative_east = east - ego_east - 0.25 * math.sin(yaw)
    bearing = math.atan2(relative_east, depth) - yaw
    u = 319.5 + FX * math.tan(bearing)
    ImageDraw.Draw(image).rectangle((round(u - 45), 0, round(u + 45), 359), fill=(220, 40, 10))
    marker_depth = 165 - 70 - 0.25 * math.cos(yaw)
    marker_east = -14 - ego_east - 0.25 * math.sin(yaw)
    marker_u = 319.5 + FX * math.tan(math.atan2(marker_east, marker_depth) - yaw)
    ImageDraw.Draw(image).rectangle(
        (round(marker_u - 5), 168, round(marker_u + 5), 191), fill=(10, 210, 230)
    )
    path = tmp_path / "rgb/00001.png"
    image.save(path)
    return {
        "elapsed_s": 10.0,
        "local_ned": [70, ego_east, -30],
        "attitude_q": [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)],
        "onboard_frame": {
            "file": "rgb/00001.png",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "received_age_s": 0.05,
            "sensor_stamp_s": 20.0,
        },
    }


@pytest.mark.parametrize("yaw,ego_east", [(0, 0), (0.08, -2), (-0.1, 3)])
def test_camera_observation_compensates_aircraft_pose(tmp_path, yaw, ego_east):
    row = frame(tmp_path, yaw=yaw, ego_east=ego_east)
    assert observe_image(tmp_path, row, 166)["obstacle_x_m"] == pytest.approx(5, abs=0.2)


@pytest.mark.parametrize("bias", [-0.03, 0.03])
def test_surveyed_marker_corrects_estimator_heading_bias(tmp_path, bias):
    row = frame(tmp_path)
    row["attitude_q"] = [math.cos(bias / 2), 0, 0, math.sin(bias / 2)]
    assert observe_image(tmp_path, row, 166)["obstacle_x_m"] == pytest.approx(5, abs=0.2)


@pytest.mark.parametrize("yaw,ego_east", [(0, 0), (0.08, -2), (-0.1, 3)])
def test_observation_uses_upper_band_when_foreground_covers_lower_edge(tmp_path, yaw, ego_east):
    row = frame(tmp_path, east=24, yaw=yaw, ego_east=ego_east)
    p = tmp_path / row["onboard_frame"]["file"]
    with Image.open(p) as image:
        im = image.copy()
    before = observe_image(tmp_path, row, 166)
    edge = int(before["center_x_px"] + 15)
    ImageDraw.Draw(im).rectangle((edge, 130, 639, 359), fill=(80, 80, 80))
    im.save(p)
    row["onboard_frame"]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    observed = observe_image(tmp_path, row, 166)
    assert observed["center_y_px"] < 130
    assert observed["obstacle_x_m"] == pytest.approx(24, abs=0.2)


@pytest.mark.parametrize("fault", ["hash", "stale", "path", "attitude", "ego", "no_red"])
def test_invalid_camera_input_cannot_propose_flight(tmp_path, fault):
    row = frame(tmp_path)
    if fault == "hash":
        row["onboard_frame"]["sha256"] = "0" * 64
    elif fault == "stale":
        row["onboard_frame"]["received_age_s"] = 0.7
    elif fault == "path":
        row["onboard_frame"]["file"] = "rgb/../secrets.png"
    elif fault == "attitude":
        row["attitude_q"] = [0, 0, 0, 0]
    elif fault == "ego":
        row["local_ned"][1] = float("nan")
    else:
        p = tmp_path / "rgb/00001.png"
        Image.new("RGB", (640, 360)).save(p)
        row["onboard_frame"]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        observe_image(tmp_path, row, 166)


def test_stopping_rule_and_velocity_receive_same_history():
    history = [
        {"observed_at_s": t, "obstacle_x_m": 4 * t - 0.5 * t * t} for t in (0, 0.5, 1, 1.5, 2)
    ]
    assert choose_onboard_action(history, "onboard_velocity")["action"] == "wait"
    assert choose_onboard_action(history, "onboard_stopping")["action"] == "detour"


def test_observation_gap_does_not_become_a_decision():
    history = [{"observed_at_s": t, "obstacle_x_m": 2 * t} for t in (0, 0.5, 4)]
    with pytest.raises(ValueError, match="discontinuous"):
        choose_onboard_action(history, "onboard_velocity")


def test_wind_publish_requires_observed_readback_and_can_retry():
    from scripts.ship_delivery_sitl_worker import apply_wind

    commands, events = [], []
    readbacks = iter(["enable_wind: false", "linear_velocity { y: 4 }\nenable_wind: true"])

    def run(args):
        commands.append(args)
        return next(readbacks) if "service" in args else ""

    apply_wind(run, lambda name, **values: events.append((name, values)), 4, sleep=lambda _: None)
    assert len([c for c in commands if "topic" in c]) == 2
    assert [e[1]["attempt"] for e in events if e[0] == "wind_observed"] == [2]


def test_unobserved_wind_stops_before_urban_dispatch():
    from scripts.ship_delivery_sitl_worker import apply_wind

    events = []
    with pytest.raises(RuntimeError, match="five bounded"):
        apply_wind(
            lambda _: "enable_wind: false",
            lambda name, **kw: events.append(name),
            4,
            sleep=lambda _: None,
        )
    assert events.count("wind_requested") == 5
    assert "wind_observed" not in events
