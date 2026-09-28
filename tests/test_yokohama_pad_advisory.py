"""Auxiliary forecasts change waiting only; current entry Rules remain mandatory."""

import copy
import json
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from scripts.yokohama_pad_advisory_host import PadAdvisoryHost
from scripts.yokohama_pad_state import sha
from src.runtime.yokohama_pad_advisory_contract import selected_action
from src.runtime.yokohama_pad_queue import (
    PadSupervisor,
    atomic_json,
    digest,
    make_request,
    propose,
    require_response,
)
from test_yokohama_pad_queue import config, observation


def setup():
    c = config()
    c["world"]["pad_state_advisory"] = dict(
        mode="assist", camera_entity="pad_state_camera", weights_sha256="a" * 64
    )
    return c


def forecast(c, request, state):
    stamp = round(request["observations"][-1]["sim_s"] * 1e9)
    return dict(
        schema="missionos.pad-state-advisory.v1",
        request_id=request["request_id"],
        camera_entity="pad_state_camera",
        model_sha256=c["world"]["pad_state_advisory"]["weights_sha256"],
        history_sha256=None,
        flight_authority_created=False,
        status="computed",
        forecast=dict(
            schema="missionos.pad-state-forecast.v1",
            supported=state != "unknown",
            flight_admitted=False,
            dispatch_invoked=False,
            action_conditioning_verified=False,
            interval_collision_verified=False,
            input_last_stamp_ns=stamp,
            forecasts=[
                dict(offset_s=i / 4, stamp_ns=stamp + i * 250_000_000, state=state)
                for i in range(17)
            ],
        ),
    )


@pytest.mark.parametrize(
    "state,expected",
    [
        ("occupied", "wait_at_current_hold"),
        ("clear", "enter_delivery_approach"),
        ("unknown", "enter_delivery_approach"),
    ],
)
def test_forecast_influences_shared_judgment_without_unknown_veto(
    tmp_path, monkeypatch, state, expected
):
    c = setup()
    req = make_request(c, 0, [observation(t) for t in range(6)])
    h = PadAdvisoryHost(tmp_path, c)
    monkeypatch.setattr(h, "forecast", lambda *args: forecast(c, req, state))
    response = h.respond(req, tmp_path, propose(c, req))
    assert require_response(c, req, response, observation(5)) == expected
    judgment = json.loads((tmp_path / "mission-assurance.json").read_text())
    assert response["mission_assurance_sha256"] == digest(judgment)
    assert judgment["proposal"]["parameters"]["action"] == expected
    assert response["approval_granted"] is False
    assert response["dispatch_authority_created"] is False
    h.close()


def test_clear_forecast_cannot_authorize_current_occupied_pad(tmp_path, monkeypatch):
    c = setup()
    req = make_request(c, 0, [observation(5, True)])
    h = PadAdvisoryHost(tmp_path, c)
    monkeypatch.setattr(h, "forecast", lambda *args: forecast(c, req, "clear"))
    response = h.respond(req, tmp_path, propose(c, req))
    assert require_response(c, req, response, observation(5, True)) == "wait_at_current_hold"
    response["proposed_action"] = "enter_delivery_approach"
    with pytest.raises(ValueError):
        require_response(c, req, response, observation(5, True))


@pytest.mark.parametrize("fault", ["foreign", "clock", "authority", "unsupported"])
def test_malformed_auxiliary_evidence_cannot_change_action(fault):
    c = setup()
    req = make_request(c, 0, [observation(t) for t in range(6)])
    receipt = forecast(c, req, "clear")
    if fault == "foreign":
        receipt["request_id"] = "foreign"
    elif fault == "clock":
        receipt["forecast"]["forecasts"][2]["stamp_ns"] += 1
    elif fault == "authority":
        receipt["forecast"]["flight_admitted"] = True
    else:
        receipt["forecast"]["supported"] = False
    with pytest.raises(ValueError):
        selected_action(c, req, receipt, "enter_delivery_approach")


def test_shadow_and_stale_forecasts_do_not_veto_current_rules():
    c = setup()
    req = make_request(c, 0, [observation(t) for t in range(6)])
    receipt = forecast(c, req, "occupied")
    c["world"]["pad_state_advisory"]["mode"] = "shadow"
    assert selected_action(c, req, receipt, "enter_delivery_approach") == "enter_delivery_approach"
    c["world"]["pad_state_advisory"]["mode"] = "assist"
    receipt["forecast"]["input_last_stamp_ns"] -= 2_000_000_000
    assert selected_action(c, req, receipt, "enter_delivery_approach") == "enter_delivery_approach"


