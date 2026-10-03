"""Native wire contracts through explicit CPU doubles confer no flight authority."""

from contextlib import contextmanager
from copy import deepcopy
import base64
from hashlib import sha256
from http.server import HTTPServer
from io import BytesIO
import json
import math
from pathlib import Path
import threading

import numpy as np
from PIL import Image
import pytest

from scripts import ship_aerovla, ship_anwm
from scripts.ship_aerovla_server import make_handler as vla_handler
from scripts.ship_anwm_server import make_handler as wam_handler
from src.runtime.yokohama_native import past_view
from src.runtime.yokohama_native_shadow import preflight, run_shadow


REPO = Path(__file__).resolve().parents[1]
SOURCE_NAMES = (
    "ship_aerovla_server.py", "ship_aerovla.py", "ship_anwm.py",
    "ship_anwm_server.py", "yokohama_appearance.py", "yokohama_wam_profile.py",
)


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False))


def png_bytes(image):
    buffer = BytesIO()
    Image.fromarray(image).save(buffer, format="PNG")
    return buffer.getvalue()


def identities(source):
    vla = dict(
        schema_version="ship_aerovla_service.v1", session_id="vla-shadow-fixture",
        fixture=True, base_revision=ship_aerovla.BASE_REVISION,
        adapter_revision=ship_aerovla.ADAPTER_REVISION,
        adapter_sha256=ship_aerovla.ADAPTER_SHA256,
        upstream_revision=ship_aerovla.UPSTREAM_REVISION,
        weights_sha256=ship_aerovla.WEIGHTS, custom_code_sha256=ship_aerovla.CUSTOM_CODE,
        runtime_sha256={name: source[name] for name in SOURCE_NAMES[:3]},
        cpu_between_requests=True, exit_after_request=False,
        dispatch_capability=False, short_segment_flight=True,
        inspection_level_flight=True, compact_city_flight=False,
        yaw_bin_range=[38, 60], vertical_bin_range=[47, 51], forward_bin_range=[0, 98],
        hold_bin_allowed=True, terminal_proposal_allowed=True,
        decoding_policy="aerovla_action_grammar.v1", warmup_completed=True,
        gpu="explicit-cpu-double", torch_version="not-loaded", load_seconds=0,
    )
    wam = dict(
        schema_version="ship_anwm_static_service.v1", session_id="wam-shadow-fixture",
        fixture=True, model_revision=ship_anwm.MODEL_REVISION,
        checkpoint_sha256=ship_anwm.MODEL_SHA256,
        upstream_revision=ship_anwm.UPSTREAM_REVISION, vae_revision=ship_anwm.VAE_REVISION,
        server_sha256=source["ship_anwm_server.py"], helper_sha256=source["ship_anwm.py"],
        appearance_sha256=source["yokohama_appearance.py"],
        profile_sha256=source["yokohama_wam_profile.py"],
        diffusion_steps=250, load_seconds=0, cpu_between_requests=True,
        candidate_contracts=["ship_anwm_request.v1", "yokohama_anwm_request.v1"],
        adapter_sha256=None, model_time_index=4, appearance_policy=None,
    )
    return {"vla": vla, "wam": wam}


