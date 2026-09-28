"""Post-training preparation boundaries without GPU or simulator execution."""

import json
from pathlib import Path
import shutil

import numpy as np
from PIL import Image
import pytest

from scripts.capture_yokohama_pad_motion import learning_cases
from scripts.prepare_yokohama_pad_temporal import read_image
from scripts.yokohama_pad_learning_data import classify, sha, validate


def tiny_dataset(root):
    (root / "histories").mkdir()
    (root / "train-targets").mkdir()
    published = Path(__file__).parents[1] / "docs/examples/yokohama-pad-temporal/prepared/reader.npz"
    shutil.copy2(published, root / "reader.npz")
    for split, offset in (("train", 0), ("test", 10_000_000_000)):
        np.savez_compressed(root / f"histories/{split}.npz",
                            rgb=np.zeros((16, 360, 640, 3), np.uint8),
                            depth=np.ones((16, 360, 640), np.float32),
                            poses=np.repeat(np.eye(4)[None], 16, axis=0), intrinsics=np.eye(3),
                            stamps_ns=np.arange(16, dtype=np.int64)*250_000_000+offset,
                            last_depth_infinite=np.zeros((360, 640), bool))
    Image.new("RGB", (640, 360)).save(root / "train-targets/target.png")
    samples = [dict(id=f"train-{i}", split="train", history="histories/train.npz", frame_offset=64,
                    horizon_sim_s=16, cutoff_stamp_ns=3_750_000_000, training_target="train-targets/target.png") for i in range(45)]
    samples += [dict(id=f"test-{i}", split="test", history="histories/test.npz", frame_offset=64,
                     horizon_sim_s=16, cutoff_stamp_ns=13_750_000_000) for i in range(8)]
    for sample, name in zip(samples, ["train-depart-20-t64", "train-stall-24-t64", "train-reenter-10-t64"]):
        sample["id"] = name
    data = dict(schema="pad_learning_dataset.v1", test_targets_uploaded=False, samples=samples,
                development_probes=["train-depart-20-t64", "train-stall-24-t64", "train-reenter-10-t64"],
                assets={str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()})
    (root / "dataset.json").write_text(json.dumps(data))
    return data


def test_evaluation_targets_and_training_history_overlap_are_rejected(tmp_path):
    data = tiny_dataset(tmp_path)
    assert len(validate(tmp_path)["samples"]) == 53
    data["samples"][-1]["training_target"] = "train-targets/target.png"
    (tmp_path / "dataset.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="leakage"):
        validate(tmp_path)
    del data["samples"][-1]["training_target"]
    data["samples"][-1]["history"] = "histories/train.npz"
    (tmp_path / "dataset.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="overlap"):
        validate(tmp_path)


def test_pre_cropped_training_target_is_rejected(tmp_path):
    data = tiny_dataset(tmp_path)
    path = tmp_path / "train-targets/target.png"
    Image.new("RGB", (224, 224)).save(path)
    data["assets"]["train-targets/target.png"] = sha(path)
    (tmp_path / "dataset.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="cropped once"):
        validate(tmp_path)


def test_portable_reader_preserves_previous_algorithm():
    published = Path(__file__).parents[1] / "docs/examples/yokohama-pad-temporal"
    with np.load(published / "prepared/reader.npz", allow_pickle=False) as archive:
        reader = {k: archive[k] for k in archive.files}
    for name in ("evaluation-depart-16", "evaluation-stall-24", "evaluation-reenter-14"):
        pixels = np.asarray(Image.open(published / f"prepared/targets/{name}.png").convert("RGB"))
        old = read_image(pixels, reader)
        assert classify(pixels, reader) == {k: old[k] for k in ("state", "grayscale_rmse", "class_margin")}


def test_fresh_timing_splits_cover_every_training_and_test_target():
    cases = learning_cases()
    assert [c["split"] for c in cases] == ["train"]*3+["test"]*3
    assert len({tuple(tuple(k) for k in c["knots"]) for c in cases}) == 6
    for case in cases:
        assert min(case["cutoffs"]) >= 4
        assert max(case["cutoffs"])+16 < case["knots"][-1][0]
