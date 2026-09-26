#!/usr/bin/env python3
"""Build a reviewed, portable replay from verified CPU SITL records (no raw logs)."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_scene import to_source  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flight", type=Path, required=True)
    parser.add_argument("--contacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    flight = read(args.flight / "verification.json")
    contacts = read(args.contacts / "verification.json")
    if flight["status"] != "passed" or contacts["status"] != "passed":
        raise SystemExit("Only verified records may be published")
    config = read(args.flight / "config.json")
    observed = read(args.flight / "worker-result.json")
    rows = [
        json.loads(s) for s in (args.flight / "flight-trajectory.jsonl").read_text().splitlines()
    ]
    start = rows[0]["sim_s"]
    trajectory = []
    # Preserve the first observed pose per clock tick; duplicate stats are not new time samples.
    for r in rows:
        if trajectory and r["sim_s"] - start <= trajectory[-1]["t"]:
            continue
        trajectory.append(
            dict(
                t=round(r["sim_s"] - start, 6),
                xyz=to_source(r["vehicle"]["xyz"], config["world"]["frame"]).round(6).tolist(),
                phase=r["phase"],
                nav_state=int(r["nav_state"]),
            )
        )
    images = root / "images"
    images.mkdir(exist_ok=True)
    holds = []
    for h, record in zip(observed["holds"], observed["frames"]):
        image = args.flight / record["onboard_rgb"]["file"]
        shutil.copy2(image, images / image.name)
        holds.append(
            dict(h, image="data:image/png;base64," + base64.b64encode(image.read_bytes()).decode())
        )
    for key in ["scene_rgb", "scene_depth"]:
        source = args.flight / read(args.flight / "scene-images.json")[key]["file"]
        if key == "scene_rgb":
            shutil.copy2(source, images / source.name)
    data = dict(
        schema="missionos.yokohama-ap-replay.v1",
        run_id=config["run_id"],
        clock="simulator seconds since first telemetry sample",
        sample_reduction="No decimation; duplicate stats ticks collapsed keeping first observation",
        trajectory=trajectory,
        points=config["world"]["points"],
        holds=holds,
    )
    (root / "trajectory.json").write_text(
        json.dumps({k: v for k, v in data.items() if k != "holds"}, indent=2) + "\n"
    )
    bundle = REPO / "docs/examples/yokohama-urban-scene"
    template = Path(__file__).with_name("replay.html").read_text()
    html = (
        template.replace("__THREE__", (bundle / "vendor/three.min.js").read_text())
        .replace("__DATA__", json.dumps(data, separators=(",", ":")))
        .replace("__SCENE__", (bundle / "scene.json").read_text())
    )
    (root / "index.html").write_text(html)
    shutil.copy2(args.flight / "verification.json", root / "verification-flight.json")
    shutil.copy2(args.contacts / "verification.json", root / "verification-contacts.json")
    public_holds = [{k: v for k, v in h.items() if k != "image"} for h in holds]
    (root / "hold-results.json").write_text(json.dumps(public_holds, indent=2) + "\n")
    contact_observed = read(args.contacts / "worker-result.json")
    summary = dict(
        flight_run_id=config["run_id"],
        contact_run_id=contacts["run_id"],
        image_id=read(args.flight / "result.json")["image_id"],
        world_sha256=config["world"]["world_sha256"],
        source_sha256=read(args.flight / "result.json")["source_sha256"],
        frame=config["world"]["frame"],
        mesh_conversion=config["world"]["mesh_conversion"],
        holds=public_holds,
        contact_cases={
            k: {field: v for field, v in case.items() if field not in ["initial", "final"]}
            for k, case in contact_observed["cases"].items()
        },
        native_vla_invoked=False,
        native_wam_invoked=False,
        gpu_spend_usd=0,
        payload_delivery_verified=False,
    )
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    # Public frame video is built separately from the untouched raw camera records.
    manifest = dict(
        schema="missionos.yokohama-sitl-evidence.v1",
        runs=[flight, contacts],
        source_scene_bundle_sha256=hashlib.sha256(
            (bundle / "files.sha256.json").read_bytes()
        ).hexdigest(),
    )
    (root / "evidence-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(output=str(root), run_id=config["run_id"], samples=len(trajectory))))


if __name__ == "__main__":
    main()
