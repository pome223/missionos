#!/usr/bin/env python3
"""Opt-in offline output-head adaptation of pinned ANWM; no flight authority.

The remote folder contains past histories and TRAIN targets only. Evaluation
targets are withheld on the host. This is supervised post-training, not RL.
"""

from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


HEAD_NAMES = {
    "final_layer.fuse_supervised.weight",
    "final_layer.fuse_supervised.bias",
    "final_layer.linear.weight",
    "final_layer.linear.bias",
}
ATTENTION_NAMES = {
    "final_layer.attn.mha.in_proj_weight",
    "final_layer.attn.mha.in_proj_bias",
    "final_layer.attn.mha.out_proj.weight",
    "final_layer.attn.mha.out_proj.bias",
}
INITIAL_ADAPTER_SHA256 = "7d69f3d7eccfee89cdd43b0fe22f5622da3d7cc03d2554be64f02d05c1a07222"


def validate_configuration(protocol):
    kind = protocol.get("learning_kind", "head-v1")
    if kind not in {"head-v1", "attention-v2"}:
        raise ValueError("Unknown adaptation configuration")
    expected = ["final_layer.fuse_supervised.", "final_layer.linear."]
    if kind == "attention-v2":
        expected.append("final_layer.attn.")
    if (
        protocol["steps"] != (512 if kind == "head-v1" else 2048)
        or protocol["lr"] != (0.0001 if kind == "head-v1" else 0.00005)
        or protocol["seed"] != 42
        or protocol["trainable_prefixes"] != expected
    ):
        raise ValueError("Unreviewed adaptation configuration")
    if (
        kind == "attention-v2"
        and protocol["payload_sha256"].get("initial-adapter.pt") != INITIAL_ADAPTER_SHA256
    ):
        raise ValueError("Unqualified initial adapter")
    return kind


