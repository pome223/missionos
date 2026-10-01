#!/usr/bin/env python3
"""CPU-only analysis of retained ANWM one-pair evidence; never a forecast gate.

Teacher images contain a noised true future. Their errors diagnose denoising,
not anticipation or flight value. The sampler trajectory was not retained.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.diagnose_yokohama_pad_latents import sha, validate, write
from scripts.evaluate_yokohama_pad_latents import evaluate
from scripts.yokohama_pad_wam_study import detect_lead, displacement, lead_mask


def alpha_bar():
    # Pinned ANWM 657a8026: linear 1000-step beta schedule, EPSILON prediction.
    return np.cumprod(1 - np.linspace(0.0001, 0.02, 1000, dtype=np.float64))


def x0_mse_from_epsilon(mse, timestep):
    """Squared-error identity for an unclipped EPSILON -> x0 conversion.

    x0_hat - x0 = -sqrt((1-alpha_bar)/alpha_bar) * (eps_hat - eps).
    This is an inferred latent error, not measured RGB error or a new rollout.
    """
    if not isinstance(timestep, int) or not 0 <= timestep < 1000:
        raise ValueError("Invalid diffusion timestep")
    if not np.isfinite(mse) or mse < 0:
        raise ValueError("Invalid epsilon MSE")
    a = alpha_bar()[timestep]
    return float(mse * (1 - a) / a)


def check_diagnostics(records, steps):
    expected = {(step, t) for step in steps for t in (100, 500, 900)}
    actual = [(r["step"], r["timestep"]) for r in records]
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError("Missing or duplicate teacher diagnostics")
    for row in records:
        if row["kind"] != "teacher_forced":
            raise ValueError("Teacher/forecast boundary changed")
        for key in ("roi_noise_mse", "background_noise_mse"):
            x0_mse_from_epsilon(row[key], row["timestep"])


def loss_groups(records, steps):
    if [r["step"] for r in records] != list(range(1, steps + 1)):
        raise ValueError("Missing or duplicate training update")
    for row in records:
        if not isinstance(row["timestep"], int) or not 0 <= row["timestep"] < 1000:
            raise ValueError("Invalid training timestep")
        if not all(np.isfinite(row[k]) for k in ("loss", "mse", "gradient")):
            raise ValueError("Nonfinite training record")
    groups = []
    for begin, end in ((1, 256), (steps - 255, steps)):
        for lo, hi in ((0, 100), (100, 400), (400, 700), (700, 1000)):
            rows = [r for r in records if begin <= r["step"] <= end and lo <= r["timestep"] < hi]
            groups.append(
                dict(
                    updates=[begin, end],
                    diffusion_t=[lo, hi],
                    count=len(rows),
                    mean_epsilon_mse=float(np.mean([r["mse"] for r in rows])),
                    mean_vb_term=float(np.mean([r["loss"] - r["mse"] for r in rows])),
                )
            )
    return dict(
        groups=groups,
        updates=len(records),
        clipped_gradient_fraction=float(np.mean([r["gradient"] > 1 for r in records])),
        scope="Different noise draws/timesteps; these group means are descriptive, not a paired ablation",
    )


def weight_audit(results, summary):
    import torch

    torch.set_num_threads(2)
    checkpoints = []
    for label in ("initial_weights", "final_weights"):
        receipt = summary[label]
        path = results / receipt["file"]
        if sha(path) != receipt["sha256"]:
            raise ValueError("Weight receipt mismatch")
        saved = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
        if (
            saved["schema"] != "anwm_one_pair_parameters.v1"
            or saved["protocol_sha256"] != summary["protocol_sha256"]
        ):
            raise ValueError("Weight protocol mismatch")
        checkpoints.append(saved["state"])
    before, after = checkpoints
    if set(before) != set(after):
        raise ValueError("Weight names changed")
    tensors = []
    for name in sorted(before):
        a, b = before[name], after[name]
        if a.shape != b.shape or a.dtype != torch.float32 or b.dtype != torch.float32:
            raise ValueError("Weight shape/dtype mismatch")
        if not (torch.isfinite(a).all() and torch.isfinite(b).all()):
            raise ValueError("Nonfinite weight")
        delta = b - a
        norm = float(torch.linalg.vector_norm(a))
        dnorm = float(torch.linalg.vector_norm(delta))
        tensors.append(
            dict(
                name=name,
                shape=list(a.shape),
                parameters=a.numel(),
                changed_values=int(torch.count_nonzero(delta)),
                initial_l2=norm,
                update_l2=dnorm,
                relative_update_l2=dnorm / norm if norm else None,
                max_abs_change=float(delta.abs().max()),
            )
        )
    # unpatchify uses [patch_y, patch_x, channel], NOT two contiguous row halves.
    heads = {}
    for label, channels in (("epsilon", slice(0, 4)), ("variance", slice(4, 8))):
        name = "final_layer.linear.weight"
        a = before[name].reshape(2, 2, 8, -1)[:, :, channels]
        b = after[name].reshape(2, 2, 8, -1)[:, :, channels]
        heads[label] = dict(
            initial_l2=float(torch.linalg.vector_norm(a)),
            update_l2=float(torch.linalg.vector_norm(b - a)),
        )
    n = sum(r["parameters"] for r in tensors)
    if n != summary["trainable_parameters"]:
        raise ValueError("Trainable parameter count mismatch")
    return dict(
        device="cpu",
        torch_version=torch.__version__,
        all_finite=True,
        parameters=n,
        tensors=len(tensors),
        changed_tensors=sum(r["changed_values"] > 0 for r in tensors),
        changed_values=sum(r["changed_values"] for r in tensors),
        final_head_channels=heads,
        per_tensor=tensors,
        scope="Selected retained parameters only; no gradient attribution, model forward or accelerator use",
    )


def analyze(root, results, *, inspect_weights=False):
    protocol = validate(root)
    evaluation = evaluate(root, results)
    if not evaluation["complete"]:
        raise ValueError("Incomplete native evidence")
    teacher = json.loads((results / "diagnostics.json").read_text())
    check_diagnostics(teacher, protocol["evaluation_at_steps"])
    training = json.loads((results / "training.json").read_text())
    summary = json.loads((results / "summary.json").read_text())
    target = np.asarray(Image.open(root / "training-target.png").convert("RGB"))
    truth = detect_lead(target, "tight")
    mask = lead_mask(target)
    # Match the exact nonoverlapping 8x8 max-pooling used by the GPU diagnostic.
    latent_mask = mask.reshape(28, 8, 28, 8).any(axis=(1, 3))
    rows = []
    for r in teacher:
        path = results / f"teacher/step-{r['step']}-t-{r['timestep']}.png"
        rgb = np.asarray(Image.open(path).convert("RGB"))
        readout = detect_lead(rgb, "tight")
        diff = np.abs(rgb.astype(float) - target.astype(float)).mean(axis=2)
        rows.append(
            dict(
                r,
                file=str(path.relative_to(results)),
                sha256=sha(path),
                true_future_supplied=True,
                alpha_bar=float(alpha_bar()[r["timestep"]]),
                implied_x0_roi_mse=x0_mse_from_epsilon(r["roi_noise_mse"], r["timestep"]),
                implied_x0_background_mse=x0_mse_from_epsilon(
                    r["background_noise_mse"], r["timestep"]
                ),
                raw_color_readout=readout,
                raw_color_error_original_px=displacement(readout, truth),
                rgb_mae=float(diff.mean()),
                roi_rgb_mae=float(diff[mask].mean()),
            )
        )
    forecasts = [r for r in evaluation["rows"] if r["route"] == "native"]
    pair_differences = []
    for step in protocol["evaluation_at_steps"]:
        images = [
            np.asarray(
                Image.open(results / f"forecasts/step-{step}-seed-{seed}-native.png").convert("RGB")
            ).astype(float)
            for seed in protocol["forecast_seeds"]
        ]
        diff = np.abs(images[0] - images[1]).mean(axis=2)
        pair_differences.append(
            dict(step=step, global_mae=float(diff.mean()), target_body_mae=float(diff[mask].mean()))
        )
    return dict(
        schema="pad_anwm_denoising_audit.v1",
        native_evidence_complete=True,
        analysis_script_sha256=sha(__file__),
        protocol_sha256=sha(root / "latent-protocol.json"),
        source_logs_sha256={
            f: sha(results / f) for f in ("diagnostics.json", "training.json", "summary.json")
        },
        target_color_pixels=int(mask.sum()),
        target_color_fraction=float(mask.mean()),
        roi_latent_cells=int(latent_mask.sum()),
        roi_latent_fraction=float(latent_mask.mean()),
        teacher=rows,
        training=loss_groups(training, protocol["steps"]),
        native_forecasts=forecasts,
        seed_image_differences=pair_differences,
        weights=weight_audit(results, summary) if inspect_weights else {"inspected": False},
        assumptions=dict(
            schedule="linear beta 0.0001..0.02, 1000 steps",
            prediction="epsilon",
            clipped_x0=False,
            upstream_revision="657a80268505fa9149c4df502e35aa0f5bce11e5",
        ),
        limitations=[
            "One training pair; three teacher timesteps, one noise draw each",
            "No saved multi-step sampler states or gradients; causal stage remains unresolved",
            "Color detection does not certify aircraft shape",
            "Native before/after fit criterion is unchanged",
        ],
        new_forecasts=0,
        new_training_updates=0,
        new_gpu_cost_usd=0,
        generalization_tested=False,
        delivery_adoption=False,
    )


def render(result, root, results, output):
    canvas = Image.new("RGB", (1016, 990), "#123039")
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (18, 14),
        "Saved one-step denoising checks: true future + noise is supplied (NOT forecasts)",
        fill="white",
    )
    draw.text(
        (18, 34),
        "t is a diffusion noise index, not seconds. Raw colour hits can be texture.",
        fill="#bbd4d9",
    )
    for i, step in enumerate((0, 256, 1024)):
        y = 75 + i * 285
        canvas.paste(Image.open(root / "training-target.png").convert("RGB"), (16, y))
        draw.text((16, y + 230), f"True future / update {step}", fill="white")
        for j, t in enumerate((100, 500, 900)):
            row = next(r for r in result["teacher"] if r["step"] == step and r["timestep"] == t)
            x = 270 + 245 * j
            canvas.paste(Image.open(results / row["file"]).convert("RGB"), (x, y))
            draw.text(
                (x, y + 230),
                f"t={t}: latent x0 ROI MSE {row['implied_x0_roi_mse']:.4f}",
                fill="white",
            )
            draw.text((x, y + 247), f"Noise ROI MSE {row['roi_noise_mse']:.6f}", fill="#bbd4d9")
    draw.text(
        (18, 945),
        "The conversion exposes error scale; it is NOT a new loss, sampler trace, or flight result.",
        fill="#bbd4d9",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--weights", action="store_true", help="Inspect retained float32 tensors using CPU Torch"
    )
    p.add_argument("--image", type=Path)
    a = p.parse_args()
    result = analyze(a.root, a.results, inspect_weights=a.weights)
    write(a.output, result)
    if a.image:
        render(result, a.root, a.results, a.image)
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("native_evidence_complete", "new_forecasts", "new_gpu_cost_usd")
            }
        )
    )
