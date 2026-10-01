#!/usr/bin/env python3
"""Freeze separated real-AP histories, with test future frames staying local."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import zlib
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runtime.yokohama_motion import load_frames, verify  # noqa: E402
from scripts.yokohama_motion_data import validate_payload, digest  # noqa: E402


def select(frames, verification, config):
    qualified = [r for r in verification["candidates"] if r["qualified"]]
    selected = []
    for phase, number in [("01-D2", 4), ("03-DELIVERY", 6)]:
        moves = [r for r in qualified if r["phase"] == phase and r.get("action") == "forward"]
        if len(moves) < number:
            raise ValueError("Insufficient frozen training coverage")
        selected.extend(dict(r, split="train") for r in moves[:number])
    # Collection geometry was screened before model work. Delivery's first
    # loiter frames are still settling, so the two qualified D2 holds supply
    # the stationary training examples. They are correlated, not two trials.
    training_holds = [r for r in qualified if r["phase"] == "01-D2" and r.get("action") == "hold"]
    if len(training_holds) < 2:
        raise ValueError("Insufficient qualified training holds")
    selected.extend(dict(r, split="train") for r in training_holds[:2])
    a, b = np.asarray(config["motion_capture"]["phase_lines"]["02-D3"])
    vector = b - a
    used = []
    for fraction in [0.60, 0.70, 0.80, 0.90]:
        options = []
        for r in qualified:
            if r["phase"] != "02-D3" or r.get("action") != "forward":
                continue
            xyz = np.asarray(frames[r["cutoff_index"]]["pose"]["xyz"])
            progress = float((xyz - a) @ vector / (vector @ vector))
            if fraction <= progress <= fraction + 0.04 and all(
                abs(r["cutoff_index"] - i) >= 20 for i in used
            ):
                options.append(r)
        if not options:
            raise ValueError("Missing predeclared held-out forward location")
        chosen = options[0]
        used.append(chosen["cutoff_index"])
        selected.append(dict(chosen, split="test"))
    holds = [r for r in qualified if r["phase"] == "02-D3" and r.get("action") == "hold"]
    if not holds:
        raise ValueError("Missing held-out hold observation")
    selected.append(dict(holds[0], split="test"))
    return selected


def prepare(root, output, heldout, sitl_verification):
    root, output, heldout = map(Path, [root, output, heldout])
    flight = json.loads(Path(sitl_verification).read_text())
    verification = verify(root)
    if flight["status"] != "passed" or flight["run_id"] != verification["run_id"]:
        raise ValueError("Complete SITL verification required")
    manifest, frames = load_frames(root)
    config = json.loads((root / "config.json").read_text())
    selected = select(frames, verification, config)
    output.mkdir(parents=True, exist_ok=False)
    heldout.mkdir(parents=True, exist_ok=False)
    for name in ["inputs", "frames", "targets"]:
        (output / name).mkdir()
    samples = []
    assets = {}
    all_points = {"train": [], "test": []}
    for i, r in enumerate(selected):
        name = f"{r['split']}-motion-{i:02d}-{r['action']}"
        past = []
        for index in r["input_indices"]:
            f = frames[index]
            record = dict(
                index=index, stamp_ns=f["stamp_ns"], camera_pose=f["camera_pose"].tolist()
            )
            all_points[r["split"]].append(f["camera_pose"][:3, 3])
            for key, alias in [("onboard_rgb", "rgb"), ("onboard_depth", "depth")]:
                a = f["assets"][key]
                dest = "frames/" + a["file"]
                p = output / dest
                if not p.exists():
                    os.link(root / "motion" / a["file"], p)
                assets[dest] = a["sha256"]
                record[alias] = dict(a, file=dest)
            past.append(record)
        descriptor = "inputs/" + name + ".json"
        (output / descriptor).write_text(
            json.dumps(
                dict(
                    frames=past,
                    intrinsics=np.asarray(manifest["camera_info"]["k"]).reshape(3, 3).tolist(),
                ),
                indent=2,
            )
            + "\n"
        )
        assets[descriptor] = digest(output / descriptor)
        actual = frames[r["target_index"]]
        a = actual["assets"]["onboard_rgb"]
        raw = zlib.decompress((root / "motion" / a["file"]).read_bytes())
        image = Image.fromarray(np.frombuffer(raw, np.uint8).reshape(360, 640, 3))
        dest = (
            (output / "targets" / f"{name}.png")
            if r["split"] == "train"
            else (heldout / f"{name}.png")
        )
        image.save(dest)
        target_hash = digest(dest)
        row = dict(
            id=name,
            site=name,
            split=r["split"],
            action=r["action"],
            delta=r["delta"],
            input=dict(file=descriptor, sha256=assets[descriptor]),
            input_indices=r["input_indices"],
            target_index=r["target_index"],
            input_cutoff_sim_s=frames[r["cutoff_index"]]["stamp_ns"] / 1e9,
            observed_elapsed_sim_s=r["observed_elapsed_sim_s"],
            position_error_m=r["endpoint_position_error_m"],
            rotation_error_rad=r["endpoint_rotation_error_rad"],
            requested_camera_pose=r["requested_camera_pose"],
            candidate_dispatched=False,
            history_displacement_m=r["history_displacement_m"],
            target_sha256=target_hash,
        )
        # Future poses remain in the local source archive, never in model payloads.
        all_points[r["split"]].append(np.asarray(r["requested_camera_pose"])[:3, 3])
        if r["split"] == "train":
            rel = str(dest.relative_to(output))
            assets[rel] = target_hash
            row["training_target"] = dict(file=rel, sha256=target_hash)
        samples.append(row)
    a, b = np.asarray(all_points["train"]), np.asarray(all_points["test"])
    separation = float(np.linalg.norm(a[:, None] - b[None], axis=2).min())
    dataset = dict(
        schema_version="yokohama_motion_payload.v1",
        qualification=dict(status="passed"),
        samples=samples,
        assets=assets,
        spatial_separation_m=separation,
        training_targets_uploaded=True,
        test_targets_uploaded=False,
        source_frames_sha256=manifest["frames_sha256"],
        source_run_id=verification["run_id"],
        sitl_verification_sha256=digest(sitl_verification),
        context_frames=16,
        time_semantics="One short-view transition; evaluation happens at +1 observed simulator second; not a general time calibration",
        evaluation_scope="New moving histories in a previously inspected city region; not an unseen-scene claim",
    )
    (output / "dataset.json").write_text(json.dumps(dataset, indent=2) + "\n")
    validate_payload(output, dict(dataset_manifest_sha256=digest(output / "dataset.json")), dataset)
    return dict(
        status="passed",
        samples=len(samples),
        train=12,
        test=5,
        spatial_separation_m=separation,
        test_targets_uploaded=False,
        models_invoked=False,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--heldout-dir", required=True, type=Path)
    p.add_argument("--sitl-verification", required=True, type=Path)
    a = p.parse_args()
    print(json.dumps(prepare(a.root, a.output_dir, a.heldout_dir, a.sitl_verification)))


if __name__ == "__main__":
    main()
