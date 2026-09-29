#!/usr/bin/env python3
"""Freeze one GPU study stage: sample selection, subset payload and protocol.

Selection reads only the host truth file (real-frame readouts and phases);
no native forecast exists yet. Held-out targets are never staged.
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
    "train_yokohama_pad_wam_study.py",
    "ship_anwm.py",
    "ship_anwm_server.py",
    "yokohama_wam_profile.py",
    "yokohama_appearance.py",
)


def base(ident):
    return ident.rsplit("-", 1)[0]


def round_robin(rows, count):
    """Spread picks across sequences, keeping each sequence's earliest first."""
    by_sequence = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        by_sequence.setdefault(r["id"].split("-")[2], []).append(r)
    picked = []
    while len(picked) < count and any(by_sequence.values()):
        for key in sorted(by_sequence):
            if by_sequence[key] and len(picked) < count:
                picked.append(by_sequence[key].pop(len(by_sequence[key]) // 2))
    return picked


def stratified(rows, count, phases=("departing", "returning", "landing", "ascending", "hover")):
    """Balance motion phases (lateral and vertical), each pick from an unused sequence."""
    groups = {
        p: sorted((r for r in rows if r["phase_now"] == p), key=lambda r: r["id"]) for p in phases
    }
    used, picked = set(), []

    def sequence(r):
        return r["id"].split("-")[2]

    while len(picked) < count and any(groups.values()):
        for p in phases:
            if not groups[p] or len(picked) >= count:
                continue
            fresh = [r for r in groups[p] if sequence(r) not in used] or groups[p]
            first = sequence(fresh[0])
            same = [r for r in fresh if sequence(r) == first]
            choice = same[len(same) // 2]
            groups[p].remove(choice)
            used.add(first)
            picked.append(choice)
    return picked


def select(truth):
    tight = {base(t["id"]): t for t in truth if t["id"].endswith("-tight")}

    def moved(t):
        return displacement(t["readout_now"], t["readout_future"])

    train = [t for k, t in tight.items() if t["split"] == "train"]
    moving12 = [t for t in train if t["id"].split("-")[-2] == "t12" and (moved(t) or 0) >= 12]
    moving04 = [t for t in train if t["id"].split("-")[-2] == "t04" and (moved(t) or 0) >= 5]
    static = [
        t
        for t in train
        if moved(t) is not None and moved(t) < 1.5 and t["phase_now"] in ("on_pad", "hover")
    ]
    moving = stratified(moving12, 12) + stratified(moving04, 4)
    static_pick = round_robin(static, 8)
    val = [t for k, t in tight.items() if t["split"] == "val"]
    evaluation = stratified(
        val,
        min(16, len(val)),
        phases=("departing", "returning", "ascending", "landing", "on_pad", "hover"),
    )
    ids = lambda rows: [base(t["id"]) for t in rows]  # noqa: E731
    return ids(moving), ids(moving + static_pick), ids(evaluation)


def stage(prepared, output, configs, steps, gate):
    truth = json.loads((prepared / "truth.json").read_text())
    data = json.loads((prepared / "payload/dataset.json").read_text())
    moving, train_ids, eval_ids = select(truth)
    crops = sorted({c["crop"] for c in configs})
    keep = {f"{b}-{c}" for b in train_ids + eval_ids for c in crops}
    samples = [s for s in data["samples"] if s["id"] in keep]
    if len(samples) != len(keep):
        raise ValueError("Missing selected samples")
    used = {s["frames"] for s in samples} | {s["target"] for s in samples if "target" in s}
    used |= {f"backgrounds/{c}.png" for c in crops}
    output.mkdir(parents=True, exist_ok=False)
    payload = output / "payload"
    for rel in sorted(used):
        (payload / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prepared / "payload" / rel, payload / rel)
    subset = dict(
        data, samples=samples, assets={k: v for k, v in data["assets"].items() if k in used}
    )
    subset["crops"] = {c: data["crops"][c] for c in crops}
    write(payload / "dataset.json", subset)
    for name in SOURCES:
        shutil.copy2(REPO / "scripts" / name, output / name)
    protocol = dict(
        schema="pad_wam_study_protocol.v1",
        dataset_sha256=sha(payload / "dataset.json"),
        source_sha256={name: sha(output / name) for name in SOURCES},
        initial_weights="pinned ANWM base + motion-v4 adapter; failed pad adapters not used",
        trainable="final two transformer blocks (26, 27) and existing output/attention heads",
        configs=configs,
        train_ids=train_ids,
        moving_ids=moving,
        eval_ids=eval_ids,
        steps=steps,
        lr=5e-5,
        roi_weight=4.0,
        seed=42,
        evaluation_steps=50,
        model_work_seconds_max=2400,
        memorization_gate=gate,
        gate_scope=(
            "Diagnostic only: separates reproducing moving training scenes from generalizing; "
            "not a delivery adoption criterion"
        ),
        on_failure=(
            "Record whether input, training, generation or readout remained limiting and stop "
            "spending on this condition; not a claim that ANWM cannot forecast the lead"
        ),
        flight_horizon_note=(
            "The 3 s horizon is for diagnosis; flight use must forecast past inference and "
            "transfer completion"
        ),
        frozen_before_native_inference=True,
        evaluation_targets_uploaded=False,
        selection="host truth readouts on real frames only; no forecast existed",
    )
    write(output / "study-protocol.json", protocol)
    with tarfile.open(output / "payload.tar.gz", "w:gz") as t:
        t.add(payload, arcname="payload")
    return dict(
        moving=len(moving),
        train=len(train_ids),
        eval=len(eval_ids),
        payload_bytes=(output / "payload.tar.gz").stat().st_size,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--stage", choices=["diagnostic", "diagnostic-rerun"], default="diagnostic")
    a = p.parse_args()
    if a.stage == "diagnostic":
        configs = [
            dict(name=f"{crop}-{cond}", crop=crop, conditioning=cond, before=cond == "latest")
            for crop in ("wide", "tight")
            for cond in ("latest", "background")
        ]
    else:
        # After session B: the wide/latest/ROI-4 run painted orange texture. Test the
        # region weight first, then crop and conditioning, all without it.
        configs = [
            dict(name="wide-latest-roi0", crop="wide", conditioning="latest", roi_weight=0.0),
            dict(
                name="tight-latest-roi0",
                crop="tight",
                conditioning="latest",
                roi_weight=0.0,
                before=True,
            ),
            dict(
                name="tight-background-roi0",
                crop="tight",
                conditioning="background",
                roi_weight=0.0,
            ),
        ]
    gate = dict(offset=12, presence_fraction=0.8, median_error_px=6.0, fraction_of_persistence=0.5)
    print(json.dumps(stage(a.prepared, a.output, configs, 1024, gate)))
