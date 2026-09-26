#!/usr/bin/env python3
"""Prepare or run a bounded ANWM shadow audit. Never uploads flight commands.

Preparation splits historical observations from held-out future outcomes.
Only the input directory is sent to the GPU. Outcomes stay on the local host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from PIL import Image

UPSTREAM_REVISION = "657a80268505fa9149c4df502e35aa0f5bce11e5"
MODEL_REVISION = "dfe59001de57a96d6620313897f09436c6940983"
MODEL_SHA256 = "bdd149cac6ec002ba7dc4ad99ec6f9eb02cd6d4f05320195cf174737b13b0bc2"
VAE_REVISION = "f04b2c4b98319346dad8c65879f680b1997b204a"
NED_FROM_ENU = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]], dtype=float)
FLU_FROM_OPTICAL = np.array([[0, 0, 1], [-1, 0, 0], [0, -1, 0]], dtype=float)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def rotation(q):
    if len(q) != 4 or not np.isfinite(q).all() or abs(np.dot(q, q) - 1) > 1e-5:
        raise ValueError("Invalid source quaternion")
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def camera_pose(frame):
    r = rotation(frame["vehicle_quaternion_wxyz"])
    pose = np.eye(4)
    pose[:3, :3] = NED_FROM_ENU @ r @ FLU_FROM_OPTICAL
    pose[:3, 3] = NED_FROM_ENU @ (np.asarray(frame["vehicle_position_enu_m"]) + r @ [0.25, 0, 0.1])
    return pose


def lateral_pose(reference, metres):
    target = reference.copy()
    forward = reference[:2, 2]
    right = np.array([-forward[1], forward[0], 0.0])
    right /= np.linalg.norm(right)
    target[:3, 3] += right * metres
    return target


def asset(root, entry):
    name = entry["file"]
    if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
        raise ValueError("Invalid captured asset path")
    path = root / name
    if path.is_symlink() or digest(path) != entry["sha256"]:
        raise ValueError("Captured asset hash mismatch")
    return path.read_bytes()


def prepare(capture, output):
    source = json.loads(capture.read_text())
    frames = source["frames"]
    if (
        source.get("schema_version") != "ship_anwm_capture.v1"
        or len(frames) != 20
        or source.get("history_indices") != list(range(16))
        or source.get("outcome_indices") != list(range(16, 20))
        or source.get("single_rgbd_sensor") is not True
        or source.get("camera_pose_flu") != [0.25, 0, 0.1, 0, 0, 0]
        or source.get("control_hold") != "px4_auto_loiter"
    ):
        raise ValueError("Unsupported native capture contract")
    stamps = np.asarray([f["simulation_time_ns"] for f in frames], dtype=np.int64)
    cadence = np.diff(stamps) / 1e9
    if np.max(np.abs(cadence - 0.25)) > 0.004000001:
        raise ValueError("Native history requires distinct 4 Hz simulation frames within 4 ms")
    rgbs, depths, poses = [], [], []
    for frame in frames:
        # Reopen all source assets, including raw protocol messages.
        data = {k: asset(capture.parent, v) for k, v in frame["assets"].items()}
        rgbs.append(np.frombuffer(data["rgb"], np.uint8).reshape(360, 640, 3))
        d = np.frombuffer(data["depth"], "<f4").reshape(360, 640)
        depths.append(np.where(np.isfinite(d) & (d > 0) & (d <= 500), d, 0))
        poses.append(camera_pose(frame))
    intrinsics = np.asarray(frames[0]["intrinsics"]).reshape(3, 3)
    expected = 640 / (2 * math.tan(math.pi / 6))
    if not all(f["intrinsics"] == frames[0]["intrinsics"] for f in frames) or not np.allclose(
        intrinsics, [[expected, 0, 320], [0, expected, 180], [0, 0, 1]], atol=1e-5
    ):
        raise ValueError("RGBD calibration differs from the bound sensor")
    output.mkdir(parents=True, exist_ok=False)
    inputs, outcomes = output / "input", output / "outcomes"
    inputs.mkdir()
    outcomes.mkdir()
    np.savez_compressed(
        inputs / "history.npz",
        rgb=np.asarray(rgbs[:16]),
        depth=np.asarray(depths[:16]),
        poses=np.asarray(poses[:16]),
        intrinsics=intrinsics,
        stamps_ns=stamps[:16],
    )
    # No goal or future image exists in the predictor archive.
    for index in (15, 19):
        Image.fromarray(rgbs[index]).save(outcomes / f"frame-{index:02}.png")
    displacement = float(np.linalg.norm(poses[19][:3, 3] - poses[15][:3, 3]))
    angle = float(
        np.arccos(np.clip((np.trace(poses[15][:3, :3].T @ poses[19][:3, :3]) - 1) / 2, -1, 1))
    )
    write_json(
        outcomes / "receipt.json",
        {
            "source_capture_sha256": digest(capture),
            "run_id": source["run_id"],
            "target_frame_index": 19,
            "target_rgb_sha256": digest(outcomes / "frame-19.png"),
            "last_history_rgb_sha256": digest(outcomes / "frame-15.png"),
            "measured_horizon_simulation_s": float((stamps[19] - stamps[15]) / 1e9),
            "ego_displacement_m": displacement,
            "ego_rotation_rad": angle,
            "hold_comparison_admitted": displacement <= 0.35 and angle <= 0.03,
            "future_used_for_prediction": False,
        },
    )
    request = {
        "schema_version": "ship_anwm_request.v1",
        "run_id": source["run_id"],
        "history_sha256": digest(inputs / "history.npz"),
        "source_history_sha256": hashlib.sha256(
            json.dumps(frames[:16], sort_keys=True).encode()
        ).hexdigest(),
        "world_sha256": source["world_sha256"],
        "plan_sha256": source["plan_sha256"],
        "source_kind": "px4_ship_rgbd_hold",
        "ego_source": source["ego_source"],
        "depth_policy": "unknown_zero_only_for_upstream_positive_z_projection",
        "source_message_decode_independently_verified": False,
        "delta_frame": "body_frd_at_observation",
        "num_timesteps": 4,
        "nominal_horizon_s": 1.0,
        "model_time_alignment_verified": False,
        "seed": 42,
        "diffusion_steps": 250,
        "candidates": [
            {"id": "hold", "delta": [0, 0, 0, 0]},
            {"id": "right_5m", "delta": [0, 5, 0, 0]},
        ],
        "future_ground_truth_used_for_forecast": False,
        "dispatch_allowed": False,
    }
    write_json(inputs / "request.json", request)
    validate(inputs / "request.json")
    return request


def validate(request_path):
    request = json.loads(request_path.read_text())
    if (
        request.get("schema_version") != "ship_anwm_request.v1"
        or request.get("source_kind") != "px4_ship_rgbd_hold"
        or request.get("ego_source") != "Gazebo_model_pose_simulator_ground_truth"
        or request.get("delta_frame") != "body_frd_at_observation"
        or request.get("num_timesteps") != 4
        or request.get("nominal_horizon_s") != 1
        or request.get("model_time_alignment_verified") is not False
        or request.get("future_ground_truth_used_for_forecast") is not False
        or request.get("dispatch_allowed") is not False
        or request.get("candidates")
        != [{"id": "hold", "delta": [0, 0, 0, 0]}, {"id": "right_5m", "delta": [0, 5, 0, 0]}]
        or request.get("seed") != 42
        or request.get("diffusion_steps") != 250
    ):
        raise ValueError("Native shadow request differs from the frozen contract")
    path = request_path.parent / "history.npz"
    if digest(path) != request["history_sha256"]:
        raise ValueError("Native input hash mismatch")
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"rgb", "depth", "poses", "intrinsics", "stamps_ns"}:
            raise ValueError("Native input must contain history only")
        a = {k: archive[k] for k in archive.files}
    if a["rgb"].shape != (16, 360, 640, 3) or a["rgb"].dtype != np.uint8:
        raise ValueError("Sixteen native RGB observations required")
    if (
        a["depth"].shape != (16, 360, 640)
        or not np.isfinite(a["depth"]).all()
        or (a["depth"] < 0).any()
        or (a["depth"] > 500).any()
        or not (a["depth"] > 0).any()
    ):
        raise ValueError("Metric depth with zero unknowns required")
    if (
        a["stamps_ns"].shape != (16,)
        or a["stamps_ns"].dtype != np.int64
        or np.max(np.abs(np.diff(a["stamps_ns"]) / 1e9 - 0.25)) > 0.004000001
    ):
        raise ValueError("Invalid observed cadence")
    if a["poses"].shape != (16, 4, 4) or not np.isfinite(a["poses"]).all():
        raise ValueError("Invalid camera poses")
    for p in a["poses"]:
        if (
            not np.allclose(p[3], [0, 0, 0, 1])
            or not np.allclose(p[:3, :3].T @ p[:3, :3], np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(p[:3, :3]), 1)
        ):
            raise ValueError("Camera pose must be rigid")
    fx = 640 / (2 * math.tan(math.pi / 6))
    if not np.allclose(a["intrinsics"], [[fx, 0, 320], [0, fx, 180], [0, 0, 1]], atol=1e-5):
        raise ValueError("Unexpected camera intrinsics")
    return request, a


def run(request_path, output, upstream, checkpoint):
    request, a = validate(request_path)
    revision = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != UPSTREAM_REVISION or digest(checkpoint) != MODEL_SHA256:
        raise ValueError("Unreviewed model or upstream revision")
    subprocess.run(["git", "-C", str(upstream), "diff", "--quiet", "HEAD", "--"], check=True)
    import torch

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("CUDA bfloat16 GPU required")
    sys.path.insert(0, str(upstream))
    from anwm.diffusion import create_diffusion
    from anwm.model import CDiT_models
    from anwm.projection import project_to_2d_image_seq2seq, reproject_depth_to_other_pose_seq2seq
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import normalize_data, transform
    from diffusers import AutoencoderKL

    output.mkdir(parents=True, exist_ok=False)
    started = time.time()
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    with torch.serialization.safe_globals([argparse.Namespace]):
        state = torch.load(checkpoint, map_location="cpu", mmap=True, weights_only=True)
    if tuple(state["ema"]["pos_embed"].shape) != (17, 196, 1152):
        raise ValueError("Released context slots changed")
    model = CDiT_models["CDiT-XL/2"](context_size=16, input_size=28, in_channels=4)
    model.load_state_dict(state["ema"], strict=True)
    del state
    model = model.eval().to("cuda")
    vae = (
        AutoencoderKL.from_pretrained(
            "stabilityai/sd-vae-ft-ema", revision=VAE_REVISION, use_safetensors=True
        )
        .eval()
        .to("cuda")
    )
    diffusion = create_diffusion("250")
    context = torch.stack([transform(Image.fromarray(im)) for im in a["rgb"]])[None].to("cuda")
    torch.cuda.synchronize()
    load_s = time.time() - started
    forecasts = []
    for candidate in request["candidates"]:
        torch.manual_seed(42)
        torch.cuda.manual_seed_all(42)
        np.random.seed(42)
        begin = time.time()
        delta = np.asarray(candidate["delta"], dtype=np.float32)
        # Yaw-only body-FRD translation; preserve the observed roll/pitch.
        target = lateral_pose(a["poses"][-1], delta[1])
        points, colors = reproject_depth_to_other_pose_seq2seq(
            a["intrinsics"], a["depth"], a["rgb"], a["poses"], target[None]
        )
        projected = project_to_2d_image_seq2seq(a["intrinsics"], points, colors, (360, 640))[0]
        projection = transform(Image.fromarray(projected))[None, None].to("cuda")
        delta[:3] = normalize_data(
            delta[:3] / 3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])}
        )
        with torch.no_grad():
            prediction = model_forward_wrapper(
                (model, diffusion, vae),
                context,
                torch.as_tensor(delta)[None, None].to("cuda"),
                4,
                28,
                device="cuda",
                num_cond=16,
                num_goals=1,
                x_supervised=projection,
            )[0]
        torch.cuda.synchronize()
        elapsed = time.time() - begin
        files = {}
        for kind, tensor in (("prediction", prediction), ("projection", projection[0, 0])):
            rgb = (
                ((tensor.detach().cpu().float().permute(1, 2, 0).numpy() + 1) * 127.5)
                .round()
                .clip(0, 255)
                .astype(np.uint8)
            )
            name = f"{candidate['id']}-{kind}.png"
            Image.fromarray(rgb).save(output / name)
            files[kind] = {"file": name, "sha256": digest(output / name)}
        forecasts.append({"candidate": candidate, "elapsed_s": elapsed, "files": files})
    result = {
        "schema_version": "ship_anwm_invocation.v1",
        "request_sha256": digest(request_path),
        "history_sha256": request["history_sha256"],
        "model_id": "EmbodiedCity/ANWM",
        "model_revision": MODEL_REVISION,
        "checkpoint_sha256": MODEL_SHA256,
        "upstream_revision": UPSTREAM_REVISION,
        "vae_revision": VAE_REVISION,
        "runtime_sha256": digest(Path(__file__)),
        "load_seconds": load_s,
        "started_at_unix_s": started,
        "completed_at_unix_s": time.time(),
        "gpu": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(),
        "forecasts": forecasts,
        "wam_inference_invoked": True,
        "vla_invoked": False,
        "dispatch_invoked": False,
        "physical_execution_invoked": False,
        "model_time_alignment_verified": False,
    }
    write_json(output / "result.json", result)
    return result


def crop_rgb(rgb):
    """Match upstream CenterCropAR then PIL bilinear Resize for 640x360 input."""
    return np.asarray(
        Image.fromarray(rgb).crop((80, 0, 560, 360)).resize((224, 224), Image.Resampling.BILINEAR)
    )


def red_mask(rgb):
    p = rgb.astype(float)
    return (p[:, :, 0] > 80) & (p[:, :, 0] > 1.6 * p[:, :, 1]) & (p[:, :, 0] > 1.6 * p[:, :, 2])


def red_center(rgb):
    rows, columns = np.where(red_mask(rgb)[110:115])
    if len(rows) < 20:
        return None
    return float((columns.min() + columns.max()) / 2)


def center_baselines(centers, times, horizon, target_center):
    """Use only the observations actually needed by each frozen comparator."""
    estimates = {}
    if target_center is None:
        return estimates
    if all(v is not None for v in centers[-6:]):
        linear = np.polyfit(times[-6:], centers[-6:], 1)
        estimates["velocity_fit"] = float(np.polyval(linear, horizon))
    if all(v is not None for v in centers[-8:]):
        quad = np.polyfit(times[-8:], centers[-8:], 2)
        v, acceleration = quad[1], 2 * quad[0]
        t = min(horizon, max(0, -v / acceleration)) if acceleration * v < 0 else horizon
        estimates["stopping_fit"] = float(quad[2] + v * t + 0.5 * acceleration * t * t)
    return {
        k: {"predicted_red_center_px": v, "error_px": abs(v - target_center)}
        for k, v in estimates.items()
    }


def evaluate(prepared, model_output, output, runtime_source=None):
    """Join predictions with outcomes only after inference, without flight authority."""
    request_path = prepared / "input/request.json"
    request, a = validate(request_path)
    invocation = json.loads((model_output / "result.json").read_text())
    receipt = json.loads((prepared / "outcomes/receipt.json").read_text())
    if (
        invocation["request_sha256"] != digest(request_path)
        or invocation["history_sha256"] != request["history_sha256"]
        or invocation["checkpoint_sha256"] != MODEL_SHA256
        or invocation["upstream_revision"] != UPSTREAM_REVISION
        or invocation["model_revision"] != MODEL_REVISION
        or invocation["vae_revision"] != VAE_REVISION
        or invocation["runtime_sha256"] != digest(runtime_source or Path(__file__))
        or invocation.get("dispatch_invoked") is not False
        or invocation.get("wam_inference_invoked") is not True
        or receipt["run_id"] != request["run_id"]
    ):
        raise ValueError("Invocation and observed-outcome bindings differ")
    target_path = prepared / "outcomes/frame-19.png"
    if digest(target_path) != receipt["target_rgb_sha256"]:
        raise ValueError("Outcome image hash mismatch")
    future = crop_rgb(np.asarray(Image.open(target_path).convert("RGB")))
    history = [crop_rgb(rgb) for rgb in a["rgb"]]
    target_mask = red_mask(future)
    target_center = red_center(future)
    output.mkdir(parents=True, exist_ok=False)
    Image.fromarray(future).save(output / "observed-future.png")
    Image.fromarray(history[-1]).save(output / "last-observation.png")
    metrics = {}

    def score(name, image):
        mask = red_mask(image)
        center = red_center(image)
        union = int(np.logical_or(mask, target_mask).sum())
        metrics[name] = {
            "rgb_mse_0_1": float(np.mean(((image.astype(float) - future) / 255) ** 2)),
            "red_mask_iou": float(np.logical_and(mask, target_mask).sum() / union)
            if union
            else None,
            "red_center_px": center,
            "red_center_error_px": abs(center - target_center)
            if center is not None and target_center is not None
            else None,
        }

    score("repeat_last_image", history[-1])
    forecasts = invocation["forecasts"]
    if [f["candidate"] for f in forecasts] != request["candidates"]:
        raise ValueError("Candidate results differ from the frozen input")
    for forecast in forecasts:
        for kind, entry in forecast["files"].items():
            data = asset(model_output, entry)
            path = output / entry["file"]
            path.write_bytes(data)
            image = np.asarray(Image.open(path).convert("RGB"))
            if image.shape != (224, 224, 3):
                raise ValueError("Unexpected forecast shape")
            if forecast["candidate"]["id"] == "hold":
                score("anwm" if kind == "prediction" else "depth_projection", image)
    centers = [red_center(im) for im in history]
    times = (a["stamps_ns"] - a["stamps_ns"][-1]) / 1e9
    horizon = receipt["measured_horizon_simulation_s"]
    # No actor speed, case label, true object location, or future frame enters these fits.
    scalar_baselines = center_baselines(centers, times, horizon, target_center)
    report = {
        "schema_version": "ship_anwm_evaluation.v1",
        "run_id": request["run_id"],
        "model_result_sha256": digest(model_output / "result.json"),
        "model_runtime_sha256": invocation["runtime_sha256"],
        "evaluation_runtime_sha256": digest(Path(__file__)),
        "outcome_receipt_sha256": digest(prepared / "outcomes/receipt.json"),
        "evaluation_scope": "offline_hold_future_image_fidelity",
        "hold_comparison_admitted": receipt["hold_comparison_admitted"],
        "measured_horizon_simulation_s": horizon,
        "ego_displacement_m": receipt["ego_displacement_m"],
        "ego_rotation_rad": receipt["ego_rotation_rad"],
        "image_metrics": metrics,
        "image_center_baselines": scalar_baselines,
        "target_red_center_px": target_center,
        "model_forecast_seconds": sum(f["elapsed_s"] for f in forecasts),
        "load_seconds": invocation["load_seconds"],
        "right_5m_outcome_observed": False,
        "mission_value_established": False,
        "dispatch_allowed": False,
        "vla_invoked": False,
        "limitations": [
            "One hold outcome; lateral candidate was not flown.",
            "Ego poses are simulator truth, not a deployable onboard estimator.",
            "Nominal training-time horizon is not calibrated for this scene.",
            "Colour masks and pixel metrics are not collision probabilities.",
            "This offline invocation cannot improve an already completed flight.",
        ],
    }
    write_json(output / "evaluation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--capture", required=True, type=Path)
    prepare_parser.add_argument("--output", required=True, type=Path)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--request", required=True, type=Path)
    runner = sub.add_parser("run")
    runner.add_argument("--request", required=True, type=Path)
    runner.add_argument("--output", required=True, type=Path)
    runner.add_argument("--upstream", required=True, type=Path)
    runner.add_argument("--checkpoint", required=True, type=Path)
    evaluator = sub.add_parser("evaluate")
    evaluator.add_argument("--prepared", required=True, type=Path)
    evaluator.add_argument("--model-output", required=True, type=Path)
    evaluator.add_argument("--output", required=True, type=Path)
    evaluator.add_argument(
        "--runtime-source",
        type=Path,
        help="Archived, reviewed inference script whose hash must match the model receipt",
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.capture, args.output)
    elif args.command == "validate":
        validate(args.request)
    elif args.command == "evaluate":
        evaluate(args.prepared, args.model_output, args.output, args.runtime_source)
    else:
        run(args.request, args.output, args.upstream.resolve(), args.checkpoint.resolve())
    print(json.dumps({"status": "completed", "command": args.command}))


if __name__ == "__main__":
    main()
