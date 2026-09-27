import copy
import json
import zlib

import numpy as np
import pytest

from scripts.ship_anwm import action_pose, camera_pose
from scripts.train_yokohama_anwm_head import BLOCK_ADAPTER_SHA256, validate_configuration
from scripts.yokohama_motion_data import digest, load_history, validate_payload


def payload(root, *, temporal_overlap=False):
    for sub in ["inputs", "frames", "targets"]:
        (root / sub).mkdir()
    rgb = zlib.compress(bytes(360 * 640 * 3))
    depth = np.full((360, 640), 2.5, "<f4")
    packed = zlib.compress(depth.view("u1").reshape(-1, 4).T.copy().tobytes())
    rows, assets = [], {}
    for j in range(17):
        split = "train" if j < 12 else "test"
        action = "hold" if j in [10, 11, 16] else "forward"
        name = f"{split}-motion-{j:02d}-{action}"
        start = j * 20 - (16 if temporal_overlap and j == 13 else 0)
        pose = camera_pose(
            dict(
                vehicle_position_enu_m=[0, 100 if split == "test" else 0, 10],
                vehicle_quaternion_wxyz=[1, 0, 0, 0],
            )
        )
        delta = [0.0, 0.0, 0.0, 0.0] if action == "hold" else [3.0, 0.0, 0.0, 0.0]
        fs = []
        for i in range(start, start + 16):
            frame = dict(index=i, stamp_ns=i * 250_000_000, camera_pose=pose.tolist())
            for key, label, raw in [
                ("rgb", "onboard_rgb", rgb),
                ("depth", "onboard_depth", packed),
            ]:
                path = f"frames/{i:05d}-{label}.z"
                (root / path).write_bytes(raw)
                assets[path] = digest(root / path)
                frame[key] = dict(file=path)
            fs.append(frame)
        fx = 640 / (2 * np.tan(np.pi / 6))
        path = f"inputs/{name}.json"
        (root / path).write_text(
            json.dumps(dict(frames=fs, intrinsics=[[fx, 0, 320], [0, fx, 180], [0, 0, 1]]))
        )
        assets[path] = digest(root / path)
        row = dict(
            id=name,
            site=name,
            split=split,
            action=action,
            delta=delta,
            input=dict(file=path, sha256=assets[path]),
            input_indices=list(range(start, start + 16)),
            target_index=start + 19,
            input_cutoff_sim_s=(start + 15) / 4,
            observed_elapsed_sim_s=1.0,
            position_error_m=0.1,
            rotation_error_rad=0.01,
            candidate_dispatched=False,
            requested_camera_pose=action_pose(pose, delta).tolist(),
        )
        if split == "train":
            tp = f"targets/{name}.png"
            (root / tp).write_bytes(b"training-only-target")
            assets[tp] = digest(root / tp)
            row["training_target"] = dict(file=tp, sha256=assets[tp])
        rows.append(row)
    data = dict(
        schema_version="yokohama_motion_payload.v1",
        qualification=dict(status="passed"),
        test_targets_uploaded=False,
        training_targets_uploaded=True,
        spatial_separation_m=100.0,
        samples=rows,
        assets=assets,
    )
    return data


def frozen(root, data):
    (root / "dataset.json").write_text(json.dumps(data))
    return dict(dataset_manifest_sha256=digest(root / "dataset.json"))


def test_decode_exact_byteplanes_and_validate_complete_past_payload(tmp_path):
    d = payload(tmp_path)
    assert validate_payload(tmp_path, frozen(tmp_path, d), d)
    a = load_history(tmp_path, d["samples"][0])
    assert a["rgb"].shape == (16, 360, 640, 3)
    assert np.all(a["depth"] == 2.5)


@pytest.mark.parametrize(
    "change",
    [
        "future_pose",
        "requested_pose",
        "extra_target",
        "path_escape",
        "corrupt_pixels",
        "relabel_hash",
        "nan_error",
        "nearby_split",
    ],
)
def test_fail_closed_before_gpu_for_causal_and_identity_errors(tmp_path, change):
    d = payload(tmp_path)
    row = d["samples"][-1]
    if change == "future_pose":
        row["actual_target_camera_pose"] = row["requested_camera_pose"]
    elif change == "requested_pose":
        row["requested_camera_pose"][0][3] += 1
    elif change == "extra_target":
        (tmp_path / "targets" / (row["id"] + ".png")).write_bytes(b"future")
    elif change == "path_escape":
        row["id"] = "../test-motion-16-hold"
    elif change == "corrupt_pixels":
        (tmp_path / next(k for k in d["assets"] if k.startswith("frames/"))).write_bytes(b"corrupt")
    elif change == "relabel_hash":
        row["input"]["sha256"] = "0" * 64
    elif change == "nan_error":
        row["position_error_m"] = float("nan")
    else:
        for s in d["samples"][12:]:
            p = tmp_path / s["input"]["file"]
            doc = json.loads(p.read_text())
            for f in doc["frames"]:
                f["camera_pose"][0][3] -= 90
            s["requested_camera_pose"][0][3] -= 90
            p.write_text(json.dumps(doc))
            s["input"]["sha256"] = d["assets"][s["input"]["file"]] = digest(p)
        d["spatial_separation_m"] = 10.0
    with pytest.raises(ValueError):
        validate_payload(tmp_path, frozen(tmp_path, d), d)


def test_test_future_cannot_be_another_test_input(tmp_path):
    d = payload(tmp_path, temporal_overlap=True)
    with pytest.raises(ValueError, match="future frame leaked"):
        validate_payload(tmp_path, frozen(tmp_path, d), d)


def test_motion_adapter_is_fixed_to_previous_block_result():
    p = dict(
        learning_kind="motion-v4",
        steps=2048,
        lr=0.00005,
        seed=42,
        trainable_prefixes=[
            "final_layer.fuse_supervised.",
            "final_layer.linear.",
            "final_layer.attn.",
            "blocks.27.",
        ],
        payload_sha256={"initial-adapter.pt": BLOCK_ADAPTER_SHA256},
    )
    assert validate_configuration(p) == "motion-v4"
    for field, value in [("steps", 4096), ("seed", 43), ("lr", 0.0001)]:
        bad = copy.deepcopy(p)
        bad[field] = value
        with pytest.raises(ValueError):
            validate_configuration(bad)
