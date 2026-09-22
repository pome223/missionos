#!/usr/bin/env python3
"""Prepare one public ANWM sample and two independently fixed candidate plans.

Downloads approximately 280 MB using pinned ranges of an official public tar.
Only sixteen historical RGB/depth/pose frames and the declared goal enter the model
archive. Future trajectory metadata never determines the candidate actions.
This sample is a runtime check, not a held-out navigation evaluation.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import pathlib
import pickle
import urllib.request
import numpy as np
from PIL import Image

try:
    from numpy._core.multiarray import _reconstruct, scalar
except ImportError:
    from numpy.core.multiarray import _reconstruct, scalar

DATASET_REVISION = "f0fcc70df0b3c8c26286adbfb39ccdf5e3b7ad83"
TRAJECTORY = "302OLP89E75X7MFPRR58QG7CIDVACC_processed"
ARCHIVE = "airvln_16-000000.tar"
URL = f"https://huggingface.co/datasets/EmbodiedCity/ANWM-Dataset/resolve/{DATASET_REVISION}/{ARCHIVE}"
RANGES = {
    "0.jpg": (5457920, 482890),
    "1.jpg": (44507648, 478742),
    "2.jpg": (12715008, 435087),
    "3.jpg": (37379584, 434935),
    "4.jpg": (367523328, 460629),
    "5.jpg": (62696960, 461892),
    "6.jpg": (19209216, 459555),
    "7.jpg": (12251136, 463133),
    "8.jpg": (35088384, 452141),
    "9.jpg": (70016512, 449752),
    "10.jpg": (57206272, 454739),
    "11.jpg": (359936, 436220),
    "12.jpg": (71762432, 452582),
    "13.jpg": (365161472, 451182),
    "14.jpg": (360612352, 438334),
    "15.jpg": (349780480, 431571),
    "19.jpg": (37015040, 363807),
    "traj_data.pkl": (72215552, 271636789),
}


class NumpyOnlyUnpickler(pickle.Unpickler):
    """Read the upstream numeric metadata without permitting arbitrary globals."""

    def find_class(self, module, name):
        permitted = {
            ("numpy", "ndarray"): np.ndarray,
            ("numpy", "dtype"): np.dtype,
            ("numpy.core.multiarray", "_reconstruct"): _reconstruct,
            ("numpy._core.multiarray", "_reconstruct"): _reconstruct,
            ("numpy.core.multiarray", "scalar"): scalar,
            ("numpy._core.multiarray", "scalar"): scalar,
        }
        if (module, name) not in permitted:
            raise pickle.UnpicklingError(f"forbidden global: {module}.{name}")
        return permitted[module, name]


def download(name, target):
    """Fetch only one known member's bytes, without extracting tar paths."""
    start, size = RANGES[name]
    if target.exists() and target.stat().st_size == size:
        return
    req = urllib.request.Request(
        URL + f"?sample_offset={start}", headers={"Range": f"bytes={start}-{start + size - 1}"}
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        expected = f"bytes {start}-{start + size - 1}/1805588480"
        if response.status != 206 or response.headers.get("Content-Range") != expected:
            raise RuntimeError("unexpected range response")
        with target.open("wb") as handle:
            remaining = size
            while remaining:
                chunk = response.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("short download")
                handle.write(chunk)
                remaining -= len(chunk)
    if target.stat().st_size != size:
        raise RuntimeError("wrong asset size")


def sha(path):
    """Hash an artifact without loading the entire file into memory."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=pathlib.Path, required=True)
    p.add_argument(
        "--runtime",
        type=pathlib.Path,
        default=pathlib.Path(__file__).with_name("aerial_anwm_runtime.py"),
    )
    p.add_argument("--upstream-root", required=True)
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--diffusion-steps", type=int, default=250)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    for name in RANGES:
        download(name, a.output / name)
    with (a.output / "traj_data.pkl").open("rb") as f:
        meta = NumpyOnlyUnpickler(f).load()
    if not isinstance(meta, dict):
        raise ValueError("expected metadata dictionary")
    for k, v in meta.items():
        if isinstance(v, np.ndarray) and v.dtype.hasobject:
            raise ValueError(f"object array: {k}")
    print(
        json.dumps(
            {
                "metadata": {
                    k: {"shape": list(v.shape), "dtype": str(v.dtype)}
                    for k, v in meta.items()
                    if isinstance(v, np.ndarray)
                }
            }
        ),
        flush=True,
    )
    # Match the released checkpoint's 17 positional slots without repeating or
    # fabricating history: sixteen observed frames, then one forecast target.
    frames = list(range(16))
    goal = 19
    # The published preprocessing code calls CSV row indices "timestamps".
    # They cannot establish measured physical frame timing.
    row_indices = np.asarray(meta["timestamps"][: goal + 1], dtype=np.float64)
    if not np.all(np.diff(row_indices) == 1) or not np.array_equal(
        row_indices, np.round(row_indices)
    ):
        raise ValueError("unexpected source row indices; inspect source timing")
    images = np.stack(
        [np.asarray(Image.open(a.output / f"{i}.jpg").convert("RGB")) for i in frames]
    )
    goal_rgb = np.asarray(Image.open(a.output / f"{goal}.jpg").convert("RGB"))
    poses = np.asarray(meta["pose"][frames], dtype=np.float64)
    depth = np.asarray(meta["depth"][frames])
    K = np.asarray(meta["K"], dtype=np.float64)
    points = np.asarray(meta["point"][frames], dtype=np.float64)
    yaws = np.asarray(meta["yaw"][frames], dtype=np.float64).reshape(-1)
    optical_to_frd = np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]], dtype=float)
    body_rotation = poses[:, :3, :3] @ optical_to_frd.T
    derived_yaw = np.arctan2(body_rotation[:, 1, 0], body_rotation[:, 0, 0])
    yaw_error = np.angle(np.exp(1j * (derived_yaw - yaws)))
    if not np.allclose(poses[:, :3, 3], points, atol=1e-4) or not np.allclose(
        yaw_error, 0, atol=1e-4
    ):
        raise ValueError(
            "metadata pose is not camera optical to world matching point/yaw; inspect before inference"
        )
    np.savez_compressed(
        a.output / "assets.npz",
        context_rgb=images,
        context_depth=depth,
        context_camera_poses=poses,
        camera_intrinsics=K,
        goal_rgb=goal_rgb,
    )
    spec = importlib.util.spec_from_file_location("aerial_anwm_runtime", a.runtime)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    # Fixed candidate plans, chosen without looking at future positions/yaws/depths.
    # Upstream evaluation assumes 4 fps: four indices correspond to one nominal
    # second, not a measured physical duration. Translation is 5 m for both.
    candidates = []
    for cid, delta in [
        ("continue_forward", [5.0, 0.0, 0.0, 0.0]),
        ("replan_left", [0.0, -5.0, 0.0, -math.pi / 12]),
    ]:
        target = runtime.target_pose_from_delta(np, poses[-1], np.asarray(delta))
        candidates.append(
            {
                "candidate_id": cid,
                "delta_local_m_rad": delta,
                "target_camera_pose": target.tolist(),
                "horizon_seconds": 1.0,
            }
        )
    request = {
        "schema_version": "aerial_anwm_request.v1",
        "request_id": "anwm-public-fixed-plan-0001",
        "assets_npz": "assets.npz",
        "upstream_root": a.upstream_root,
        "checkpoint_path": a.checkpoint,
        "delta_frame": "body_frd_at_observation",
        "num_timesteps": 4,
        "frame_interval_seconds": 0.25,
        "frame_interval_source": "ANWM public benchmark 4 fps",
        "physical_frame_timing_verified": False,
        "horizon_seconds_nominal": True,
        "source_timing": {
            "metadata_field": "timestamps",
            "metadata_semantics": "source_csv_row_indices",
            "source_row_indices": row_indices.astype(int).tolist(),
            "nominal_input_fps": 4,
        },
        "diffusion_steps": a.diffusion_steps,
        "seed": 42,
        "public_provenance": {
            "dataset_repository": "EmbodiedCity/ANWM-Dataset",
            "dataset_revision": DATASET_REVISION,
            "archive": ARCHIVE,
            "trajectory": TRAJECTORY,
            "context_frame_indices": frames,
            "goal_frame_index": goal,
        },
        "candidates": candidates,
    }
    (a.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    # Evaluator-only geometry is computed after the independent plans are fixed.
    # It is never placed in the request, inference NPZ, or model input manifest.
    goal_position = np.asarray(meta["point"][goal], dtype=np.float64)
    oracle_candidates = [
        {
            "candidate_id": candidate["candidate_id"],
            "geometric_endpoint_distance_m": float(
                np.linalg.norm(np.asarray(candidate["target_camera_pose"])[:3, 3] - goal_position)
            ),
        }
        for candidate in candidates
    ]
    oracle = {
        "schema_version": "aerial_anwm_geometric_oracle.v1",
        "source_kind": "dataset_goal_pose",
        "goal_frame_index": goal,
        "goal_world_position_m": goal_position.tolist(),
        "candidates": oracle_candidates,
        "best_candidate_id": min(
            oracle_candidates, key=lambda value: value["geometric_endpoint_distance_m"]
        )["candidate_id"],
        "flight_outcome_observed": False,
        "used_to_choose_candidate_plans": False,
        "allowed_as_model_or_jev_input": False,
        "limitation": "endpoint geometry does not establish collision avoidance or execution success",
    }
    (a.output / "oracle.json").write_text(json.dumps(oracle, indent=2) + "\n")
    provenance = {
        "source_url": URL,
        "source_files": {
            name: {"bytes": size, "tar_offset": offset, "sha256": sha(a.output / name)}
            for name, (offset, size) in RANGES.items()
        },
        "context_frames": frames,
        "goal_frame": goal,
        "candidate_source": "fixed plans defined before reading future metadata",
        "inference_npz_keys": [
            "context_rgb",
            "context_depth",
            "context_camera_poses",
            "camera_intrinsics",
            "goal_rgb",
        ],
        "pose_yaw_max_error_rad": float(abs(yaw_error).max()),
        "source_timing": request["source_timing"],
        "physical_frame_timing_verified": False,
        "horizon_seconds_nominal": True,
        "limits": [
            "public trajectory may be in released training data; not held-out performance evidence",
            "goal image is a declared task input",
            "no measured future observation corresponds to either fixed alternative plan",
            "one second is nominal under upstream 4 fps evaluation; physical frame timing is unavailable",
        ],
    }
    (a.output / "source-manifest.json").write_text(json.dumps(provenance, indent=2) + "\n")
    runtime.validate_request(request, a.output)
    print(
        json.dumps(
            {
                "prepared": True,
                "request": "request.json",
                "downloaded_bytes": sum(v[1] for v in RANGES.values()),
                "asset_npz_sha256": sha(a.output / "assets.npz"),
            }
        )
    )


if __name__ == "__main__":
    main()
