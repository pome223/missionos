#!/usr/bin/env python3
"""Build past-only WAM inputs and separately held-back dynamic evaluation targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import zlib

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ship_anwm import camera_pose, crop_rgb  # noqa: E402
from scripts.screen_yokohama_pad_models import reference_eligibility  # noqa: E402


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(p, value):
    p.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def label(frame, pad):
    distance = math.dist(frame["lead_pose"]["xyz"][:2], pad[:2])
    return "occupied" if distance < 5.2 else "clear" if distance > 6.8 else "unknown"


def gray_features(rgb, mask=None):
    # Luminance only: no orange/red threshold and no pose/schedule input.
    g = rgb.astype(np.float32).mean(axis=2)
    highpass = (
        g - sum(np.roll(g, (dy, dx), (0, 1)) for dy in range(-2, 3) for dx in range(-2, 3)) / 25
    )
    highpass[:3] = highpass[-3:] = 0
    highpass[:, :3] = highpass[:, -3:] = 0
    return highpass if mask is None else highpass[mask]


def read_image(rgb, reader):
    values = gray_features(rgb, reader["mask"])
    distances = np.sqrt(np.mean((reader["features"] - values) ** 2, axis=1))
    scores = {k: float(distances[reader["labels"] == k].min()) for k in ("occupied", "clear")}
    winner = min(scores, key=scores.get)
    gap = abs(scores["occupied"] - scores["clear"])
    predicted = winner if scores[winner] <= 24 and gap >= 1 else "unknown"
    return dict(
        state=predicted,
        grayscale_rmse=scores,
        class_margin=gap,
        scope="fixed camera, known lead corridor, development-frame templates; not general object detection",
    )


def prepare(source, output):
    receipt = json.loads((source / "capture-result.json").read_text())
    if receipt["status"] != "passed":
        raise ValueError("Acquisition did not pass")
    config = json.loads((source / "config.json").read_text())
    pad = config["world"]["pad_queue"]["pad_xyz_m"]
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    targets = output / "targets"
    targets.mkdir()
    images, framesets, records = {}, {}, []
    for item in receipt["cases"]:
        folder = source / item["id"]
        capture = folder / "capture.json"
        if sha(capture) != item["capture_sha256"]:
            raise ValueError("Capture hash changed")
        record = json.loads(capture.read_text())
        if record["world_sha256"] != config["world"]["world_sha256"]:
            raise ValueError("Foreign world")
        rows = record["frames"]
        framesets[item["id"]] = rows
        values = []
        for row in rows:
            for entry in row["assets"].values():
                if (
                    Path(entry["file"]).name != entry["file"]
                    or sha(folder / entry["file"]) != entry["sha256"]
                ):
                    raise ValueError("Changed or unsafe frame asset")
            values.append(
                crop_rgb(
                    np.asarray(Image.open(folder / row["assets"]["rgb"]["file"]).convert("RGB"))
                )
            )
        images[item["id"]] = np.stack(values)
        records.append(record)
    development = records[0]
    if development["case"]["split"] != "development":
        raise ValueError("Development split changed")
    dev_images = images[development["case"]["id"]]
    high = np.stack([gray_features(im) for im in dev_images])
    mask = high.std(axis=0) > 3
    if not 20 <= int(mask.sum()) <= 10000:
        raise ValueError("Reader development visibility insufficient")
    indices = [
        i for i, r in enumerate(development["frames"]) if i % 2 == 0 and label(r, pad) != "unknown"
    ]
    reader = dict(
        mask=mask,
        features=high[indices][:, mask],
        labels=np.array([label(development["frames"][i], pad) for i in indices]),
    )
    np.savez_compressed(output / "reader.npz", **reader)
    reader_rows = []
    for record in records[1:]:
        for i, frame in enumerate(record["frames"]):
            truth = label(frame, pad)
            result = read_image(images[record["case"]["id"]][i], reader)
            reader_rows.append(
                dict(
                    case=record["case"]["id"],
                    frame=i,
                    stamp_ns=frame["stamp_ns"],
                    truth=truth,
                    **result,
                )
            )
    known = [r for r in reader_rows if r["truth"] != "unknown"]
    metrics = {
        k: dict(
            count=sum(r["truth"] == k for r in known),
            correct=sum(r["truth"] == k and r["state"] == k for r in known),
        )
        for k in ("occupied", "clear")
    }
    passed = all(v["count"] > 0 and v["correct"] / v["count"] >= 0.85 for v in metrics.values())
    write(
        output / "reader-evaluation.json",
        dict(
            schema="pad_grayscale_reader_evaluation.v1",
            passed=passed,
            metrics=metrics,
            rows=reader_rows,
            training="development images only; WAM weights unchanged",
            scope="same camera and corridor, different actor timings, no generalization to other scenes",
        ),
    )
    samples = []
    for record in records[1:]:
        case = record["case"]
        folder = source / case["id"]
        rows = record["frames"]
        for cutoff in case["cutoffs"]:
            last = min(range(len(rows)), key=lambda i: abs(rows[i]["elapsed_sim_s"] - cutoff))
            selected = rows[last - 15 : last + 1]
            if len(selected) != 16:
                raise ValueError("Insufficient history")
            arrays = dict(rgb=[], depth=[], poses=[], stamps_ns=[])
            raw = None
            for f in selected:
                arrays["rgb"].append(
                    np.asarray(Image.open(folder / f["assets"]["rgb"]["file"]).convert("RGB"))
                )
                raw = np.frombuffer(
                    zlib.decompress((folder / f["assets"]["depth"]["file"]).read_bytes()), "<f4"
                ).reshape(360, 640)
                arrays["depth"].append(
                    np.where(np.isfinite(raw) & (raw > 0) & (raw <= 500), raw, 0)
                )
                pose = f["rig_pose"]
                arrays["poses"].append(
                    camera_pose(
                        dict(
                            vehicle_position_enu_m=pose["xyz"],
                            vehicle_quaternion_wxyz=pose["quat_wxyz"],
                        )
                    )
                )
                arrays["stamps_ns"].append(f["stamp_ns"])
            arrays = {k: np.asarray(v) for k, v in arrays.items()}
            fx = 640 / (2 * math.tan(math.pi / 6))
            arrays["intrinsics"] = np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]])
            arrays["last_depth_infinite"] = np.isposinf(raw)
            check = reference_eligibility(arrays, [0, 0, 0, 0])
            name = case["id"] + f"-{cutoff:02d}"
            dest = inputs / name
            dest.mkdir()
            np.savez_compressed(dest / "history.npz", **arrays)
            Image.fromarray(images[case["id"]][last]).save(dest / "observed.png")
            future = rows[last + 64]
            if abs((future["stamp_ns"] - selected[-1]["stamp_ns"]) / 1e9 - 16) > 0.008:
                raise ValueError("Wrong target horizon")
            Image.fromarray(images[case["id"]][last + 64]).save(targets / (name + ".png"))
            req = dict(
                schema="yokohama_pad_temporal_request.v1",
                history_sha256=sha(dest / "history.npz"),
                source_run_id=config["run_id"],
                world_sha256=config["world"]["world_sha256"],
                cutoff_stamp_ns=selected[-1]["stamp_ns"],
                frame_period_s=0.25,
                model_frame_offset=64,
                requested_horizon_sim_s=16,
                diffusion_steps=50,
                seed=42,
                candidates=[dict(id="hold", delta=[0, 0, 0, 0])],
                dispatch_allowed=False,
                model_time_alignment_verified=False,
                future_ground_truth_used_for_forecast=False,
            )
            write(dest / "request.json", req)
            samples.append(
                dict(
                    id=name,
                    request_sha256=sha(dest / "request.json"),
                    input_self_check=check,
                    target_file=name + ".png",
                    target_sha256=sha(targets / (name + ".png")),
                    source_frame_index=last,
                    target_frame_index=last + 64,
                    source_state=label(selected[-1], pad),
                    target_state=label(future, pad),
                    source_stamp_ns=selected[-1]["stamp_ns"],
                    target_stamp_ns=future["stamp_ns"],
                    actual_target_reader=read_image(images[case["id"]][last + 64], reader),
                )
            )
    write(
        inputs / "manifest.json",
        dict(
            schema="pad_temporal_inputs.v1",
            samples=[dict(id=s["id"], request_sha256=s["request_sha256"]) for s in samples],
            future_targets_included=False,
        ),
    )
    write(
        output / "dataset.json",
        dict(
            schema="pad_temporal_dataset.v1",
            reader_passed=passed,
            reader_sha256=sha(output / "reader.npz"),
            samples=samples,
            training_on_evaluation=False,
            model_weights_changed=False,
            flight_claimed=False,
        ),
    )
    print(
        json.dumps(
            dict(
                reader_passed=passed,
                metrics=metrics,
                samples=len(samples),
                input_eligibility=all(s["input_self_check"]["passed"] for s in samples),
            )
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    prepare(a.capture, a.output)
