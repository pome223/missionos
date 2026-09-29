#!/usr/bin/env python3
"""Opt-in native ANWM lead-forecast study on a separately provisioned GPU.

Each configuration restores the same initial weights (pinned base plus the
motion-v4 adapter), trains on the frozen training pairs of its crop, and
records forecasts for the listed samples. Held-out targets never reach the
GPU; scoring happens on the host. No aircraft API, dispatch or VLA.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def validate(root):
    protocol = json.loads((root / "study-protocol.json").read_text())
    data_root = root / "payload"
    data = json.loads((data_root / "dataset.json").read_text())
    if protocol["dataset_sha256"] != sha(data_root / "dataset.json"):
        raise ValueError("Dataset changed")
    if data["evaluation_targets_uploaded"] is not False:
        raise ValueError("Evaluation targets must stay on the host")
    for name, digest in data["assets"].items():
        path = data_root / name
        if path.is_symlink() or not path.resolve().is_relative_to(data_root.resolve()):
            raise ValueError("Unsafe asset")
        if sha(path) != digest:
            raise ValueError("Asset changed: " + name)
    for name, digest in protocol["source_sha256"].items():
        if sha(root / name) != digest:
            raise ValueError("Source changed: " + name)
    samples = {s["id"]: s for s in data["samples"]}
    for config in protocol["configs"]:
        for key in ("train_ids", "eval_ids"):
            for base in protocol[key]:
                sample = samples[f"{base}-{config['crop']}"]
                if key == "train_ids" and sample["split"] != "train":
                    raise ValueError("Training pair outside the training split")
                if sample["split"] == "test":
                    raise ValueError("Test samples are reserved for the final study")
                if sample["split"] != "train" and (
                    not sample["frames"].startswith("histories/")
                    or sample["last_index"] != data["history"] - 1
                ):
                    raise ValueError("Held-out input must be a cutoff-bounded history")
                if sample["split"] != "train":
                    with np.load(data_root / sample["frames"], allow_pickle=False) as a:
                        if (
                            len(a["rgb"]) != data["history"]
                            or int(a["stamps_ns"][-1]) != sample["cutoff_stamp_ns"]
                        ):
                            raise ValueError("Held-out history extends past its cutoff")
    return protocol, data, samples


def run(root):
    protocol, data, samples = validate(root)
    import torch

    from ship_anwm_server import NativeModel
    import ship_anwm as native
    from yokohama_wam_profile import ADAPTER_NAMES

    out = root / "results"
    out.mkdir(exist_ok=False)
    started = time.monotonic()
    model = NativeModel(
        root / "upstream",
        root / "assets/0200000.pth.tar",
        motion_adapter=root / "motion-adapter.pt",
    )
    # NativeModel places the pinned upstream on the import path.
    from anwm.diffusion import create_diffusion
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import normalize_data

    summary = dict(
        schema="pad_wam_study_run.v1",
        status="failed",
        base_sha256=native.MODEL_SHA256,
        motion_adapter_sha256=sha(root / "motion-adapter.pt"),
        protocol_sha256=sha(root / "study-protocol.json"),
        native_anwm=True,
        native_vla=False,
        dispatch_invoked=False,
        evaluation_targets_uploaded=False,
        model_load_seconds=model.load_seconds,
        configs=[],
    )
    records = []
    frames_cache = {}
    backgrounds = {
        name: np.asarray(Image.open(root / "payload/backgrounds" / f"{name}.png").convert("RGB"))
        for name in data["crops"]
    }
    action = np.zeros(4, np.float32)
    action[:3] = normalize_data(
        action[:3] / 3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])}
    )
    action = torch.as_tensor(action)[None].cuda()

    def deadline():
        if time.monotonic() - started > protocol["model_work_seconds_max"]:
            raise TimeoutError("Model work deadline")

    def tensor(rgb):
        return (
            torch.from_numpy(np.array(rgb, copy=True)).permute(0, 3, 1, 2).float().cuda() / 127.5
            - 1
        )

    def pixels(t):
        return (
            ((t.detach().float().cpu().permute(1, 2, 0).numpy() + 1) * 127.5)
            .round()
            .clip(0, 255)
            .astype(np.uint8)
        )

    def history(sample):
        if sample["frames"] not in frames_cache:
            with np.load(root / "payload" / sample["frames"], allow_pickle=False) as a:
                frames_cache[sample["frames"]] = a["rgb"]
        rgb = frames_cache[sample["frames"]]
        last = sample["last_index"]
        return rgb[last - data["history"] + 1 : last + 1]

    def conditioning(sample, config, hist):
        if config["conditioning"] == "latest":
            return hist[-1:]
        return backgrounds[config["crop"]][None]

    for name, p in model.model.named_parameters():
        p.requires_grad_(name in ADAPTER_NAMES or name.startswith("blocks.26."))
    model.vae.requires_grad_(False)
    trainable = [(n, p) for n, p in model.model.named_parameters() if p.requires_grad]
    initial = {n: p.detach().cpu().clone() for n, p in trainable}

    def predict(sample, config, stage):
        deadline()
        dest = out / config["name"] / stage / sample["id"]
        dest.mkdir(parents=True, exist_ok=False)
        hist = history(sample)
        context = tensor(hist)[None]
        projection = tensor(conditioning(sample, config, hist))[None]
        torch.manual_seed(protocol["seed"])
        torch.cuda.manual_seed_all(protocol["seed"])
        np.random.seed(protocol["seed"])
        began = time.monotonic()
        prediction = model_forward_wrapper(
            (model.model, create_diffusion(str(protocol["evaluation_steps"])), model.vae),
            context,
            action[:, None],
            sample["offset"],
            28,
            "cuda",
            16,
            x_supervised=projection,
        )[0]
        torch.cuda.synchronize()
        elapsed = time.monotonic() - began
        Image.fromarray(pixels(prediction)).save(dest / "prediction.png")
        value = dict(
            schema="pad_wam_study_forecast.v1",
            sample_id=sample["id"],
            config=config["name"],
            stage=stage,
            frames_sha256=data["assets"][sample["frames"]],
            last_index=sample["last_index"],
            offset=sample["offset"],
            conditioning=config["conditioning"],
            prediction_sha256=sha(dest / "prediction.png"),
            seconds=elapsed,
            native_anwm=True,
        )
        write(dest / "result.json", value)
        records.append(
            dict(
                file=str((dest / "result.json").relative_to(out)), sha256=sha(dest / "result.json")
            )
        )
        write(out / "forecast-manifest.json", dict(records=records))
        del context, projection, prediction

    try:
        diffusion = create_diffusion("")
        for config in protocol["configs"]:
            deadline()
            with torch.no_grad():
                for n, p in trainable:
                    p.copy_(initial[n].to(p.device))
            if config.get("before", False):
                for base in protocol["moving_ids"]:
                    predict(samples[f"{base}-{config['crop']}"], config, "before")
            prepared = []
            for base in protocol["train_ids"]:
                s = samples[f"{base}-{config['crop']}"]
                hist = history(s)
                target = np.asarray(Image.open(root / "payload" / s["target"]).convert("RGB"))
                motion = (
                    np.max(
                        np.abs(target.astype(float) - backgrounds[config["crop"]].astype(float)),
                        axis=2,
                    )
                    > 20
                ).astype(np.float32)
                roi = torch.nn.functional.max_pool2d(torch.as_tensor(motion)[None, None].cuda(), 8)
                with torch.no_grad(), torch.amp.autocast("cuda", dtype=torch.bfloat16):
                    context = model.vae.encode(tensor(hist)).latent_dist.sample() * 0.18215
                    projection = (
                        model.vae.encode(tensor(conditioning(s, config, hist))).latent_dist.sample()
                        * 0.18215
                    )
                    latent = model.vae.encode(tensor(target[None])).latent_dist.sample() * 0.18215
                prepared.append(
                    dict(
                        sample=s,
                        context=context[None],
                        projection=projection,
                        latent=latent,
                        roi=roi,
                    )
                )
            optimizer = torch.optim.AdamW(
                [p for _, p in trainable], lr=protocol["lr"], weight_decay=0
            )
            rng = np.random.default_rng(protocol["seed"])
            torch.manual_seed(protocol["seed"])
            losses, began = [], time.monotonic()
            for step in range(protocol["steps"]):
                deadline()
                row = prepared[int(rng.integers(len(prepared)))]
                optimizer.zero_grad(set_to_none=True)
                with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                    t = torch.randint(0, diffusion.num_timesteps, (1,), device="cuda")
                    noise = torch.randn_like(row["latent"])
                    captured = {}

                    def forward(x, t, captured=captured, **kwargs):
                        value = model.model(x, t, **kwargs)
                        captured["eps"] = value[:, :4]
                        return value

                    kwargs = dict(
                        y=action,
                        x_cond=row["context"],
                        x_supervised=row["projection"],
                        rel_t=torch.tensor([row["sample"]["offset"] / 128], device="cuda"),
                    )
                    ordinary = diffusion.training_losses(
                        forward, row["latent"], t, kwargs, noise=noise
                    )["loss"].mean()
                    area = row["roi"].sum()
                    roi_loss = (
                        ((captured["eps"] - noise).square() * row["roi"]).sum() / (area * 4)
                        if area > 0
                        else torch.zeros((), device="cuda")
                    )
                    loss = ordinary + config.get("roi_weight", protocol["roi_weight"]) * roi_loss
                if not torch.isfinite(loss):
                    raise ValueError("Nonfinite loss")
                loss.backward()
                grad = torch.nn.utils.clip_grad_norm_(
                    [p for _, p in trainable], 1, error_if_nonfinite=True
                )
                optimizer.step()
                losses.append(
                    dict(
                        step=step + 1,
                        sample_id=row["sample"]["id"],
                        loss=float(loss.detach()),
                        ordinary=float(ordinary.detach()),
                        roi=float(roi_loss.detach()),
                        gradient=float(grad),
                    )
                )
            (out / config["name"]).mkdir(parents=True, exist_ok=True)
            write(out / config["name"] / "training.json", losses)
            summary["configs"].append(
                dict(
                    name=config["name"],
                    steps=len(losses),
                    training_seconds=time.monotonic() - began,
                )
            )
            del optimizer, prepared
            gc.collect()
            torch.cuda.empty_cache()
            for base in protocol["train_ids"] + protocol["eval_ids"]:
                predict(samples[f"{base}-{config['crop']}"], config, "after")
        summary["status"] = "completed"
    finally:
        summary.update(total_model_seconds=time.monotonic() - started, inference_calls=len(records))
        write(out / "summary.json", summary)
        model.model.to("cpu")
        model.vae.to("cpu")
        gc.collect()
        native.clear_cuda_workspaces(torch)
        torch.cuda.empty_cache()
        write(
            out / "shutdown.json",
            dict(cuda_allocated_bytes=torch.cuda.memory_allocated(), dispatch_invoked=False),
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--execute-training", action="store_true")
    a = p.parse_args()
    if a.execute_training:
        run(a.root)
    else:
        protocol, _, _ = validate(a.root)
        print(
            json.dumps(
                dict(
                    status="passed",
                    configs=[c["name"] for c in protocol["configs"]],
                    gpu_requested=False,
                )
            )
        )
