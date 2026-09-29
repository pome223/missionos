#!/usr/bin/env python3
"""Opt-in native ANWM rule-learning test on a separately provisioned GPU.

Trains the motion-v4 weights on every training pair of the tight crop (latest
image conditioning, plain diffusion loss), then records forecasts for held-out
validation histories at fixed checkpoints. At the final checkpoint it repeats
the moving validation forecasts with a still history (the latest image 16x) and
with a second seed, and saves the trained parameters. Held-out futures never
reach the GPU; scoring is on the host. No aircraft API, dispatch or VLA.
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
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024**2), b""):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def validate(root):
    protocol = json.loads((root / "rule-protocol.json").read_text())
    data_root = root / "payload"
    data = json.loads((data_root / "dataset.json").read_text())
    if protocol["schema"] != "pad_wam_rule_protocol.v1" or protocol["dataset_sha256"] != sha(
        data_root / "dataset.json"
    ):
        raise ValueError("Unreviewed protocol or dataset")
    if data["evaluation_targets_uploaded"] is not False:
        raise ValueError("Evaluation targets must stay on the host")
    for name, digest in data["assets"].items():
        path = data_root / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(data_root.resolve())
            or sha(path) != digest
        ):
            raise ValueError("Unsafe or changed asset: " + name)
    for name, digest in protocol["source_sha256"].items():
        if sha(root / name) != digest:
            raise ValueError("Source changed: " + name)
    samples = {s["id"]: s for s in data["samples"]}
    for s in samples.values():
        if s["split"] == "test":
            raise ValueError("Test samples are reserved")
        if s["split"] != "train":
            if not s["frames"].startswith("histories/") or s["last_index"] != 15:
                raise ValueError("Held-out input must be a cutoff-bounded history")
            with np.load(data_root / s["frames"], allow_pickle=False) as a:
                if len(a["rgb"]) != 16 or int(a["stamps_ns"][-1]) != s["cutoff_stamp_ns"]:
                    raise ValueError("Held-out history extends past its cutoff")
    for ident in protocol["moving_ids"] + protocol["static_ids"]:
        if samples[ident]["split"] != "val":
            raise ValueError("Evaluation ids must be validation samples")
    if (
        sorted(protocol["checkpoints"]) != protocol["checkpoints"]
        or protocol["checkpoints"][-1] != protocol["steps"]
    ):
        raise ValueError("Checkpoints must end at the final step")
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
    from anwm.diffusion import create_diffusion
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import normalize_data

    summary = dict(
        schema="pad_wam_rule_run.v1",
        status="failed",
        native_anwm=True,
        native_vla=False,
        dispatch_invoked=False,
        evaluation_targets_uploaded=False,
        protocol_sha256=sha(root / "rule-protocol.json"),
        model_load_seconds=model.load_seconds,
    )
    records, losses = [], []

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

    action = np.zeros(4, np.float32)
    action[:3] = normalize_data(
        action[:3] / 3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])}
    )
    action = torch.as_tensor(action)[None].cuda()
    for name, p in model.model.named_parameters():
        p.requires_grad_(name in ADAPTER_NAMES or name.startswith("blocks.26."))
    model.vae.requires_grad_(False)
    trainable = [(n, p) for n, p in model.model.named_parameters() if p.requires_grad]

    def history(sample):
        with np.load(root / "payload" / sample["frames"], allow_pickle=False) as a:
            rgb = a["rgb"]
        last = sample["last_index"]
        return rgb[last - 15 : last + 1]

    def predict(sample, stage, seed, still=False):
        deadline()
        hist = history(sample)
        context_rgb = np.repeat(hist[-1:], 16, axis=0) if still else hist
        tag = f"{stage}/{'still' if still else 'moving'}-seed-{seed}/{sample['id']}"
        dest = out / tag
        dest.mkdir(parents=True, exist_ok=False)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        tick = time.monotonic()
        with torch.no_grad():
            prediction = model_forward_wrapper(
                (model.model, create_diffusion(str(protocol["evaluation_steps"])), model.vae),
                tensor(context_rgb)[None],
                action[:, None],
                sample["offset"],
                28,
                "cuda",
                16,
                x_supervised=tensor(hist[-1:])[None],
            )[0]
        torch.cuda.synchronize()
        Image.fromarray(pixels(prediction)).save(dest / "prediction.png")
        value = dict(
            sample_id=sample["id"],
            stage=stage,
            seed=seed,
            history="still" if still else "moving",
            frames_sha256=data["assets"][sample["frames"]],
            prediction_sha256=sha(dest / "prediction.png"),
            seconds=time.monotonic() - tick,
        )
        write(dest / "result.json", value)
        records.append(
            dict(
                file=str((dest / "result.json").relative_to(out)), sha256=sha(dest / "result.json")
            )
        )
        write(out / "forecast-manifest.json", dict(records=records))

    try:
        # Encode each training sequence once; pairs slice one latent sample per frame.
        latents = {}
        torch.manual_seed(protocol["seed"])
        for rel in sorted({s["frames"] for s in samples.values() if s["split"] == "train"}):
            deadline()
            with np.load(root / "payload" / rel, allow_pickle=False) as a:
                rgb = a["rgb"]
            chunks = []
            with torch.no_grad(), torch.amp.autocast("cuda", dtype=torch.bfloat16):
                for i in range(0, len(rgb), 32):
                    chunks.append(
                        (
                            model.vae.encode(tensor(rgb[i : i + 32])).latent_dist.sample() * 0.18215
                        ).float()
                    )
            latents[rel] = torch.cat(chunks)
        pairs = [s for s in samples.values() if s["split"] == "train"]
        summary["training_pairs"] = len(pairs)
        optimizer = torch.optim.AdamW([p for _, p in trainable], lr=protocol["lr"], weight_decay=0)
        diffusion = create_diffusion("")
        rng = np.random.default_rng(protocol["seed"])
        began, step = time.monotonic(), 0
        for checkpoint in protocol["checkpoints"]:
            while step < checkpoint:
                deadline()
                s = pairs[int(rng.integers(len(pairs)))]
                lat, last = latents[s["frames"]], s["last_index"]
                context, projection, target = (
                    lat[last - 15 : last + 1][None],
                    lat[last : last + 1],
                    lat[last + s["offset"] : last + s["offset"] + 1],
                )
                optimizer.zero_grad(set_to_none=True)
                with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                    t = torch.randint(0, diffusion.num_timesteps, (1,), device="cuda")
                    kwargs = dict(
                        y=action,
                        x_cond=context,
                        x_supervised=projection,
                        rel_t=torch.tensor([s["offset"] / 128], device="cuda"),
                    )
                    loss = diffusion.training_losses(model.model, target, t, kwargs)["loss"].mean()
                if not torch.isfinite(loss):
                    raise ValueError("Nonfinite loss")
                loss.backward()
                grad = torch.nn.utils.clip_grad_norm_(
                    [p for _, p in trainable], 1, error_if_nonfinite=True
                )
                optimizer.step()
                step += 1
                if step % 16 == 0:
                    losses.append(dict(step=step, loss=float(loss.detach()), gradient=float(grad)))
            write(out / "training.json", losses)
            stage = f"ckpt-{checkpoint:05d}"
            for ident in protocol["moving_ids"] + protocol["static_ids"]:
                predict(samples[ident], stage, protocol["seed"])
        summary["training_seconds"] = time.monotonic() - began
        final = f"ckpt-{protocol['checkpoints'][-1]:05d}"
        for ident in protocol["moving_ids"]:
            predict(samples[ident], final, protocol["seed"], still=True)
            predict(samples[ident], final, protocol["second_seed"])
        state = {n: p.detach().float().cpu().clone() for n, p in trainable}
        (out / "weights").mkdir()
        torch.save(
            dict(
                schema="pad_wam_rule_parameters.v1",
                protocol_sha256=summary["protocol_sha256"],
                base_sha256=native.MODEL_SHA256,
                step=step,
                state=state,
            ),
            out / "weights/final.pt",
        )
        summary["final_weights_sha256"] = sha(out / "weights/final.pt")
        summary["steps"] = step
        summary["status"] = "completed"
    finally:
        summary.update(total_model_seconds=time.monotonic() - started, inference_calls=len(records))
        write(out / "summary.json", summary)
        model.model.to("cpu")
        model.vae.to("cpu")
        gc.collect()
        native.clear_cuda_workspaces(torch)
        torch.cuda.empty_cache()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--execute-training", action="store_true")
    a = p.parse_args()
    if a.execute_training:
        run(a.root)
    else:
        protocol, data, samples = validate(a.root)
        n = len(protocol["checkpoints"]) * (
            len(protocol["moving_ids"]) + len(protocol["static_ids"])
        ) + 2 * len(protocol["moving_ids"])
        print(
            json.dumps(
                dict(
                    status="passed",
                    training_pairs=sum(s["split"] == "train" for s in samples.values()),
                    planned_forecasts=n,
                    gpu_requested=False,
                )
            )
        )
