"""Same-flight composition must reject stale history and unexecuted native chains."""

from copy import deepcopy
import json
import xml.etree.ElementTree as ET

import pytest

from src.runtime.ship_native_integration import CONTRACT, verify_integration
from src.runtime.ship_onboard import add_onboard_camera


def evidence(tmp_path):
    config = dict(
        run_id="run",
        world_sha256="world",
        plan_sha256="plan",
        urban={
            "native_integration": dict(CONTRACT),
            "aerovla_live": {"native": True},
            "anwm_static": {"native": True},
            "policy": "onboard_anwm_static",
            "stationary_obstacle_east_m": 0,
        },
    )
    vla = dict(
        verified=True,
        vla_inference_invoked=True,
        mission_motion_resumed=True,
        native_inference_chain={"verified": True, "request_id": "vla"},
        observed_displacement_m=2.8,
    )
    onboard = {
        "reasons": [],
        "anwm_verification": dict(
            verified=True, wam_invoked=True, maximum_prediction_position_error_m=1.2
        ),
    }
    names = [
        "vla_execution_observed",
        "native_history_started",
        "native_history_captured",
        "urban_decision",
        "anwm_dispatch_revalidated",
        "urban_requested",
        "vla_mission_resumed",
    ]
    events = [dict(event=name, elapsed_s=10 + i, run_id="run") for i, name in enumerate(names)]
    events[1]["source_sample_elapsed_s"] = 10.5
    events[3]["decision"] = dict(
        wam_invoked=True, policy="onboard_anwm_static", anwm={"request_id": "wam"}
    )
    row = dict(
        run_id="run",
        world_sha256="world",
        elapsed_s=10.5,
        nav_state=4,
        arming_state=2,
        onboard_frame={"sensor_stamp_s": 100},
    )
    capture = {k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")}
    capture["frames"] = [
        {"simulation_time_ns": 101_000_000_000 + i * 250_000_000} for i in range(20)
    ]
    (tmp_path / "native-capture").mkdir()
    (tmp_path / "native-capture/capture.json").write_text(json.dumps(capture))
    return config, [row], events, vla, onboard


def test_composes_independent_native_chains_without_dispatch_authority(tmp_path):
    result = verify_integration(tmp_path, *evidence(tmp_path))
    assert result["verified"] and result["new_history_after_vla_handoff"]
    assert result["native_vla_request_id"] == "vla" and result["native_wam_request_id"] == "wam"
    assert not result["physical_execution_invoked"]
    assert CONTRACT["dispatch_allowed"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "fixture",
        "unused_wam",
        "unverified_wam",
        "no_native_chain",
        "cross_run",
        "duplicate",
        "reordered",
        "early_capture",
        "non_loiter",
        "old_history",
        "history_gap",
        "other_plan",
    ],
)
def test_composition_rejects_unproven_or_mixed_chains(tmp_path, fault):
    config, samples, events, vla, onboard = evidence(tmp_path)
    path = tmp_path / "native-capture/capture.json"
    capture = json.loads(path.read_text())
    if fault == "fixture":
        vla["vla_inference_invoked"] = False
    elif fault == "unused_wam":
        events[3]["decision"]["wam_invoked"] = False
    elif fault == "unverified_wam":
        onboard["anwm_verification"]["verified"] = False
    elif fault == "no_native_chain":
        vla["native_inference_chain"] = None
    elif fault == "cross_run":
        events[2]["run_id"] = "another"
    elif fault == "duplicate":
        events.append(deepcopy(events[2]))
    elif fault == "reordered":
        events[2]["elapsed_s"] = 9
    elif fault == "early_capture":
        samples[0]["elapsed_s"] = events[1]["source_sample_elapsed_s"] = 9
    elif fault == "non_loiter":
        samples[0]["nav_state"] = 14
    elif fault == "old_history":
        capture["frames"][0]["simulation_time_ns"] = 99_000_000_000
    elif fault == "history_gap":
        capture["frames"][5]["simulation_time_ns"] += 250_000_000
    elif fault == "other_plan":
        capture["plan_sha256"] = "another"
    path.write_text(json.dumps(capture))
    with pytest.raises(ValueError):
        verify_integration(tmp_path, config, samples, events, vla, onboard)


