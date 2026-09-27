"""Data-boundary faults, not a substitute for actual AP flight evidence."""

import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest
from scripts.yokohama_motion_recorder import MotionRecorder
from src.runtime.yokohama_motion import candidate_windows, load_frames


def config():
    return dict(
        phases=["01-D2"],
        hold_tail_s=8,
        max_frames=100,
        max_compressed_bytes=10_000_000,
        reserve_bytes=0,
        phase_lines={"01-D2": [[0, 0, 15], [100, 0, 15]]},
        fraction_ranges={"01-D2": [0, 1]},
    )


def fixture(root):
    recorder = MotionRecorder(root, config())
    history = {key: {} for key in ["onboard_rgb", "onboard_depth"]}
    poses = []
    for i in range(44):
        stamp = int((i * 0.25 + 10) * 1e9)
        for key, size, encoding in [("onboard_rgb", 3, 3), ("onboard_depth", 4, 13)]:
            raw = (
                bytes([80]) * (640 * 360 * 3)
                if size == 3
                else np.full((360, 640), 10, dtype="<f4").tobytes()
            )
            history[key][stamp] = (
                SimpleNamespace(width=640, height=360, pixel_format_type=encoding, data=raw),
                0,
            )
        poses.append(dict(sensor_sim_s=stamp / 1e9, xyz=[i * 0.75, 0, 15], quat_wxyz=[1, 0, 0, 0]))
    poses.append(dict(poses[-1], sensor_sim_s=21))
    state = dict(
        phase="01-D2", sim_s=10, arming_state=2, landed=False, nav_state=3, velocity_ned=[0, 3, 0]
    )
    recorder.append(history, poses, state)
    f = 640 / (2 * np.tan(np.pi / 6))
    recorder.close(dict(width=640, height=360, k=[f, 0, 320, 0, f, 180, 0, 0, 1]))
    return history, poses, state


def test_lossless_motion_has_actual_offsets_and_past_only_candidate(tmp_path):
    fixture(tmp_path)
    manifest, frames = load_frames(tmp_path)
    assert manifest["frames"] == 44
    assert frames[0]["camera_pose"][2, 3] == pytest.approx(-15.1)
    rows = candidate_windows(frames)
    assert all(r["qualified"] and r["delta"] == [3.0, 0.0, 0.0, 0.0] for r in rows)
    assert all(r["history_displacement_m"] == pytest.approx(11.25) for r in rows)
    assert all(r["candidate_dispatched"] is False for r in rows)


def test_future_pose_cannot_rewrite_prediction_request(tmp_path):
    fixture(tmp_path)
    _, frames = load_frames(tmp_path)
    before = candidate_windows(frames)[0]
    altered = copy.deepcopy(frames)
    altered[before["target_index"]]["camera_pose"][:3, 3] += 100
    after = candidate_windows(altered)[0]
    assert after["delta"] == before["delta"]
    assert after["requested_camera_pose"] == before["requested_camera_pose"]
    assert not after["qualified"] and after["reason"] == "requested_view_not_reached_at_fixed_time"


def test_gap_is_retained_as_rejection(tmp_path):
    fixture(tmp_path)
    _, frames = load_frames(tmp_path)
    frames[5]["stamp_ns"] += 100_000_000
    first = candidate_windows(frames)[0]
    assert not first["qualified"] and first["reason"] == "noncontiguous_or_cross_phase"


@pytest.mark.parametrize("fault", ["compressed_bytes", "intrinsics", "pose_time", "future_schema"])
def test_capture_tampering_rejected(tmp_path, fault):
    fixture(tmp_path)
    m = tmp_path / "motion/manifest.json"
    v = json.loads(m.read_text())
    if fault == "compressed_bytes":
        p = tmp_path / "motion/00000-onboard_rgb.z"
        p.write_bytes(p.read_bytes() + b"bad")
    elif fault == "intrinsics":
        v["camera_info"]["k"][0] = 10
        m.write_text(json.dumps(v))
    elif fault == "future_schema":
        v["schema_version"] = "yokohama_rgbd_history.v1"
        m.write_text(json.dumps(v))
    else:
        from src.runtime.yokohama_motion import sha

        p = tmp_path / "motion/frames.jsonl"
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        rows[0]["pose"]["sensor_sim_s"] += 1
        p.write_text("".join(json.dumps(x) + "\n" for x in rows))
        v["frames_sha256"] = sha(p.read_bytes())
        m.write_text(json.dumps(v))
    with pytest.raises(ValueError):
        load_frames(tmp_path)


def test_recorder_requires_pose_join_and_enforces_storage_bound(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    history, poses, state = fixture(source)
    target = tmp_path / "join"
    target.mkdir()
    rec = MotionRecorder(target, config())
    with pytest.raises(ValueError, match="12 ms"):
        rec.append(history, [poses[-1]], state)
    rec.close({})
    target = tmp_path / "bound"
    target.mkdir()
    cfg = config()
    cfg["max_compressed_bytes"] = 1
    rec = MotionRecorder(target, cfg)
    with pytest.raises(ValueError, match="byte bound"):
        rec.append(history, poses, state)
    assert rec.count == 0
    rec.close({})


def test_skipped_spatial_frames_do_not_reappear_when_mode_changes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    history, poses, state = fixture(source)
    target = tmp_path / "window"
    target.mkdir()
    cfg = config()
    cfg["fraction_ranges"]["01-D2"] = [0.1, 0.2]
    recorder = MotionRecorder(target, cfg)
    recorder.append(history, poses, state)
    count = recorder.count
    assert 0 < count < 44
    recorder.append(history, poses, dict(state, nav_state=4, sim_s=22))
    assert recorder.count == count
    recorder.close({})
