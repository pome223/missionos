#!/usr/bin/env python3
"""Opt-in development ablation and conditional WAM post-training; no aircraft API."""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

if __package__:
    from scripts.yokohama_pad_learning_data import classify, history, sha, validate, write
else:
    from yokohama_pad_learning_data import classify, history, sha, validate, write


def validate_protocol(root):
    protocol = json.loads((root / "learning-protocol.json").read_text())
    expected = dict(schema="pad_wam_learning_protocol.v1", steps=2048, lr=0.00005,
                    seed=42, gradient_clip=1.0, roi_extra_weight=4.0,
                    evaluation_steps=50, evaluation_frame_offset=64,
                    development_rgb_mae_max=12, model_work_seconds_max=2400,
                    flight_admitted=False, vla_training=False)
    if any(protocol.get(key) != value for key, value in expected.items()):
        raise ValueError("Unreviewed training protocol")
    if sha(root / "dataset/dataset.json") != protocol["dataset_sha256"]:
        raise ValueError("Dataset changed after protocol freeze")
    for filename, digest in protocol["source_sha256"].items():
        if Path(filename).name != filename or sha(root / filename) != digest:
            raise ValueError("Frozen source changed")
    return protocol, validate(root / "dataset")


def run(root):
    protocol, data = validate_protocol(root)
    if __package__:
        from scripts import ship_anwm as native
        from scripts.ship_anwm_server import NativeModel
        from scripts.yokohama_appearance import fill_infinite_appearance
        from scripts.yokohama_wam_profile import ADAPTER_NAMES
    else:
        import ship_anwm as native
        from ship_anwm_server import NativeModel
        from yokohama_appearance import fill_infinite_appearance
        from yokohama_wam_profile import ADAPTER_NAMES
    import torch

    started = time.monotonic()
    output = root / "results"
    output.mkdir(exist_ok=False)
    model = NativeModel(root / "upstream", root / "assets/0200000.pth.tar", motion_adapter=root / "motion-adapter.pt")
    from anwm.diffusion import create_diffusion
    from anwm.projection import project_to_2d_image_seq2seq, reproject_depth_to_other_pose_seq2seq
    from anwm.utils import normalize_data, transform

    dataset = root / "dataset"
    by_id = {sample["id"]: sample for sample in data["samples"]}
    with np.load(dataset / "reader.npz", allow_pickle=False) as archive:
        reader = {key: archive[key] for key in archive.files}
    records = []

    def deadline():
        if time.monotonic() - started > protocol["model_work_seconds_max"]:
            raise TimeoutError("Model-work deadline")

    def predict(sample, stage, steps=50, offset=64, adapter_sha256=None):
        deadline()
        arrays = history(dataset, sample)
        destination = output / stage / sample["id"]
        destination.mkdir(parents=True, exist_ok=False)
        model.diffusion = create_diffusion(str(steps))
        request = dict(schema_version=native.MOTION_CONTRACT, num_timesteps=offset,
                       candidates=[dict(id="hold", delta=[0, 0, 0, 0])])
        began = time.monotonic()
        result = model.predict(request, arrays, destination)
        for forecast in result["forecasts"]:
            for asset in forecast["files"].values():
                asset.pop("png_base64", None)
        result.update(schema="pad_learning_forecast.v1", sample_id=sample["id"],
                      stage=stage, history_sha256=data["assets"][sample["history"]],
                      model_frame_offset=offset, requested_horizon_sim_s=offset/4,
                      cutoff_stamp_ns=sample["cutoff_stamp_ns"],
                      requested_future_stamp_ns=sample["cutoff_stamp_ns"]+int(offset/4*1e9),
                      diffusion_steps=steps, seed=42, request_total_seconds=time.monotonic()-began,
                      adapter_sha256=adapter_sha256 or native.MOTION_ADAPTER_SHA256,
                      native_inference_invoked=True, flight_admitted=False, dispatch_invoked=False)
        path = destination / "result.json"
        write(path, result)
        records.append(dict(file=str(path.relative_to(output)), sha256=sha(path)))
        write(output / "forecast-manifest.json", dict(records=records))
        print(json.dumps(dict(stage=stage, sample=sample["id"], seconds=result["request_total_seconds"])), flush=True)
        return np.asarray(Image.open(destination / "hold-prediction.png").convert("RGB"))

    def development_score(sample, pixels):
        target = native.crop_rgb(np.asarray(Image.open(dataset / sample["training_target"]).convert("RGB")))
        prediction, truth = classify(pixels, reader), classify(target, reader)
        mae = float(np.abs(pixels.astype(float)-target.astype(float)).mean())
        return dict(sample_id=sample["id"], reader=prediction, target_reader=truth, rgb_mae=mae,
                    passed=truth["state"] != "unknown" and prediction["state"] == truth["state"]
                    and mae <= protocol["development_rgb_mae_max"])

    summary = dict(status="failed", schema="pad_wam_post_training.v1", protocol_sha256=sha(root / "learning-protocol.json"),
                   base_sha256=native.MODEL_SHA256, initial_adapter_sha256=native.MOTION_ADAPTER_SHA256,
                   model_load_seconds=model.load_seconds, flight_admitted=False, dispatch_invoked=False,
                   mission_judgment_invoked=False, physical_execution=False, vla_inference_invoked=False,
                   test_targets_uploaded=False, training_trigger_source="three development samples only")
    try:
        # Same historical observation, varying one factor at a time. Targets are
        # development-only and correspond to each requested frame offset.
        ablation = []
        for steps in (50, 250):
            for offset in (1, 64):
                sample = by_id[f"train-depart-20-t{offset:02d}"]
                pixels = predict(sample, f"ablation-{steps}-{offset}", steps, offset)
                ablation.append(dict(diffusion_steps=steps, frame_offset=offset, **development_score(sample, pixels)))
        write(output / "ablation.json", ablation)
        development = []
        for name in data["development_probes"]:
            sample = by_id[name]
            development.append(development_score(sample, predict(sample, "development-before")))
        training_needed = not all(row["passed"] for row in development)
        write(output / "training-decision.json", dict(training_needed=training_needed,
              reason="fixed 50-step/64-frame development capability gate", development=development,
              evaluation_targets_used=False))
        for sample in data["samples"]:
            if sample["split"] == "test":
                predict(sample, "before")
        if not training_needed:
            summary.update(status="completed", training_performed=False, reason="development gate already passed")
            return

        for name, parameter in model.model.named_parameters():
            parameter.requires_grad_(name in ADAPTER_NAMES)
        model.vae.requires_grad_(False)
        trainable = [(name, parameter) for name, parameter in model.model.named_parameters() if parameter.requires_grad]
        if {name for name, _ in trainable} != ADAPTER_NAMES:
            raise ValueError("Trainable scope changed")

        def state_digest(training):
            import hashlib
            digest = hashlib.sha256()
            for name, parameter in model.model.named_parameters():
                if parameter.requires_grad == training:
                    digest.update(name.encode())
                    digest.update(parameter.detach().cpu().contiguous().numpy().tobytes())
            return digest.hexdigest()

        frozen_before = state_digest(False)
        initial = {name: parameter.detach().cpu().clone() for name, parameter in trainable}
        initial_hash = state_digest(True)
        summary.update(trainable_names=sorted(ADAPTER_NAMES), trainable_parameters=sum(p.numel() for _, p in trainable),
                       frozen_before_sha256=frozen_before, trainable_before_sha256=initial_hash)
        prepared = []
        cache = {}
        torch.manual_seed(42)
        np.random.seed(42)
        for sample in data["samples"]:
            if sample["split"] != "train":
                continue
            deadline()
            key = sample["history"]
            if key not in cache:
                arrays = history(dataset, sample)
                target_pose = arrays["poses"][-1]
                points, colors = reproject_depth_to_other_pose_seq2seq(arrays["intrinsics"], arrays["depth"],
                                                                     arrays["rgb"], arrays["poses"], target_pose[None])
                projection = project_to_2d_image_seq2seq(arrays["intrinsics"], points, colors, (360, 640))[0]
                known = project_to_2d_image_seq2seq(arrays["intrinsics"], points,
                                                  [np.full_like(color, 255) for color in colors], (360, 640))[0][..., 0] > 0
                raw = np.where(arrays["last_depth_infinite"], np.inf, arrays["depth"][-1])
                filled, mask = fill_infinite_appearance(projection, known, arrays["rgb"][-1], raw,
                                                       arrays["intrinsics"], target_pose, target_pose)
                if not np.array_equal(filled[known], projection[known]) or (mask & known).any():
                    raise ValueError("Appearance altered metric geometry")
                context = torch.stack([transform(Image.fromarray(rgb)) for rgb in arrays["rgb"]]).cuda()
                projected = transform(Image.fromarray(filled))[None].cuda()
                with torch.no_grad(), torch.amp.autocast("cuda", dtype=torch.bfloat16):
                    cache[key] = dict(x_cond=(model.vae.encode(context).latent_dist.sample()*0.18215)[None],
                                      x_supervised=model.vae.encode(projected).latent_dist.sample()*0.18215)
                del context, projected, points, colors
            # Raw targets are transformed once, preserving the same 4:3 crop as input.
            target = transform(Image.open(dataset / sample["training_target"]).convert("RGB"))[None].cuda()
            with torch.no_grad(), torch.amp.autocast("cuda", dtype=torch.bfloat16):
                latent = model.vae.encode(target).latent_dist.sample()*0.18215
            prepared.append(dict(sample=sample, x_start=latent, **cache[key]))
        action = np.zeros(4, np.float32)
        action[:3] = normalize_data(action[:3]/3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])})
        action = torch.as_tensor(action)[None].cuda()
        roi = torch.nn.functional.max_pool2d(torch.as_tensor(reader["mask"].astype(np.float32))[None, None].cuda(), 8)
        if roi.shape != (1, 1, 28, 28) or roi.sum() <= 0:
            raise ValueError("Invalid development-only motion region")
        optimizer = torch.optim.AdamW([p for _, p in trainable], lr=protocol["lr"], weight_decay=0)
        diffusion = create_diffusion("")
        generator = np.random.default_rng(42)
        torch.manual_seed(42)
        losses = []
        training_start = time.monotonic()
        for step in range(protocol["steps"]):
            deadline()
            row = prepared[int(generator.integers(len(prepared)))]
            optimizer.zero_grad(set_to_none=True)
            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                timestep = torch.randint(0, diffusion.num_timesteps, (1,), device="cuda")
                noise = torch.randn_like(row["x_start"])
                captured = {}

                def forward(x, t, _captured=captured, **kwargs):
                    result = model.model(x, t, **kwargs)
                    _captured["eps"] = result[:, :4]
                    return result

                kwargs = dict(y=action, x_cond=row["x_cond"], x_supervised=row["x_supervised"],
                              rel_t=torch.tensor([row["sample"]["frame_offset"]/128], device="cuda"))
                ordinary = diffusion.training_losses(forward, row["x_start"], timestep, kwargs, noise=noise)["loss"].mean()
                roi_loss = ((captured["eps"]-noise).square()*roi).sum()/(roi.sum()*4)
                loss = ordinary + protocol["roi_extra_weight"]*roi_loss
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite training loss")
            loss.backward()
            grad = torch.nn.utils.clip_grad_norm_([p for _, p in trainable], protocol["gradient_clip"], error_if_nonfinite=True)
            optimizer.step()
            losses.append(dict(step=step+1, sample_id=row["sample"]["id"], loss=float(loss.detach()),
                               ordinary_loss=float(ordinary.detach()), roi_loss=float(roi_loss.detach()), gradient_norm=float(grad)))
            if step == 0 or (step+1) % 64 == 0:
                write(output / "training.json", losses)
                print(json.dumps(dict(stage="training", **losses[-1])), flush=True)
        training_seconds = time.monotonic()-training_start
        final_state = {name: parameter.detach().cpu().clone() for name, parameter in trainable}
        delta_l2 = sum(float((final_state[name]-initial[name]).square().sum()) for name in initial)**0.5
        if delta_l2 <= 0 or state_digest(False) != frozen_before:
            raise ValueError("No update or frozen base changed")
        adapter = output / "adapter.pt"
        torch.save(dict(schema_version="anwm_head_adaptation.v1", base_checkpoint_sha256=native.MODEL_SHA256,
                        state=final_state, protocol_sha256=sha(root / "learning-protocol.json")), adapter)
        with torch.no_grad():
            for name, parameter in trainable:
                parameter.copy_(initial[name])
        saved = torch.load(adapter, map_location="cpu", weights_only=True)
        with torch.no_grad():
            for name, parameter in trainable:
                parameter.copy_(saved["state"][name])
                if not torch.equal(parameter.detach().cpu(), final_state[name]):
                    raise ValueError("Serialized adapter reload failed")
        final_hash = state_digest(True)
        if final_hash == initial_hash:
            raise ValueError("Reloaded model did not change")
        summary.update(training_performed=True, steps=len(losses), training_seconds=training_seconds,
                       head_change_l2=delta_l2, frozen_base_unchanged=True, checkpoint_reloaded=True,
                       final_adapter_sha256=sha(adapter), final_trainable_sha256=final_hash)
        write(output / "training-receipt.json", summary)
        del optimizer, prepared, cache, initial, saved, final_state
        del row, target, latent, action, roi, loss, ordinary, roi_loss, grad, timestep, noise, captured, kwargs, forward
        gc.collect()
        torch.cuda.empty_cache()
        for sample in data["samples"]:
            if sample["split"] == "test":
                predict(sample, "after", adapter_sha256=sha(adapter))
        summary["status"] = "completed"
    finally:
        summary.update(total_model_seconds=time.monotonic()-started, inference_calls=len(records))
        write(output / "summary.json", summary)
        model.model.to("cpu")
        model.vae.to("cpu")
        gc.collect()
        native.clear_cuda_workspaces(torch)
        torch.cuda.empty_cache()
        write(output / "shutdown.json", dict(cuda_allocated_bytes=torch.cuda.memory_allocated(), dispatch_invoked=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute-training", action="store_true")
    args = parser.parse_args()
    if args.execute_training:
        run(args.root)
    else:
        _, data = validate_protocol(args.root)
        print(json.dumps(dict(status="passed", samples=len(data["samples"]), gpu_requested=False)))