@pytest.fixture
def shadow_plan(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    # Identical stationary pixels are permitted; timestamps remain 24 distinct
    # observations. Shared files avoid duplicating multi-megabyte frames.
    yy, xx = np.indices((360, 640))
    texture = np.where((xx // 64 + yy // 64) % 2, 190, 60).astype(np.uint8)
    rgb = np.repeat(texture[..., None], 3, axis=2)
    files = {
        "onboard_rgb_raw": ("rgb.raw", rgb.tobytes()),
        "onboard_depth_raw": ("depth.raw", np.full((360, 640), 60, dtype="<f4").tobytes()),
        "onboard_rgb_png": ("rgb.png", png_bytes(rgb)),
        "down_rgb_png": ("down.png", png_bytes(rgb)),
    }
    assets = {}
    for name, (filename, data) in files.items():
        (inputs / filename).write_bytes(data)
        assets[name] = {"file": filename, "sha256": sha256(data).hexdigest()}
    packets = {}
    for name, end_s, wall_s in (("vla", 100, 200), ("wam", 110, 210)):
        capture = {
            "schema_version": "yokohama_rgbd_history.v1",
            "startup_indices": list(range(8)), "history_indices": list(range(8, 24)),
            "future_frames_included": False,
            "frames": [{
                "stamp_ns": int((end_s - (23 - index) / 4) * 1e9),
                "pose": {"sensor_sim_s": end_s - (23 - index) / 4,
                         "xyz": [0, 0, 15], "quat_wxyz": [1, 0, 0, 0]},
                "assets": deepcopy(assets),
            } for index in range(24)],
        }
        capture_path = inputs / (name + "-capture.json")
        write_json(capture_path, capture)
        packets[name] = {
            "capture": {"file": str(capture_path.relative_to(tmp_path)),
                        "sha256": ship_anwm.digest(capture_path)},
            "observation": {
                "sim_s": end_s, "wall_s": wall_s, "phase": "00-D1",
                "vehicle": {"xyz": [0, 0, 15], "quat_wxyz": [1, 0, 0, 0]},
                "heading_ned_rad": math.pi / 2, "nav_state": 4, "arming_state": 2,
                "landed": False, "position_valid": True, "battery_fraction": 0.8,
                "velocity_ned": [0, 0, 0], "reset_counters": {},
            },
        }
    collision_map = tmp_path / "collision-footprints.geojson"
    write_json(collision_map, {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"zmin": 0, "zmax": 30},
        "geometry": {"type": "Polygon", "coordinates": [
            [[100, 100], [110, 100], [110, 110], [100, 110], [100, 100]],
        ]},
    }]})
    source = {name: ship_anwm.digest(REPO / "scripts" / name) for name in SOURCE_NAMES}
    config = {
        "run_id": "shadow-contract-fixture",
        "world": {"world_sha256": "a" * 64, "frame": {
            "source_to_world_matrix": np.eye(3).tolist(), "source_origin_xyz_m": [0, 0, 0],
        }},
        "decisions": {"points": ["D1", "D2"], "wam_profile": "legacy"},
        "flight_stages": [{"name": phase, "target_world_xyz_m": [10 * index, 0, 15]}
                          for index, phase in enumerate(("00-D1", "01-D2", "02-D3"))],
    }
    plan = {
        "schema_version": "yokohama_native_shadow_plan.v1",
        "approval_ref": "operator:explicit-shadow-fixture", "execution_mode": "mock_http",
        "config": config, "cycle": 1, "next_target_world_xyz_m": [10, 0, 15],
        **packets,
        "collision_map": {"file": collision_map.name, "sha256": ship_anwm.digest(collision_map)},
        "source_sha256": source, "services": identities(source),
        "ports": {"vla": 18117, "wam": 18118},
        "limits": {"max_vla_requests": 1, "max_wam_requests": 1,
                   "request_timeout_s": 75, "total_timeout_s": 180},
    }
    path = tmp_path / "plan.json"
    write_json(path, plan)
    return path


class VLADouble:
    def predict(self, prompt, images):
        assert prompt and len(images) == 2
        return {
            "generated_text": "49 49 49", "generated_token_ids": [49, 49, 49],
            "pixel_values_shape": [1, 6, 224, 224], "cuda_allocated_after_request_bytes": 0,
        }, png_bytes(np.asarray(images[0]))


class WAMDouble:
    def __init__(self, fault):
        self.fault = fault

    def predict(self, request, arrays, output):
        forecasts = []
        for candidate in request["candidates"]:
            target = ship_anwm.action_pose(arrays["poses"][-1], candidate["delta"])
            image, _ = past_view(arrays, target)
            if self.fault == "inconsistent_image":
                image = np.zeros_like(image)
            data = png_bytes(image)
            entry = {"file": candidate["id"] + "-prediction.png",
                     "sha256": sha256(data).hexdigest(),
                     "png_base64": base64.b64encode(data).decode()}
            forecasts.append({"candidate": candidate, "files": {"prediction": entry}})
        return {"forecasts": forecasts, "fixture": True, "cuda_allocated_after_request_bytes": 0}


