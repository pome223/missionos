#!/usr/bin/env python3
"""CPU-only reopening of the published negative native temporal diagnostic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_yokohama_pad_temporal import evaluate  # noqa: E402
from scripts.prepare_yokohama_pad_temporal import label, sha  # noqa: E402
from scripts.probe_yokohama_pad_temporal import validate_inputs  # noqa: E402
from scripts.ship_anwm import crop_rgb, MODEL_SHA256, UPSTREAM_REVISION  # noqa: E402
from scripts.yokohama_wam_profile import MOTION_ADAPTER_SHA256  # noqa: E402


def same(actual, expected):
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            same(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(same(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, float):
        return bool(np.isclose(actual, expected, rtol=1e-6, atol=1e-7))
    return actual == expected


def check(bundle):
    manifest = json.loads((bundle / "evidence-manifest.json").read_text())
    listed = set()
    for item in manifest["files"]:
        path = bundle / item["path"]
        if not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink():
            raise ValueError("Unsafe manifest entry")
        if item["path"] in listed:
            raise ValueError("Duplicate manifest entry")
        listed.add(item["path"])
        if path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
            raise ValueError(f"Changed evidence: {item['path']}")
    files = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if files - {"evidence-manifest.json"} != listed:
        raise ValueError("Unlisted or missing evidence")
    config = json.loads((bundle / "capture/config.json").read_text())
    receipt = json.loads((bundle / "capture/capture-result.json").read_text())
    if sha(bundle / "capture/world.sdf") != config["world"]["world_sha256"]:
        raise ValueError("World changed")
    captures = {}
    for case in receipt["cases"]:
        path = bundle / "capture" / (case["id"] + ".json")
        if sha(path) != case["capture_sha256"]:
            raise ValueError("Capture changed")
        captures[case["id"]] = json.loads(path.read_text())
    protocol = json.loads((bundle / "protocol.json").read_text())
    for path, digest in protocol["source_files_sha256"].items():
        if sha(bundle / "executed-source" / Path(path).name) != digest:
            raise ValueError("Executed source changed")
    references = json.loads((bundle / "past-references/manifest.json").read_text())
    if references["past_view_source_sha256"] != sha(bundle / "executed-source/yokohama_native.py"):
        raise ValueError("Original reference reconstruction source changed")
    identity = json.loads((bundle / "results/identity.json").read_text())
    if (
        identity["base_sha256"] != MODEL_SHA256
        or identity["adapter_sha256"] != MOTION_ADAPTER_SHA256
        or identity["upstream_revision"] != UPSTREAM_REVISION
        or identity["physical_time_prediction_validated"] is not False
        or identity["source_sha256"] != sha(bundle / "executed-source/probe_yokohama_pad_temporal.py")
    ):
        raise ValueError("Native identity changed")
    inputs = {p.name: (p, r, a) for p, r, a in validate_inputs(bundle / "prepared/inputs")}
    dataset = json.loads((bundle / "prepared/dataset.json").read_text())
    for sample in dataset["samples"]:
        folder, request, arrays = inputs[sample["id"]]
        record = captures[sample["id"].rsplit("-", 1)[0]]
        source = record["frames"][sample["source_frame_index"]]
        target = record["frames"][sample["target_frame_index"]]
        if (
            request["source_run_id"] != record["run_id"]
            or request["world_sha256"] != record["world_sha256"]
            or source["stamp_ns"] != sample["source_stamp_ns"]
            or target["stamp_ns"] != sample["target_stamp_ns"]
            or label(source, config["world"]["pad_queue"]["pad_xyz_m"]) != sample["source_state"]
            or label(target, config["world"]["pad_queue"]["pad_xyz_m"]) != sample["target_state"]
        ):
            raise ValueError("Observed state/time binding changed")
        raw = bundle / "capture/target-frames" / (sample["id"] + ".png")
        if sha(raw) != target["assets"]["rgb"]["sha256"]:
            raise ValueError("Actual target frame changed")
        if not np.array_equal(
            crop_rgb(np.asarray(Image.open(raw).convert("RGB"))),
            np.asarray(Image.open(bundle / "prepared/targets" / sample["target_file"]).convert("RGB")),
        ) or not np.array_equal(
            crop_rgb(arrays["rgb"][-1]),
            np.asarray(Image.open(folder / "observed.png").convert("RGB")),
        ):
            raise ValueError("Displayed source/target is not the actual crop")
    actual = evaluate(bundle / "prepared", bundle / "results", bundle / "past-references")
    if not same(actual, json.loads((bundle / "evaluation.json").read_text())):
        raise ValueError("Reopened evaluation differs")
    if actual["native_flight_admitted"] or actual["mission_judgment_invoked"]:
        raise ValueError("Unqualified forecast admitted")
    return dict(
        status="passed", files_verified=len(listed), native_results_reopened=4,
        endpoint_matches=actual["endpoint_matches"], unreadable=actual["unreadable_forecasts"],
        native_flight_admitted=False, new_inference=False, gpu_requested=False,
        original_diagnostic_references_reopened=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().bundle)))
