#!/usr/bin/env python3
"""Score the ANWM input ablation on the host against withheld real futures.

Per weights x sample x seed it reports where the lead is read in each condition
and how much swapping the history (A vs B, C vs D) or the conditioning image
(A vs C, B vs D) moves the forecast inside the lead's now/future regions.
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
from scripts.yokohama_pad_wam_study import detect_lead, lead_mask, sha, write  # noqa: E402

PAIRS = {
    "history_with_latest": ("A", "B"),
    "history_with_background": ("C", "D"),
    "conditioning_with_moving": ("A", "C"),
    "conditioning_with_repeated": ("B", "D"),
}


def dilate(mask, k=3):
    out = mask.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            out |= np.roll(mask, (dy, dx), (0, 1))
    return out


def error(pred, real):
    if not pred["present"]:
        return "missed"
    return float(np.hypot(*np.subtract(pred["center_px"], real["center_px"])))


def evaluate(staged, results, futures):
    protocol = json.loads((staged / "input-ablation-protocol.json").read_text())
    summary = json.loads((results / "summary.json").read_text())
    records = json.loads((results / "forecasts.json").read_text())
    expected = {
        (w, s["id"], c, seed)
        for w in protocol["weights"]
        for s in protocol["samples"]
        for c in protocol["conditions"]
        for seed in protocol["seeds"]
    }
    actual = [(r["weights"], r["sample"], r["condition"], r["seed"]) for r in records]
    complete = (
        summary.get("status") == "completed"
        and set(actual) == expected
        and len(actual) == len(expected)
    )
    images, rows = {}, []
    for r in records:
        path = results / r["final"]["file"]
        if sha(path) != r["final"]["sha256"]:
            raise ValueError("Forecast image changed")
        images[(r["weights"], r["sample"], r["condition"], r["seed"])] = np.asarray(
            Image.open(path).convert("RGB")
        )
    for sample in protocol["samples"]:
        with np.load(staged / sample["history"], allow_pickle=False) as a:
            now = a["rgb"][-1]
        future = np.asarray(Image.open(futures[sample["id"]]).convert("RGB"))
        real_now, real_future = detect_lead(now, "tight"), detect_lead(future, "tight")
        mnow, mfut = dilate(lead_mask(now)), dilate(lead_mask(future))
        regions = {"now": mnow & ~mfut, "future": mfut & ~mnow}
        for w in protocol["weights"]:
            for seed in protocol["seeds"]:
                row = dict(
                    weights=w,
                    sample=sample["id"],
                    trained=sample["trained_in_after_1024"],
                    seed=seed,
                    real_move_px=float(
                        np.hypot(*np.subtract(real_future["center_px"], real_now["center_px"]))
                    ),
                )
                for c in protocol["conditions"]:
                    pred = detect_lead(images[(w, sample["id"], c, seed)], "tight")
                    row[c] = dict(
                        present=pred["present"],
                        error_to_future=error(pred, real_future),
                        error_to_now=error(pred, real_now),
                    )
                for name, (a, b) in PAIRS.items():
                    x = images[(w, sample["id"], a, seed)].astype(float)
                    y = images[(w, sample["id"], b, seed)].astype(float)
                    row[name] = {k: float(np.abs(x - y)[m].mean()) for k, m in regions.items()}
                    row[name]["elsewhere"] = float(np.abs(x - y)[~(mnow | mfut)].mean())
                rows.append(row)
    table = {}
    for w in protocol["weights"]:
        for trained in (True, False):
            group = [r for r in rows if r["weights"] == w and r["trained"] == trained]
            if not group:
                continue
            table[f"{w}/{'trained' if trained else 'untrained'}"] = {
                name: {
                    k: statistics.median(g[name][k] for g in group)
                    for k in ("now", "future", "elsewhere")
                }
                for name in PAIRS
            }
    return dict(
        schema="pad_anwm_input_ablation_evaluation.v1",
        complete=complete,
        rows=rows,
        sensitivity=table,
        scope=protocol["interpretation"]["scope"],
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--staged", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument(
        "--future",
        action="append",
        required=True,
        help="SAMPLE_ID=PATH to the withheld real future",
    )
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    futures = dict(item.split("=", 1) for item in a.future)
    value = evaluate(a.staged, a.results, {k: Path(v) for k, v in futures.items()})
    write(a.output, value)
    print(json.dumps(dict(complete=value["complete"], sensitivity=value["sensitivity"]), indent=1))
