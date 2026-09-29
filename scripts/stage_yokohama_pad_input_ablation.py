#!/usr/bin/env python3
"""Freeze the ANWM input ablation: retained weights, two histories, backgrounds, protocol.

The trained example is the one-pair C1 history; a second, untrained moving
validation history is chosen from host truth readouts only. Futures stay local.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_wam_study import displacement, sha, write  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SOURCES = (
    "ablate_yokohama_pad_inputs.py",
    "trace_yokohama_pad_sampling.py",
    "ship_anwm.py",
    "ship_anwm_server.py",
    "yokohama_wam_profile.py",
    "yokohama_appearance.py",
)
TRAINED = "wam-train-10-return-031-t12-tight"


def untrained_moving(truth):
    rows = [
        t
        for t in truth
        if t["split"] == "val"
        and t["id"].endswith("-t12-tight")
        and t["phase_now"] in ("departing", "returning")
        and (displacement(t["readout_now"], t["readout_future"]) or 0) >= 12
    ]
    return sorted(rows, key=lambda t: t["id"])[0]


def stage(study, c1, output):
    truth = json.loads((study / "prepared/truth.json").read_text())
    data = json.loads((study / "prepared/payload/dataset.json").read_text())
    samples = {s["id"]: s for s in data["samples"]}
    val = untrained_moving(truth)
    output.mkdir(parents=True, exist_ok=False)
    c1_protocol = json.loads((c1 / "staged/latent-protocol.json").read_text())
    shutil.copy2(c1 / "remote/results/weights/after-1024.pt", output / "after-1024.pt")
    shutil.copy2(c1 / "staged/motion-adapter.pt", output / "motion-adapter.pt")
    shutil.copy2(study / "prepared/payload/backgrounds/tight.png", output / "background-tight.png")
    # C1's pair.npz holds only the 16 observed frames; its future stays on the host.
    shutil.copy2(c1 / "staged/pair.npz", output / "history-trained.npz")
    cutoff = c1_protocol["cutoff_stamp_ns"]
    held = samples[val["id"]]
    shutil.copy2(study / "prepared/payload" / held["frames"], output / "history-untrained.npz")
    for name in SOURCES:
        shutil.copy2(REPO / "scripts" / name, output / name)
    files = [
        "after-1024.pt",
        "motion-adapter.pt",
        "background-tight.png",
        "history-trained.npz",
        "history-untrained.npz",
        *SOURCES,
    ]
    protocol = dict(
        schema="pad_anwm_input_ablation.v1",
        purpose=(
            "Separate the effect of a moving history from the effect of the conditioning image, "
            "with weights, seeds and horizon fixed"
        ),
        weights=["initial", "after-1024"],
        training_protocol_sha256=sha(c1 / "staged/latent-protocol.json"),
        samples=[
            dict(
                id=TRAINED,
                history="history-trained.npz",
                background="background-tight.png",
                cutoff_stamp_ns=cutoff,
                trained_in_after_1024=True,
            ),
            dict(
                id=val["id"],
                history="history-untrained.npz",
                background="background-tight.png",
                cutoff_stamp_ns=held["cutoff_stamp_ns"],
                trained_in_after_1024=False,
            ),
        ],
        conditions={
            "A": "moving history + latest image",
            "B": "latest image repeated 16x + latest image",
            "C": "moving history + lead-free background",
            "D": "latest image repeated 16x + lead-free background",
        },
        seeds=[42, 43],
        sampling_steps=50,
        offset=12,
        display_anchors=[999, 750, 500, 250, 0],
        training_allowed=False,
        model_work_seconds_max=900,
        interpretation=dict(
            history_used="A places the lead nearer its future position than B, beyond seed spread",
            conditioning_dependence="Replacing the latest image with the background (A->C, B->D) changes the lead region strongly",
            neither="Neither swap changes the lead region: memorized appearance or unused conditioning inputs",
            scope="Only these weights and scenes; not a general claim about ANWM",
        ),
        c1_training_seed=c1_protocol.get("train_seed"),
        files={name: sha(output / name) for name in files},
        frozen_before_native_inference=True,
    )
    write(output / "input-ablation-protocol.json", protocol)
    return dict(untrained=val["id"], cutoff_trained=cutoff)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--study", type=Path, required=True)
    p.add_argument("--c1", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(stage(a.study, a.c1, a.output)))
