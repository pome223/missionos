#!/usr/bin/env python3
"""Run an explicitly installed ANWM on public, image-conditioned candidates.

This optional research CLI imports the external upstream implementation. It
neither downloads models nor controls an aircraft. Goal MSE is an uncalibrated
image comparison, not collision probability or authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any


UPSTREAM_REVISION = "657a80268505fa9149c4df502e35aa0f5bce11e5"
MODEL_REVISION = "dfe59001de57a96d6620313897f09436c6940983"
MODEL_SHA256 = "bdd149cac6ec002ba7dc4ad99ec6f9eb02cd6d4f05320195cf174737b13b0bc2"
VAE_REVISION = "f04b2c4b98319346dad8c65879f680b1997b204a"
# The released EMA checkpoint has 17 positional slots: 16 history + 1 target.
# Its public YAML says four; retain the actual strict checkpoint contract.
CONTEXT_SIZE = 16


def digest_file(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _array(np: Any, value: Any, shape: tuple[int, ...], name: str) -> Any:
    array = np.asarray(value)
    if array.shape != shape or array.dtype.kind not in "uif" or not np.isfinite(array).all():
        raise ValueError(f"{name} must have shape {shape} and contain finite values")
    return array


def _pose(np: Any, value: Any, name: str) -> Any:
    pose = _array(np, value, (4, 4), name).astype(np.float64)
    rotation = pose[:3, :3]
    if (
        not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-5)
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5)
        or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5)
    ):
        raise ValueError(f"{name} must be a rigid camera-to-world transform")
    return pose


def target_pose_from_delta(np: Any, reference: Any, delta: Any) -> Any:
    """Yaw-only FRD displacement, preserving the reference roll and pitch."""
    optical_to_frd = np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]], dtype=float)
    body_rotation = reference[:3, :3] @ optical_to_frd.T
    yaw = math.atan2(body_rotation[1, 0], body_rotation[0, 0])

    def yaw_rotation(angle: float) -> Any:
        c, s = math.cos(angle), math.sin(angle)
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)

    target = reference.copy()
    target[:3, 3] += yaw_rotation(yaw) @ delta[:3]
    target[:3, :3] = yaw_rotation(float(delta[3])) @ reference[:3, :3]
    return target


def validate_request(request: dict[str, Any], base: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    if request.get("schema_version") != "aerial_anwm_request.v1":
        raise ValueError("unsupported request schema")
    if request.get("source_kind", "public_dataset_replay") != "public_dataset_replay":
        raise ValueError("this runner only accepts public_dataset_replay inputs")
    if not isinstance(request.get("request_id"), str) or not request["request_id"]:
        raise ValueError("request_id is required")
    if request.get("delta_frame") != "body_frd_at_observation":
        raise ValueError("delta_frame must be body_frd_at_observation")
    num_timesteps = request.get("num_timesteps")
    frame_interval = request.get("frame_interval_seconds")
    if type(num_timesteps) is not int or not 1 <= num_timesteps <= 128:
        raise ValueError("num_timesteps must be an integer from 1 through 128")
    if type(frame_interval) not in (int, float) or not 0 < frame_interval <= 10:
        raise ValueError("frame_interval_seconds must describe the source data")
    if request.get("frame_interval_source") != "ANWM public benchmark 4 fps":
        raise ValueError("this bounded runner requires the documented public 4 fps benchmark")
    if frame_interval != 0.25:
        raise ValueError("public benchmark frame interval must be 0.25 seconds")
    if (
        request.get("physical_frame_timing_verified") is not False
        or request.get("horizon_seconds_nominal") is not True
    ):
        raise ValueError("public replay timing must be explicitly marked nominal")
    source_timing = request.get("source_timing", {})
    if (
        not isinstance(source_timing, dict)
        or source_timing.get("metadata_field") != "timestamps"
        or source_timing.get("metadata_semantics") != "source_csv_row_indices"
        or source_timing.get("nominal_input_fps") != 4
        or type(source_timing.get("nominal_input_fps")) is bool
    ):
        raise ValueError("source timing must distinguish row indices from nominal FPS")
    raw_indices = np.asarray(source_timing.get("source_row_indices", []))
    if (
        raw_indices.shape != (CONTEXT_SIZE + num_timesteps,)
        or raw_indices.dtype.kind not in "uif"
        or not np.isfinite(raw_indices).all()
        or not np.array_equal(raw_indices, np.round(raw_indices))
        or not np.all(np.diff(raw_indices) == 1)
    ):
        raise ValueError("source timing must bind contiguous public row indices")
    clean_source_timing = {
        "metadata_field": "timestamps",
        "metadata_semantics": "source_csv_row_indices",
        "source_row_indices": raw_indices.astype(int).tolist(),
        "nominal_input_fps": 4,
        "nominal_fps_source": "upstream infer.py --input_fps default",
        "model_time_basis": "frame_index_offset_divided_by_128",
    }
    steps = request.get("diffusion_steps", 250)
    if type(steps) is not int or steps not in (10, 25, 50, 100, 250):
        raise ValueError("unsupported diffusion step count")
    seed = request.get("seed", 0)
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be an unsigned 32-bit integer")
    path = (base / request["assets_npz"]).resolve()
    with np.load(path, allow_pickle=False) as archive:
        expected_keys = {
            "context_rgb",
            "context_depth",
            "context_camera_poses",
            "camera_intrinsics",
            "goal_rgb",
        }
        if set(archive.files) != expected_keys:
            raise ValueError(
                "asset archive must contain only historical observations and the goal image"
            )
        arrays = {key: archive[key] for key in expected_keys}
    rgb = arrays["context_rgb"]
    if rgb.dtype != np.uint8 or rgb.ndim != 4 or rgb.shape[0] != CONTEXT_SIZE or rgb.shape[-1] != 3:
        raise ValueError("context_rgb must contain sixteen real historical uint8 RGB images")
    _, height, width, _ = rgb.shape
    if not 16 <= height <= 2048 or not 16 <= width <= 2048:
        raise ValueError("unsupported source image dimensions")
    depth = _array(np, arrays["context_depth"], (CONTEXT_SIZE, height, width), "context_depth")
    if (depth < 0).any() or not (depth > 0).any():
        raise ValueError("context_depth must contain nonnegative metric depths")
    poses = _array(np, arrays["context_camera_poses"], (CONTEXT_SIZE, 4, 4), "context_camera_poses")
    for index, pose in enumerate(poses):
        _pose(np, pose, f"context pose {index}")
    intrinsics = _array(np, arrays["camera_intrinsics"], (3, 3), "camera_intrinsics")
    if intrinsics[0, 0] <= 0 or intrinsics[1, 1] <= 0 or not np.allclose(intrinsics[2], [0, 0, 1]):
        raise ValueError("invalid camera intrinsics")
    goal = arrays["goal_rgb"]
    if goal.dtype != np.uint8 or goal.shape != (height, width, 3):
        raise ValueError("goal_rgb must use the same uint8 RGB geometry as context")
    candidates = request.get("candidates")
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 8:
        raise ValueError("two through eight explicit candidates are required")
    clean_candidates = []
    ids: set[str] = set()
    delta_digests: set[str] = set()
    for candidate in candidates:
        identifier = candidate["candidate_id"]
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identifier)
            or identifier in ids
        ):
            raise ValueError("candidate IDs must be unique safe identifiers")
        ids.add(identifier)
        delta = _array(np, candidate["delta_local_m_rad"], (4,), "candidate delta")
        if abs(delta[3]) > math.pi or np.linalg.norm(delta[:3]) > 100:
            raise ValueError("candidate exceeds the bounded displacement contract")
        target = _pose(np, candidate["target_camera_pose"], "target_camera_pose")
        if not np.allclose(target, target_pose_from_delta(np, poses[-1], delta), atol=1e-4):
            raise ValueError("candidate target pose does not match its declared action")
        horizon = candidate.get("horizon_seconds")
        if type(horizon) not in (int, float) or not math.isclose(
            horizon, num_timesteps * frame_interval
        ):
            raise ValueError("candidate horizon must match source frame timing")
        cleaned = {
            "candidate_id": identifier,
            "delta_local_m_rad": delta.astype(float).tolist(),
            "target_camera_pose": target.tolist(),
            "horizon_seconds": float(horizon),
        }
        delta_digests.add(digest_json(cleaned["delta_local_m_rad"]))
        cleaned["candidate_sha256"] = digest_json(cleaned)
        clean_candidates.append(cleaned)
    if len(delta_digests) < 2:
        raise ValueError("candidates must include at least two distinct actions")
    provenance = request.get("public_provenance", {})
    if (
        not isinstance(provenance, dict)
        or provenance.get("dataset_repository") != "EmbodiedCity/ANWM-Dataset"
    ):
        raise ValueError("public dataset provenance is required")
    # Whitelist public identifiers; never propagate workstation or checkpoint paths.
    public_provenance = {
        key: provenance[key]
        for key in (
            "dataset_repository",
            "dataset_revision",
            "archive",
            "trajectory",
            "context_frame_indices",
            "goal_frame_index",
        )
        if key in provenance
    }
    manifest = {
        "schema_version": "missionos_aerial_anwm_input.v1",
        "request_id": request["request_id"],
        "source_kind": "public_dataset_replay",
        "source_dataset": public_provenance["dataset_repository"],
        "source_revision": public_provenance.get("dataset_revision"),
        "source_trajectory": public_provenance.get("trajectory"),
        "public_provenance": public_provenance,
        "asset_npz_sha256": digest_file(path),
        "delta_frame": request["delta_frame"],
        "num_timesteps": num_timesteps,
        "context_size": CONTEXT_SIZE,
        "frame_interval_seconds": float(frame_interval),
        "frame_interval_source": request["frame_interval_source"],
        "physical_frame_timing_verified": False,
        "horizon_seconds_nominal": True,
        "source_timing": clean_source_timing,
        "candidates": clean_candidates,
        "seed": seed,
        "diffusion_steps": steps,
        "goal_image_used_for_scoring_only": True,
        "future_ground_truth_used_for_forecast": False,
    }
    return arrays, manifest


def run(
    request: dict[str, Any], base: Path, output: Path, validate_only: bool = False
) -> dict[str, Any]:
    arrays, manifest = validate_request(request, base)
    if validate_only:
        return {
            "validated": True,
            "input_manifest": manifest,
            "input_manifest_sha256": digest_json(manifest),
        }

    import numpy as np
    import torch
    from PIL import Image

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError(
            "the upstream ANWM inference path requires a CUDA device with bfloat16 support"
        )
    upstream = (base / request["upstream_root"]).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError("upstream revision differs from the reviewed implementation")
    subprocess.run(["git", "-C", str(upstream), "diff", "--quiet", "HEAD", "--"], check=True)
    checkpoint = (base / request["checkpoint_path"]).resolve()
    checkpoint_hash = digest_file(checkpoint)
    if checkpoint_hash != MODEL_SHA256:
        raise ValueError("checkpoint SHA256 differs from the official released model")
    sys.path.insert(0, str(upstream))
    from anwm.diffusion import create_diffusion
    from anwm.model import CDiT_models
    from anwm.projection import project_to_2d_image_seq2seq, reproject_depth_to_other_pose_seq2seq
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import normalize_data, transform
    from diffusers import AutoencoderKL

    device = torch.device("cuda:0")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    started = time.perf_counter()
    # The pinned upstream training checkpoint stores its CLI configuration as
    # argparse.Namespace. Permit only that inert container in this load scope.
    with torch.serialization.safe_globals([argparse.Namespace]):
        state = torch.load(checkpoint, map_location="cpu", mmap=True, weights_only=True)
    if tuple(state["ema"]["pos_embed"].shape) != (CONTEXT_SIZE + 1, 196, 1152):
        raise ValueError("checkpoint positional slots differ from the reviewed 16-frame contract")
    model = CDiT_models["CDiT-XL/2"](context_size=CONTEXT_SIZE, input_size=28, in_channels=4)
    model.load_state_dict(state["ema"], strict=True)
    del state
    model = model.eval().to(device)
    vae = (
        AutoencoderKL.from_pretrained(
            "stabilityai/sd-vae-ft-ema",
            revision=VAE_REVISION,
            use_safetensors=True,
        )
        .eval()
        .to(device)
    )
    diffusion = create_diffusion(str(manifest["diffusion_steps"]))
    torch.cuda.synchronize()
    load_seconds = time.perf_counter() - started
    context = torch.stack([transform(Image.fromarray(image)) for image in arrays["context_rgb"]])[
        None
    ].to(device)
    goal = (transform(Image.fromarray(arrays["goal_rgb"])).float() + 1) / 2
    current = (context[0, -1].detach().cpu().float() + 1) / 2
    baseline_mse = float(torch.mean((current - goal) ** 2))
    output.mkdir(parents=True, exist_ok=True)
    forecasts = []
    torch.cuda.reset_peak_memory_stats()
    for candidate in manifest["candidates"]:
        torch.manual_seed(manifest["seed"])
        torch.cuda.manual_seed_all(manifest["seed"])
        np.random.seed(manifest["seed"])
        candidate_started = time.perf_counter()
        points, colors = reproject_depth_to_other_pose_seq2seq(
            arrays["camera_intrinsics"],
            arrays["context_depth"],
            arrays["context_rgb"],
            arrays["context_camera_poses"],
            np.asarray(candidate["target_camera_pose"])[None],
        )
        projection_rgb = project_to_2d_image_seq2seq(
            arrays["camera_intrinsics"],
            points,
            colors,
            arrays["context_depth"].shape[-2:],
        )[0]
        projection = transform(Image.fromarray(projection_rgb))[None, None].to(device)
        action = np.asarray(candidate["delta_local_m_rad"], dtype=np.float32).copy()
        action[:3] = normalize_data(
            action[:3] / 3.30,
            {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])},
        )
        with torch.no_grad():
            prediction = model_forward_wrapper(
                (model, diffusion, vae),
                context,
                torch.as_tensor(action)[None, None].to(device),
                manifest["num_timesteps"],
                28,
                device=device,
                num_cond=CONTEXT_SIZE,
                num_goals=1,
                x_supervised=projection,
            )[0]
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - candidate_started
        predicted_rgb = (prediction.detach().cpu().float() + 1) / 2
        projection_float = (projection[0, 0].detach().cpu().float() + 1) / 2
        filename = f"{candidate['candidate_id']}.png"
        Image.fromarray(
            (predicted_rgb.permute(1, 2, 0).numpy() * 255).round().clip(0, 255).astype(np.uint8)
        ).save(output / filename)
        projection_filename = f"{candidate['candidate_id']}-projection.png"
        Image.fromarray(
            (projection_float.permute(1, 2, 0).numpy() * 255).round().clip(0, 255).astype(np.uint8)
        ).save(output / projection_filename)
        forecasts.append(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_sha256": candidate["candidate_sha256"],
                "goal_mse": float(torch.mean((predicted_rgb - goal) ** 2)),
                "projection_goal_mse": float(torch.mean((projection_float - goal) ** 2)),
                "predicted_image": filename,
                "predicted_image_sha256": digest_file(output / filename),
                "projection_image": projection_filename,
                "projection_image_sha256": digest_file(output / projection_filename),
                "elapsed_seconds": elapsed,
            }
        )
    return {
        "schema_version": "aerial_anwm_result.v1",
        "input_manifest": manifest,
        "input_manifest_sha256": digest_json(manifest),
        "model": {
            "model_id": "EmbodiedCity/ANWM",
            "checkpoint_sha256": checkpoint_hash,
            "model_revision": MODEL_REVISION,
            "upstream_revision": revision,
            "vae_repository": "stabilityai/sd-vae-ft-ema",
            "vae_revision": VAE_REVISION,
            "context_size": CONTEXT_SIZE,
        },
        "candidates": forecasts,
        "current_frame_goal_mse": baseline_mse,
        "score_is_calibrated_risk": False,
        "runtime_invocation_evidence": {
            "schema_version": "runtime_invocation_evidence.v1",
            "model_execution_verified": True,
            "fixture_invocation": False,
            "input_manifest_sha256": digest_json(manifest),
            "model_sha256": checkpoint_hash,
            "forecasts_sha256": digest_json(forecasts),
            "actual_model_calls": len(forecasts),
            "seed": manifest["seed"],
            "common_random_seed_per_candidate": True,
            "sampling_steps": manifest["diffusion_steps"],
            "context_size": CONTEXT_SIZE,
            "physical_frame_timing_verified": False,
            "horizon_seconds_nominal": True,
            "source_timing": manifest["source_timing"],
            "published_sampling_steps": 250,
            "device": torch.cuda.get_device_name(0),
            "device_total_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
            "peak_allocated_gpu_bytes": torch.cuda.max_memory_allocated(),
            "model_load_seconds": load_seconds,
            "forecast_seconds": sum(item["elapsed_seconds"] for item in forecasts),
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("torch", "torchvision", "diffusers", "timm", "numpy")
            },
            "execution_scope": "public_dataset_offline_candidate_forecast",
            "px4_runtime_invoked": False,
            "physical_execution_observed": False,
            "torch_compile_enabled": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    result = run(request, args.request.resolve().parent, args.output_dir, args.validate_only)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "result.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                "result": "validated" if args.validate_only else "forecast_complete",
                "candidates": len(result["input_manifest"]["candidates"]),
            }
        )
    )


if __name__ == "__main__":
    main()
