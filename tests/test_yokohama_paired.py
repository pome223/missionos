import json
import hashlib
import numpy as np
import pytest

from src.runtime.yokohama_native import load_capture
from src.runtime.yokohama_paired import evaluation_frames, fill_infinite_appearance


def test_evaluation_is_not_model_input_and_rejects_nonfuture_frames(tmp_path):
    path = tmp_path / "capture.json"
    assets = {}
    for name, size in [
        ("onboard_rgb_raw", 360 * 640 * 3),
        ("onboard_depth_raw", 360 * 640 * 4),
    ]:
        data = bytes(size)
        (tmp_path / name).write_bytes(data)
        assets[name] = dict(file=name, sha256=hashlib.sha256(data).hexdigest())
    record = dict(
        schema_version="yokohama_rgbd_evaluation.v1",
        evaluation_only=True,
        future_frames_included=True,
        startup_indices=[],
        history_indices=[],
        frames=[
            dict(
                stamp_ns=int((10.25 + i / 4) * 1e9),
                pose=dict(sensor_sim_s=10.25 + i / 4),
                assets=assets,
            )
            for i in range(24)
        ],
    )
    path.write_text(json.dumps(record))
    assert evaluation_frames(path, 10) == record
    with pytest.raises(ValueError, match="Unsupported city sensor history"):
        load_capture(path)
    with pytest.raises(ValueError, match="cutoff"):
        evaluation_frames(path, 11)
    with pytest.raises(ValueError, match="cutoff"):
        evaluation_frames(path, 9)
    record["frames"][5]["stamp_ns"] = record["frames"][4]["stamp_ns"]
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="cutoff"):
        evaluation_frames(path, 10)


def test_infinite_appearance_keeps_geometry_and_invalid_depth_unknown():
    rgb = np.full((3, 4, 3), 218, np.uint8)
    projection = np.zeros_like(rgb)
    known = np.zeros((3, 4), bool)
    known[0, 0] = True
    projection[0, 0] = 17
    depth = np.full((3, 4), np.inf)
    depth[0, 1:] = [np.nan, -np.inf, 0]
    k = np.array([[2.0, 0, 2], [0, 2, 2], [0, 0, 1]])
    # Camera +x right, +y down, +z forward; world NED (+z down).
    pose = np.eye(4)
    pose[:3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    result, filled = fill_infinite_appearance(projection, known, rgb, depth, k, pose, pose)
    assert (result[known] == projection[known]).all()
    assert not filled[0].any()  # finite structure, NaN, -inf and zero are untouched
    assert filled[1].all()
    assert not filled[2].any()  # horizontal rays cannot supply sky appearance
    assert known.sum() == 1  # appearance never expands the metric geometry mask
    translated = pose.copy()
    translated[:3, 3] = [3, 2, 1]
    other, other_mask = fill_infinite_appearance(projection, known, rgb, depth, k, pose, translated)
    np.testing.assert_array_equal(result, other)
    np.testing.assert_array_equal(filled, other_mask)
