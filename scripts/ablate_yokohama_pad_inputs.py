#!/usr/bin/env python3
"""Opt-in frozen-weight ANWM input ablation. Default is CPU input validation.

With weights, seeds and horizon fixed, swap only the inputs:
A moving history + latest image, B repeated latest + latest image,
C moving history + lead-free background, D repeated latest + background.
No training, no future frame on the GPU, no aircraft API.
"""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from trace_yokohama_pad_sampling import TraceSampler, selected_times, sha, timestep_map, write

CONDITIONS = {
    "A": ("moving", "latest"),
    "B": ("repeated", "latest"),
    "C": ("moving", "background"),
    "D": ("repeated", "background"),
}


def validate(root):
    protocol = json.loads((root / "input-ablation-protocol.json").read_text())
    if (
        protocol["schema"] != "pad_anwm_input_ablation.v1"
        or protocol["training_allowed"] is not False
    ):
        raise ValueError("Unreviewed ablation protocol")
    if sorted(protocol["conditions"]) != sorted(CONDITIONS) or protocol["offset"] != 12:
        raise ValueError("Unreviewed conditions or horizon")
    for name, digest in protocol["files"].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe input path")
        if sha(path) != digest:
            raise ValueError("Changed input: " + name)
    for bad in ("training-target.png", "training-latents.pt", "pair.npz", "targets"):
        if (root / bad).exists():
            raise ValueError("Future-bearing files must remain on the host")
    for sample in protocol["samples"]:
        with np.load(root / sample["history"], allow_pickle=False) as data:
            if set(data.files) != {"rgb", "stamps_ns"}:
                raise ValueError("History must contain only observations and timestamps")
            rgb, stamps = data["rgb"], data["stamps_ns"]
            if rgb.shape != (16, 224, 224, 3) or int(stamps[-1]) != sample["cutoff_stamp_ns"]:
                raise ValueError("History must be 16 frames ending at the cutoff")
        background = np.asarray(Image.open(root / sample["background"]).convert("RGB"))
        if background.shape != (224, 224, 3):
            raise ValueError("Background shape")
    return protocol


def inputs(history, background, condition):
    kind, projection = CONDITIONS[condition]
    context = history if kind == "moving" else np.repeat(history[-1:], len(history), axis=0)
    return context, (history[-1:] if projection == "latest" else background[None])