def test_live_mailbox_learned_inference_then_close(tmp_path):
    """Actual CPU weights through the mailbox; resized recorded images are a fixture."""
    bundle = Path(__file__).resolve().parents[1] / "docs/examples/yokohama-pad-state"
    c = setup()
    c["world"]["pad_state_advisory"]["weights_sha256"] = sha(bundle / "model/model.npz")
    shutil.copytree(bundle / "model", tmp_path / "pad-state-model")
    folder = tmp_path / "pad-decisions/000"
    (folder / "history").mkdir(parents=True)
    source = json.loads((bundle / "test-capture/state-depart.json").read_text())["frames"][:16]
    with np.load(bundle / "test-data/state-depart.npz", allow_pickle=False) as data:
        rgb = data["rgb"][:16]
    frames = []
    for i, (image, original) in enumerate(zip(rgb, source)):
        path = folder / "history" / f"{i:02d}.png"
        Image.fromarray(image).resize((640, 360)).save(path)
        frames.append(
            dict(
                file=path.name,
                sha256=sha(path),
                pose=original["rig_pose"],
                stamp_ns=original["stamp_ns"],
            )
        )
    manifest = folder / "history/capture.json"
    atomic_json(
        manifest,
        dict(
            run_id=c["run_id"],
            world_sha256=c["world"]["world_sha256"],
            camera_entity="pad_state_camera",
            frames=frames,
        ),
    )
    row = observation(frames[-1]["stamp_ns"] / 1e9, True)
    req = make_request(c, 0, [row], dict(file="history/capture.json", sha256=sha(manifest)))
    host = PadSupervisor(tmp_path, c)
    try:
        assert host.advisory.model is None  # no pre-city load or warm-up
        atomic_json(folder / "request.json", req)
        deadline = time.monotonic() + 5
        while not (folder / "response.json").exists():
            if (tmp_path / "pad-supervisor-error.json").exists():
                pytest.fail((tmp_path / "pad-supervisor-error.json").read_text())
            assert time.monotonic() < deadline
            time.sleep(0.01)
        response = json.loads((folder / "response.json").read_text())
        assert response["advisory"]["status"] == "computed"
        assert host.advisory.calls == 1
        assert require_response(c, req, response, row) == "wait_at_current_hold"
        atomic_json(tmp_path / "pad-advisory-closed.json", dict(run_id=c["run_id"]))
        while not host.advisory.closed:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert host.advisory.model is None
        late = copy.deepcopy(req)
        late["sequence"] = 1
        folder2 = tmp_path / "pad-decisions/001"
        folder2.mkdir()
        late = make_request(c, 1, [row])
        atomic_json(folder2 / "request.json", late)
        while not (tmp_path / "pad-supervisor-error.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not (folder2 / "response.json").exists()
        assert host.advisory.calls == 1
    finally:
        host.close()


@pytest.mark.parametrize("pose_available", [True, False])
def test_delayed_image_joins_measured_past_pose(monkeypatch, tmp_path, pose_available):
    import importlib
    import sys
    from types import SimpleNamespace as NS
    from scripts import ship_urban_camera_worker
    from src.runtime import yokohama_pad_queue

    class Node:
        def subscribe(self, *args):
            return True

        def unsubscribe(self, *args):
            return True

    monkeypatch.setitem(sys.modules, "gz.transport13", NS(Node=Node))
    monkeypatch.setitem(sys.modules, "gz.msgs10.pose_v_pb2", NS(Pose_V=object))
    monkeypatch.setitem(sys.modules, "gz.msgs10.image_pb2", NS(Image=object))
    monkeypatch.setitem(sys.modules, "ship_urban_camera_worker", ship_urban_camera_worker)
    monkeypatch.setitem(sys.modules, "yokohama_pad_queue", yokohama_pad_queue)
    worker = importlib.import_module("scripts.yokohama_pad_advisory_worker")
    c = setup()
    c["world"]["pad_state_advisory"]["topic"] = "/fixed-camera/image"
    recorder = worker.PadCameraRecorder(tmp_path, c, NS(subscribe=lambda *args: None))

    def header(t):
        return NS(stamp=NS(sec=int(t), nsec=round((t % 1) * 1e9)))

    def pose(t):
        return NS(
            header=header(t),
            pose=[
                NS(
                    name="pad_state_camera",
                    id=7,
                    position=NS(x=1, y=2, z=3),
                    orientation=NS(w=1, x=0, y=0, z=0),
                )
            ],
        )

    recorder.start()
    if pose_available:
        recorder.receive_pose(pose(10))
    recorder.receive_pose(pose(10.24))  # latest pose arrives before the older image
    recorder.receive(
        NS(header=header(10), width=640, height=360, pixel_format_type=3, data=bytes(640 * 360 * 3))
    )
    assert len(recorder.history) == int(pose_available)
    if pose_available:
        assert recorder.history[0]["pose"]["sensor_sim_s"] == 10
    recorder.close(dict(sim_s=11))
    recorder.receive_pose(pose(11))
    assert not recorder.history and not recorder.poses
