"""Live inference admission; test doubles never establish native model execution."""

import base64
import hashlib
from http.server import HTTPServer
from io import BytesIO
import json
import threading

from PIL import Image
import pytest

from test_ship_vla_execution import execution_context
from src.runtime.ship_aerovla_host import connect, process_native_request, request_json
from src.runtime.ship_aerovla_live import make_request, validate_inference
from src.runtime.ship_vla_adapter import (
    bind_proposal,
    content_hash,
    make_envelope,
    snapshot_from_px4,
)
from src.runtime.ship_vla_execution import authorize_candidate, executor_contract


def native_context():
    config, row, _ = execution_context()
    config["goal_north_m"] = 300
    config["urban"]["aerovla_live"] = {"session_id": "fixture_session_not_native"}
    config["urban"]["vla_executor_contract"] = executor_contract(native=True)
    row["onboard_frame"].update(sensor_stamp_s=10, file="rgb/01.png")
    row["onboard_frame"]["paired_down"] = {
        "sensor_stamp_s": 10,
        "sha256": "b" * 64,
        "received_age_s": 0.1,
        "file": "down/01.png",
    }
    request = make_request(config, row, "a" * 32)
    response = {
        "schema_version": "ship_aerovla_live_invocation.v1",
        "request_sha256": content_hash(request),
        "service": config["urban"]["aerovla_live"],
        "vla_inference_invoked": True,
        "dispatch_invoked": False,
        "physical_execution_invoked": False,
        "input_images_sha256": request["images_sha256"],
        "generated_text": "94 49 49</s>",
        "generated_token_ids": [10, 20, 2],
        "pixel_values_shape": [1, 6, 224, 224],
    }
    inference = {
        "request": request,
        "response": response,
        "host_started_s": 50.01,
        "host_completed_s": 50.8,
    }
    proposal = bind_proposal(
        response["generated_text"], snapshot_from_px4(row, config), make_envelope(config)
    )
    return config, row, proposal, inference


def test_native_permit_requires_actual_bound_response_without_changing_guard_authority():
    config, row, proposal, inference = native_context()
    with pytest.raises(ValueError, match="requires_bound_inference"):
        authorize_candidate(config, proposal, row, row, now_s=51)
    permit = authorize_candidate(config, proposal, row, row, now_s=51, inference=inference)
    assert permit["vla_inference_invoked"] and permit["dispatch_allowed"]
    assert not permit["assessment"]["dispatch_allowed"]
    assert permit["candidate"]["target_ned_m"][0] > 74
    assert permit["inference_sha256"] == content_hash(inference)


@pytest.mark.parametrize(
    "kind",
    [
        "old_image",
        "mismatch",
        "session",
        "request_id",
        "no_model",
        "authority",
        "future_clock",
        "down_stamp",
        "old_down",
    ],
)
def test_native_admission_fails_closed(kind):
    config, row, proposal, inference = native_context()
    now = 51.0
    if kind == "old_image":
        now = 53
    elif kind == "mismatch":
        proposal["generated_text"] = "55 47 55"
    elif kind == "session":
        inference["response"]["service"] = {"session_id": "other"}
    elif kind == "request_id":
        inference["request"]["request_id"] = "c" * 32
    elif kind == "no_model":
        inference["response"]["vla_inference_invoked"] = False
    elif kind == "authority":
        inference["response"]["dispatch_invoked"] = True
    elif kind == "future_clock":
        inference["host_completed_s"] = 55
    elif kind == "down_stamp":
        row["onboard_frame"]["paired_down"]["sensor_stamp_s"] = 10.1
    elif kind == "old_down":
        row["onboard_frame"]["paired_down"]["received_age_s"] = 5
    with pytest.raises(ValueError):
        authorize_candidate(config, proposal, row, row, now_s=now, inference=inference)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18117",
        "http://localhost:18117",
        "http://1.2.3.4:18117",
        "http://user:secret@127.0.0.1:18117",
        "http://127.0.0.1:18117/infer",
        "http://127.0.0.1:18117?x=1",
    ],
)
def test_only_explicit_local_tunnel_is_supported(url):
    with pytest.raises(ValueError, match="loopback"):
        connect(url)


