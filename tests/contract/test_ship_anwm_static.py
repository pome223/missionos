"""Absolute native-view qualification, authority boundaries and real HTTP IO."""

import base64
import json
from http.server import HTTPServer
import threading
import urllib.request

from PIL import Image, ImageDraw
import pytest
from click.testing import CliRunner
from missionos_cli.cli import missionos
from scripts import ship_anwm as native
from scripts.ship_anwm_server import make_handler
from src.runtime.ship_anwm_static import endpoint, interpret, obstacle_interval
from src.runtime.ship_urban_world import configure_urban
from src.runtime.ship_delivery import ShipDeliveryScenario


def picture():
    im = Image.new("RGB", (224, 224), (100, 100, 100))
    ImageDraw.Draw(im).rectangle((100, 20, 140, 220), fill=(220, 60, 20))
    return im


def test_major_object_ignores_small_red_texture():
    im = picture()
    ImageDraw.Draw(im).rectangle((0, 110, 12, 115), fill=(220, 60, 20))
    assert obstacle_interval(im) == [100, 140]


@pytest.mark.parametrize("color", [(220, 60, 20), (220, 130, 95), (200, 160, 145)])
def test_native_orange_palette_keeps_position_and_rejects_ambiguous_objects(color):
    im = Image.new("RGB", (224, 224), (170, 170, 170))
    draw = ImageDraw.Draw(im)
    draw.rectangle((85, 20, 135, 220), fill=color)
    assert obstacle_interval(im) == [85, 135]
    draw.rectangle((15, 20, 65, 220), fill=color)
    with pytest.raises(ValueError, match="ambiguous"):
        obstacle_interval(im)


@pytest.mark.parametrize("color", [(160, 160, 160), (70, 150, 80), (100, 120, 210)])
def test_non_warm_geometry_is_not_a_qualified_prediction(color):
    im = Image.new("RGB", (224, 224), (170, 170, 170))
    ImageDraw.Draw(im).rectangle((85, 20, 135, 220), fill=color)
    with pytest.raises(ValueError, match="missing"):
        obstacle_interval(im)


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "clipped"])
def test_unusable_forecast_is_not_clear_space(fault):
    im = picture()
    d = ImageDraw.Draw(im)
    if fault == "missing":
        d.rectangle((0, 0, 223, 223), fill=(100, 100, 100))
    if fault == "ambiguous":
        d.rectangle((40, 20, 80, 220), fill=(220, 60, 20))
    if fault == "clipped":
        d.rectangle((0, 20, 140, 220), fill=(220, 60, 20))
    with pytest.raises(ValueError):
        obstacle_interval(im)


def test_native_views_choose_routes_without_gaining_authority():
    blocked = interpret([{"obstacle_x_m": 0.5}, {"obstacle_x_m": 1}], 0, 42)
    clear = interpret([{"obstacle_x_m": 23}, {"obstacle_x_m": 24}], 24, 42)
    assert blocked["action"] == "detour" and clear["action"] == "wait"
    assert clear["wam_invoked"] and not clear["dispatch_allowed"]


@pytest.mark.parametrize(
    "xs,current,elapsed",
    [([0, 6], 3, 42), ([0, 0], 6, 42), ([24, 24], 24, 76), ([float("nan"), 0], 0, 42)],
)
def test_absolute_bounds_fail_closed(xs, current, elapsed):
    with pytest.raises(ValueError):
        interpret([{"obstacle_x_m": x} for x in xs], current, elapsed)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18118",
        "http://localhost:18118",
        "http://example.com:18118",
        "http://x@127.0.0.1:18118",
        "http://127.0.0.1:18118/infer",
    ],
)
def test_only_reviewed_loopback_transport(url):
    with pytest.raises(ValueError):
        endpoint(url)


def test_static_case_needs_explicit_simulator_and_model_opt_in(tmp_path):
    args = [
        "ship-delivery",
        "run-sitl",
        "--urban-case",
        "static_center",
        "--urban-policy",
        "onboard_anwm_static",
        "--output-dir",
        str(tmp_path / "new"),
    ]
    result = CliRunner().invoke(missionos, args)
    assert result.exit_code != 0 and "approve-sitl" in result.output
    result = CliRunner().invoke(missionos, [*args, "--approve-sitl"])
    assert result.exit_code != 0 and "--anwm-url" in result.output
    assert not (tmp_path / "new").exists()


def test_static_world_is_explicit():
    for name, x in [("static_center", 0), ("static_near", 8), ("static_clear", 24)]:
        config = configure_urban(ShipDeliveryScenario(), name, "onboard_anwm_static")
        assert config["stationary_obstacle_east_m"] == x


def test_real_http_service_with_explicit_double(tmp_path):
    # Exercises the production request decoder and response path, not CUDA quality.
    from test_ship_anwm import native_request

    path, _, request = native_request.__wrapped__(tmp_path)

    class ExplicitDouble:
        def predict(self, request, arrays, output):
            return {"forecasts": [], "fixture": True}

    remote = tmp_path / "remote"
    remote.mkdir()
    service = HTTPServer(
        ("127.0.0.1", 0), make_handler(ExplicitDouble(), {"fixture": True}, remote)
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {
            "request_id": "1" * 32,
            "request_base64": base64.b64encode(path.read_bytes()).decode(),
            "history_base64": base64.b64encode((tmp_path / "history.npz").read_bytes()).decode(),
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{service.server_port}/infer",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            result = json.load(r)
        assert result["request_sha256"] == native.digest(path)
        assert result["request_id"] == "1" * 32 and not result["dispatch_allowed"]
        assert result["fixture"] is True
        assert result["wam_inference_invoked"] is False
        assert (remote / ("1" * 32) / "response.json").is_file()
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=3)
