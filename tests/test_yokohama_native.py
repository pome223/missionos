"""Negative contracts for the city model boundary; native capability is not mocked into proof."""

import json
import math

import numpy as np
import pytest

from scripts.ship_anwm import action_pose
from scripts.yokohama_decision_worker import CityDecisions
from src.runtime.yokohama_native import (
    forecast_consistency,
    load_capture,
    past_view,
    read_asset,
    vla_candidate,
)


def test_native_action_keeps_exact_bins_and_axes():
    row = {
        "vehicle": {"xyz": [10, 20, 30], "quat_wxyz": [1, 0, 0, 0]},
        "heading_ned_rad": math.pi / 2,
    }
    value = vla_candidate("49 49 49</s>", row)
    assert value["target_world_xyz_m"] == [12.5, 20, 30]
    assert value["delta_body_frd"] == [2.5, 0, 0, 0]


def test_ekf_yaw_offset_does_not_rotate_visual_forward_motion():
    row = {"vehicle": {"xyz": [0, 0, 15], "quat_wxyz": [1, 0, 0, 0]}, "heading_ned_rad": 1.3}
    value = vla_candidate("49 49 49", row)
    assert value["target_world_xyz_m"] == [2.5, 0, 15]
    assert value["target_heading_ned_rad"] == 1.3
    assert value["target_heading_world_ned_rad"] == round(math.pi / 2, 12)


@pytest.mark.parametrize("text", ["LAND", "0 49 49", "99 49 49", "49 90 49", "49 49 85", "bad"])
def test_unsuitable_native_output_cannot_be_repaired_into_motion(text):
    with pytest.raises(ValueError):
        vla_candidate(text, {"vehicle": {"xyz": [0, 0, 15]}, "heading_ned_rad": 0})