def validate(root):
    protocol = json.loads((root / "protocol.json").read_text())
    data = json.loads((root / "dataset/dataset.json").read_text())
    if (
        data.get("qualification", {}).get("status") != "passed"
        or len(data["samples"]) != 32
        or sum(s["split"] == "train" for s in data["samples"]) != 24
        or sum(s["split"] == "test" for s in data["samples"]) != 8
    ):
        raise ValueError("Unqualified adaptation dataset")
    for name, value in protocol["payload_sha256"].items():
        if digest(root / name) != value:
            raise ValueError("Frozen payload changed: " + name)
    expected = {"dataset.json", *data["assets"]}
    actual = {
        str(p.relative_to(root / "dataset")) for p in (root / "dataset").rglob("*") if p.is_file()
    }
    if actual != expected or data["test_targets_uploaded"] is not False:
        raise ValueError("Unexpected evaluation payload")
    for name, value in data["assets"].items():
        if digest(root / "dataset" / name) != value:
            raise ValueError("Dataset asset changed")
    for sample in data["samples"]:
        if sample["input"]["file"] != f"inputs/{sample['site']}.npz":
            raise ValueError("Input asset role mismatch")
        if sample["input"]["file"] not in data["assets"]:
            raise ValueError("Missing input asset")
        if sample["split"] == "test" and (
            "training_target" in sample or f"targets/{sample['id']}.png" in expected
        ):
            raise ValueError("Evaluation target leaked")
        if sample["split"] == "train" and (
            sample.get("training_target", {}).get("file") != f"targets/{sample['id']}.png"
            or sample["training_target"]["file"] not in data["assets"]
        ):
            raise ValueError("Training target role mismatch")
    kind = validate_configuration(protocol)
    if kind == "attention-v2":
        if data.get("plan_version") != "attention-v2" or digest(
            root / "dataset/dataset.json"
        ) != protocol.get("dataset_manifest_sha256"):
            raise ValueError("Fresh evaluation sites required")
        new = np.asarray(
            [s[p] for s in data["sites"] if s["split"] == "test" for p in ("xyz", "endpoint_xyz")]
        )
        previous = np.asarray(
            [s[p] for s in data["previous_inspected_sites"] for p in ("xyz", "endpoint_xyz")]
        )
        separation = float(np.linalg.norm(new[:, None] - previous[None], axis=2).min())
        if (
            not np.isfinite(separation)
            or separation < 15
            or abs(separation - data["previous_site_separation_m"]) > 1e-8
        ):
            raise ValueError("Previously inspected evaluation spatial overlap")
    return protocol, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--execute-training", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    protocol, data = validate(root)
    if not args.execute_training:
        print(json.dumps(dict(status="passed", samples=len(data["samples"]), gpu_requested=False)))
        return
    import torch
    import ship_anwm as native
    from ship_anwm_server import NativeModel
    from paired_appearance import fill_infinite_appearance

    output = root / "results"
    output.mkdir(exist_ok=False)
    started = time.monotonic()
    model = NativeModel(root / "upstream", root / "assets/0200000.pth.tar")
    from anwm.diffusion import create_diffusion
    from anwm.projection import reproject_depth_to_other_pose_seq2seq, project_to_2d_image_seq2seq
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import transform, normalize_data

    def deadline():
        if time.monotonic() - started > protocol["model_work_seconds_max"]:
            raise TimeoutError("Frozen model-work duration exceeded")

    def state_digest(trainable):
        h = hashlib.sha256()
        for name, p in model.model.named_parameters():
            if p.requires_grad == trainable:
                h.update(name.encode())
                h.update(p.detach().cpu().contiguous().numpy().tobytes())
        return h.hexdigest()

    learning_kind = protocol.get("learning_kind", "head-v1")
    initial_adapter_sha256 = None
    if learning_kind == "attention-v2":
        initial_adapter_sha256 = digest(root / "initial-adapter.pt")
        saved = torch.load(root / "initial-adapter.pt", map_location="cpu", weights_only=True)
        if (
            saved["base_checkpoint_sha256"] != native.MODEL_SHA256
            or set(saved["state"]) != HEAD_NAMES
        ):
            raise ValueError("Initial adapter identity mismatch")
        with torch.no_grad():
            parameters = dict(model.model.named_parameters())
            for name, value in saved["state"].items():
                if value.shape != parameters[name].shape or not torch.isfinite(value).all():
                    raise ValueError("Invalid initial adapter tensor")
                parameters[name].copy_(value)

    for name, p in model.model.named_parameters():
        p.requires_grad_(any(name.startswith(prefix) for prefix in protocol["trainable_prefixes"]))
    model.vae.requires_grad_(False)
    trainable = [(n, p) for n, p in model.model.named_parameters() if p.requires_grad]
    if set(n for n, _ in trainable) != HEAD_NAMES | (
        ATTENTION_NAMES if learning_kind == "attention-v2" else set()
    ):
        raise ValueError("Unexpected trainable parameters")
    initial = {n: p.detach().cpu().clone() for n, p in trainable}
    frozen_before = state_digest(False)
    trainable_before = state_digest(True)
    identity = dict(
        total_parameters=sum(p.numel() for p in model.model.parameters()),
        trainable_parameters=sum(p.numel() for _, p in trainable),
        trainable_names=[n for n, _ in trainable],
        base_checkpoint_sha256=native.MODEL_SHA256,
        frozen_parameters_sha256=frozen_before,
        initial_head_sha256=trainable_before,
        torch_version=torch.__version__,
        gpu=torch.cuda.get_device_name(0),
        learning_kind=learning_kind,
        initial_adapter_sha256=initial_adapter_sha256,
    )
    write(output / "identity.json", identity)
    prepared = []
    conditioning = []
    contexts = {}
    torch.manual_seed(42)
    np.random.seed(42)
    for sample in data["samples"]:
        deadline()
        with np.load(root / "dataset" / sample["input"]["file"], allow_pickle=False) as a:
            arrays = {k: a[k] for k in a.files}
        if sample["site"] not in contexts:
            contexts[sample["site"]] = torch.stack(
                [transform(Image.fromarray(im)) for im in arrays["rgb"]]
            )[None].cuda()
        context = contexts[sample["site"]]
        raw_depth = arrays["depth"][-1]
        depth = np.where(
            np.isfinite(arrays["depth"]) & (arrays["depth"] > 0) & (arrays["depth"] <= 500),
            arrays["depth"],
            0,
        )
        delta = np.asarray(sample["delta"], np.float32)
        target_pose = native.action_pose(arrays["poses"][-1], delta)
        points, colors = reproject_depth_to_other_pose_seq2seq(
            arrays["intrinsics"], depth, arrays["rgb"], arrays["poses"], target_pose[None]
        )
        geometry = project_to_2d_image_seq2seq(arrays["intrinsics"], points, colors, (360, 640))[0]
        known = (
            project_to_2d_image_seq2seq(
                arrays["intrinsics"], points, [np.full_like(c, 255) for c in colors], (360, 640)
            )[0][..., 0]
            > 0
        )
        filled, mask = fill_infinite_appearance(
            geometry,
            known,
            arrays["rgb"][-1],
            raw_depth,
            arrays["intrinsics"],
            arrays["poses"][-1],
            target_pose,
        )
        if not np.array_equal(filled[known], geometry[known]) or (mask & known).any():
            raise ValueError("Appearance changed metric projection")
        projection = transform(Image.fromarray(filled))[None, None].cuda()
        if sample["split"] == "test":
            crop = (
                Image.fromarray(filled)
                .crop((80, 0, 560, 360))
                .resize((224, 224), Image.Resampling.BILINEAR)
            )
            crop.save(output / (sample["id"] + "-projection.png"))
        conditioning.append(
            dict(
                sample=sample["id"],
                known_fraction=float(known.mean()),
                appearance_added_fraction=float(mask.mean()),
                metric_geometry_changed=False,
                time_index=1,
            )
        )
        delta[:3] = normalize_data(
            delta[:3] / 3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])}
        )
        action = torch.as_tensor(delta)[None, None].cuda()
        row = dict(sample=sample, context=context, action=action, projection=projection)
        if sample["split"] == "train":
            truth = transform(
                Image.open(root / "dataset" / sample["training_target"]["file"]).convert("RGB")
            )[None].cuda()
            with torch.no_grad(), torch.amp.autocast("cuda", dtype=torch.bfloat16):
                row["x_start"] = model.vae.encode(truth).latent_dist.sample() * 0.18215
                row["x_cond"] = (model.vae.encode(context[0]).latent_dist.sample() * 0.18215)[None]
                row["x_sup"] = model.vae.encode(projection[0]).latent_dist.sample() * 0.18215
        prepared.append(row)
        print(json.dumps(dict(stage="prepared", sample=sample["id"])), flush=True)
        del points, colors
    write(output / "conditioning.json", conditioning)

    def infer(stage):
        records = []
        for row in prepared:
            sample = row["sample"]
            if sample["split"] != "test":
                continue
            deadline()
            torch.manual_seed(42)
            torch.cuda.manual_seed_all(42)
            np.random.seed(42)
            begin = time.monotonic()
            with torch.no_grad():
                result = model_forward_wrapper(
                    (model.model, model.diffusion, model.vae),
                    row["context"],
                    row["action"],
                    1,
                    28,
                    "cuda",
                    16,
                    x_supervised=row["projection"],
                )[0]
            torch.cuda.synchronize()
            pixels = (
                ((result.detach().float().cpu().permute(1, 2, 0).numpy() + 1) * 127.5)
                .round()
                .clip(0, 255)
                .astype(np.uint8)
            )
            filename = stage + "-" + sample["id"] + ".png"
            Image.fromarray(pixels).save(output / filename)
            records.append(
                dict(
                    sample=sample["id"],
                    image=filename,
                    sha256=digest(output / filename),
                    elapsed_s=time.monotonic() - begin,
                )
            )
            write(output / (stage + ".json"), records)
            print(json.dumps(dict(stage=stage, sample=sample["id"])), flush=True)
        return records

    before = infer("before")
    optimizer = torch.optim.AdamW([p for _, p in trainable], lr=protocol["lr"], weight_decay=0)
    diffusion = create_diffusion("")
    training = [r for r in prepared if r["sample"]["split"] == "train"]
    generator = np.random.default_rng(42)
    torch.manual_seed(42)
    losses = []
    training_started = time.monotonic()
    for step in range(protocol["steps"]):
        deadline()
        row = training[int(generator.integers(len(training)))]
        optimizer.zero_grad(set_to_none=True)
        with torch.amp.autocast("cuda", dtype=torch.bfloat16):
            t = torch.randint(0, diffusion.num_timesteps, (1,), device="cuda")
            kwargs = dict(
                y=row["action"][0],
                x_cond=row["x_cond"],
                rel_t=torch.tensor([1 / 128], device="cuda"),
                x_supervised=row["x_sup"],
            )
            loss = diffusion.training_losses(model.model, row["x_start"], t, kwargs)["loss"].mean()
        if not torch.isfinite(loss):
            raise ValueError("Nonfinite training loss")
        loss.backward()
        grad = torch.nn.utils.clip_grad_norm_(
            [p for _, p in trainable], 1.0, error_if_nonfinite=True
        )
        optimizer.step()
        losses.append(
            dict(
                step=step + 1,
                sample=row["sample"]["id"],
                loss=float(loss.detach()),
                gradient_norm=float(grad),
            )
        )
        if step == 0 or (step + 1) % 64 == 0:
            write(output / "training.json", losses)
            print(json.dumps(dict(stage="training", **losses[-1])), flush=True)
    training_seconds = time.monotonic() - training_started
    final_head = {n: p.detach().cpu().clone() for n, p in trainable}
    change_l2 = sum(float((final_head[n] - initial[n]).square().sum()) for n in initial) ** 0.5
    if change_l2 <= 0 or state_digest(False) != frozen_before:
        raise ValueError("Head did not update or frozen base changed")
    torch.save(
        dict(
            schema_version="anwm_head_adaptation.v1",
            base_checkpoint_sha256=native.MODEL_SHA256,
            state=final_head,
            protocol_sha256=digest(root / "protocol.json"),
        ),
        output / "adapter.pt",
    )
    with torch.no_grad():
        for name, param in trainable:
            param.copy_(initial[name])
    saved = torch.load(output / "adapter.pt", map_location="cpu", weights_only=True)
    with torch.no_grad():
        for name, param in trainable:
            param.copy_(saved["state"][name])
    reloaded_hash = state_digest(True)
    if reloaded_hash == trainable_before:
        raise ValueError("Reloaded head remained unchanged")
    # Reopened serialized tensors are independently checked before inference.
    saved_hash = hashlib.sha256()
    for name, param in trainable:
        value = saved["state"][name]
        if not torch.isfinite(value).all() or not torch.equal(param.detach().cpu(), value):
            raise ValueError("Saved adapter reload mismatch")
        saved_hash.update(name.encode())
        saved_hash.update(value.contiguous().numpy().tobytes())
    if saved_hash.hexdigest() != reloaded_hash:
        raise ValueError("Saved adapter hash mismatch")
    write(
        output / "saved-head-audit.json",
        dict(
            status="saved_head_reopened",
            head_sha256=reloaded_hash,
            all_tensors_finite=True,
            parameters=identity["trainable_parameters"],
            adapter_sha256=digest(output / "adapter.pt"),
            protocol_sha256=digest(root / "protocol.json"),
        ),
    )
    after = infer("after")
    write(
        output / "summary.json",
        dict(
            schema_version="anwm_head_training_receipt.v1",
            **identity,
            steps=len(losses),
            training_seconds=training_seconds,
            head_change_l2=change_l2,
            final_head_sha256=reloaded_hash,
            frozen_base_unchanged=True,
            checkpoint_reloaded=True,
            adapter_sha256=digest(output / "adapter.pt"),
            before=before,
            after=after,
            max_cuda_allocated_bytes=torch.cuda.max_memory_allocated(),
            protocol_sha256=digest(root / "protocol.json"),
            training_targets_uploaded=True,
            test_targets_uploaded=False,
            flight_invoked=False,
            vla_invoked=False,
            inference_calls=len(before) + len(after),
            total_model_seconds=time.monotonic() - started,
        ),
    )
    print("ADAPTATION_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
