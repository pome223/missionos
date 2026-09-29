#!/usr/bin/env python3
"""Freeze the ANWM rule-learning test: all training pairs, held-out ids, gate.

Held-out moving/static ids are chosen from host truth readouts only (no forecast
exists). Only cutoff-bounded validation histories are staged; futures stay local.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_pad_wam_study import displacement, sha, write  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SOURCES = (
    "train_yokohama_pad_wam_rule.py",
    "ship_anwm.py",
    "ship_anwm_server.py",
    "yokohama_wam_profile.py",
    "yokohama_appearance.py",
)
GATE = dict(
    offset=12,
    readable_fraction=0.5,
    fraction_of_persistence=0.5,
    both_seeds=True,
    history_used="moving-history median error below still-history median error",
)


def spread(rows, per_sequence):
    by = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        by.setdefault(r["id"].split("-")[2], []).append(r)
    picked = []
    for key in sorted(by):
        group = by[key]
        step = max(1, len(group) // per_sequence)
        picked += group[step // 2 :: step][:per_sequence]
    return picked


def select(truth):
    val = [t for t in truth if t["split"] == "val" and t["id"].endswith("-t12-tight")]
    moved = [t for t in val if (displacement(t["readout_now"], t["readout_future"]) or 0) >= 12]
    still = [
        t
        for t in val
        if displacement(t["readout_now"], t["readout_future"]) is not None
        and displacement(t["readout_now"], t["readout_future"]) < 1.5
    ]
    return [t["id"] for t in spread(moved, 3)], [t["id"] for t in spread(still, 1)]


def stage(prepared, output, steps, checkpoints):
    truth = json.loads((prepared / "truth.json").read_text())
    data = json.loads((prepared / "payload/dataset.json").read_text())
    moving, static = select(truth)
    keep = [
        s
        for s in data["samples"]
        if s["crop"] == "tight" and (s["split"] == "train" or s["id"] in moving + static)
    ]
    used = {s["frames"] for s in keep}
    output.mkdir(parents=True, exist_ok=False)
    payload = output / "payload"
    for rel in sorted(used):
        (payload / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prepared / "payload" / rel, payload / rel)
    samples = [
        {k: v for k, v in s.items() if k != "target"} for s in keep
    ]  # targets come from frames
    subset = dict(
        data,
        samples=samples,
        assets={k: v for k, v in data["assets"].items() if k in used},
        crops={"tight": data["crops"]["tight"]},
    )
    write(payload / "dataset.json", subset)
    for name in SOURCES:
        shutil.copy2(REPO / "scripts" / name, output / name)
    protocol = dict(
        schema="pad_wam_rule_protocol.v1",
        question="Can native ANWM learn a transferable lead-motion rule from diverse training pairs?",
        dataset_sha256=sha(payload / "dataset.json"),
        source_sha256={n: sha(output / n) for n in SOURCES},
        crop="tight",
        conditioning="latest image",
        loss="plain diffusion loss (no region weight)",
        initial_weights="pinned ANWM base + motion-v4 adapter",
        trainable="blocks 26-27 and existing output/attention heads",
        training_pairs=sum(s["split"] == "train" for s in samples),
        steps=steps,
        checkpoints=checkpoints,
        lr=5e-5,
        seed=42,
        second_seed=43,
        evaluation_steps=50,
        moving_ids=moving,
        static_ids=static,
        gate=GATE,
        gate_scope="Held-out validation sequences at 3 s; diagnostic, not delivery adoption; test split reserved",
        on_failure="Stop native ANWM lead prediction at this scale; move delivery to an explicit predictor",
        model_work_seconds_max=6000,
        evaluation_targets_uploaded=False,
        frozen_before_native_inference=True,
    )
    write(output / "rule-protocol.json", protocol)
    with tarfile.open(output / "payload.tar.gz", "w:gz") as t:
        t.add(payload, arcname="payload")
    return dict(
        training_pairs=protocol["training_pairs"],
        moving=len(moving),
        static=len(static),
        sequences=len({i.split("-")[2] for i in moving}),
        payload_bytes=(output / "payload.tar.gz").stat().st_size,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(stage(a.prepared, a.output, 30000, [8000, 16000, 30000])))