@contextmanager
def services(plan_path, *, fault=None):
    """Real HTTP and production decoding, with no NativeModel construction."""
    plan = json.loads(plan_path.read_text())
    calls, servers, threads = [], [], []
    remote = plan_path.parent / "mock-remote"
    remote.mkdir()
    for model in ("vla", "wam"):
        folder = remote / model
        folder.mkdir()
        identity = deepcopy(plan["services"][model])
        if fault == "health_mode" and model == "wam":
            identity.pop("fixture")
        base = (vla_handler(VLADouble(), identity, folder) if model == "vla"
                else wam_handler(WAMDouble(fault), identity, folder))

        def handler_type(base, model):
            class Tracked(base):
                def do_GET(self):
                    calls.append((model, "GET", self.path))
                    super().do_GET()

                def do_POST(self):
                    calls.append((model, "POST", self.path))
                    if model == "vla" and fault == "mutate_source":
                        # Admission has finished. The later WAM input must use
                        # the frozen arrays, not reread this changed source.
                        (plan_path.parent / "inputs/rgb.raw").write_bytes(b"changed after admission")
                    if fault == model + "_503":
                        send = self.send_json if model == "vla" else self.send
                        send(503, {"error": "explicit model unavailable"})
                        return
                    super().do_POST()

                def send_json(self, status, value):
                    if model == "vla" and fault == "vla_hash" and "request_sha256" in value:
                        value["request_sha256"] = "0" * 64
                    if model == "vla" and fault == "native_fixture_response" and "request_sha256" in value:
                        value["fixture"] = True
                    return super().send_json(status, value)

                def send(self, status, value):
                    if "forecasts" in value:
                        if fault == "candidate":
                            value["forecasts"][0]["candidate"]["id"] = "changed-candidate"
                        elif fault == "missing_forecast":
                            value["forecasts"].pop()
                        elif fault == "png_hash":
                            value["forecasts"][0]["files"]["prediction"]["sha256"] = "0" * 64
                        elif fault == "png_shape":
                            data = png_bytes(np.zeros((16, 16, 3), np.uint8))
                            value["forecasts"][0]["files"]["prediction"].update(
                                png_base64=base64.b64encode(data).decode(), sha256=sha256(data).hexdigest())
                        elif fault == "wam_physical":
                            value["physical_execution_invoked"] = True
                        elif fault == "wam_request_id":
                            value["request_id"] = "0" * 32
                    return super().send(status, value)
            return Tracked

        server = HTTPServer(("127.0.0.1", 0), handler_type(base, model))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        servers.append(server)
        threads.append(thread)
        plan["ports"][model] = server.server_port
        thread.start()
    write_json(plan_path, plan)
    try:
        yield calls
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()


def assert_no_authority(result):
    assert result["native_inference_verified"] is False
    assert result["dispatch_allowed"] is False
    assert result["flight_authority_created"] is False
    assert result["externally_owned_services"] is True
    assert result["remote_cleanup_verified"] is False


def test_preflight_is_offline_and_does_not_create_output(shadow_plan, monkeypatch):
    def network_forbidden(*args, **kwargs):
        raise AssertionError("preflight made an HTTP call")
    monkeypatch.setattr("urllib.request.urlopen", network_forbidden)
    before = set(shadow_plan.parent.iterdir())
    assert preflight(shadow_plan)["valid"] is True
    assert set(shadow_plan.parent.iterdir()) == before


def test_explicit_approval_precedes_network_and_output(shadow_plan):
    output = shadow_plan.parent / "run"
    with services(shadow_plan) as calls:
        with pytest.raises(PermissionError):
            run_shadow(shadow_plan, output)
        assert calls == []
    assert not output.exists()


def test_two_production_http_requests_remain_an_explicit_mock(shadow_plan):
    output = shadow_plan.parent / "run"
    with services(shadow_plan) as calls:
        result = run_shadow(shadow_plan, output, approved=True)
    assert result["status"] == "completed", result
    assert result["model_contract_verified"] is True
    assert result["visible_structure_consistent"] is True
    assert result["model_requests"] == 2
    assert result["vla_requests"] == result["wam_requests"] == 1
    assert calls == [("vla", "GET", "/health"), ("wam", "GET", "/health"),
                     ("vla", "POST", "/infer"), ("wam", "POST", "/infer")]
    assert_no_authority(result)
    raw_vla = next((shadow_plan.parent / "mock-remote/vla").glob("*/result.json"))
    # Production handler sets this flag even with an injected double. The
    # facade must not infer a native execution claim from that field alone.
    assert json.loads(raw_vla.read_text())["vla_inference_invoked"] is True
    assert not list(output.rglob("*permit*"))


