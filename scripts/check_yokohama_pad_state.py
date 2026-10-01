#!/usr/bin/env python3
"""Reopen public CPU learning evidence and rerun RGB-only state forecasts."""

from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_yokohama_pad_temporal import same  # noqa: E402
from scripts.run_yokohama_pad_state import evaluate, load_data  # noqa: E402
from scripts.ship_anwm import camera_pose  # noqa: E402
from scripts.yokohama_pad_state import sha  # noqa: E402


def without_timing(value):
    if isinstance(value, dict):
        return {
            k: without_timing(v)
            for k, v in value.items()
            if k not in {"compute_seconds", "mean_compute_seconds", "p95_compute_seconds"}
        }
    if isinstance(value, list):
        return [without_timing(v) for v in value]
    return value


def check(bundle):
    manifest = json.loads((bundle / "evidence-manifest.json").read_text())
    listed = set()
    for item in manifest["files"]:
        path = bundle / item["path"]
        if (
            not path.resolve().is_relative_to(bundle.resolve())
            or path.is_symlink()
            or item["path"] in listed
            or path.stat().st_size != item["bytes"]
            or sha(path) != item["sha256"]
        ):
            raise ValueError("Public evidence changed")
        listed.add(item["path"])
    if {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} - {
        "evidence-manifest.json"
    } != listed:
        raise ValueError("Unlisted or missing evidence")
    protocol = json.loads((bundle / "model/protocol.json").read_text())
    for name, digest in protocol["source_sha256"].items():
        if Path(name).name != name or sha(Path(__file__).parent / name) != digest:
            raise ValueError(
                "Frozen source changed; preserve and explicitly version the experiment"
            )
    if sha(bundle / "development-data/dataset.json") != protocol["dataset_sha256"]:
        raise ValueError("Training/development data changed")
    for data, capture in [
        ("development-data", "development-capture"),
        ("test-data", "test-capture"),
    ]:
        folder = bundle / capture
        receipt = json.loads((folder / "capture-result.json").read_text())
        config = json.loads((folder / "config.json").read_text())
        if (
            receipt["status"] != "passed"
            or receipt["aircraft_flown"]
            or receipt["native_inference"]
            or not json.loads((folder / "cleanup.json").read_text())["removed"]
        ):
            raise ValueError("Capture/cleanup boundary changed")
        if data == "test-data" and config["motion_cases"] != protocol["evaluation_cases"]:
            raise ValueError("Fresh evaluation timings changed")
        records = json.loads((bundle / data / "dataset.json").read_text())["records"]
        sequences = load_data(bundle / data, {"train", "development", "unseen_test"})
        for sequence, row, acquired in zip(sequences, records, receipt["cases"], strict=True):
            path = folder / (row["id"] + ".json")
            if (
                row["capture_sha256"] != acquired["capture_sha256"]
                or sha(path) != row["capture_sha256"]
            ):
                raise ValueError("Measured record binding changed")
            frames = json.loads(path.read_text())["frames"]
            if len(sequence["rgb"]) != len(frames) or row["frames"] != len(frames):
                raise ValueError("Frame count binding")
            if [f["assets"]["rgb"]["sha256"] for f in frames] != row["raw_rgb_sha256"]:
                raise ValueError("Raw source identity binding")
            for i, f in enumerate(frames):
                pose = camera_pose(
                    dict(
                        vehicle_position_enu_m=f["rig_pose"]["xyz"],
                        vehicle_quaternion_wxyz=f["rig_pose"]["quat_wxyz"],
                    )
                )
                if (
                    sequence["stamps_ns"][i] != f["stamp_ns"]
                    or not np.allclose(sequence["camera_poses"][i], pose, atol=1e-9, rtol=0)
                    or not np.allclose(
                        sequence["xyz"][i],
                        np.array(f["lead_pose"]["xyz"]) - config["world"]["pad_queue"]["pad_xyz_m"],
                        atol=1e-9,
                        rtol=0,
                    )
                ):
                    raise ValueError("Camera time or measured target binding changed")
    expected = json.loads((bundle / "evaluation/evaluation.json").read_text())
    with TemporaryDirectory() as temp:
        output = Path(temp) / "reopened"
        with redirect_stdout(io.StringIO()):
            evaluate(bundle / "test-data", bundle / "model", output)
        actual = json.loads((output / "evaluation.json").read_text())
        if not same(without_timing(actual), without_timing(expected)):
            raise ValueError("Rerun metrics differ")
        actual_predictions = json.loads((output / "predictions.json").read_text())
        expected_predictions = json.loads((bundle / "evaluation/predictions.json").read_text())
        if not same(without_timing(actual_predictions), without_timing(expected_predictions)):
            raise ValueError("RGB-only rerun predictions differ")
    return dict(
        status="passed",
        files_verified=len(listed),
        learned_forecasts_recomputed=len(actual_predictions),
        capability_result=actual["status"],
        false_clear=actual["totals"]["learned"]["false_clear"],
        flight_admitted=False,
        native_anwm_improved=False,
        gpu_used=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().bundle)))
