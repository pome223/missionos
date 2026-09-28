#!/usr/bin/env python3
"""Publish portable reentry observations, comparison and CPU replay receipts."""

from __future__ import annotations

import argparse
import copy
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.yokohama_pad_state import sha, write  # noqa: E402

SOURCES = [
    "scripts/run_yokohama_pad_reentry.py",
    "scripts/evaluate_yokohama_pad_reentry.py",
    "scripts/check_yokohama_pad_reentry.py",
    "scripts/export_yokohama_pad_reentry.py",
    "scripts/yokohama_pad_reentry_report.html",
    "scripts/yokohama_pad_state.py",
    "scripts/run_yokohama_pad_state.py",
    "scripts/capture_yokohama_pad_motion.py",
    "scripts/yokohama_pad_advisory_host.py",
    "src/runtime/yokohama_pad_queue.py",
    "src/runtime/yokohama_pad_advisory_contract.py",
    "src/runtime/yokohama_pad_reentry_risk.py",
]


def manifest(output):
    write(output / "source-sha256.json", {name: sha(REPO / name) for name in SOURCES})
    write(
        output / "evidence-manifest.json",
        dict(
            schema="missionos.public-evidence.v1",
            files=[
                dict(path=p.relative_to(output).as_posix(), bytes=p.stat().st_size, sha256=sha(p))
                for p in sorted(output.rglob("*"))
                if p.is_file() and p.name != "evidence-manifest.json"
            ],
        ),
    )


def export(root, output):
    output.mkdir(parents=True, exist_ok=False)
    for name in ("protocol.json", "replay-protocol.json", "admission.json"):
        shutil.copy2(root / name, output / name)
    if (root / "training-protocol.json").exists():
        shutil.copy2(root / "training-protocol.json", output / "training-protocol.json")
    for name in ("data", "model", "evaluation"):
        shutil.copytree(root / name, output / name)
    predictions_path = output / "evaluation/predictions.json"
    with gzip.open(predictions_path.with_suffix(".json.gz"), "wb") as stream:
        stream.write(predictions_path.read_bytes())
    predictions_path.unlink()
    (output / "capture").mkdir()
    for name in ("config.json", "capture-result.json", "runtime.json", "cleanup.json"):
        shutil.copy2(
            root / "capture" / name,
            output / ("capture-config.json" if name == "config.json" else name),
        )
    protocol = json.loads((root / "protocol.json").read_text())
    records = json.loads((root / "evaluation/predictions.json").read_text())
    view = dict(
        schema="missionos.pad-reentry-view.v1",
        time_unit="simulated seconds since each capture epoch",
        own_aircraft_flown=False,
        cases=[],
    )
    for case in protocol["cases"]:
        name = case["id"]
        capture = root / "capture" / name
        shutil.copy2(capture / "capture.json", output / "capture" / (name + ".json"))
        if case["split"] != "unseen_test":
            continue
        target = output / "images" / name
        target.mkdir(parents=True)
        selected = [copy.deepcopy(r) for r in records if r["case"] == name]
        with np.load(root / "data" / (name + ".npz"), allow_pickle=False) as data:
            for row in selected:
                i = row["frame_index"]
                Image.fromarray(data["rgb"][i]).save(target / f"{i:04d}.png")
            observed = [
                dict(t=float(t), radius_m=float(np.linalg.norm(xyz[:2])))
                for t, xyz in zip(data["elapsed_s"], data["xyz"], strict=True)
            ]
        video = output / (name + ".mp4")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                "4",
                "-i",
                str(capture / "%04d-rgb.png"),
                "-an",
                "-c:v",
                "libx264",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(video),
            ],
            check=True,
            timeout=120,
        )
        for row in selected:
            row["image"] = f"images/{name}/{row['frame_index']:04d}.png"
            for method, decision in row["decisions"].items():
                f = decision.pop("forecast")
                if method in {"existing", "post_trained"}:
                    decision["forecast"] = dict(
                        forecasts=[
                            dict(
                                offset_s=x["offset_s"],
                                xyz_relative_to_pad_m=x["xyz_relative_to_pad_m"],
                            )
                            for x in f["forecasts"]
                        ]
                    )
        view["cases"].append(
            dict(
                id=name,
                observed=observed,
                records=selected,
                video=video.name,
                capture_sha256=sha(capture / "capture.json"),
            )
        )
    write(output / "view-data.json", view)
    write(output / "cost.json", protocol["budget"])
    shutil.copy2(REPO / "scripts/yokohama_pad_reentry_report.html", output / "index.html")
    manifest(output)
    print(
        json.dumps(
            dict(
                status="exported",
                files=len(list(output.rglob("*"))),
                video_boundary="recorded fixed camera, no aircraft flight or battery telemetry",
            )
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.root, args.output)