def test_image_gate_failure_is_a_completed_assessment_not_authority(shadow_plan):
    with services(shadow_plan, fault="inconsistent_image") as calls:
        result = run_shadow(shadow_plan, shadow_plan.parent / "run", approved=True)
    assert result["status"] == "completed", result
    assert result["model_contract_verified"] is True
    assert result["visible_structure_consistent"] is False
    assert len([call for call in calls if call[1] == "POST"]) == 2
    assert_no_authority(result)


def test_motion_profile_keeps_native_input_contract_in_mock_execution(shadow_plan):
    plan = json.loads(shadow_plan.read_text())
    plan["config"]["decisions"]["wam_profile"] = "motion-v4"
    plan["services"]["vla"].update(
        compact_city_flight=True, yaw_bin_range=[45, 53], forward_bin_range=[20, 58],
        hold_bin_allowed=False, terminal_proposal_allowed=False,
        decoding_policy="aerovla_compact_city_grammar.v2",
    )
    plan["services"]["wam"].update(
        candidate_contracts=[ship_anwm.MOTION_CONTRACT],
        adapter_sha256=ship_anwm.MOTION_ADAPTER_SHA256, model_time_index=1,
        appearance_policy=ship_anwm.APPEARANCE_POLICY,
    )
    write_json(shadow_plan, plan)
    with services(shadow_plan) as calls:
        result = run_shadow(shadow_plan, shadow_plan.parent / "run", approved=True)
    assert result["status"] == "completed", result
    assert result["model_contract_verified"] is True
    assert len([call for call in calls if call[1] == "POST"]) == 2
    assert_no_authority(result)


def test_source_mutation_after_admission_cannot_change_wam_history(shadow_plan):
    with services(shadow_plan, fault="mutate_source") as calls:
        result = run_shadow(shadow_plan, shadow_plan.parent / "run", approved=True)
    assert result["status"] == "completed", result
    assert result["visible_structure_consistent"] is True
    assert len([call for call in calls if call[1] == "POST"]) == 2
    assert_no_authority(result)


@pytest.mark.parametrize("fault,requests", [
    ("vla_hash", 1), ("vla_503", 1), ("wam_503", 2), ("candidate", 2),
    ("missing_forecast", 2), ("png_hash", 2), ("png_shape", 2), ("health_mode", 0),
    ("wam_physical", 2), ("wam_request_id", 2),
])
def test_http_failures_are_retained_without_retry_or_substitution(shadow_plan, fault, requests):
    output = shadow_plan.parent / "run"
    with services(shadow_plan, fault=fault) as calls:
        result = run_shadow(shadow_plan, output, approved=True)
    assert result["status"] == "blocked", result
    assert result["failure"]
    assert result["model_contract_verified"] is False
    assert result["visible_structure_consistent"] is False
    assert len([call for call in calls if call[1] == "POST"]) == requests
    assert result["model_requests"] == requests
    assert_no_authority(result)


@pytest.mark.parametrize("change", ["stale", "same_history", "capture_hash", "source_hash",
                                     "map_hash", "outside_path", "future_capture", "wrong_goal",
                                     "nonunit_orientation", "changed_orientation", "wam_drift"])
