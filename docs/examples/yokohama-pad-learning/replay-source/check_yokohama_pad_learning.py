#!/usr/bin/env python3
"""CPU-only integrity and evaluation reopening of the dynamic post-training bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_yokohama_pad_temporal import same  # noqa: E402
from scripts.evaluate_yokohama_pad_learning import evaluate  # noqa: E402
from scripts.prepare_yokohama_pad_temporal import label  # noqa: E402
from scripts.ship_anwm import camera_pose, crop_rgb  # noqa: E402
from scripts.yokohama_pad_learning_data import history, sha  # noqa: E402


def check(bundle):
    manifest = json.loads((bundle / "evidence-manifest.json").read_text())
    listed = set()
    for item in manifest["files"]:
        path = bundle / item["path"]
        if (not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink()
                or item["path"] in listed or path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]):
            raise ValueError("Public evidence changed")
        listed.add(item["path"])
    if {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} - {"evidence-manifest.json"} != listed:
        raise ValueError("Unlisted or missing evidence")
    protocol = json.loads((bundle / "learning-protocol.json").read_text())
    for filename, digest in protocol["source_sha256"].items():
        if Path(filename).name != filename or sha(bundle / "executed-source" / filename) != digest:
            raise ValueError("Executed model source changed")
    capture = bundle / "capture"
    config = json.loads((capture / "config.json").read_text())
    receipt = json.loads((capture / "capture-result.json").read_text())
    if (receipt["status"] != "passed" or receipt["aircraft_flown"] or receipt["native_inference"]
            or not json.loads((capture / "cleanup.json").read_text())["removed"]
            or sha(capture / "world.sdf") != config["world"]["world_sha256"]):
        raise ValueError("CPU acquisition boundary changed")
    captures = {}
    for entry in receipt["cases"]:
        path = capture / (entry["id"] + ".json")
        if sha(path) != entry["capture_sha256"]:
            raise ValueError("Capture records changed")
        captures[entry["id"]] = json.loads(path.read_text())
    prepared = bundle / "prepared"
    data = json.loads((prepared / "dataset/dataset.json").read_text())
    truth = {r["id"]: r for r in json.loads((prepared / "evaluation-targets.json").read_text())["rows"]}
    for sample in data["samples"]:
        if sample["id"].startswith("regression-"):
            continue  # Old capture bindings remain verified in the temporal bundle.
        case, cutoff = Path(sample["history"]).stem.rsplit("-", 1)
        record = captures[case]
        frames = record["frames"]
        last = min(range(len(frames)), key=lambda i: abs(frames[i]["elapsed_sim_s"] - int(cutoff)))
        arrays = history(prepared / "dataset", sample)
        for frame, pose, stamp in zip(frames[last-15:last+1], arrays["poses"], arrays["stamps_ns"]):
            expected_pose = camera_pose(dict(vehicle_position_enu_m=frame["rig_pose"]["xyz"],
                                            vehicle_quaternion_wxyz=frame["rig_pose"]["quat_wxyz"]))
            if stamp != frame["stamp_ns"] or not np.allclose(pose, expected_pose, atol=1e-8):
                raise ValueError("Measured camera history binding changed")
        future = frames[last + sample["frame_offset"]]
        if sample["source_run_id"] != record["run_id"] or sample["world_sha256"] != record["world_sha256"]:
            raise ValueError("Source world binding changed")
        if sample["split"] == "train":
            if sha(prepared / "dataset" / sample["training_target"]) != future["assets"]["rgb"]["sha256"]:
                raise ValueError("Training target no longer matches captured future")
        else:
            target = truth[sample["id"]]
            raw = capture / "target-frames" / (sample["id"] + ".png")
            if (sha(raw) != future["assets"]["rgb"]["sha256"] or sha(raw) != target["target_raw_sha256"]
                    or target["source_frame_index"] != last or target["target_frame_index"] != last + 64
                    or target["target_stamp_ns"] != future["stamp_ns"]
                    or label(future, config["world"]["pad_queue"]["pad_xyz_m"]) != target["target_state"]
                    or label(frames[last], config["world"]["pad_queue"]["pad_xyz_m"]) != target["source_state"]):
                raise ValueError("Held-out measured target binding changed")
            if not np.array_equal(crop_rgb(np.asarray(Image.open(raw).convert("RGB"))),
                                  np.asarray(Image.open(prepared / "targets" / (sample["id"] + ".png")).convert("RGB"))):
                raise ValueError("Actual target crop changed")
    result = evaluate(prepared, bundle / "results", bundle / "learning-protocol.json")
    if not same(result, json.loads((bundle / "evaluation.json").read_text())):
        raise ValueError("Reopened metrics differ")
    return dict(status="passed", files_verified=len(listed),
                native_forecasts_reopened=result["native_inference_calls"],
                training_performed=result["training_performed"],
                endpoint_candidate_qualified=result["endpoint_candidate_qualified"],
                flight_admitted=False, new_inference=False, gpu_requested=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().bundle)))