def run(root):
    protocol = validate(root)
    import torch
    from ship_anwm_server import NativeModel
    import ship_anwm as native
    from yokohama_wam_profile import ADAPTER_NAMES

    out = root / "results"
    out.mkdir(exist_ok=False)
    summary = dict(
        status="failed",
        native_anwm=True,
        native_vla=False,
        training_updates=0,
        future_input=False,
        protocol_sha256=sha(root / "input-ablation-protocol.json"),
    )
    records, start, model = [], time.monotonic(), None

    def deadline():
        if time.monotonic() - start > protocol["model_work_seconds_max"]:
            raise TimeoutError("Ablation deadline")

    def tensor(rgb):
        return (
            torch.from_numpy(np.array(rgb, copy=True)).permute(0, 3, 1, 2).float().cuda() / 127.5
            - 1
        )

    def save_image(value, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        pixels = (
            ((value.detach().float().cpu().permute(1, 2, 0).numpy() + 1) * 127.5)
            .round()
            .clip(0, 255)
            .astype(np.uint8)
        )
        Image.fromarray(pixels).save(path)
        return dict(file=str(path.relative_to(out)), sha256=sha(path))

    try:
        model = NativeModel(
            root / "upstream",
            root / "assets/0200000.pth.tar",
            motion_adapter=root / "motion-adapter.pt",
        )
        from anwm.diffusion import create_diffusion
        from anwm.rollout import model_forward_wrapper
        from anwm.utils import normalize_data

        parameters = dict(model.model.named_parameters())
        names = {n for n in parameters if n in ADAPTER_NAMES or n.startswith("blocks.26.")}
        initial = {n: parameters[n].detach().clone() for n in names}
        saved = torch.load(root / "after-1024.pt", map_location="cpu", weights_only=True)
        if (
            saved["protocol_sha256"] != protocol["training_protocol_sha256"]
            or set(saved["state"]) != names
        ):
            raise ValueError("Retained weight provenance or scope mismatch")
        weight_sets = {
            "initial": initial,
            "after-1024": {n: v.cuda() for n, v in saved["state"].items()},
        }
        del saved
        model.model.requires_grad_(False)
        model.vae.requires_grad_(False)
        action = np.zeros(4, np.float32)
        action[:3] = normalize_data(
            action[:3] / 3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])}
        )
        action = torch.as_tensor(action)[None, None].cuda()
        sampler = create_diffusion(str(protocol["sampling_steps"]))
        if sampler.timestep_map != timestep_map(protocol["sampling_steps"]):
            raise ValueError("Unexpected upstream timestep map")
        anchors = selected_times(protocol["sampling_steps"], protocol["display_anchors"])
        for weights in protocol["weights"]:
            with torch.no_grad():
                for n, v in weight_sets[weights].items():
                    parameters[n].copy_(v)
            for sample in protocol["samples"]:
                with np.load(root / sample["history"], allow_pickle=False) as data:
                    history = data["rgb"].copy()
                background = np.asarray(Image.open(root / sample["background"]).convert("RGB"))
                for condition in protocol["conditions"]:
                    context_rgb, projection_rgb = inputs(history, background, condition)
                    context, projection = tensor(context_rgb)[None], tensor(projection_rgb)[None]
                    for value in protocol["seeds"]:
                        deadline()
                        torch.manual_seed(value)
                        torch.cuda.manual_seed_all(value)
                        trace = TraceSampler(sampler, deadline)
                        tick = time.monotonic()
                        prediction = model_forward_wrapper(
                            (model.model, trace, model.vae),
                            context,
                            action,
                            protocol["offset"],
                            28,
                            "cuda",
                            16,
                            x_supervised=projection,
                        )[0]
                        torch.cuda.synchronize()
                        seconds = time.monotonic() - tick
                        stem = f"{weights}/{sample['id']}/{condition}/seed-{value}"
                        final = save_image(prediction, out / f"{stem}/final.png")
                        frames = []
                        for row in trace.records:
                            if row["original_t"] not in anchors:
                                continue
                            with torch.no_grad(), torch.amp.autocast("cuda", dtype=torch.bfloat16):
                                decoded = model.vae.decode(
                                    row["pred_xstart"].cuda() / 0.18215
                                ).sample.clamp(-1, 1)[0]
                            frames.append(
                                dict(
                                    save_image(
                                        decoded, out / f"{stem}/t-{row['original_t']:03d}.png"
                                    ),
                                    original_t=row["original_t"],
                                )
                            )
                        records.append(
                            dict(
                                weights=weights,
                                sample=sample["id"],
                                condition=condition,
                                seed=value,
                                history=CONDITIONS[condition][0],
                                projection=CONDITIONS[condition][1],
                                final=final,
                                frames=frames,
                                seconds=seconds,
                                trace_steps=len(trace.records),
                            )
                        )
                        write(out / "forecasts.json", records)
                        print(
                            json.dumps(
                                dict(
                                    weights=weights,
                                    sample=sample["id"],
                                    condition=condition,
                                    seed=value,
                                    seconds=seconds,
                                )
                            ),
                            flush=True,
                        )
                        del trace, prediction
        summary["status"] = "completed"
    finally:
        summary.update(elapsed_model_seconds=time.monotonic() - start, forecasts=len(records))
        write(out / "summary.json", summary)
        if model is not None:
            model.model.to("cpu")
            model.vae.to("cpu")
            gc.collect()
            native.clear_cuda_workspaces(torch)
            torch.cuda.empty_cache()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--execute-inference", action="store_true")
    a = p.parse_args()
    if a.execute_inference:
        run(a.root)
    else:
        v = validate(a.root)
        n = len(v["weights"]) * len(v["samples"]) * len(v["conditions"]) * len(v["seeds"])
        print(json.dumps(dict(status="passed", gpu_requested=False, planned_forecasts=n)))
