#!/usr/bin/env python3
"""Prepare actual recorded RGB crops, with evaluation future images kept local."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_focus_data import (
    CROP,
    OFFSETS,
    crop,
    feature,
    classify,
    sha,
    write,
    validate,
)


def state(frame, pad):
    d = float(np.linalg.norm(np.asarray(frame["lead_pose"]["xyz"][:2]) - pad[:2]))
    return "occupied" if d < 5.2 else "clear" if d > 6.8 else "unknown"


def prepare(training, evaluation, output):
    output.mkdir(parents=True, exist_ok=False)
    data = output / "dataset"
    for d in [data / "histories", data / "train-targets", output / "targets", output / "observed"]:
        d.mkdir(parents=True)
    samples, truth, train_pixels, train_labels, bindings = [], [], [], [], []
    training_hashes = set()
    for capture, split in [(p, "train") for p in training] + [(evaluation, "test")]:
        result = json.loads((capture / "capture-result.json").read_text())
        cfg = json.loads((capture / "config.json").read_text())
        assert (
            result["status"] == "passed"
            and json.loads((capture / "cleanup.json").read_text())["removed"]
        )
        pad = np.asarray(cfg["world"]["pad_queue"]["pad_xyz_m"])
        for entry in result["cases"]:
            folder = capture / entry["id"]
            assert sha(folder / "capture.json") == entry["capture_sha256"]
            record = json.loads((folder / "capture.json").read_text())
            if split == "train" and record["case"]["split"] != "train":
                continue
            frames = record["frames"]
            rgb = []
            for frame in frames:
                asset = frame["assets"]["rgb"]
                assert (
                    Path(asset["file"]).name == asset["file"]
                    and sha(folder / asset["file"]) == asset["sha256"]
                )
                rgb.append(crop(np.asarray(Image.open(folder / asset["file"]).convert("RGB"))))
            rgb = np.asarray(rgb)
            if split == "train":
                for im, f in zip(rgb[::4], frames[::4]):
                    label = state(f, pad)
                    if label != "unknown":
                        train_pixels.append(im)
                        train_labels.append(label)
                cutoffs = range(4, int(frames[-1]["elapsed_sim_s"]) - 4, 2)
            else:
                cutoffs = record["case"]["cutoffs"]
            for cutoff in cutoffs:
                last = min(
                    range(len(frames)), key=lambda i: abs(frames[i]["elapsed_sim_s"] - cutoff)
                )
                assert last >= 15 and last + 16 < len(frames)
                site = entry["id"] + f"-{cutoff:02d}"
                h = f"histories/{site}.npz"
                hist = rgb[last - 15 : last + 1]
                np.savez_compressed(
                    data / h,
                    rgb=hist,
                    stamps_ns=[f["stamp_ns"] for f in frames[last - 15 : last + 1]],
                )
                import hashlib

                hh = hashlib.sha256(hist.tobytes()).hexdigest()
                if split == "train":
                    training_hashes.add(hh)
                else:
                    Image.fromarray(
                        np.asarray(
                            Image.open(folder / frames[last]["assets"]["rgb"]["file"]).convert(
                                "RGB"
                            )
                        )
                    ).save(output / "observed" / (site + ".png"))
                for offset in OFFSETS:
                    future = frames[last + offset]
                    assert (
                        abs((future["stamp_ns"] - frames[last]["stamp_ns"]) / 1e9 - offset / 4)
                        < 0.008
                    )
                    name = site + f"-t{offset:02d}"
                    sample = dict(
                        id=name,
                        split=split,
                        history=h,
                        offset=offset,
                        cutoff_stamp_ns=frames[last]["stamp_ns"],
                    )
                    target = rgb[last + offset]
                    if split == "train":
                        sample["target"] = f"train-targets/{name}.png"
                        Image.fromarray(target).save(data / sample["target"])
                    else:
                        Image.fromarray(target).save(output / "targets" / (name + ".png"))
                        truth.append(
                            dict(
                                id=name,
                                state=state(future, pad),
                                current_state=state(frames[last], pad),
                                stamp_ns=future["stamp_ns"],
                                target_sha256=sha(output / "targets" / (name + ".png")),
                                history_pixels_sha256=hh,
                                exact_training_history_overlap=hh in training_hashes,
                            )
                        )
                    samples.append(sample)
                    bindings.append(
                        dict(
                            id=name,
                            run_id=cfg["run_id"],
                            capture_sha256=entry["capture_sha256"],
                            world_sha256=record["world_sha256"],
                            source_index=last,
                            target_index=last + offset,
                            raw_future_sha256=future["assets"]["rgb"]["sha256"],
                        )
                    )
    # Fit the restricted image reader and foreground definition on train RGB only.
    pixels = np.asarray(train_pixels)
    background = np.median(pixels, axis=0).astype(np.uint8)
    mask = np.max(np.abs(pixels.astype(float) - background), axis=(0, 3)) > 24
    mask[:3] = mask[-3:] = False
    mask[:, :3] = mask[:, -3:] = False
    reader = dict(
        features=np.asarray([feature(p)[mask] for p in pixels]),
        labels=np.asarray(train_labels),
        mask=mask,
    )
    np.savez_compressed(data / "reader.npz", **reader)
    Image.fromarray(background).save(data / "background.png")
    manifest = dict(
        schema="pad_native_focus_dataset.v1",
        crop_xyxy=list(CROP),
        test_targets_uploaded=False,
        samples=samples,
        assets={str(p.relative_to(data)): sha(p) for p in sorted(data.rglob("*")) if p.is_file()},
    )
    write(data / "dataset.json", manifest)
    for row in truth:
        row["actual_reader"] = classify(
            np.asarray(Image.open(output / "targets" / (row["id"] + ".png"))), reader
        )
    write(output / "truth.json", truth)
    write(output / "capture-bindings.json", bindings)
    transition = [r for r in truth if r["state"] != "unknown" and r["state"] != r["current_state"]]
    write(
        output / "admission.json",
        dict(
            boundary="offline native endpoint capability; no mission-value adoption",
            evaluation_conditions=len(truth),
            changed_state_conditions=len(transition),
            current_state_persistence_errors=sum(
                r["state"] in {"clear", "occupied"} and r["state"] != r["current_state"]
                for r in truth
            ),
            future_truth_oracle_errors=0,
            ideal_rules_win_required=False,
            fixed_accuracy_gate=False,
            test_reader_matches=sum(r["state"] == r["actual_reader"]["state"] for r in truth),
            no_flight_or_terminal_benefit_claim=True,
        ),
    )
    validate(data)
    write(
        output / "input-checks.json",
        dict(
            status="passed",
            training_pairs=sum(s["split"] == "train" for s in samples),
            test_pairs=len(truth),
            gpu_requested=False,
        ),
    )
    print((output / "input-checks.json").read_text())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--training", type=Path, nargs="+", required=True)
    p.add_argument("--evaluation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    prepare(a.training, a.evaluation, a.output)
