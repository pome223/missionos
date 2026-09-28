"""Pure temporal-input and grayscale-reader contracts; no paid model calls."""

import json

import numpy as np
import pytest

from scripts.capture_yokohama_pad_motion import actor_xyz, cases
from scripts.prepare_yokohama_pad_temporal import gray_features, label, read_image
from scripts.probe_yokohama_pad_temporal import sha, validate_inputs


def inputs(root):
    stamps = np.arange(16, dtype=np.int64) * 250_000_000
    arrays = dict(
        rgb=np.zeros((16, 360, 640, 3), np.uint8),
        depth=np.ones((16, 360, 640), np.float32),
        poses=np.repeat(np.eye(4)[None], 16, axis=0),
        intrinsics=np.eye(3),
        stamps_ns=stamps,
        last_depth_infinite=np.zeros((360, 640), bool),
    )
    manifest = dict(schema="pad_temporal_inputs.v1", future_targets_included=False, samples=[])
    for i in range(4):
        folder = root / str(i)
        folder.mkdir()
        np.savez_compressed(folder / "history.npz", **arrays)
        request = dict(
            schema="yokohama_pad_temporal_request.v1",
            history_sha256=sha(folder / "history.npz"),
            cutoff_stamp_ns=int(stamps[-1]),
            frame_period_s=0.25,
            model_frame_offset=64,
            requested_horizon_sim_s=16,
            diffusion_steps=50,
            seed=42,
            candidates=[dict(id="hold", delta=[0, 0, 0, 0])],
            dispatch_allowed=False,
            model_time_alignment_verified=False,
            future_ground_truth_used_for_forecast=False,
        )
        (folder / "request.json").write_text(json.dumps(request))
        manifest["samples"].append(dict(id=str(i), request_sha256=sha(folder / "request.json")))
    (root / "manifest.json").write_text(json.dumps(manifest))
    return arrays, manifest


def test_temporal_input_has_exact_historical_cadence_and_no_future_arrays(tmp_path):
    arrays, manifest = inputs(tmp_path)
    assert len(validate_inputs(tmp_path)) == 4
    arrays["future_rgb"] = arrays["rgb"][-1]
    p = tmp_path / "0/history.npz"
    np.savez_compressed(p, **arrays)
    request = tmp_path / "0/request.json"
    r = json.loads(request.read_text())
    r["history_sha256"] = sha(p)
    request.write_text(json.dumps(r))
    manifest["samples"][0]["request_sha256"] = sha(request)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unexpected arrays"):
        validate_inputs(tmp_path)


def test_frame_offset_cannot_be_relabelled_as_seconds(tmp_path):
    _, manifest = inputs(tmp_path)
    request = tmp_path / "0/request.json"
    r = json.loads(request.read_text())
    r["model_frame_offset"] = 16
    request.write_text(json.dumps(r))
    manifest["samples"][0]["request_sha256"] = sha(request)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="protocol changed"):
        validate_inputs(tmp_path)


def test_image_reader_uses_luminance_not_entity_color():
    clear = np.full((224, 224, 3), 120, np.uint8)
    busy = clear.copy()
    busy[70:110, 90:130] = [220, 20, 10]
    mask = np.ones((224, 224), bool)
    reader = dict(
        mask=mask,
        features=np.stack([gray_features(clear, mask), gray_features(busy, mask)]),
        labels=np.array(["clear", "occupied"]),
    )
    assert read_image(clear, reader)["state"] == "clear"
    assert read_image(busy, reader)["state"] == "occupied"
    assert read_image(busy[..., ::-1], reader) == read_image(busy, reader)
    rng = np.random.default_rng(0)
    assert (
        read_image(rng.integers(0, 256, (224, 224, 3), dtype=np.uint8), reader)["state"]
        == "unknown"
    )


def test_measured_boundary_is_not_reported_as_certain_occupancy():
    def frame(x):
        return {"lead_pose": {"xyz": [x, 0, 5]}}

    assert label(frame(0), [0, 0, 0]) == "occupied"
    assert label(frame(6), [0, 0, 0]) == "unknown"
    assert label(frame(8), [0, 0, 0]) == "clear"


def test_authored_actor_is_bounded_and_data_splits_are_predeclared():
    p = dict(start=[0, 0, 1], up=[0, 0, 7], end=[12, 0, 7])
    c = cases()
    assert [s["split"] for s in c] == ["development", *(["evaluation"] * 3)]
    assert actor_xyz(c[2]["knots"], 24, p) == p["up"]
    assert actor_xyz(c[3]["knots"], 14, p) == [6, 0, 7]
    assert actor_xyz(c[0]["knots"], 100, p) == p["end"]
