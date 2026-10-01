#!/usr/bin/env python3
"""Prepare bounded dynamic WAM pairs; withhold every evaluation future image."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys
import zlib

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.prepare_yokohama_pad_temporal import label, read_image  # noqa: E402
from scripts.ship_anwm import camera_pose, crop_rgb  # noqa: E402
from scripts.screen_yokohama_pad_models import reference_eligibility  # noqa: E402
from scripts.yokohama_pad_learning_data import classify, sha, validate, write  # noqa: E402


def prepare(capture, previous, output):
    config = json.loads((capture / "config.json").read_text())
    receipt = json.loads((capture / "capture-result.json").read_text())
    if (receipt["status"] != "passed" or config["motion_case_set"] != "learning-v1"
            or not json.loads((capture / "cleanup.json").read_text())["removed"]):
        raise ValueError("Fresh acquisition/cleanup did not pass")
    output.mkdir(parents=True, exist_ok=False)
    data_root = output / "dataset"
    for path in (data_root / "histories", data_root / "train-targets", output / "targets"):
        path.mkdir(parents=True)
    shutil.copy2(previous / "prepared/reader.npz", data_root / "reader.npz")
    with np.load(data_root / "reader.npz", allow_pickle=False) as z:
        reader = {key: z[key] for key in z.files}
    pad = config["world"]["pad_queue"]["pad_xyz_m"]
    samples, truth, checks = [], [], []
    for entry in receipt["cases"]:
        folder = capture / entry["id"]
        if sha(folder / "capture.json") != entry["capture_sha256"]:
            raise ValueError("Capture changed")
        record = json.loads((folder / "capture.json").read_text())
        if record["world_sha256"] != config["world"]["world_sha256"]:
            raise ValueError("Foreign world")
        frames = record["frames"]
        for frame in frames:
            for asset in frame["assets"].values():
                if Path(asset["file"]).name != asset["file"] or sha(folder / asset["file"]) != asset["sha256"]:
                    raise ValueError("Frame bytes changed")
        for cutoff in record["case"]["cutoffs"]:
            last = min(range(len(frames)), key=lambda i: abs(frames[i]["elapsed_sim_s"] - cutoff))
            selected = frames[last - 15:last + 1]
            if len(selected) != 16:
                raise ValueError("Short history")
            arrays = dict(rgb=[], depth=[], poses=[], stamps_ns=[])
            for frame in selected:
                arrays["rgb"].append(np.asarray(Image.open(folder / frame["assets"]["rgb"]["file"]).convert("RGB")))
                raw = np.frombuffer(zlib.decompress((folder / frame["assets"]["depth"]["file"]).read_bytes()), "<f4").reshape(360, 640)
                arrays["depth"].append(np.where(np.isfinite(raw) & (raw > 0) & (raw <= 500), raw, 0))
                pose = frame["rig_pose"]
                arrays["poses"].append(camera_pose(dict(vehicle_position_enu_m=pose["xyz"], vehicle_quaternion_wxyz=pose["quat_wxyz"])))
                arrays["stamps_ns"].append(frame["stamp_ns"])
            arrays = {key: np.asarray(value) for key, value in arrays.items()}
            fx = 640 / (2 * math.tan(math.pi / 6))
            arrays.update(intrinsics=np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]]), last_depth_infinite=np.isposinf(raw))
            site = entry["id"] + f"-{cutoff:02d}"
            hist = f"histories/{site}.npz"
            np.savez_compressed(data_root / hist, **arrays)
            check = reference_eligibility(arrays, [0, 0, 0, 0])
            if not check["passed"]:
                raise ValueError("Input/reference eligibility failed before compute")
            checks.append(dict(id=site, **check))
            split = record["case"]["split"]
            for offset in ([1, 16, 64] if split == "train" else [64]):
                future = frames[last + offset]
                name = site + f"-t{offset:02d}"
                raw_target = folder / future["assets"]["rgb"]["file"]
                cropped = crop_rgb(np.asarray(Image.open(raw_target).convert("RGB")))
                sample = dict(id=name, split=split, history=hist, frame_offset=offset, horizon_sim_s=offset/4,
                              cutoff_stamp_ns=selected[-1]["stamp_ns"], source_run_id=config["run_id"], world_sha256=config["world"]["world_sha256"])
                if abs((future["stamp_ns"] - selected[-1]["stamp_ns"]) / 1e9 - offset/4) > 0.008:
                    raise ValueError("Incorrect target clock binding")
                if split == "train":
                    sample["training_target"] = f"train-targets/{name}.png"
                    # Keep 640x360: upstream transforms must crop exactly once.
                    shutil.copy2(raw_target, data_root / sample["training_target"])
                else:
                    Image.fromarray(cropped).save(output / "targets" / (name + ".png"))
                    predicted = classify(cropped, reader)
                    if predicted["state"] != read_image(cropped, reader)["state"]:
                        raise ValueError("Reader implementation changed")
                    target_label = label(future, pad)
                    if target_label == "unknown" or predicted["state"] != target_label:
                        raise ValueError("Fresh target not readable before GPU")
                    truth.append(dict(id=name, cohort="fresh", target_state=target_label, source_state=label(selected[-1], pad),
                                      target_stamp_ns=future["stamp_ns"], target_sha256=sha(output / "targets" / (name + ".png")),
                                      target_raw_sha256=sha(raw_target), capture_sha256=entry["capture_sha256"],
                                      source_frame_index=last, target_frame_index=last+offset, actual_reader=predicted))
                samples.append(sample)
    previous_data = json.loads((previous / "prepared/dataset.json").read_text())
    for old in previous_data["samples"]:
        name = "regression-" + old["id"]
        request = json.loads((previous / "prepared/inputs" / old["id"] / "request.json").read_text())
        hist = f"histories/{name}.npz"
        shutil.copy2(previous / "prepared/inputs" / old["id"] / "history.npz", data_root / hist)
        if sha(data_root / hist) != request["history_sha256"]:
            raise ValueError("Previous history changed")
        shutil.copy2(previous / "prepared/targets" / old["target_file"], output / "targets" / (name + ".png"))
        samples.append(dict(id=name, split="test", history=hist, frame_offset=64, horizon_sim_s=16,
                            cutoff_stamp_ns=old["source_stamp_ns"], source_run_id=request["source_run_id"], world_sha256=request["world_sha256"]))
        truth.append(dict(id=name, cohort="previously_inspected_regression", target_state=old["target_state"], source_state=old["source_state"],
                          target_stamp_ns=old["target_stamp_ns"], target_sha256=old["target_sha256"]))
    data = dict(schema="pad_learning_dataset.v1", samples=samples, test_targets_uploaded=False,
                reader_role="previous development-only grayscale templates, unchanged",
                development_probes=["train-depart-20-t64", "train-stall-24-t64", "train-reenter-10-t64"],
                assets={str(p.relative_to(data_root)): sha(p) for p in sorted(data_root.rglob("*")) if p.is_file()})
    write(data_root / "dataset.json", data)
    write(output / "evaluation-targets.json", dict(schema="pad_learning_targets.v1", rows=truth, dataset_sha256=sha(data_root / "dataset.json")))
    write(output / "input-checks.json", dict(status="passed", checks=checks, all_fresh_targets_readable=True, future_test_targets_uploaded=False))
    validate(data_root)
    print(json.dumps(dict(training_pairs=45, fresh_evaluation=4, regression=4, input_checks="passed", gpu_requested=False)))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    prepare(a.capture, a.previous, a.output)
