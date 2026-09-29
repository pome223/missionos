#!/usr/bin/env python3
"""Score the ANWM rule-learning test on the host against withheld real futures.

Every planned forecast must exist once and the run must have completed before
the frozen gate can pass. A missed readout counts as the worst error.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_wam_study import detect_lead, sha, write  # noqa: E402


def err(pred, real):
    if not (pred["present"] and real["present"]):
        return math.inf
    return float(np.hypot(*np.subtract(pred["center_px"], real["center_px"])))


def extrapolate(history, offset, rate=4):
    a, b = detect_lead(history[-1 - rate], "tight"), detect_lead(history[-1], "tight")
    if not (a["present"] and b["present"]):
        return b
    v = np.subtract(b["center_px"], a["center_px"]) / rate
    return dict(present=True, center_px=list(np.add(b["center_px"], v * offset)))


def med(values):
    return statistics.median(values) if values else math.inf


def jsonable(x):
    return "missed" if x == math.inf else x


def evaluate(staged, prepared, results):
    protocol = json.loads((staged / "rule-protocol.json").read_text())
    data = json.loads((staged / "payload/dataset.json").read_text())
    samples = {s["id"]: s for s in data["samples"]}
    truth = {t["id"]: t for t in json.loads((prepared / "truth.json").read_text())}
    summary = json.loads((results / "summary.json").read_text())
    manifest = json.loads((results / "forecast-manifest.json").read_text())["records"]
    final = f"ckpt-{protocol['checkpoints'][-1]:05d}"
    expected = [
        (f"ckpt-{c:05d}", "moving", protocol["seed"], i)
        for c in protocol["checkpoints"]
        for i in protocol["moving_ids"] + protocol["static_ids"]
    ]
    expected += [(final, "still", protocol["seed"], i) for i in protocol["moving_ids"]]
    expected += [(final, "moving", protocol["second_seed"], i) for i in protocol["moving_ids"]]
    forecasts = {}
    for r in manifest:
        if sha(results / r["file"]) != r["sha256"]:
            raise ValueError("Forecast receipt changed")
        receipt = json.loads((results / r["file"]).read_text())
        image = results / Path(r["file"]).parent / "prediction.png"
        if (
            sha(image) != receipt["prediction_sha256"]
            or receipt["frames_sha256"] != data["assets"][samples[receipt["sample_id"]]["frames"]]
        ):
            raise ValueError("Forecast image or history binding changed")
        key = (receipt["stage"], receipt["history"], receipt["seed"], receipt["sample_id"])
        forecasts.setdefault(key, []).append(
            detect_lead(np.asarray(Image.open(image).convert("RGB")), "tight")
        )
    complete = (
        summary.get("status") == "completed"
        and summary.get("inference_calls") == len(manifest)
        and set(forecasts) == set(expected)
        and all(len(v) == 1 for v in forecasts.values())
    )
    rows = {}
    for key, preds in forecasts.items():
        stage, hist, seed, ident = key
        t = truth[ident]
        with np.load(staged / "payload" / samples[ident]["frames"], allow_pickle=False) as a:
            history = a["rgb"]
        rows[key] = dict(
            model=err(preds[0], t["readout_future"]),
            to_now=err(preds[0], t["readout_now"]),
            persistence=err(t["readout_now"], t["readout_future"]),
            extrapolation=err(extrapolate(history, samples[ident]["offset"]), t["readout_future"]),
            present=preds[0]["present"],
        )

    def group(stage, hist, seed, ids):
        g = [rows[(stage, hist, seed, i)] for i in ids if (stage, hist, seed, i) in rows]
        return dict(
            n=len(g),
            readable=sum(r["present"] for r in g),
            median_error_px=jsonable(med([r["model"] for r in g])),
            median_to_now_px=jsonable(med([r["to_now"] for r in g])),
            persistence_px=jsonable(med([r["persistence"] for r in g])),
            extrapolation_px=jsonable(med([r["extrapolation"] for r in g])),
        )

    curve = {
        f"ckpt-{c:05d}": dict(
            moving=group(f"ckpt-{c:05d}", "moving", protocol["seed"], protocol["moving_ids"]),
            static=group(f"ckpt-{c:05d}", "moving", protocol["seed"], protocol["static_ids"]),
        )
        for c in protocol["checkpoints"]
    }
    gate = protocol["gate"]
    checks = {}
    for seed in (protocol["seed"], protocol["second_seed"]):
        g = [
            rows[(final, "moving", seed, i)]
            for i in protocol["moving_ids"]
            if (final, "moving", seed, i) in rows
        ]
        persistence = med([r["persistence"] for r in g])
        checks[f"seed_{seed}_readable"] = bool(g) and sum(r["present"] for r in g) >= gate[
            "readable_fraction"
        ] * len(g)
        checks[f"seed_{seed}_beats_persistence"] = (
            med([r["model"] for r in g]) <= gate["fraction_of_persistence"] * persistence
        )
    still = med(
        [
            rows[(final, "still", protocol["seed"], i)]["model"]
            for i in protocol["moving_ids"]
            if (final, "still", protocol["seed"], i) in rows
        ]
    )
    moving = med(
        [
            rows[(final, "moving", protocol["seed"], i)]["model"]
            for i in protocol["moving_ids"]
            if (final, "moving", protocol["seed"], i) in rows
        ]
    )
    checks["history_used"] = moving < still
    return dict(
        schema="pad_wam_rule_evaluation.v1",
        complete=complete,
        gate=gate,
        checks=checks,
        passed=complete and all(checks.values()),
        learning_curve=curve,
        final_second_seed=group(final, "moving", protocol["second_seed"], protocol["moving_ids"]),
        final_still_history=group(final, "still", protocol["seed"], protocol["moving_ids"]),
        rows=[
            dict(
                stage=k[0],
                history=k[1],
                seed=k[2],
                id=k[3],
                **{m: jsonable(v) if isinstance(v, float) else v for m, v in r.items()},
            )
            for k, r in sorted(rows.items())
        ],
        readout="orange-body colour readout validated on real crops; a missed readout counts as the worst error",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--staged", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    value = evaluate(a.staged, a.prepared, a.results)
    write(a.output, value)
    print(
        json.dumps(
            {
                k: value[k]
                for k in (
                    "complete",
                    "checks",
                    "passed",
                    "learning_curve",
                    "final_second_seed",
                    "final_still_history",
                )
            },
            indent=1,
        )
    )