def test_candidate_pose_maps_camera_forward_right_and_world_down():
    # Optical right=east, down=world down, forward=north in NED.
    pose = np.eye(4)
    pose[:3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    target = action_pose(pose, [2, 1, 0.1, 0.2])
    assert np.allclose(target[:3, 3], [2, 1, 0.1])
    assert np.allclose(target[:3, 2], [math.cos(0.2), math.sin(0.2), 0])
    assert np.array_equal(pose[:3, 3], [0, 0, 0])


@pytest.mark.parametrize("delta", [[np.nan, 0, 0, 0], [6, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
def test_candidate_pose_refuses_unsupported_view(delta):
    with pytest.raises(ValueError):
        action_pose(np.eye(4), delta)


def test_forecast_gate_rejects_blank_noise_missing_geometry():
    image = np.full((224, 224, 3), 180, np.uint8)
    image[30:190, 70:150] = 50
    mask = np.ones((224, 224), bool)
    assert forecast_consistency(image, image, mask)["passed"]
    assert not forecast_consistency(np.zeros_like(image), image, mask)["passed"]
    noise = np.random.default_rng(42).integers(0, 256, image.shape, dtype=np.uint8)
    assert not forecast_consistency(noise, image, mask)["passed"]
    assert not forecast_consistency(image, image, np.zeros_like(mask))["passed"]
    assert not forecast_consistency(image, np.zeros_like(image), mask)["passed"]


def test_capture_rejects_repeated_frames_before_any_model(tmp_path):
    capture = dict(
        schema_version="yokohama_rgbd_history.v1",
        startup_indices=list(range(8)),
        history_indices=list(range(8, 24)),
        future_frames_included=False,
        frames=[{"stamp_ns": 1000}] * 24,
    )
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="gaps or duplicate"):
        load_capture(path)


@pytest.mark.parametrize("name", ["../secret", ".", "/etc/passwd"])
def test_capture_does_not_read_outside_input(tmp_path, name):
    with pytest.raises(ValueError, match="path"):
        read_asset(tmp_path, {"file": name, "sha256": "a" * 64})


def test_tampered_input_rejected(tmp_path):
    (tmp_path / "frame").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_asset(tmp_path, {"file": "frame", "sha256": "a" * 64})


def test_unknown_depth_never_fills_reference():
    arrays = dict(
        rgb=np.ones((1, 360, 640, 3), dtype=np.uint8) * 100,
        depth=np.zeros((1, 360, 640)),
        poses=np.eye(4)[None],
        intrinsics=np.array([[554, 0, 320], [0, 554, 180], [0, 0, 1]]),
    )
    reference, mask = past_view(arrays, np.eye(4))
    assert not mask.any()
    assert not reference.any()


def test_revoked_worker_rejects_response_before_touching_files(tmp_path):
    worker = CityDecisions(tmp_path, {}, lambda: {}, lambda *a, **k: None, lambda: 0)
    worker.closed = True
    with pytest.raises(ValueError, match="inactive"):
        worker.exchange("authorize", {})
    assert not list(tmp_path.iterdir())


def test_hold_rejects_drift_even_if_ap_still_reports_loiter(tmp_path):
    row = dict(
        nav_state=4,
        arming_state=2,
        landed=False,
        position_valid=True,
        battery_fraction=0.9,
        velocity_ned=[0, 0, 0],
        vehicle={"xyz": [1, 0, 10]},
        heading_ned_rad=0,
        reset_counters=[0, 0, 0],
    )
    worker = CityDecisions(tmp_path, {}, lambda: row, lambda *a, **k: None, lambda: 0)
    anchor = dict(row, vehicle={"xyz": [0, 0, 10]})
    with pytest.raises(ValueError, match="hold"):
        worker.held(anchor)


def test_hold_rejects_physical_yaw_drift_hidden_by_estimated_heading(tmp_path):
    anchor = dict(nav_state=4, arming_state=2, landed=False, position_valid=True,
                  battery_fraction=0.9, velocity_ned=[0, 0, 0],
                  vehicle={"xyz": [0, 0, 10], "quat_wxyz": [1, 0, 0, 0]},
                  heading_ned_rad=1.57, reset_counters=[0, 0, 0])
    row = dict(anchor, vehicle={"xyz": [0, 0, 10],
                               "quat_wxyz": [math.cos(.113 / 2), 0, 0, math.sin(.113 / 2)]})
    worker = CityDecisions(tmp_path, {}, lambda: row, lambda *a, **k: None, lambda: 0)
    with pytest.raises(ValueError, match="hold"):
        worker.held(anchor)


@pytest.mark.parametrize("kind", ["vla", "wam"])
@pytest.mark.parametrize("failure", [True, False])
def test_serial_gpu_residency_returns_to_cpu_on_success_and_failure(kind, failure):
    from types import SimpleNamespace
    from scripts.ship_aerovla_server import NativeModel as VLA
    from scripts.ship_anwm_server import NativeModel as WAM

    calls = []
    model = object.__new__(VLA if kind == "vla" else WAM)
    model.cpu_between_requests = True
    model.model = SimpleNamespace(to=lambda device: calls.append(("model", device)))
    model.vae = SimpleNamespace(to=lambda device: calls.append(("vae", device)))
    model.torch = SimpleNamespace(
        cuda=SimpleNamespace(
            empty_cache=lambda: calls.append(("cache", "empty")),
            memory_allocated=lambda: 0,
            synchronize=lambda: calls.append(("cuda", "synchronize")),
        ),
        _C=SimpleNamespace(
            _cuda_clearCublasWorkspaces=lambda: calls.append(("workspace", "clear"))
        ),
    )

    def predict(*args):
        if failure:
            raise RuntimeError("inference failure")
        return ({}, b"image") if kind == "vla" else {}

    model._predict = predict
    if failure:
        with pytest.raises(RuntimeError, match="inference failure"):
            model.predict(*([None, None] if kind == "vla" else [None, None, None]))
    else:
        value = model.predict(*([None, None] if kind == "vla" else [None, None, None]))
        assert (value[0] if kind == "vla" else value)["cuda_allocated_after_request_bytes"] == 0
    assert ("model", "cuda") in calls and ("model", "cpu") in calls
    assert calls[-1] == ("cache", "empty")


def test_native_http_failure_is_not_retried_or_converted_to_a_proposal():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from threading import Thread
    from urllib.error import HTTPError
    from scripts.yokohama_decision_host import exchange

    calls = []

    class Failure(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error":"model unavailable"}')

    server = HTTPServer(("127.0.0.1", 0), Failure)
    thread = Thread(target=server.handle_request)
    thread.start()
    try:
        with pytest.raises(HTTPError):
            exchange(server.server_port, "infer", {"request": "bounded test"}, timeout=1)
    finally:
        thread.join(timeout=2)
        server.server_close()
    assert len(calls) == 1


def test_vla_releases_cyclic_temporaries_before_measuring_cuda_residency():
    import gc
    import weakref
    from types import SimpleNamespace
    from scripts.ship_aerovla_server import NativeModel

    refs, cache_measurements = [], []

    class TensorLifetimeDouble:
        def __init__(self):
            self.cycle = self

    def allocated():
        return sum(ref() is not None for ref in refs) * 9568256

    def predict(*args):
        temporary = TensorLifetimeDouble()
        refs.append(weakref.ref(temporary))
        return {"generated_text": "79 48 0</s>"}, b"image"

    model = object.__new__(NativeModel)
    model.cpu_between_requests = True
    model.model = SimpleNamespace(to=lambda device: None)
    model.torch = SimpleNamespace(
        cuda=SimpleNamespace(
            memory_allocated=allocated,
            synchronize=lambda: None,
            empty_cache=lambda: cache_measurements.append(allocated()),
        ),
        _C=SimpleNamespace(_cuda_clearCublasWorkspaces=lambda: None),
    )
    model._predict = predict
    enabled = gc.isenabled()
    gc.disable()
    try:
        value, _ = model.predict(None, None)
    finally:
        if enabled:
            gc.enable()
    assert value["cuda_allocated_before_gc_bytes"] == 9568256
    assert value["cuda_allocated_after_request_bytes"] == 0
    assert value["gc_collected_objects"] > 0
    assert cache_measurements == [0]
    assert value["generated_text"] == "79 48 0</s>"


def test_cross_cycle_response_file_cannot_authorize_motion(tmp_path):
    from threading import Thread
    import time

    folder = tmp_path / "decisions"
    folder.mkdir()
    row = dict(
        nav_state=4,
        arming_state=2,
        landed=False,
        position_valid=True,
        battery_fraction=0.9,
        velocity_ned=[0, 0, 0],
        vehicle={"xyz": [0, 0, 15], "quat_wxyz": [1, 0, 0, 0]},
        heading_ned_rad=0,
        reset_counters=[0, 0, 0],
    )
    worker = CityDecisions(
        tmp_path, {"run_id": "run"}, lambda: row, lambda *a, **k: None, lambda: 0
    )
    worker.active = True

    def old_response():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (folder / "001-request.json").exists():
                (folder / "001-response.json").write_text(
                    json.dumps(
                        {
                            "run_id": "run",
                            "operation": "authorize",
                            "request_sha256": "0" * 64,
                            "value": {"dispatch_allowed": True},
                        }
                    )
                )
                return
            time.sleep(0.01)

    thread = Thread(target=old_response)
    thread.start()
    try:
        with pytest.raises(ValueError, match="cross-cycle"):
            worker.exchange("authorize", row)
    finally:
        thread.join(timeout=2)
    worker.closed = True
    with pytest.raises(ValueError, match="inactive"):
        worker.exchange("authorize", row)
    assert len(list(folder.glob("*-request.json"))) == 1


def test_cuda_workspace_release_is_synchronized_and_requires_supported_api():
    from types import SimpleNamespace
    from scripts.ship_anwm import clear_cuda_workspaces

    calls = []
    runtime = SimpleNamespace(
        cuda=SimpleNamespace(synchronize=lambda: calls.append("synchronize")),
        _C=SimpleNamespace(_cuda_clearCublasWorkspaces=lambda: calls.append("clear")),
    )
    clear_cuda_workspaces(runtime)
    assert calls == ["synchronize", "clear"]
    with pytest.raises(RuntimeError, match="API unavailable"):
        clear_cuda_workspaces(SimpleNamespace(cuda=runtime.cuda))
    assert calls == ["synchronize", "clear"]


@pytest.mark.parametrize('enu_yaw', [0, .5, -2.35, 3.0])
def test_pinned_gazebo_compass_declination_sign_matches_world_heading(enu_yaw):
    # Gazebo 8.11 NED field is transformed by its ENU pose, then this PX4
    # bridge maps x=-field.y, y=-field.x. A geographic declination added
    # again has the wrong sign; the bounded SITL correction cancels it.
    from scripts.yokohama_decision_worker import physical_heading
    declination = math.radians(-7.634295074001246)
    body_angle = declination - enu_yaw
    sensor_x, sensor_y = -math.sin(body_angle), -math.cos(body_angle)
    estimated = -math.atan2(sensor_y, sensor_x) - declination
    row = {'vehicle': {'quat_wxyz': [math.cos(enu_yaw / 2), 0, 0,
                                      math.sin(enu_yaw / 2)]}}
    assert abs(math.remainder(estimated - physical_heading(row), 2 * math.pi)) < 1e-12
