#!/usr/bin/env python3
"""Score native lead forecasts on the host against withheld real futures.

Reads forecast PNGs with the colour readout validated on real frames and
compares them with the real future frame, holding the current frame
(persistence) and constant-velocity extrapolation of the readout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_wam_study import detect_lead, sha, write  # noqa: E402


def error(pred, real):
    if not real["present"]:
        return None
    if not pred["present"]:
        return "missed"
    return float(np.hypot(*(np.subtract(pred["center_px"], real["center_px"]))))


def extrapolate(frames, last, offset, crop, rate=4):
    """Constant velocity over the last second of readouts, or None if unavailable."""
    a, b = detect_lead(frames[last - rate], crop), detect_lead(frames[last], crop)
    if not (a["present"] and b["present"]):
        return b
    v = np.subtract(b["center_px"], a["center_px"]) / rate
    return dict(present=True, center_px=list(np.add(b["center_px"], v * offset)))


def summarize(rows, key):
    finite = [r[key] for r in rows if isinstance(r[key], float)]
    present = [r for r in rows if r["real_present"]]
    return dict(
        n=len(rows),
        real_present=len(present),
        predicted_present_when_real=sum(isinstance(r[key], float) for r in present),
        median_error_px=statistics.median(finite) if finite else None,
        within_6px=sum(e <= 6 for e in finite),
    )


def evaluate(study, prepared, results):
    protocol = json.loads((study / "study-protocol.json").read_text())
    data = json.loads((study / "payload/dataset.json").read_text())
    samples = {s["id"]: s for s in data["samples"]}
    truth = {t["id"]: t for t in json.loads((prepared / "truth.json").read_text())}
    manifest = json.loads((results / "forecast-manifest.json").read_text())["records"]
    for r in manifest:
        if sha(results / r["file"]) != r["sha256"]:
            raise ValueError("Forecast receipt changed")
    summary = json.loads((results / "summary.json").read_text())
    expected = []
    for config in protocol["configs"]:
        crop = config["crop"]
        if config.get("before"):
            expected += [(config["name"], "before", f"{b}-{crop}") for b in protocol["moving_ids"]]
        expected += [
            (config["name"], "after", f"{b}-{crop}")
            for b in protocol["train_ids"] + protocol["eval_ids"]
        ]
    frames = {}
    rows = []
    for r in manifest:
        receipt = json.loads((results / r["file"]).read_text())
        sample = samples[receipt["sample_id"]]
        if receipt["frames_sha256"] != data["assets"][sample["frames"]]:
            raise ValueError("Forecast history binding")
        image = results / Path(r["file"]).parent / "prediction.png"
        if sha(image) != receipt["prediction_sha256"]:
            raise ValueError("Forecast image changed")
        if sample["frames"] not in frames:
            with np.load(study / "payload" / sample["frames"], allow_pickle=False) as a:
                frames[sample["frames"]] = a["rgb"]
        rgb = frames[sample["frames"]]
        t = truth[sample["id"]]
        real = t["readout_future"]
        pred = detect_lead(np.asarray(Image.open(image).convert("RGB")), sample["crop"])
        base = sample["id"].rsplit("-", 1)[0]
        rows.append(
            dict(
                id=sample["id"],
                config=receipt["config"],
                stage=receipt["stage"],
                subset="moving"
                if base in protocol["moving_ids"]
                else "static"
                if base in protocol["train_ids"]
                else "val",
                offset=sample["offset"],
                phase_now=t["phase_now"],
                real_present=real["present"],
                model=error(pred, real),
                persistence=error(t["readout_now"], real),
                extrapolation=error(
                    extrapolate(rgb, sample["last_index"], sample["offset"], sample["crop"]), real
                ),
                predicted=pred,
                real=real,
            )
        )
    actual = [(r["config"], r["stage"], r["id"]) for r in rows]
    completeness = dict(
        run_completed=summary.get("status") == "completed"
        and summary.get("inference_calls") == len(manifest),
        missing=sorted(map(list, set(expected) - set(actual))),
        unexpected=sorted(map(list, set(actual) - set(expected))),
        duplicates=len(actual) - len(set(actual)),
    )
    complete = (
        completeness["run_completed"]
        and not completeness["missing"]
        and not completeness["unexpected"]
        and not completeness["duplicates"]
    )
    table = {}
    for config, stage, subset in sorted({(r["config"], r["stage"], r["subset"]) for r in rows}):
        group = [
            r for r in rows if (r["config"], r["stage"], r["subset"]) == (config, stage, subset)
        ]
        table[f"{config}/{stage}/{subset}"] = {
            k: summarize(group, k) for k in ("model", "persistence", "extrapolation")
        }
    gate = protocol["memorization_gate"]
    passed = []
    for config in protocol["configs"]:
        group = [
            r
            for r in rows
            if r["config"] == config["name"]
            and r["stage"] == "after"
            and r["subset"] == "moving"
            and r["offset"] == gate["offset"]
        ]
        s, p = summarize(group, "model"), summarize(group, "persistence")
        ok = (
            s["real_present"] > 0
            and s["predicted_present_when_real"] >= gate["presence_fraction"] * s["real_present"]
            and s["median_error_px"] is not None
            and s["median_error_px"] <= gate["median_error_px"]
            and p["median_error_px"] is not None
            and s["median_error_px"] <= gate["fraction_of_persistence"] * p["median_error_px"]
        )
        if ok and complete:
            passed.append(config["name"])
    return dict(
        schema="pad_wam_study_evaluation.v1",
        memorization_gate=gate,
        complete=complete,
        completeness=completeness,
        memorization_passed=passed,
        table=table,
        rows=rows,
        readout="orange-body colour readout; validated on real crops only",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--study", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    value = evaluate(a.study, a.prepared, a.results)
    write(a.output, value)
    print(
        json.dumps(
            dict(memorization_passed=value["memorization_passed"], table=value["table"]), indent=1
        )
    )
