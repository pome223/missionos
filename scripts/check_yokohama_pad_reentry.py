#!/usr/bin/env python3
"""Recompute public reentry forecasts and check the shared judgment replay."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import gzip
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.check_yokohama_pad_state import without_timing  # noqa: E402
from scripts.check_yokohama_pad_temporal import same  # noqa: E402
from scripts.evaluate_yokohama_pad_reentry import evaluate  # noqa: E402
from scripts.run_yokohama_pad_state import load_data  # noqa: E402
from scripts.ship_anwm import camera_pose  # noqa: E402
from scripts.yokohama_pad_state import sha  # noqa: E402
from src.runtime.yokohama_pad_queue import digest, require_response  # noqa: E402


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
            raise ValueError("Public manifest identity")
        listed.add(item["path"])
    if {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} - {
        "evidence-manifest.json"
    } != listed:
        raise ValueError("Missing or unlisted public file")
    for name, value in json.loads((bundle / "source-sha256.json").read_text()).items():
        if not (REPO / name).resolve().is_relative_to(REPO) or sha(REPO / name) != value:
            raise ValueError("Frozen experiment source changed")
    protocol = json.loads((bundle / "protocol.json").read_text())
    for name, value in protocol.get("risk_source_sha256", {}).items():
        if sha(REPO / name) != value:
            raise ValueError("Pre-acquisition risk policy changed")
    if (
        protocol.get("risk_followup")
        and sha(bundle / "training-protocol.json") != protocol["training_protocol_sha256"]
    ):
        raise ValueError("Parent training protocol changed")
    config = json.loads((bundle / "capture-config.json").read_text())
    if config["motion_cases"] != protocol["cases"]:
        raise ValueError("Authored motion cases changed")
    receipt = json.loads((bundle / "capture-result.json").read_text())
    runtime = json.loads((bundle / "runtime.json").read_text())
    if (
        receipt["status"] != "passed"
        or receipt["aircraft_flown"]
        or receipt["native_inference"]
        or not json.loads((bundle / "cleanup.json").read_text())["removed"]
        or runtime["network"] != "none"
        or runtime["device_requests"]
        or runtime["aircraft_flown"]
    ):
        raise ValueError("Acquisition/CPU/cleanup boundary")
    metadata = json.loads((bundle / "data/dataset.json").read_text())["records"]
    sequences = load_data(bundle / "data", {"train", "development", "unseen_test"})
    for sequence, row, acquired, case in zip(
        sequences, metadata, receipt["cases"], protocol["cases"], strict=True
    ):
        path = bundle / "capture" / (row["id"] + ".json")
        if (
            sha(path) != row["capture_sha256"]
            or sha(path) != acquired["capture_sha256"]
            or row["split"] != case["split"]
            or row["id"] != case["id"]
        ):
            raise ValueError("Case provenance/split changed")
        captured = json.loads(path.read_text())
        if (
            captured["case"] != case
            or captured["world_sha256"] != config["world"]["world_sha256"]
            or len(captured["frames"]) != len(sequence["rgb"])
        ):
            raise ValueError("Capture/world binding changed")
        for i, frame in enumerate(captured["frames"]):
            pose = camera_pose(
                dict(
                    vehicle_position_enu_m=frame["rig_pose"]["xyz"],
                    vehicle_quaternion_wxyz=frame["rig_pose"]["quat_wxyz"],
                )
            )
            if (
                sequence["stamps_ns"][i] != frame["stamp_ns"]
                or not np.allclose(sequence["camera_poses"][i], pose, atol=1e-9, rtol=0)
                or not np.allclose(
                    sequence["xyz"][i],
                    np.array(frame["lead_pose"]["xyz"]) - config["world"]["pad_queue"]["pad_xyz_m"],
                    atol=1e-9,
                    rtol=0,
                )
                or frame["assets"]["rgb"]["sha256"] != row["raw_rgb_sha256"][i]
            ):
                raise ValueError("Observation target/clock binding changed")
    with TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
        output = Path(temp) / "recomputed"
        result = evaluate(bundle, output)
        for name in ("evaluation.json", "predictions.json", "episodes.json"):
            actual = json.loads((output / name).read_text())
            if name == "predictions.json":
                with gzip.open(bundle / "evaluation/predictions.json.gz", "rt") as stream:
                    expected = json.load(stream)
            else:
                expected = json.loads((bundle / "evaluation" / name).read_text())
            if not same(without_timing(actual), without_timing(expected)):
                raise ValueError("Recomputed evidence differs: " + name)
    config["run_id"] = "pad-reentry-offline-replay"
    config["operator_approval"] = "explicit offline fixture replay only; no aircraft dispatch"
    count = 0
    with gzip.open(bundle / "evaluation/runtime-trace.jsonl.gz", "rt") as stream:
        for line in stream:
            record = json.loads(line)
            request, response = record["request"], record["response"]
            config["world"]["pad_state_advisory"] = dict(
                mode="assist",
                camera_entity="pad_state_camera",
                weights_sha256=result["weights_sha256"][
                    "post_trained" if record["method"].startswith("post_trained") else "existing"
                ],
            )
            if record["method"] == "post_trained_risk":
                config["world"]["pad_state_advisory"]["reentry_risk_advisory"] = True
            require_response(config, request, response, request["observations"][-1])
            if (
                digest(record["judgment"]) != response["mission_assurance_sha256"]
                or record["judgment"]["proposal"]["parameters"]["action"]
                != response["proposed_action"]
            ):
                raise ValueError("Shared Mission Assurance identity changed")
            count += 1
    if count != result["shared_mission_judgments"]:
        raise ValueError("Missing judgment receipts")
    return dict(
        status="passed",
        files=len(listed),
        inference_calls=result["inference_calls"],
        shared_judgments=count,
        mission_benefit_demonstrated=False,
        new_gpu_usd=0,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(check(args.bundle)))
