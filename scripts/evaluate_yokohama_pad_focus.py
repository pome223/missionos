#!/usr/bin/env python3
"""Reopen real native forecast bytes against host-only recorded future RGB."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_focus_data import classify, sha, validate, write


def evaluate(prepared, results, protocol_path):
    data = validate(prepared / "dataset")
    protocol = json.loads(protocol_path.read_text())
    summary = json.loads((results / "summary.json").read_text())
    if (
        summary["status"] != "completed"
        or summary["protocol_sha256"] != sha(protocol_path)
        or summary["native_anwm"] is not True
        or summary["dispatch_invoked"] is not False
        or summary["test_targets_uploaded"] is not False
    ):
        raise ValueError("Native run boundary")
    if protocol["dataset_sha256"] != sha(prepared / "dataset/dataset.json"):
        raise ValueError("Foreign data")
    with np.load(prepared / "dataset/reader.npz", allow_pickle=False) as a:
        reader = {k: a[k] for k in a.files}
    truth = {r["id"]: r for r in json.loads((prepared / "truth.json").read_text())}
    bound = json.loads((results / "forecast-manifest.json").read_text())["records"]
    if len(bound) != summary["inference_calls"]:
        raise ValueError("Call count")
    for r in bound:
        if sha(results / r["file"]) != r["sha256"]:
            raise ValueError("Native receipt changed")
    rows = []
    stages = ["before", "after"] if summary.get("training_performed") else ["before"]
    for sample in data["samples"]:
        if sample["split"] != "test":
            continue
        t = truth[sample["id"]]
        target_path = prepared / "targets" / (sample["id"] + ".png")
        if (
            sha(target_path) != t["target_sha256"]
            or t["stamp_ns"] != sample["cutoff_stamp_ns"] + sample["offset"] * 250000000
        ):
            raise ValueError("Evaluation clock or target changed")
        target = np.asarray(Image.open(target_path).convert("RGB"))
        with np.load(prepared / "dataset" / sample["history"], allow_pickle=False) as a:
            current = a["rgb"][-1]
        row = dict(
            id=sample["id"],
            horizon_s=sample["offset"] / 4,
            truth=t["state"],
            current_pose_state=t["current_state"],
            target_reader=classify(target, reader),
            persistence=classify(current, reader),
            exact_training_history_overlap=t["exact_training_history_overlap"],
            methods={},
        )
        for stage in stages:
            folder = results / stage / sample["id"]
            r = json.loads((folder / "result.json").read_text())
            adapter = (
                summary["initial_adapter_sha256"]
                if stage == "before"
                else summary["final_adapter_sha256"]
            )
            if (
                r["history_sha256"] != sha(prepared / "dataset" / sample["history"])
                or r["prediction_sha256"] != sha(folder / "prediction.png")
                or r["adapter_sha256"] != adapter
                or r["sample_id"] != sample["id"]
                or r["stage"] != stage
                or r["future_stamp_ns"] != t["stamp_ns"]
                or r["frame_offset"] != sample["offset"]
                or not r["native_anwm"]
                or r["flight_admitted"]
                or r["dispatch_invoked"]
            ):
                raise ValueError("Forecast binding or authority mismatch")
            image = np.asarray(Image.open(folder / "prediction.png").convert("RGB"))
            read = classify(image, reader)
            if read["state"] != r["reader"]["state"]:
                raise ValueError("Reader result changed")
            row["methods"][stage] = dict(
                state=read["state"],
                reader=read,
                rgb_mae=float(np.abs(image.astype(float) - target).mean()),
                seconds=r["seconds"],
            )
        rows.append(row)
    if set(truth) != {r["id"] for r in rows}:
        raise ValueError("Missing test forecasts")
    counts = {}
    for method in ["persistence", *stages]:

        def read(row):
            return (
                row["persistence"]["state"]
                if method == "persistence"
                else row["methods"][method]["state"]
            )

        known = [r for r in rows if r["truth"] != "unknown"]
        counts[method] = dict(
            known_targets=len(known),
            matches=sum(read(r) == r["truth"] for r in known),
            false_clear=sum(read(r) == "clear" and r["truth"] == "occupied" for r in known),
            false_occupied=sum(read(r) == "occupied" and r["truth"] == "clear" for r in known),
            unknown=sum(read(r) == "unknown" for r in known),
            useful_future_occupied=sum(
                r["current_pose_state"] == "clear"
                and r["truth"] == "occupied"
                and read(r) == "occupied"
                for r in known
            ),
            unnecessary_future_wait=sum(
                r["current_pose_state"] == "clear"
                and r["truth"] == "clear"
                and read(r) == "occupied"
                for r in known
            ),
        )
    latency_reference = {}
    for stage in stages:
        seconds = [r["methods"][stage]["seconds"] for r in rows]
        latency_reference[stage] = dict(
            warm_wall_seconds_min=min(seconds),
            warm_wall_seconds_median=float(np.median(seconds)),
            warm_wall_seconds_max=max(seconds),
            endpoint_already_past_at_one_times_speed=sum(
                r["methods"][stage]["seconds"] >= r["horizon_s"] for r in rows
            ),
            conditions=len(rows),
            includes_model_load_or_transport=False,
            actual_flight_clock_binding_verified=False,
        )
    return dict(
        schema="native_pad_focus_evaluation.v1",
        counts=counts,
        latency_reference=latency_reference,
        rows=rows,
        native_anwm=True,
        native_vla=False,
        aircraft_flown=False,
        mission_benefit_demonstrated=False,
        flight_admitted=False,
        no_fixed_accuracy_gate=True,
        ideal_rules_win_required=False,
        evaluation_conditions=len(rows),
        unknown_truth_conditions=sum(r["truth"] == "unknown" for r in rows),
        exact_training_history_overlap=sum(r["exact_training_history_overlap"] for r in rows),
        boundary="Three timing sequences; paired horizons and overlapping histories are correlated, not independent missions",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    value = evaluate(a.prepared, a.results, a.protocol)
    write(a.output, value)
    print(json.dumps(value["counts"], indent=2))
