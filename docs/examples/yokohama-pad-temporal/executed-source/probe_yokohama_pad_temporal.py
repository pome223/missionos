#!/usr/bin/env python3
"""Opt-in native WAM endpoint diagnostic. No executor, training or future targets."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import time

import numpy as np


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_inputs(root):
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        manifest.get("schema") != "pad_temporal_inputs.v1"
        or manifest.get("future_targets_included") is not False
    ):
        raise ValueError("Wrong temporal manifest")
    if len(manifest["samples"]) != 4:
        raise ValueError("Exactly four frozen temporal cases required")
    result = []
    for sample in manifest["samples"]:
        name = sample["id"]
        if Path(name).name != name:
            raise ValueError("Unsafe input name")
        folder = root / name
        path = folder / "request.json"
        if sha(path) != sample["request_sha256"]:
            raise ValueError("Temporal request changed")
        r = json.loads(path.read_text())
        expected = dict(
            schema="yokohama_pad_temporal_request.v1",
            frame_period_s=0.25,
            model_frame_offset=64,
            requested_horizon_sim_s=16,
            diffusion_steps=50,
            seed=42,
            candidates=[dict(id="hold", delta=[0, 0, 0, 0])],
            dispatch_allowed=False,
            model_time_alignment_verified=False,
            future_ground_truth_used_for_forecast=False,
        )
        if any(r.get(k) != v for k, v in expected.items()):
            raise ValueError("Temporal protocol changed")
        if sha(folder / "history.npz") != r["history_sha256"]:
            raise ValueError("History changed")
        with np.load(folder / "history.npz", allow_pickle=False) as z:
            a = {k: z[k] for k in z.files}
        if set(a) != {"rgb", "depth", "poses", "stamps_ns", "intrinsics", "last_depth_infinite"}:
            raise ValueError("History contains unexpected arrays")
        if a["rgb"].shape != (16, 360, 640, 3) or a["rgb"].dtype != np.uint8:
            raise ValueError("RGB history shape")
        if (
            a["depth"].shape != (16, 360, 640)
            or not np.isfinite(a["depth"]).all()
            or (a["depth"] < 0).any()
            or (a["depth"] > 500).any()
        ):
            raise ValueError("Metric depth contract")
        if (
            a["stamps_ns"].shape != (16,)
            or a["stamps_ns"][-1] != r["cutoff_stamp_ns"]
            or np.max(np.abs(np.diff(a["stamps_ns"]) / 1e9 - 0.25)) > 0.004000001
        ):
            raise ValueError("Temporal cadence mismatch")
        if (
            a["poses"].shape != (16, 4, 4)
            or not np.isfinite(a["poses"]).all()
            or not np.allclose(a["poses"], a["poses"][-1], atol=1e-5)
        ):
            raise ValueError("Camera not stationary")
        if (
            a["last_depth_infinite"].shape != (360, 640)
            or a["last_depth_infinite"].dtype != np.bool_
        ):
            raise ValueError("Unknown depth mask")
        result.append((folder, r, a))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--execute-native", action="store_true")
    p.add_argument("--upstream", type=Path)
    p.add_argument("--checkpoint", type=Path)
    p.add_argument("--adapter", type=Path)
    a = p.parse_args()
    inputs = validate_inputs(a.inputs)
    if not a.execute_native:
        print(json.dumps(dict(status="passed", cases=len(inputs), gpu_requested=False)))
        return
    if a.output.exists():
        p.error("Preserve previous native outputs")
    if __package__:
        from scripts import ship_anwm as native, ship_anwm_server as service
    else:
        import ship_anwm as native
        import ship_anwm_server as service
    a.output.mkdir(parents=True)
    model = service.NativeModel(a.upstream, a.checkpoint, motion_adapter=a.adapter)
    from anwm.diffusion import create_diffusion
    model.diffusion = create_diffusion("50")
    identity = dict(
        schema="pad_temporal_native_identity.v1",
        base_sha256=native.MODEL_SHA256,
        adapter_sha256=sha(a.adapter),
        upstream_revision=native.UPSTREAM_REVISION,
        source_sha256=sha(Path(__file__)),
        diffusion_steps=50,
        model_frame_offset=64,
        input_period_s=0.25,
        requested_horizon_sim_s=16,
        model_load_seconds=model.load_seconds,
        weights_changed=False,
        physical_time_prediction_validated=False,
    )
    (a.output / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    for folder, request, arrays in inputs:
        dest = a.output / folder.name
        dest.mkdir()
        # This internal rendering call is separate from the frozen v2 service
        # validator. The public temporal request and source hash remain bound.
        internal = dict(
            schema_version=native.MOTION_CONTRACT,
            num_timesteps=64,
            candidates=request["candidates"],
        )
        start = time.monotonic()
        value = model.predict(internal, arrays, dest)
        value.update(
            schema="pad_temporal_native_result.v1",
            request_sha256=sha(folder / "request.json"),
            request_total_seconds=time.monotonic() - start,
            requested_future_stamp_ns=request["cutoff_stamp_ns"] + 16_000_000_000,
            time_conditioning="64 frame offsets / 128, 4 Hz acquisition; unvalidated physical forecast",
            wam_inference_invoked=True,
            dispatch_invoked=False,
            aircraft_flown=False,
            model_weights_updated=False,
        )
        for f in value["forecasts"]:
            for entry in f["files"].values():
                entry.pop("png_base64", None)
        (dest / "result.json").write_text(json.dumps(value, indent=2) + "\n")
        print(
            json.dumps(
                dict(case=folder.name, request_total_seconds=value["request_total_seconds"])
            ),
            flush=True,
        )
    model.model.to("cpu")
    model.vae.to("cpu")
    gc.collect()
    native.clear_cuda_workspaces(model.torch)
    model.torch.cuda.empty_cache()
    (a.output / "shutdown.json").write_text(
        json.dumps(
            dict(cuda_allocated_bytes=model.torch.cuda.memory_allocated(), dispatch_invoked=False)
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
