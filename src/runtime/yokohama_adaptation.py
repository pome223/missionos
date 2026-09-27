"""Offline adaptation data contracts. No flight, inference or cloud authority."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import json
import shutil

import numpy as np


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def site_plan(points):
    """Keep an entire middle-route region out of training before rendering."""
    groups = [
        (0, "train", [0.15, 0.35, 0.55, 0.75]),
        (1, "train", [0.10, 0.17, 0.24, 0.31]),
        (2, "train", [0.20, 0.40, 0.60, 0.80]),
        (1, "test", [0.60, 0.72, 0.84, 0.96]),
    ]
    sites = []
    for segment, split, fractions in groups:
        a, b = np.asarray(points[segment]), np.asarray(points[segment + 1])
        direction = (b - a) / np.linalg.norm(b - a)
        yaw = math.atan2(direction[1], direction[0])
        for fraction in fractions:
            xyz = a + fraction * (b - a)
            sites.append(
                dict(
                    id=f"{split}-{len(sites):02d}",
                    split=split,
                    segment=segment,
                    fraction=fraction,
                    xyz=xyz.tolist(),
                    yaw_enu_rad=yaw,
                    endpoint_xyz=(xyz + 2.8 * direction).tolist(),
                )
            )
    validate_split(sites)
    return sites


def validate_split(sites, minimum_m=15):
    ids = [s["id"] for s in sites]
    if len(set(ids)) != len(ids) or {s["split"] for s in sites} != {"train", "test"}:
        raise ValueError("Invalid adaptation split")
    groups = {
        k: np.asarray([s[p] for s in sites if s["split"] == k for p in ("xyz", "endpoint_xyz")])
        for k in ("train", "test")
    }
    if not all(np.isfinite(g).all() and g.ndim == 2 and g.shape[1] == 3 for g in groups.values()):
        raise ValueError("Invalid adaptation pose")
    separation = float(
        np.linalg.norm(groups["train"][:, None] - groups["test"][None], axis=2).min()
    )
    if separation < minimum_m:
        raise ValueError("Training/evaluation spatial overlap")
    return separation


def optical_pose(xyz, yaw_enu):
    from scripts.ship_anwm import NED_FROM_ENU, FLU_FROM_OPTICAL

    c, s = math.cos(yaw_enu), math.sin(yaw_enu)
    r = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    out = np.eye(4)
    out[:3, :3] = NED_FROM_ENU @ r @ FLU_FROM_OPTICAL
    out[:3, 3] = NED_FROM_ENU @ np.asarray(xyz)
    return out


def verify_export(root, manifest):
    """Verify split, strict target role and hashes before any paid operation."""
    root = Path(root)
    separation = validate_split(manifest["sites"])
    sources = {s["id"]: s["split"] for s in manifest["sites"]}
    expected = set()
    for sample in manifest["samples"]:
        split = sources[sample["site"]]
        if sample["split"] != split or sample["action"] not in ("hold", "forward"):
            raise ValueError("Sample split/action mismatch")
        sample_id = sample["id"]
        if sample_id != sample["site"] + "-" + sample["action"] or sample_id in expected:
            raise ValueError("Duplicate sample")
        expected.add(sample_id)
        for key in ("input", "target"):
            item = sample[key]
            p = Path(item["file"])
            wanted = (
                f"inputs/{sample['site']}.npz" if key == "input" else f"targets/{sample_id}.png"
            )
            if str(p) != wanted:
                raise ValueError("Adaptation asset role mismatch")
            if p.is_absolute() or ".." in p.parts or (root / p).is_symlink():
                raise ValueError("Invalid adaptation asset")
            if digest(root / p) != item["sha256"]:
                raise ValueError("Adaptation asset hash mismatch")
        if sample["target_sim_s"] <= sample["input_cutoff_sim_s"]:
            raise ValueError("Target is not a later observation")
        if sample["position_error_m"] > 0.03 or sample["rotation_error_rad"] > 0.01:
            raise ValueError("Rendered target pose mismatch")
    if len(expected) != 2 * len(sources):
        raise ValueError("Incomplete adaptation pairs")
    train_targets = {s["target"]["sha256"] for s in manifest["samples"] if s["split"] == "train"}
    test_targets = {s["target"]["sha256"] for s in manifest["samples"] if s["split"] == "test"}
    if train_targets & test_targets:
        raise ValueError("Identical train/test target images")
    return dict(
        status="passed",
        samples=len(expected),
        spatial_separation_m=separation,
        train_samples=sum(s["split"] == "train" for s in manifest["samples"]),
        test_samples=sum(s["split"] == "test" for s in manifest["samples"]),
        flight_invoked=False,
        gpu_requested=False,
    )


def prepare_payload(collection, output):
    """Expose train targets only. Evaluation targets stay on the local host."""
    collection, output = Path(collection), Path(output)
    manifest = json.loads((collection / "manifest.json").read_text())
    receipt = verify_export(collection, manifest)
    if json.loads((collection / "result.json").read_text()).get("status") != "passed":
        raise ValueError("Unqualified collection")
    output.mkdir(parents=True, exist_ok=False)
    rows, assets = [], {}
    for sample in manifest["samples"]:
        with np.load(collection / sample["input"]["file"], allow_pickle=False) as data:
            if (
                set(data.files) != {"rgb", "depth", "poses", "intrinsics", "stamps_s"}
                or data["rgb"].shape != (16, 360, 640, 3)
                or data["rgb"].dtype != np.uint8
                or data["depth"].shape != (16, 360, 640)
                or data["poses"].shape != (16, 4, 4)
                or data["intrinsics"].shape != (3, 3)
                or not np.isfinite(data["poses"]).all()
                or data["stamps_s"].shape != (16,)
                or not np.isfinite(data["stamps_s"]).all()
                or np.max(np.abs(np.diff(data["stamps_s"]) - 0.25)) > 0.004
                or abs(float(data["stamps_s"][-1]) - sample["input_cutoff_sim_s"]) > 1e-8
            ):
                raise ValueError("Invalid past-only adaptation history")
        row = {k: v for k, v in sample.items() if k != "target"}
        row["evaluation_target_sha256"] = (
            sample["target"]["sha256"] if sample["split"] == "test" else None
        )
        include = [sample["input"]]
        if sample["split"] == "train":
            row["training_target"] = sample["target"]
            include.append(sample["target"])
        for asset in include:
            path = output / asset["file"]
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                shutil.copyfile(collection / asset["file"], path)
            assets[asset["file"]] = asset["sha256"]
        rows.append(row)
    payload = dict(
        schema_version="yokohama_adaptation_payload.v1",
        sites=manifest["sites"],
        samples=rows,
        assets=assets,
        qualification=receipt,
        training_targets_uploaded=True,
        test_targets_uploaded=False,
        input_manifest_sha256=digest(collection / "manifest.json"),
        context_frames=16,
        transition_index=1,
        time_semantics=manifest["time_semantics"],
    )
    (output / "dataset.json").write_text(json.dumps(payload, indent=2) + "\n")
    return payload
