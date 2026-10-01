"""Past-only regional RGB contract for an offline native ANWM experiment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

CROP = (256, 48, 584, 328)
OFFSETS = (4, 16)  # 4 Hz: one and four simulator seconds


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def crop(rgb):
    return np.asarray(Image.fromarray(rgb).crop(CROP).resize((224, 224), Image.Resampling.BILINEAR))


def feature(rgb):
    gray = rgb.astype(np.float32).mean(axis=2)
    high = (
        gray - sum(np.roll(gray, (y, x), (0, 1)) for y in range(-2, 3) for x in range(-2, 3)) / 25
    )
    high[:3] = high[-3:] = 0
    high[:, :3] = high[:, -3:] = 0
    return high


def classify(rgb, reader):
    value = feature(rgb)[reader["mask"]]
    distance = np.sqrt(np.mean((reader["features"] - value) ** 2, axis=1))
    scores = {k: float(distance[reader["labels"] == k].min()) for k in ("occupied", "clear")}
    winner = min(scores, key=scores.get)
    margin = abs(scores["occupied"] - scores["clear"])
    return dict(
        state=winner if scores[winner] <= 24 and margin >= 1 else "unknown",
        scores=scores,
        margin=margin,
    )


def validate(root):
    data = json.loads((root / "dataset.json").read_text())
    if (
        data["schema"] != "pad_native_focus_dataset.v1"
        or data["crop_xyxy"] != list(CROP)
        or data["test_targets_uploaded"] is not False
    ):
        raise ValueError("Unreviewed regional input profile")
    files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if files != {"dataset.json", *data["assets"]}:
        raise ValueError("Unexpected payload")
    for name, digest in data["assets"].items():
        p = root / name
        if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()) or sha(p) != digest:
            raise ValueError("Unsafe or changed asset")
    seen = set()
    for sample in data["samples"]:
        if sample["id"] in seen:
            raise ValueError("Duplicate sample")
        seen.add(sample["id"])
        if sample["offset"] not in OFFSETS or sample["history"] not in data["assets"]:
            raise ValueError("Time or history contract")
        if sample["split"] == "train":
            if sample.get("target") not in data["assets"] or not sample["target"].startswith(
                "train-targets/"
            ):
                raise ValueError("Unbound training target")
        elif sample["split"] != "test" or "target" in sample or "truth" in sample:
            raise ValueError("Evaluation target leakage")
        with np.load(root / sample["history"], allow_pickle=False) as a:
            if (
                set(a.files) != {"rgb", "stamps_ns"}
                or a["rgb"].shape != (16, 224, 224, 3)
                or a["rgb"].dtype != np.uint8
            ):
                raise ValueError("Unexpected observation fields")
            if a["stamps_ns"][-1] != sample["cutoff_stamp_ns"] or not np.all(
                np.abs(np.diff(a["stamps_ns"]) - 250000000) <= 4000001
            ):
                raise ValueError("Future input or wrong cadence")
    return data
