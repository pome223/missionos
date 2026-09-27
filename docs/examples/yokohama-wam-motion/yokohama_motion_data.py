"""Compact, past-only ANWM histories; no simulator, model or cloud side effects."""

from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
import zlib
import numpy as np


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_history(root, sample):
    root = Path(root)
    d = json.loads((root / sample["input"]["file"]).read_text())
    rgb, depth = [], []
    for f in d["frames"]:
        rgb.append(
            np.frombuffer(
                zlib.decompress((root / f["rgb"]["file"]).read_bytes()), np.uint8
            ).reshape(360, 640, 3)
        )
        raw = zlib.decompress((root / f["depth"]["file"]).read_bytes())
        if len(raw) != 360 * 640 * 4:
            raise ValueError("Invalid packed depth")
        packed = np.frombuffer(raw, np.uint8).reshape(4, -1).T.copy()
        depth.append(packed.reshape(-1).view("<f4").reshape(360, 640))
    return dict(
        rgb=np.asarray(rgb),
        depth=np.asarray(depth),
        poses=np.asarray([f["camera_pose"] for f in d["frames"]]),
        stamps_s=np.asarray([f["stamp_ns"] / 1e9 for f in d["frames"]]),
        intrinsics=np.asarray(d["intrinsics"]),
    )


def validate_payload(root, protocol, data):
    root = Path(root)
    if (
        data.get("schema_version") != "yokohama_motion_payload.v1"
        or data.get("qualification", {}).get("status") != "passed"
        or data.get("test_targets_uploaded") is not False
        or data.get("training_targets_uploaded") is not True
        or len(data.get("samples", [])) != 17
        or sum(s["split"] == "train" for s in data["samples"]) != 12
        or sum(s["split"] == "test" for s in data["samples"]) != 5
        or digest(root / "dataset.json") != protocol["dataset_manifest_sha256"]
    ):
        raise ValueError("Unqualified motion payload")
    expected = {"dataset.json"}
    all_input_indices = set()
    test_target_indices = set()
    points = {"train": [], "test": []}
    ids = set()
    for s in data["samples"]:
        if (
            not re.fullmatch(r"(train|test)-motion-\d{2}-(hold|forward)", s["id"])
            or not s["id"].startswith(s["split"] + "-")
            or not s["id"].endswith("-" + s["action"])
            or s["id"] in ids
            or s["site"] != s["id"]
            or s["split"] not in points
            or "actual_target_camera_pose" in s
        ):
            raise ValueError("Invalid motion sample identity")
        ids.add(s["id"])
        if s["input"]["file"] != f"inputs/{s['id']}.json":
            raise ValueError("Invalid motion history role")
        expected.add(s["input"]["file"])
        if s["input"]["sha256"] != data["assets"].get(s["input"]["file"]):
            raise ValueError("History hash binding mismatch")
        d = json.loads((root / s["input"]["file"]).read_text())
        if len(d["frames"]) != 16:
            raise ValueError("Motion context requires 16 frames")
        stamps = np.array([f["stamp_ns"] / 1e9 for f in d["frames"]])
        indices = [f["index"] for f in d["frames"]]
        if (
            any(type(i) is not int or i < 0 for i in indices)
            or indices != s["input_indices"]
            or any(b != a + 1 for a, b in zip(indices, indices[1:]))
            or np.max(np.abs(np.diff(stamps) - 0.25)) > 0.004000001
            or abs(stamps[-1] - s["input_cutoff_sim_s"]) > 1e-8
            or s["target_index"] != indices[-1] + 4
            or abs(s["observed_elapsed_sim_s"] - 1) > 0.008000001
        ):
            raise ValueError("Noncausal or incomplete motion history")
        all_input_indices.update(indices)
        for f in d["frames"]:
            for k, label in [("rgb", "onboard_rgb"), ("depth", "onboard_depth")]:
                name = f"frames/{f['index']:05d}-{label}.z"
                if f[k]["file"] != name:
                    raise ValueError("Invalid frame asset role")
                expected.add(name)
            p = np.asarray(f["camera_pose"])
            if p.shape != (4, 4) or not np.isfinite(p).all() or not np.allclose(p[3], [0, 0, 0, 1]):
                raise ValueError("Invalid motion camera pose")
            if (
                not np.allclose(p[:3, :3].T @ p[:3, :3], np.eye(3), atol=1e-6)
                or abs(np.linalg.det(p[:3, :3]) - 1) > 1e-6
            ):
                raise ValueError("Motion pose is not rigid")
            points[s["split"]].append(p[:3, 3])
        k = np.asarray(d["intrinsics"])
        fx = 640 / (2 * np.tan(np.pi / 6))
        if not np.allclose(k, [[fx, 0, 320], [0, fx, 180], [0, 0, 1]], atol=1e-5):
            raise ValueError("Unexpected motion intrinsics")
        if s["action"] not in ["hold", "forward"] or s["delta"] != (
            [0.0, 0.0, 0.0, 0.0] if s["action"] == "hold" else [3.0, 0.0, 0.0, 0.0]
        ):
            raise ValueError("Unqualified motion action")
        if (
            not 0 <= s["position_error_m"] <= 0.3
            or not 0 <= s["rotation_error_rad"] <= 0.05
            or s["candidate_dispatched"] is not False
        ):
            raise ValueError("Motion target or authority mismatch")
        target = np.asarray(d["frames"][-1]["camera_pose"]).copy()
        forward = np.r_[target[:2, 2], 0.0]
        forward /= np.linalg.norm(forward)
        target[:3, 3] += forward * s["delta"][0]
        if not np.allclose(target, s["requested_camera_pose"], atol=1e-8, rtol=0):
            raise ValueError("Prediction target was not derived from past pose")
        points[s["split"]].append(target[:3, 3])
        if s["split"] == "train":
            if s.get("training_target", {}).get("file") != f"targets/{s['id']}.png":
                raise ValueError("Training target role mismatch")
            expected.add(s["training_target"]["file"])
            if s["training_target"]["sha256"] != data["assets"].get(s["training_target"]["file"]):
                raise ValueError("Training target hash binding mismatch")
        else:
            if "training_target" in s:
                raise ValueError("Test target leaked")
            test_target_indices.add(s["target_index"])
    if all_input_indices & test_target_indices:
        raise ValueError("A test future frame leaked through another input history")
    a, b = np.asarray(points["train"]), np.asarray(points["test"])
    gap = float(np.linalg.norm(a[:, None] - b[None], axis=2).min())
    # Both actual endpoints lie within 0.3 m of requested endpoints. This
    # conservative 0.6 m margin keeps actual train/test positions >= 15 m apart.
    if not np.isfinite(gap) or gap < 15.6 or abs(gap - data["spatial_separation_m"]) > 1e-8:
        raise ValueError("Motion train/test overlap")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual != expected or set(data["assets"]) != expected - {"dataset.json"}:
        raise ValueError("Unexpected motion payload asset")
    for name, h in data["assets"].items():
        p = root / name
        if p.is_symlink() or digest(p) != h:
            raise ValueError("Motion payload hash mismatch")
    if len(test_target_indices) != 5:
        raise ValueError("Duplicate test outcomes")
    # Decode all inputs as a final shape/numeric check before any GPU import.
    for s in data["samples"]:
        arrays = load_history(root, s)
        if (
            arrays["rgb"].shape != (16, 360, 640, 3)
            or arrays["depth"].shape != (16, 360, 640)
            or np.isnan(arrays["depth"]).any()
            or np.isneginf(arrays["depth"]).any()
            or (arrays["depth"] < 0).any()
        ):
            raise ValueError("Invalid motion pixel array")
    return True