def test_http_and_mailbox_runtime_with_explicit_model_double(tmp_path):
    from scripts.ship_aerovla_server import make_handler

    config, row, _, _ = native_context()
    for key, color in (("rgb", "red"), ("down", "blue")):
        folder = tmp_path / key
        folder.mkdir()
        image = Image.new("RGB", (640, 360), color)
        path = folder / "01.png"
        image.save(path)
        frame = row["onboard_frame"] if key == "rgb" else row["onboard_frame"]["paired_down"]
        frame["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    request = make_request(config, row, "a" * 32)
    (tmp_path / "telemetry.jsonl").write_text(json.dumps(row) + "\n")
    (tmp_path / "aerovla-request.json").write_text(
        json.dumps({"request": request, "input_elapsed_s": 50})
    )
    calls = []

    class ExplicitModelDouble:
        def predict(self, prompt, images):
            calls.append((prompt, [im.getpixel((0, 0)) for im in images]))
            buffer = BytesIO()
            Image.new("RGB", (224, 448)).save(buffer, format="PNG")
            return {
                "generated_text": "94 49 49</s>",
                "generated_token_ids": [10, 20, 2],
                "pixel_values_shape": [1, 6, 224, 224],
            }, buffer.getvalue()

    remote = tmp_path / "remote"
    remote.mkdir()
    server = HTTPServer(
        ("127.0.0.1", 0),
        make_handler(ExplicitModelDouble(), config["urban"]["aerovla_live"], remote),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        process_native_request(tmp_path, config, url)
        process_native_request(tmp_path, config, url)
        response = json.loads((tmp_path / "aerovla-response.json").read_text())
        assert "error" not in response and len(calls) == 1
        assert calls[0][1] == [(255, 0, 0), (0, 0, 255)]
        inference = {
            "request": request,
            "response": response["response"],
            "host_started_s": 50.01,
            "host_completed_s": 50.5,
        }
        assert validate_inference(config, row, inference, 51) == "94 49 49</s>"
        payload = {
            "request": request,
            "images_base64": {
                k: base64.b64encode((tmp_path / k / "01.png").read_bytes()).decode()
                for k in ("rgb", "down")
            },
        }
        with pytest.raises(ValueError, match="HTTP 400"):
            request_json(url, "POST", "/infer", payload)
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dual_camera_joins_exact_stamps_and_does_not_regress():
    from types import SimpleNamespace
    import sys
    from scripts import ship_urban_camera_worker

    # Container module has a script-relative import.
    sys.modules.setdefault("ship_urban_camera_worker", ship_urban_camera_worker)
    from scripts.ship_onboard_camera import OnboardCamera

    camera = OnboardCamera.__new__(OnboardCamera)
    camera.dual_view, camera.pairs, camera.latest = True, {}, None
    camera.lock = threading.Lock()

    def message(sec):
        return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nsec=0)))

    camera.receive(message(10))
    camera.receive(message(11), "down")
    assert camera.latest is None
    camera.receive(message(11))
    assert camera.latest[2] == 11 * 10**9
    camera.receive(message(10), "down")
    assert camera.latest[2] == 11 * 10**9


def test_native_and_offline_modes_cannot_be_combined(tmp_path):
    from src.runtime.ship_delivery import ShipDeliveryScenario
    from src.runtime.ship_delivery_sitl import run_ship_delivery_sitl

    with pytest.raises(ValueError, match="separate modes"):
        run_ship_delivery_sitl(
            ShipDeliveryScenario(),
            output_dir=tmp_path / "run",
            operator_approved=True,
            aerovla_url="http://127.0.0.1:18117",
            vla_executor_smoke=True,
        )
