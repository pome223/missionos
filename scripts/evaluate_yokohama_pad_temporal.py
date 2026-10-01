#!/usr/bin/env python3
"""Reopen native endpoint predictions against withheld timestamped observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.prepare_yokohama_pad_temporal import read_image, sha, write  # noqa: E402
from scripts.probe_yokohama_pad_temporal import validate_inputs  # noqa: E402
from src.runtime.yokohama_native import forecast_consistency, past_view  # noqa: E402


def saved_reference(root, name, history_sha256):
    """Reopen original diagnostic raster; never substitute one from another input."""
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["schema"] != "pad_past_reference_rasters.v1":
        raise ValueError("Wrong reference manifest")
    entry = manifest["samples"][name]
    path = root / entry["file"]
    if (
        Path(entry["file"]).name != entry["file"]
        or path.is_symlink()
        or entry["history_sha256"] != history_sha256
        or sha(path) != entry["sha256"]
    ):
        raise ValueError("Diagnostic reference changed or belongs to another input")
    with np.load(path, allow_pickle=False) as archive:
        reference, mask = archive["reference"], archive["mask"]
    if reference.shape != (224, 224, 3) or reference.dtype != np.uint8:
        raise ValueError("Wrong reference image")
    if mask.shape != (224, 224) or mask.dtype != np.bool_:
        raise ValueError("Wrong reference mask")
    return reference, mask


def evaluate(prepared, results, reference_dir=None):
    inputs = {p.name: (p, r, a) for p, r, a in validate_inputs(prepared / "inputs")}
    data = json.loads((prepared / "dataset.json").read_text())
    if sha(prepared / "reader.npz") != data["reader_sha256"]:
        raise ValueError("Reader changed")
    with np.load(prepared / "reader.npz", allow_pickle=False) as z:
        reader = {k: z[k] for k in z.files}
    identity = json.loads((results / "identity.json").read_text())
    if (
        identity["weights_changed"] is not False
        or identity["model_frame_offset"] != 64
        or identity["diffusion_steps"] != 50
    ):
        raise ValueError("Wrong native profile")
    rows = []
    for s in data["samples"]:
        folder, request, arrays = inputs[s["id"]]
        dest = results / s["id"]
        result = json.loads((dest / "result.json").read_text())
        if (
            result["request_sha256"] != sha(folder / "request.json")
            or not result["wam_inference_invoked"]
            or result["dispatch_invoked"]
            or result["aircraft_flown"]
            or result["model_weights_updated"]
        ):
            raise ValueError("Wrong native invocation boundary")
        if len(result["forecasts"]) != 1 or result["forecasts"][0]["candidate"] != {
            "id": "hold",
            "delta": [0, 0, 0, 0],
        }:
            raise ValueError("Changed candidate")
        if result["requested_future_stamp_ns"] != s["target_stamp_ns"]:
            raise ValueError("Future observation timestamp mismatch")
        f = result["forecasts"][0]
        entry = f["files"]["prediction"]
        if (
            Path(entry["file"]).name != entry["file"]
            or sha(dest / entry["file"]) != entry["sha256"]
        ):
            raise ValueError("Forecast bytes changed")
        if sha(prepared / "targets" / s["target_file"]) != s["target_sha256"]:
            raise ValueError("Target bytes changed")
        predicted = np.asarray(Image.open(dest / entry["file"]).convert("RGB"))
        actual = np.asarray(Image.open(prepared / "targets" / s["target_file"]).convert("RGB"))
        reading = read_image(predicted, reader)
        source_reading = read_image(
            np.asarray(Image.open(folder / "observed.png").convert("RGB")), reader
        )
        actual_reading = read_image(actual, reader)
        reference, mask = (
            saved_reference(reference_dir, s["id"], request["history_sha256"])
            if reference_dir is not None
            else past_view(arrays, arrays["poses"][-1])
        )
        rows.append(
            dict(
                id=s["id"],
                source_state=s["source_state"],
                actual_state=s["target_state"],
                predicted_state=reading["state"],
                reader=reading,
                actual_target_reader=actual_reading,
                current_image_reader=source_reading,
                endpoint_match=reading["state"] == s["target_state"],
                persistence_match=source_reading["state"] == s["target_state"],
                source_stamp_ns=s["source_stamp_ns"],
                target_stamp_ns=s["target_stamp_ns"],
                observed_horizon_sim_s=(s["target_stamp_ns"] - s["source_stamp_ns"]) / 1e9,
                request_total_seconds=result["request_total_seconds"],
                warm_compute_before_target=result["request_total_seconds"] < 16,
                past_structure_consistency=forecast_consistency(predicted, reference, mask),
                prediction_sha256=entry["sha256"],
                future_target_sha256=s["target_sha256"],
                result_sha256=sha(dest / "result.json"),
            )
        )
    shutdown = json.loads((results / "shutdown.json").read_text())
    return dict(
        schema="pad_temporal_endpoint_evaluation.v1",
        evidence_integrity="passed",
        native_forecasts=len(rows),
        endpoint_matches=sum(r["endpoint_match"] for r in rows),
        unreadable_forecasts=sum(r["predicted_state"] == "unknown" for r in rows),
        persistence_matches=sum(r["persistence_match"] for r in rows),
        warm_compute_before_target_count=sum(r["warm_compute_before_target"] for r in rows),
        model_load_seconds=identity["model_load_seconds"],
        cuda_allocated_after_work_bytes=shutdown["cuda_allocated_bytes"],
        rows=rows,
        time_conditioning="64 frame offsets with observed 0.25 second cadence; parameter binding is not physical forecast calibration",
        timing_limit="warm request computation only; excludes model load and transport; comparison assumes real-time simulation, not measured live AP timing",
        interval_conflict_prediction_verified=False,
        native_flight_admitted=False,
        mission_judgment_invoked=False,
        aircraft_flown=False,
        weights_changed=False,
        physical_execution=False,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--reference-dir", type=Path, help="Original hash-bound diagnostic rasters")
    a = p.parse_args()
    if a.output.exists():
        p.error("Preserve previous evaluations")
    result = evaluate(a.prepared, a.results, a.reference_dir)
    write(a.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