@pytest.mark.parametrize(
    "vla,capture,executor,expected",
    [
        (True, False, True, 10),
        (False, True, False, 4),
        (True, True, True, 20),
        (False, True, True, 20),
    ],
)
def test_camera_schedule_preserves_exact_model_input_joins(
    tmp_path, vla, capture, executor, expected
):
    for name in ("x500_base", "x500", "worlds"):
        (tmp_path / name).mkdir()
    (tmp_path / "x500_base/model.sdf").write_text(
        '<sdf><model><link name="base_link"/></model></sdf>'
    )
    (tmp_path / "x500/model.sdf").write_text("<sdf><model/></sdf>")
    (tmp_path / "worlds/default.sdf").write_text("<sdf><world/></sdf>")
    add_onboard_camera(
        tmp_path,
        dict(
            aerovla_live=vla,
            capture_anwm=capture,
            vla_executor_contract=executor,
            obstacle_north_m=170,
        ),
    )
    sensors = ET.parse(tmp_path / "x500_base/model.sdf").getroot().findall("model/link/sensor")
    rates = {s.attrib["name"]: int(s.findtext("update_rate")) for s in sensors}
    assert rates["mission_down"] == expected and rates["mission_rgb"] == 10
    if capture:
        assert rates["mission_rgbd"] == 4
        assert expected % 4 == 0
    if vla:
        assert expected % 10 == 0


def test_combined_native_services_are_admitted_but_do_not_start_cloud(monkeypatch, tmp_path):
    from src.runtime.ship_delivery import ShipDeliveryScenario
    from src.runtime.ship_delivery_sitl import run_ship_delivery_sitl

    calls = []
    monkeypatch.setattr(
        "src.runtime.ship_anwm_static.check_service", lambda url: {"identity": "explicit-double"}
    )
    monkeypatch.setattr(
        "src.runtime.ship_aerovla_host.check_service", lambda url: {"identity": "explicit-double"}
    )

    def stop(args, **kwargs):
        calls.append(args)
        raise OSError("test stops before Docker")

    monkeypatch.setattr("src.runtime.ship_delivery_sitl._run", stop)
    result = run_ship_delivery_sitl(
        ShipDeliveryScenario(),
        output_dir=tmp_path / "run",
        operator_approved=True,
        urban_case="static_center",
        urban_policy="onboard_anwm_static",
        aerovla_url="http://127.0.0.1:18117",
        anwm_url="http://127.0.0.1:18118",
    )
    assert calls and result["status"] == "blocked"
    assert any("test stops before Docker" in reason for reason in result["blocking_reasons"])


@pytest.mark.parametrize("failed", [False, True])
def test_wam_releases_gpu_after_success_and_failure(failed):
    from types import SimpleNamespace
    from scripts.ship_anwm_server import NativeModel

    calls = []
    model = NativeModel.__new__(NativeModel)
    model.cpu_between_requests = True
    model.model = SimpleNamespace(to=lambda device: calls.append(("model", device)))
    model.vae = SimpleNamespace(to=lambda device: calls.append(("vae", device)))
    model.torch = SimpleNamespace(
        cuda=SimpleNamespace(empty_cache=lambda: calls.append("released"))
    )

    def predict(*args):
        if failed:
            raise ValueError("explicit inference failure")
        return {"actual": "preserved"}

    model._predict = predict
    if failed:
        with pytest.raises(ValueError, match="explicit inference failure"):
            model.predict(None, None, None)
    else:
        assert model.predict(None, None, None) == {"actual": "preserved"}
    assert calls == [
        ("model", "cuda"),
        ("vae", "cuda"),
        ("model", "cpu"),
        ("vae", "cpu"),
        "released",
    ]


def test_vla_single_request_flushes_receipt_before_process_exit(tmp_path, monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace
    import scripts.ship_aerovla_server as server

    request = {"request_id": "single", "prompt": "explicit fixture", "images_sha256": {}}
    monkeypatch.setattr(server, "decode_request", lambda payload: (request, [], {}))
    model = SimpleNamespace(predict=lambda *args: ({"generated_text": "55 49 49"}, b"fixture"))
    handler = server.make_handler(model, {}, tmp_path, exit_after_request=True).__new__(
        server.make_handler(model, {}, tmp_path, exit_after_request=True)
    )
    handler.connection = SimpleNamespace(settimeout=lambda value: None)
    handler.headers = {"Content-Length": "2"}
    handler.path = "/infer"
    handler.rfile = BytesIO(b"{}")
    handler.wfile = BytesIO()
    handler.send_json = lambda status, value: handler.wfile.write(
        json.dumps({"status": status, "value": value}).encode()
    )
    with pytest.raises(SystemExit) as exit_info:
        handler.do_POST()
    assert exit_info.value.code == 0
    response = json.loads(handler.wfile.getvalue())
    assert response["status"] == 200 and response["value"]["vla_inference_invoked"]
    assert (tmp_path / server.digest(request) / "result.json").is_file()
