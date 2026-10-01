#!/usr/bin/env python3
"""Prepare the ANWM lead-forecast study from recorded RGB, with held-out targets kept local.

Writes a GPU payload (per-sequence crop frames, lead-free backgrounds, sample
lists, training targets only) and a host-only truth file. Actor poses and
schedules label outcomes; they are never model inputs.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_wam_study import (  # noqa: E402
    CROPS,
    HISTORY,
    OFFSETS,
    crop,
    detect_lead,
    sha,
    write,
)


def phase(knots, t):
    for (ta, a), (tb, b) in zip(knots, knots[1:]):
        if t <= tb:
            if a == b:
                return "on_pad" if a == "start" else "hover" if a.startswith("up") else "away"
            if a == "start":
                return "ascending"
            if b == "start":
                return "landing"
            return "departing" if a.startswith("up") else "returning"
    return "on_pad" if knots[-1][1] == "start" else "away"


def occupancy(frame, pad):
    d = math.dist(frame["lead_pose"]["xyz"][:2], pad[:2])
    return "occupied" if d < 5.2 else "clear" if d > 6.8 else "unknown"


def prepare(capture, output, stride_train, stride_eval):
    output.mkdir(parents=True, exist_ok=False)
    payload = output / "payload"
    for d in [
        payload / "frames",
        payload / "train-targets",
        payload / "histories",
        payload / "backgrounds",
        output / "targets",
    ]:
        d.mkdir(parents=True)
    result = json.loads((capture / "capture-result.json").read_text())
    config = json.loads((capture / "config.json").read_text())
    if result["status"] != "passed" or config.get("motion_case_set") != "wam-improve-v1":
        raise ValueError("Unqualified capture")
    pad = config["world"]["pad_queue"]["pad_xyz_m"]
    samples, truth, assets = [], [], {}
    background_pool = {name: [] for name in CROPS}
    for entry in result["cases"]:
        folder = capture / entry["id"]
        if sha(folder / "capture.json") != entry["capture_sha256"]:
            raise ValueError("Capture changed")
        record = json.loads((folder / "capture.json").read_text())
        case, frames = record["case"], record["frames"]
        rgb = [
            np.asarray(Image.open(folder / f["assets"]["rgb"]["file"]).convert("RGB"))
            for f in frames
        ]
        for f in frames:
            if sha(folder / f["assets"]["rgb"]["file"]) != f["assets"]["rgb"]["sha256"]:
                raise ValueError("Frame changed")
        stamps = np.array([f["stamp_ns"] for f in frames], np.int64)
        for name in CROPS:
            crops = np.stack([crop(im, name) for im in rgb])
            train = case["split"] == "train"
            if train:
                # Training sequences may ship whole: their futures are training targets.
                rel = f"frames/{case['id']}-{name}.npz"
                np.savez_compressed(payload / rel, rgb=crops, stamps_ns=stamps)
                assets[rel] = sha(payload / rel)
                background_pool[name].extend(crops[::12])
            stride = stride_train if case["split"] == "train" else stride_eval
            for last in range(HISTORY - 1, len(frames) - max(OFFSETS), stride):
                if not train:
                    # Held-out samples ship only the 16 observed frames up to the cutoff.
                    rel = f"histories/{case['id']}-{last:03d}-{name}.npz"
                    np.savez_compressed(
                        payload / rel,
                        rgb=crops[last - HISTORY + 1 : last + 1],
                        stamps_ns=stamps[last - HISTORY + 1 : last + 1],
                    )
                    assets[rel] = sha(payload / rel)
                for offset in OFFSETS:
                    ident = f"{case['id']}-{last:03d}-t{offset:02d}-{name}"
                    now, future = frames[last], frames[last + offset]
                    sample = dict(
                        id=ident,
                        split=case["split"],
                        sequence=case["id"],
                        crop=name,
                        frames=rel,
                        last_index=last if train else HISTORY - 1,
                        offset=offset,
                        cutoff_stamp_ns=now["stamp_ns"],
                    )
                    target = crops[last + offset]
                    if train:
                        sample["target"] = f"train-targets/{ident}.png"
                        Image.fromarray(target).save(payload / sample["target"])
                        assets[sample["target"]] = sha(payload / sample["target"])
                    else:
                        Image.fromarray(target).save(output / "targets" / f"{ident}.png")
                    samples.append(sample)
                    truth.append(
                        dict(
                            id=ident,
                            split=case["split"],
                            phase_now=phase(case["knots"], now["elapsed_sim_s"]),
                            phase_future=phase(case["knots"], future["elapsed_sim_s"]),
                            occupancy_now=occupancy(now, pad),
                            occupancy_future=occupancy(future, pad),
                            lead_now_xyz_m=now["lead_pose"]["xyz"],
                            lead_future_xyz_m=future["lead_pose"]["xyz"],
                            readout_now=detect_lead(crops[last], name),
                            readout_future=detect_lead(target, name),
                            target_sha256=None
                            if train
                            else sha(output / "targets" / f"{ident}.png"),
                        )
                    )
    for name, pool in background_pool.items():
        plate = np.median(np.stack(pool), axis=0).astype(np.uint8)
        if detect_lead(plate, name)["present"]:
            raise ValueError("Background plate still contains the lead")
        rel = f"backgrounds/{name}.png"
        Image.fromarray(plate).save(payload / rel)
        assets[rel] = sha(payload / rel)
    write(
        payload / "dataset.json",
        dict(
            schema="pad_wam_study_dataset.v1",
            crops=CROPS,
            offsets=list(OFFSETS),
            history=HISTORY,
            capture_run_id=config["run_id"],
            evaluation_targets_uploaded=False,
            samples=samples,
            assets=assets,
        ),
    )
    write(output / "truth.json", truth)
    return dict(samples=len(samples), payload_files=len(assets))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--stride-train", type=int, default=4)
    p.add_argument("--stride-eval", type=int, default=4)
    a = p.parse_args()
    print(json.dumps(prepare(a.capture, a.output, a.stride_train, a.stride_eval)))
