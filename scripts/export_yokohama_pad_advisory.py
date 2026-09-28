#!/usr/bin/env python3
"""Export reviewed CPU pad-advisory evidence, replay and battery videos."""

from __future__ import annotations
import argparse
import gzip
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_yokohama_battery_video import build  # noqa: E402
from scripts.yokohama_pad_state import sha  # noqa: E402
from src.runtime.yokohama_payload import read_jsonl  # noqa: E402
from src.runtime.yokohama_scene import to_world  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def export(run, out):
    def read(p):
        return json.loads(p.read_text())

    c = read(run / "config.json")
    verified = read(run / "advisory-verification.json")
    if any(
        read(run / name)["status"] != "passed"
        for name in ["advisory-verification.json", "verification.json", "payload-verification.json"]
    ):
        raise ValueError("The flight evidence must pass before publication")
    out.mkdir(parents=True, exist_ok=False)
    archived = out / "run"
    archived.mkdir()
    names = [
        "config.json",
        "result.json",
        "worker-result.json",
        "pad-advisory-closed.json",
        "pad-advisory-release.json",
        "pad-entry-permit.json",
        "payload-request.json",
        "payload-receipt.json",
        "hold-results.json",
    ]
    for name in names:
        shutil.copy2(run / name, archived / name)
    for name in [
        "flight-trajectory.jsonl",
        "flight-events.jsonl",
        "sensor-events.jsonl",
        "payload-joint-events.jsonl",
        "pad-advisory-events.jsonl",
    ]:
        (archived / (name + ".gz")).write_bytes(gzip.compress((run / name).read_bytes(), mtime=0))
    for name in ["pad-decisions", "pad-state-model", "sources"]:
        shutil.copytree(run / name, archived / name)
    for name in [
        "advisory-verification.json",
        "verification.json",
        "payload-verification.json",
        "pad-verification.json",
    ]:
        shutil.copy2(run / name, out / name)
    rows, events = (
        read_jsonl(run / "flight-trajectory.jsonl"),
        read_jsonl(run / "flight-events.jsonl"),
    )
    unique = {}
    for row in rows:
        unique[row["sim_s"]] = dict(
            t=row["sim_s"],
            xyz=row["vehicle"]["xyz"],
            lead=row["queue_lead"]["xyz"],
            phase=row["phase"],
            battery=row["battery_fraction"],
        )
    samples = sorted(unique.values(), key=lambda r: r["t"])
    decisions = []
    for item in verified["requests"]:
        folder = run / "pad-decisions" / f"{item['sequence']:03d}"
        response = read(folder / "response.json")
        received = next(
            e
            for e in events
            if e["event"] == "missionos_advice_received" and e["response"] == response
        )
        image, future = None, []
        if (folder / "history/capture.json").exists():
            frames = read(folder / "history/capture.json")["frames"]
            image = f"run/pad-decisions/{folder.name}/history/{frames[-1]['file']}"
        if response["advisory"]["status"] == "computed":
            future = [
                dict(t=f["stamp_ns"] / 1e9, state=f["state"])
                for f in response["advisory"]["forecast"]["forecasts"]
            ]
        decisions.append(
            dict(item, received_sim_s=received["observation"]["sim_s"], image=image, future=future)
        )
    features = read(REPO / "docs/examples/yokohama-urban-scene/collision-footprints.geojson")[
        "features"
    ]
    buildings = []
    for feature in features:
        geom = feature["geometry"]
        for poly in [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]:
            buildings.append(
                to_world([[x, y, 0] for x, y, *_ in poly[0]], c["world"]["frame"])[:, :2].tolist()
            )
    scene = dict(
        buildings=buildings,
        pad=c["world"]["pad_queue"]["pad_xyz_m"],
        wait=c["world"]["pad_queue"]["wait_xyz_m"],
        ship=c["world"]["sea_extension"]["ship_deck_world_xyz_m"],
    )
    payload = dict(
        run_id=c["run_id"],
        samples=samples,
        decisions=decisions,
        scene=scene,
        closed_sim_s=read(run / "pad-advisory-closed.json")["closed_sim_s"],
        source_trajectory_sha256=sha(run / "flight-trajectory.jsonl"),
        projection="One display row per simulator timestamp, keeping its last observation; verification uses every original row.",
    )
    write(out / "replay.json", payload)
    battery = next(
        r["battery_fraction"]
        for r in reversed(rows)
        if r["landed"] is False and r["arming_state"] == 2
    )
    measured = dict(
        run_id=c["run_id"],
        duration_sim_s=rows[-1]["sim_s"] - rows[0]["sim_s"],
        trajectory_rows=len(rows),
        wait_sim_s=verified["queue_summary"]["wait_sim_s"],
        supported=verified["supported"],
        inference_calls=verified["inference_calls"],
        decisions=len(decisions),
        action_changes=verified["action_changes_against_same_observations"],
        signals=verified["signals"],
        airborne_battery_end_fraction=battery,
        minimum_sampled_separation_m=verified["queue_summary"]["minimum_sampled_separation_m"],
        mission_advantage_demonstrated=False,
    )
    write(out / "metrics.json", measured)
    write(
        out / "cost.json",
        dict(
            new_gpu_usd=0,
            cumulative_estimate_usd=18.532818962573373,
            authorized_ceiling_usd=20,
            new_cloud_resources_created=False,
            estimate_not_invoice=True,
        ),
    )
    for camera, speed in [("onboard", 8), ("queue", 2)]:
        build(run, out / ("video-" + camera), camera=camera, speed=speed)
    html = (REPO / "scripts/yokohama_pad_advisory_report.html").read_text()
    html = html.replace(
        "__DATA__",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"),
    )
    for key, value in measured.items():
        html = html.replace(
            "__" + key.upper() + "__",
            str(round(value, 2)) if isinstance(value, float) else str(value),
        )
    (out / "index.html").write_text(html)
    write(
        out / "evidence-manifest.json",
        dict(
            run_id=c["run_id"],
            raw_source_sha256={
                n: sha(run / n)
                for n in names
                + [
                    "flight-trajectory.jsonl",
                    "flight-events.jsonl",
                    "sensor-events.jsonl",
                    "payload-joint-events.jsonl",
                    "pad-advisory-events.jsonl",
                ]
            },
            files=[
                dict(path=p.relative_to(out).as_posix(), sha256=sha(p), bytes=p.stat().st_size)
                for p in sorted(out.rglob("*"))
                if p.is_file() and p.name != "evidence-manifest.json"
            ],
        ),
    )
    return measured


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.run, args.output)))
