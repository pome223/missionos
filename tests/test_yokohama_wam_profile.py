"""CPU contracts and real HTTP for the opt-in learned city WAM integration."""

import base64
import json
import math
import threading
import urllib.request
from http.server import HTTPServer

import numpy as np
import pytest

from scripts import ship_anwm
from scripts.ship_anwm_server import NativeModel, make_handler
from scripts.yokohama_appearance import fill_infinite_appearance
from scripts.yokohama_wam_profile import (
    APPEARANCE_POLICY,
    MOTION_ADAPTER_SHA256,
    MOTION_CONTRACT,
    validate_service_profile,
)
from src.runtime.yokohama_native import write_request


def request(tmp_path):
    fx = 640 / (2 * math.tan(math.pi / 6))
    depth = np.full((16, 360, 640), 10, np.float32)
    depth[-1, :10] = 0
    poses = np.repeat(np.eye(4)[None], 16, axis=0)
    poses[:, :3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    arrays = dict(
        rgb=np.full((16, 360, 640, 3), 90, np.uint8),
        depth=depth,
        poses=poses,
        intrinsics=np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]]),
        stamps_ns=np.arange(16, dtype=np.int64) * 250_000_000,
        last_depth_infinite=depth[-1] == 0,
    )
    folder = tmp_path / "input"
    value = write_request(
        folder,
        arrays,
        {
            "run_id": "test",
            "world": {"world_sha256": "a" * 64},
            "decisions": {"wam_profile": "motion-v4"},
        },
        {"delta_body_frd": [2.5, 0, 0, 0]},
        "b" * 64,
    )
    return folder, value, arrays


def test_motion_contract_real_http(tmp_path):
    folder, value, arrays = request(tmp_path)

    class Double:
        def predict(self, received, history, output):
            assert received == value and received["num_timesteps"] == 1
            assert np.array_equal(history["last_depth_infinite"], arrays["last_depth_infinite"])
            return {"forecasts": [], "fixture": True}

    remote = tmp_path / "remote"
    remote.mkdir()
    server = HTTPServer(("127.0.0.1", 0), make_handler(Double(), {"fixture": True}, remote))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {
            "request_id": "1" * 32,
            "request_base64": base64.b64encode((folder / "request.json").read_bytes()).decode(),
            "history_base64": base64.b64encode((folder / "history.npz").read_bytes()).decode(),
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/infer",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            receipt = json.load(response)
        assert receipt["request_sha256"] == ship_anwm.digest(folder / "request.json")
        assert not receipt["wam_inference_invoked"] and not receipt["dispatch_allowed"]
        assert (remote / ("1" * 32) / "response.json").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize("fault", ["adapter", "time", "mask", "overlap", "future"])
def test_unqualified_motion_request_rejected(tmp_path, fault):
    folder, value, arrays = request(tmp_path)
    if fault == "adapter":
        value["adapter_sha256"] = "0" * 64
    if fault == "time":
        value["num_timesteps"] = 4
    if fault == "mask":
        arrays["last_depth_infinite"] = arrays["last_depth_infinite"].astype(np.uint8)
    if fault == "overlap":
        arrays["last_depth_infinite"][20, 20] = True
    if fault == "future":
        arrays["future_rgb"] = arrays["rgb"][-1]
    np.savez_compressed(folder / "history.npz", **arrays)
    value["history_sha256"] = ship_anwm.digest(folder / "history.npz")
    (folder / "request.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        ship_anwm.validate(folder / "request.json")


@pytest.mark.parametrize(
    "adapted,contract", [(True, "yokohama_anwm_request.v1"), (False, MOTION_CONTRACT)]
)
def test_model_profile_mismatch_rejected_before_cuda(adapted, contract):
    model = object.__new__(NativeModel)
    model.adapter_sha256 = MOTION_ADAPTER_SHA256 if adapted else None
    with pytest.raises(ValueError, match="profile mismatch"):
        model.predict({"schema_version": contract}, {}, None)


def test_service_identity_cannot_silently_use_other_weights():
    identity = dict(
        adapter_sha256=MOTION_ADAPTER_SHA256,
        model_time_index=1,
        appearance_policy=APPEARANCE_POLICY,
        appearance_sha256="a" * 64,
        profile_sha256="b" * 64,
        candidate_contracts=[MOTION_CONTRACT],
    )
    validate_service_profile(
        identity, "motion-v4", appearance_sha256="a" * 64, profile_sha256="b" * 64
    )
    with pytest.raises(ValueError):
        validate_service_profile(
            identity, "legacy", appearance_sha256="a" * 64, profile_sha256="b" * 64
        )
    for key in identity:
        bad = dict(identity)
        bad[key] = None
        with pytest.raises(ValueError):
            validate_service_profile(
                bad, "motion-v4", appearance_sha256="a" * 64, profile_sha256="b" * 64
            )


def test_appearance_fills_only_observed_infinity_upward_and_preserves_geometry():
    pose = np.eye(4)
    pose[:3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    k = np.array([[10, 0, 5], [0, 10, 5], [0, 0, 1]], float)
    rgb = np.full((10, 10, 3), 100, np.uint8)
    projection = np.full_like(rgb, 20)
    raw = np.full((10, 10), np.inf)
    raw[0, 0] = np.nan
    raw[0, 1] = 0
    known = np.zeros((10, 10), bool)
    known[0, 2] = True
    result, mask = fill_infinite_appearance(projection, known, rgb, raw, k, pose, pose)
    assert mask[0, 3] and not mask[0, :3].any() and not mask[5:].any()
    assert np.array_equal(result[~mask], projection[~mask])
