import copy
import json

import numpy as np
import pytest

from scripts.train_yokohama_anwm_head import validate
from src.runtime.yokohama_adaptation import (
    digest,
    optical_pose,
    site_plan,
    validate_split,
    verify_export,
)
from scripts.ship_anwm import action_pose


def test_spatial_split_rejects_nearby_context_and_target():
    sites = site_plan([[0, 0, 10], [70, 0, 10], [235, 0, 10], [235, 80, 10]])
    assert validate_split(sites) > 15
    bad = copy.deepcopy(sites)
    bad[-1]["endpoint_xyz"] = bad[0]["xyz"]
    with pytest.raises(ValueError, match="overlap"):
        validate_split(bad)


def test_optical_and_native_action_frames_agree():
    yaw = 0.73
    pose = optical_pose([5, 7, 18], yaw)
    target = action_pose(pose, [2.8, 0, 0, 0])
    expected = optical_pose([5 + 2.8 * np.cos(yaw), 7 + 2.8 * np.sin(yaw), 18], yaw)
    np.testing.assert_allclose(target, expected, atol=1e-12)


def manifest_at(root):
    sites = [
        dict(id=k, split=k, xyz=[x, 0, 10], endpoint_xyz=[x + 2.8, 0, 10])
        for k, x in [("train", 0), ("test", 50)]
    ]
    (root / "inputs").mkdir()
    (root / "targets").mkdir()
    rows = []
    for s in sites:
        ip = root / "inputs" / (s["id"] + ".npz")
        ip.write_bytes(s["id"].encode())
        for action in ["hold", "forward"]:
            name = s["id"] + "-" + action
            tp = root / "targets" / (name + ".png")
            tp.write_bytes(name.encode())
            rows.append(
                dict(
                    id=name,
                    site=s["id"],
                    split=s["split"],
                    action=action,
                    input=dict(file=str(ip.relative_to(root)), sha256=digest(ip)),
                    target=dict(file=str(tp.relative_to(root)), sha256=digest(tp)),
                    input_cutoff_sim_s=4,
                    target_sim_s=5,
                    position_error_m=0,
                    rotation_error_rad=0,
                )
            )
    return dict(sites=sites, samples=rows)


@pytest.mark.parametrize(
    "problem", ["past_target", "wrong_role", "bad_hash", "split_relabel", "pose_mismatch"]
)
def test_capture_verifier_rejects_evidence_errors(tmp_path, problem):
    m = manifest_at(tmp_path)
    assert verify_export(tmp_path, m)["status"] == "passed"
    row = m["samples"][-1]
    if problem == "past_target":
        row["target_sim_s"] = row["input_cutoff_sim_s"]
    elif problem == "wrong_role":
        row["input"] = row["target"]
    elif problem == "bad_hash":
        row["target"]["sha256"] = "0" * 64
    elif problem == "split_relabel":
        row["split"] = "train"
    elif problem == "pose_mismatch":
        row["rotation_error_rad"] = 0.02
    with pytest.raises(ValueError):
        verify_export(tmp_path, m)


def test_training_preflight_rejects_extra_evaluation_target(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    (d / "inputs").mkdir()
    (d / "targets").mkdir()
    rows, assets = [], {}
    for i in range(16):
        split = "train" if i < 12 else "test"
        site = f"{split}-{i}"
        ip = d / "inputs" / (site + ".npz")
        ip.write_bytes(site.encode())
        assets[str(ip.relative_to(d))] = digest(ip)
        for action in ["hold", "forward"]:
            name = site + "-" + action
            row = dict(id=name, site=site, split=split, input=dict(file=str(ip.relative_to(d))))
            if split == "train":
                tp = d / "targets" / (name + ".png")
                tp.write_bytes(name.encode())
                assets[str(tp.relative_to(d))] = digest(tp)
                row["training_target"] = dict(file=str(tp.relative_to(d)))
            rows.append(row)
    dataset = dict(
        assets=assets,
        samples=rows,
        test_targets_uploaded=False,
        qualification=dict(status="passed"),
    )
    (d / "dataset.json").write_text(json.dumps(dataset))
    protocol = dict(
        payload_sha256={},
        steps=512,
        lr=0.0001,
        seed=42,
        trainable_prefixes=["final_layer.fuse_supervised.", "final_layer.linear."],
    )
    (tmp_path / "protocol.json").write_text(json.dumps(protocol))
    assert validate(tmp_path)[0]["steps"] == 512
    (d / "unlisted-evaluation.png").write_bytes(b"withheld target")
    with pytest.raises(ValueError, match="payload"):
        validate(tmp_path)
