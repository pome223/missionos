#!/usr/bin/env python3
"""Reopen fixed native forecasts against CPU-held future images, without a GPU."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ship_anwm import MODEL_SHA256, crop_rgb  # noqa: E402
from scripts.yokohama_pad_learning_data import classify, history, sha, validate, write  # noqa: E402
from scripts.yokohama_wam_profile import MOTION_ADAPTER_SHA256  # noqa: E402


def image(path):
    with Image.open(path) as im:
        if im.size != (224, 224) or im.mode != "RGB":
            raise ValueError("Expected original 224x224 RGB forecast/target")
        return np.asarray(im).copy()


def statistics(rows):
    return dict(count=len(rows), endpoint_matches=sum(r["endpoint_match"] for r in rows),
                unknown=sum(r["predicted_state"] == "unknown" for r in rows),
                false_clear=sum(r["predicted_state"] == "clear" and r["actual_state"] == "occupied" for r in rows),
                mean_rgb_mae=float(np.mean([r["rgb_mae"] for r in rows])),
                max_rgb_mae=max(r["rgb_mae"] for r in rows),
                persistence_matches=sum(r["persistence_match"] for r in rows),
                request_seconds_min=min(r["request_total_seconds"] for r in rows),
                request_seconds_max=max(r["request_total_seconds"] for r in rows))


def evaluate(prepared, results, protocol_path):
    data = validate(prepared / "dataset")
    protocol = json.loads(protocol_path.read_text())
    summary = json.loads((results / "summary.json").read_text())
    truth = json.loads((prepared / "evaluation-targets.json").read_text())
    if (protocol["dataset_sha256"] != sha(prepared / "dataset/dataset.json")
            or truth["dataset_sha256"] != protocol["dataset_sha256"]
            or summary["protocol_sha256"] != sha(protocol_path)
            or summary["status"] != "completed"
            or summary["base_sha256"] != MODEL_SHA256
            or summary["initial_adapter_sha256"] != MOTION_ADAPTER_SHA256
            or any(summary[k] for k in ("flight_admitted", "dispatch_invoked", "mission_judgment_invoked",
                                        "physical_execution", "vla_inference_invoked", "test_targets_uploaded"))):
        raise ValueError("Native evidence boundary or fixed inputs changed")
    with np.load(prepared / "dataset/reader.npz", allow_pickle=False) as z:
        reader = {k: z[k] for k in z.files}
    samples = {s["id"]: s for s in data["samples"]}
    training_rgb_hashes, training_target_pixels = set(), set()
    seen = set()
    for sample in data["samples"]:
        if sample["split"] != "train":
            continue
        if sample["history"] not in seen:
            training_rgb_hashes.add(hashlib.sha256(history(prepared / "dataset", sample)["rgb"].tobytes()).hexdigest())
            seen.add(sample["history"])
        pixels = crop_rgb(np.asarray(Image.open(prepared / "dataset" / sample["training_target"]).convert("RGB")))
        training_target_pixels.add(hashlib.sha256(pixels.tobytes()).hexdigest())
    truth = {r["id"]: r for r in truth["rows"]}
    if set(truth) != {s["id"] for s in data["samples"] if s["split"] == "test"}:
        raise ValueError("Evaluation cohort changed")
    manifest = json.loads((results / "forecast-manifest.json").read_text())
    paths = {}
    for item in manifest["records"]:
        path = results / item["file"]
        if (not path.resolve().is_relative_to(results.resolve()) or path.is_symlink()
                or item["file"] in paths or sha(path) != item["sha256"]):
            raise ValueError("Forecast manifest changed")
        record = json.loads(path.read_text())
        sample = samples[record["sample_id"]]
        trained = record["stage"] == "after"
        if (record["history_sha256"] != data["assets"][sample["history"]]
                or record["cutoff_stamp_ns"] != sample["cutoff_stamp_ns"]
                or record["requested_future_stamp_ns"] != sample["cutoff_stamp_ns"] + record["model_frame_offset"]*250_000_000
                or record["requested_horizon_sim_s"] != record["model_frame_offset"]/4
                or record["adapter_sha256"] != (summary["final_adapter_sha256"] if trained else MOTION_ADAPTER_SHA256)
                or not record["native_inference_invoked"] or record["dispatch_invoked"] or record["flight_admitted"]
                or record["seed"] != 42 or record["model_frame_offset"] != sample["frame_offset"]
                or len(record["forecasts"]) != 1):
            raise ValueError("Native input, weights or time binding changed")
        forecast = record["forecasts"][0]
        if (forecast["candidate"] != dict(id="hold", delta=[0, 0, 0, 0])
                or forecast["model_time_index"] != record["model_frame_offset"]
                or forecast["conditioning"]["metric_geometry_changed"]):
            raise ValueError("Candidate or metric geometry changed")
        for asset in forecast["files"].values():
            asset_path = path.parent / asset["file"]
            if Path(asset["file"]).name != asset["file"] or asset_path.is_symlink() or sha(asset_path) != asset["sha256"]:
                raise ValueError("Native pixels changed")
            image(asset_path)
        paths[item["file"]] = record
    trained = summary["training_performed"]
    expected_paths = {f"ablation-{steps}-{offset}/train-depart-20-t{offset:02d}/result.json"
                      for steps in (50, 250) for offset in (1, 64)}
    expected_paths |= {f"development-before/{name}/result.json" for name in data["development_probes"]}
    expected_paths |= {f"{stage}/{name}/result.json" for name in truth
                       for stage in (("before", "after") if trained else ("before",))}
    if set(paths) != expected_paths or summary["inference_calls"] != len(paths):
        raise ValueError("Missing, duplicate or extra model calls")
    decision = json.loads((results / "training-decision.json").read_text())
    if decision["evaluation_targets_used"] or [r["sample_id"] for r in decision["development"]] != data["development_probes"]:
        raise ValueError("Training trigger data changed")
    development = []
    for stored in decision["development"]:
        sample = samples[stored["sample_id"]]
        predicted = image(results / "development-before" / sample["id"] / "hold-prediction.png")
        target = crop_rgb(np.asarray(Image.open(prepared / "dataset" / sample["training_target"]).convert("RGB")))
        reading, actual = classify(predicted, reader), classify(target, reader)
        mae = float(np.abs(predicted.astype(float)-target.astype(float)).mean())
        passed = actual["state"] != "unknown" and reading["state"] == actual["state"] and mae <= 12
        if (stored["reader"]["state"] != reading["state"] or stored["target_reader"]["state"] != actual["state"]
                or not np.isclose(stored["rgb_mae"], mae, rtol=1e-6, atol=1e-7) or stored["passed"] != passed):
            raise ValueError("Development training trigger changed")
        development.append(dict(id=sample["id"], passed=passed, rgb_mae=mae))
    if decision["training_needed"] != trained or trained != (not all(r["passed"] for r in development)):
        raise ValueError("Training no longer follows development-only gate")
    if trained:
        if (summary["steps"] != protocol["steps"] or summary["head_change_l2"] <= 0
                or not summary["frozen_base_unchanged"] or not summary["checkpoint_reloaded"]):
            raise ValueError("No verified serialized weight update")
        losses = json.loads((results / "training.json").read_text())
        if len(losses) != protocol["steps"] or [r["step"] for r in losses] != list(range(1, len(losses)+1)):
            raise ValueError("Training trace incomplete")
        if any(samples[r["sample_id"]]["split"] != "train" for r in losses):
            raise ValueError("Evaluation data used for training")
    rows = []
    for name, target in truth.items():
        sample = samples[name]
        path = prepared / "targets" / (name + ".png")
        if sha(path) != target["target_sha256"]:
            raise ValueError("Withheld future observation changed")
        actual = image(path)
        arrays = history(prepared / "dataset", sample)
        current = classify(crop_rgb(arrays["rgb"][-1]), reader)
        actual_reader = classify(actual, reader)
        if actual_reader["state"] != target["target_state"]:
            raise ValueError("Previously readable target changed")
        for stage in (("before", "after") if trained else ("before",)):
            key = f"{stage}/{name}/result.json"
            result = paths[key]
            if (result["diffusion_steps"] != 50 or result["model_frame_offset"] != 64
                    or result["stage"] != stage
                    or abs(result["requested_future_stamp_ns"] - target["target_stamp_ns"]) > 8_000_000):
                raise ValueError("Evaluation protocol/target time changed")
            predicted = image(results / stage / name / "hold-prediction.png")
            reading = classify(predicted, reader)
            rows.append(dict(id=name, stage=stage, cohort=target["cohort"],
                             source_state=target["source_state"], actual_state=target["target_state"],
                             predicted_state=reading["state"], reader=reading,
                             actual_reader=actual_reader, current_image_reader=current,
                             endpoint_match=reading["state"] == target["target_state"],
                             persistence_match=current["state"] == target["target_state"],
                             historical_rgb_exact_training_overlap=hashlib.sha256(arrays["rgb"].tobytes()).hexdigest() in training_rgb_hashes,
                             target_rgb_exact_training_overlap=hashlib.sha256(actual.tobytes()).hexdigest() in training_target_pixels,
                             rgb_mae=float(np.abs(predicted.astype(float)-actual.astype(float)).mean()),
                             source_stamp_ns=sample["cutoff_stamp_ns"], target_stamp_ns=target["target_stamp_ns"],
                             request_total_seconds=result["request_total_seconds"],
                             result_sha256=sha(results / key), prediction_sha256=sha(results / stage / name / "hold-prediction.png"),
                             target_sha256=target["target_sha256"]))
    aggregates = {stage: {cohort: statistics([r for r in rows if r["stage"] == stage and r["cohort"] == cohort])
                         for cohort in ("fresh", "previously_inspected_regression")}
                  for stage in (("before", "after") if trained else ("before",))}
    for stage, values in aggregates.items():
        values["fresh_distinct_rgb_history"] = statistics([r for r in rows if r["stage"] == stage
            and r["cohort"] == "fresh" and not r["historical_rgb_exact_training_overlap"]])
    selected_stage = "after" if trained else "before"
    final = {r["id"]: r for r in rows if r["stage"] == selected_stage}
    required = {r["id"] for r in rows if r["stage"] == "before" and r["endpoint_match"]}
    required.add("regression-evaluation-depart-32")  # Prior published native success.
    preserved = all(final[name]["endpoint_match"] for name in required)
    fresh = aggregates[selected_stage]["fresh"]
    bounds = protocol["candidate_qualification"]
    gates = dict(fresh_matches=fresh["endpoint_matches"] >= bounds["fresh_matches_min"],
                 no_false_clear=all(r["predicted_state"] != "clear" or r["actual_state"] != "occupied" for r in final.values()),
                 rgb_mae=all(r["rgb_mae"] <= bounds["pixel_mae_max"] for r in final.values()),
                 prior_successes_preserved=preserved)
    shutdown = json.loads((results / "shutdown.json").read_text())
    return dict(schema="pad_learning_evaluation.v1", evidence_integrity="passed", training_performed=trained,
                aggregates=aggregates, rows=rows, development_gate=development,
                qualification_checks=gates, endpoint_candidate_qualified=all(gates.values()),
                frozen_reader_sha256=sha(prepared / "dataset/reader.npz"),
                native_inference_calls=len(paths), cuda_allocated_after_work_bytes=shutdown["cuda_allocated_bytes"],
                flight_admitted=False, mission_judgment_invoked=False, interval_occupancy_verified=False,
                both_action_candidates_verified=False, ap_dispatch_invoked=False,
                scope="same camera, object and path; held-out timing only, not unseen appearances",
                timing_limit="warm computation only; model loading and transport excluded; no measured live AP timing")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve original evaluation")
    result = evaluate(args.prepared, args.results, args.protocol)
    write(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