def test_invalid_frozen_inputs_fail_before_output_or_http(shadow_plan, change):
    with services(shadow_plan) as calls:
        plan = json.loads(shadow_plan.read_text())
        if change == "stale":
            plan["vla"]["observation"]["sim_s"] += 3
        elif change == "same_history":
            plan["wam"] = deepcopy(plan["vla"])
        elif change == "capture_hash":
            plan["vla"]["capture"]["sha256"] = "0" * 64
        elif change == "source_hash":
            plan["source_sha256"]["ship_anwm.py"] = "0" * 64
        elif change == "map_hash":
            plan["collision_map"]["sha256"] = "0" * 64
        elif change == "outside_path":
            plan["vla"]["capture"]["file"] = "../outside-capture.json"
        elif change == "future_capture":
            path = shadow_plan.parent / plan["vla"]["capture"]["file"]
            capture = json.loads(path.read_text())
            capture["future_frames_included"] = True
            write_json(path, capture)
            plan["vla"]["capture"]["sha256"] = ship_anwm.digest(path)
        elif change == "nonunit_orientation":
            plan["vla"]["observation"]["vehicle"]["quat_wxyz"] = [2, 0, 0, 0]
        elif change == "changed_orientation":
            plan["vla"]["observation"]["vehicle"]["quat_wxyz"] = [math.cos(0.05), 0, 0, math.sin(0.05)]
        elif change == "wam_drift":
            # Both each capture and its row remain internally consistent and
            # inside the approved hold; the cross-model stationary view fails.
            plan["wam"]["observation"]["vehicle"]["xyz"][0] += 0.6
            path = shadow_plan.parent / plan["wam"]["capture"]["file"]
            capture = json.loads(path.read_text())
            for frame in capture["frames"]:
                frame["pose"]["xyz"][0] += 0.6
            write_json(path, capture)
            plan["wam"]["capture"]["sha256"] = ship_anwm.digest(path)
        else:
            plan["next_target_world_xyz_m"][0] += 1
        write_json(shadow_plan, plan)
        output = shadow_plan.parent / "run"
        with pytest.raises(ValueError):
            run_shadow(shadow_plan, output, approved=True)
        assert calls == []
        assert not output.exists()


def test_mock_service_identity_cannot_be_relabeled_native(shadow_plan):
    with services(shadow_plan) as calls:
        plan = json.loads(shadow_plan.read_text())
        plan["execution_mode"] = "native_shadow"
        write_json(shadow_plan, plan)
        # Reject either during offline admission or when inspecting health;
        # no native inference request may be sent to a fixture service.
        try:
            result = run_shadow(shadow_plan, shadow_plan.parent / "run", approved=True)
        except ValueError:
            pass
        else:
            assert result["status"] == "blocked"
            assert result["native_inference_verified"] is False
        assert not any(call[1] == "POST" for call in calls)


def claimed_native_plan(path):
    """An adversarial service claim used only in tests that must reject it.

    The CPU double never completes a native-mode assessment successfully.
    """
    plan = json.loads(path.read_text())
    plan["execution_mode"] = "native_shadow"
    plan["config"]["world"]["source_sha256"] = {
        "collision-footprints.geojson": plan["collision_map"]["sha256"],
    }
    for model in ("vla", "wam"):
        plan[model]["observation"].update(
            run_id=plan["config"]["run_id"],
            world_sha256=plan["config"]["world"]["world_sha256"],
        )
    for index, identity in enumerate(plan["services"].values(), 1):
        identity.pop("fixture")
        identity["session_id"] = str(index) * 32
        identity["gpu"] = "NVIDIA claimed-test-device"
    return plan


@pytest.mark.parametrize("fault", ["adapter", "gpu"])
def test_native_identity_rejected_before_any_http(shadow_plan, fault):
    plan = claimed_native_plan(shadow_plan)
    if fault == "adapter":
        plan["services"]["vla"]["adapter_sha256"] = "0" * 64
    else:
        plan["services"]["vla"].pop("gpu")
    write_json(shadow_plan, plan)
    with services(shadow_plan) as calls:
        with pytest.raises(ValueError, match="identity"):
            preflight(shadow_plan)
        with pytest.raises(ValueError, match="identity"):
            run_shadow(shadow_plan, shadow_plan.parent / "run", approved=True)
        assert calls == []
    assert not (shadow_plan.parent / "run").exists()


def test_fixture_response_contradicts_native_identity_and_stops_after_one_call(shadow_plan):
    write_json(shadow_plan, claimed_native_plan(shadow_plan))
    with services(shadow_plan, fault="native_fixture_response") as calls:
        assert preflight(shadow_plan)["valid"] is True
        result = run_shadow(shadow_plan, shadow_plan.parent / "run", approved=True)
    assert result["status"] == "blocked", result
    assert result["failure"]
    assert result["model_contract_verified"] is False
    assert result["model_requests"] == result["vla_requests"] == 1
    assert result["wam_requests"] == 0
    assert calls == [("vla", "GET", "/health"), ("wam", "GET", "/health"),
                     ("vla", "POST", "/infer")]
    assert_no_authority(result)
