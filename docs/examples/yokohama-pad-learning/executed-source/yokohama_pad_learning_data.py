"""Portable, past-only data contract for offline pad WAM post-training."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

READER_SHA256 = "247f26aec4cbbe76e89871a30863adf484dadd3f02eba20c71d61ddbba0500b4"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def classify(rgb, reader):
    """The previous frozen grayscale reader; no new fitting on model outputs."""
    gray = rgb.astype(np.float32).mean(axis=2)
    high = gray - sum(np.roll(gray, (y, x), (0, 1)) for y in range(-2, 3) for x in range(-2, 3)) / 25
    high[:3] = high[-3:] = 0
    high[:, :3] = high[:, -3:] = 0
    distance = np.sqrt(np.mean((reader["features"] - high[reader["mask"]]) ** 2, axis=1))
    scores = {key: float(distance[reader["labels"] == key].min()) for key in ("occupied", "clear")}
    winner = min(scores, key=scores.get)
    margin = abs(scores["occupied"] - scores["clear"])
    return dict(state=winner if scores[winner] <= 24 and margin >= 1 else "unknown",
                grayscale_rmse=scores, class_margin=margin)


def history(root, sample):
    with np.load(root / sample["history"], allow_pickle=False) as archive:
        arrays = {key: archive[key] for key in archive.files}
    if set(arrays) != {"rgb", "depth", "poses", "stamps_ns", "intrinsics", "last_depth_infinite"}:
        raise ValueError("Unexpected history arrays")
    if arrays["rgb"].shape != (16, 360, 640, 3) or arrays["rgb"].dtype != np.uint8:
        raise ValueError("Wrong historical RGB")
    if (arrays["depth"].shape != (16, 360, 640) or not np.isfinite(arrays["depth"]).all()
            or (arrays["depth"] < 0).any() or (arrays["depth"] > 500).any()):
        raise ValueError("Wrong metric depth")
    if (arrays["poses"].shape != (16, 4, 4) or not np.isfinite(arrays["poses"]).all()
            or not np.allclose(arrays["poses"], arrays["poses"][-1], atol=1e-5)):
        raise ValueError("Nonstationary camera")
    stamps = arrays["stamps_ns"]
    if (stamps.shape != (16,) or stamps[-1] != sample["cutoff_stamp_ns"]
            or not np.all(np.abs(np.diff(stamps) - 250_000_000) <= 4_000_001)):
        raise ValueError("Cadence or cutoff changed")
    if arrays["intrinsics"].shape != (3, 3) or not np.isfinite(arrays["intrinsics"]).all():
        raise ValueError("Wrong camera intrinsics")
    if arrays["last_depth_infinite"].shape != (360, 640) or arrays["last_depth_infinite"].dtype != np.bool_:
        raise ValueError("Wrong unknown-depth mask")
    return arrays


def validate(root):
    data = json.loads((root / "dataset.json").read_text())
    if data["schema"] != "pad_learning_dataset.v1" or data["test_targets_uploaded"] is not False:
        raise ValueError("Wrong dataset boundary")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual != {"dataset.json", *data["assets"]}:
        raise ValueError("Unlisted payload or missing asset")
    for name, digest in data["assets"].items():
        path = root / name
        if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                or sha(path) != digest):
            raise ValueError("Changed or unsafe dataset asset")
    if data["assets"].get("reader.npz") != READER_SHA256:
        raise ValueError("Frozen development reader changed")
    samples = data["samples"]
    if len({s["id"] for s in samples}) != len(samples):
        raise ValueError("Duplicate sample IDs")
    if sum(s["split"] == "train" for s in samples) != 45:
        raise ValueError("Expected 45 fixed training pairs")
    if sum(s["split"] == "test" for s in samples) != 8:
        raise ValueError("Expected four fresh and four regression histories")
    if {s["history"] for s in samples if s["split"] == "train"} & {s["history"] for s in samples if s["split"] == "test"}:
        raise ValueError("Training/evaluation histories overlap")
    seen = set()
    for sample in samples:
        if (sample["frame_offset"] not in {1, 16, 64}
                or sample["horizon_sim_s"] != sample["frame_offset"] / 4
                or sample["history"] not in data["assets"]):
            raise ValueError("Time contract or input changed")
        if sample["split"] == "train":
            if (sample["training_target"] not in data["assets"]
                    or not sample["training_target"].startswith("train-targets/")
                    or not sample["id"].startswith("train-")):
                raise ValueError("Training target boundary")
            with Image.open(root / sample["training_target"]) as target:
                if target.size != (640, 360) or target.mode != "RGB":
                    raise ValueError("Training targets must be raw 640x360 RGB, cropped once")
        elif sample["split"] != "test" or "training_target" in sample or sample["frame_offset"] != 64:
            raise ValueError("Evaluation target leakage or time mismatch")
        if sample["history"] not in seen:
            history(root, sample)
            seen.add(sample["history"])
    if set(data["development_probes"]) != {
        "train-depart-20-t64", "train-stall-24-t64", "train-reenter-10-t64"
    }:
        raise ValueError("Development-only training trigger changed")
    if not set(data["development_probes"]) <= {s["id"] for s in samples if s["split"] == "train"}:
        raise ValueError("Development probes must belong to training data")
    return data
