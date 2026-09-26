#!/usr/bin/env python3
"""Loaded-once, loopback-only ANWM service for stationary candidate views."""

from __future__ import annotations
import argparse
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import numpy as np
from PIL import Image

if __package__:
    from . import ship_anwm as native
else:
    import ship_anwm as native


class NativeModel:
    def __init__(self, upstream, checkpoint, *, cpu_between_requests=False):
        revision = subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        if revision != native.UPSTREAM_REVISION or native.digest(checkpoint) != native.MODEL_SHA256:
            raise ValueError("Unreviewed ANWM source or checkpoint")
        subprocess.run(["git", "-C", str(upstream), "diff", "--quiet", "HEAD", "--"], check=True)
        import torch

        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError("CUDA bfloat16 required")
        sys.path.insert(0, str(upstream))
        from anwm.diffusion import create_diffusion
        from anwm.model import CDiT_models
        from diffusers import AutoencoderKL

        self.torch = torch
        self.cpu_between_requests = cpu_between_requests
        device = "cpu" if cpu_between_requests else "cuda"
        started = time.monotonic()
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        with torch.serialization.safe_globals([argparse.Namespace]):
            state = torch.load(checkpoint, map_location="cpu", mmap=True, weights_only=True)
        if tuple(state["ema"]["pos_embed"].shape) != (17, 196, 1152):
            raise ValueError("Unexpected released context slots")
        self.model = CDiT_models["CDiT-XL/2"](context_size=16, input_size=28, in_channels=4)
        self.model.load_state_dict(state["ema"], strict=True)
        del state
        self.model = self.model.eval().to(device)
        self.vae = (
            AutoencoderKL.from_pretrained(
                "stabilityai/sd-vae-ft-ema", revision=native.VAE_REVISION, use_safetensors=True
            )
            .eval()
            .to(device)
        )
        self.diffusion = create_diffusion("250")
        torch.cuda.synchronize()
        self.load_seconds = time.monotonic() - started

    def predict(self, request, arrays, output):
        if not self.cpu_between_requests:
            return self._predict(request, arrays, output)
        try:
            self.model.to("cuda")
            self.vae.to("cuda")
            return self._predict(request, arrays, output)
        finally:
            self.model.to("cpu")
            self.vae.to("cpu")
            self.torch.cuda.empty_cache()

    def _predict(self, request, arrays, output):
        from anwm.projection import (
            project_to_2d_image_seq2seq,
            reproject_depth_to_other_pose_seq2seq,
        )
        from anwm.rollout import model_forward_wrapper
        from anwm.utils import normalize_data, transform

        torch = self.torch
        context = torch.stack([transform(Image.fromarray(im)) for im in arrays["rgb"]])[None].to(
            "cuda"
        )
        forecasts = []
        torch.cuda.reset_peak_memory_stats()
        for candidate in request["candidates"]:
            torch.manual_seed(42)
            torch.cuda.manual_seed_all(42)
            np.random.seed(42)
            begin = time.monotonic()
            delta = np.asarray(candidate["delta"], dtype=np.float32)
            target = native.lateral_pose(arrays["poses"][-1], float(delta[1]))
            points, colors = reproject_depth_to_other_pose_seq2seq(
                arrays["intrinsics"], arrays["depth"], arrays["rgb"], arrays["poses"], target[None]
            )
            projected = project_to_2d_image_seq2seq(
                arrays["intrinsics"], points, colors, (360, 640)
            )[0]
            projection = transform(Image.fromarray(projected))[None, None].to("cuda")
            delta[:3] = normalize_data(
                delta[:3] / 3.30, {"min": np.array([-2.5, -4, -3]), "max": np.array([5, 4, 3])}
            )
            with torch.no_grad():
                prediction = model_forward_wrapper(
                    (self.model, self.diffusion, self.vae),
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
            files = {}
            for kind, tensor in (("prediction", prediction), ("projection", projection[0, 0])):
                rgb = (
                    ((tensor.detach().cpu().float().permute(1, 2, 0).numpy() + 1) * 127.5)
                    .round()
                    .clip(0, 255)
                    .astype(np.uint8)
                )
                path = output / f"{candidate['id']}-{kind}.png"
                Image.fromarray(rgb).save(path)
                files[kind] = {
                    "file": path.name,
                    "sha256": native.digest(path),
                    "png_base64": base64.b64encode(path.read_bytes()).decode(),
                }
            forecasts.append(
                {"candidate": candidate, "elapsed_s": time.monotonic() - begin, "files": files}
            )
        return {
            "forecasts": forecasts,
            "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(),
            "gpu": torch.cuda.get_device_name(0),
            "torch_version": torch.__version__,
        }


def make_handler(model, identity, root):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send(
                200 if self.path == "/health" else 404,
                identity if self.path == "/health" else {"error": "not_found"},
            )

        def do_POST(self):
            job = None
            try:
                self.connection.settimeout(90)
                length = int(self.headers.get("Content-Length", 0))
                if self.path != "/infer" or not 0 < length <= 24_000_000:
                    raise ValueError("Invalid request size/path")
                payload = json.loads(self.rfile.read(length))
                if set(payload) != {"request_base64", "history_base64", "request_id"}:
                    raise ValueError("Unexpected fields")
                request_id = payload["request_id"]
                if (
                    not isinstance(request_id, str)
                    or len(request_id) != 32
                    or any(c not in "0123456789abcdef" for c in request_id)
                ):
                    raise ValueError("Invalid request identity")
                job = root / request_id
                job.mkdir()
                for key, filename in [
                    ("request_base64", "request.json"),
                    ("history_base64", "history.npz"),
                ]:
                    (job / filename).write_bytes(base64.b64decode(payload[key], validate=True))
                request, arrays = native.validate(job / "request.json")
                started = time.time()
                predictions = model.predict(request, arrays, job)
                result = {
                    "schema_version": "ship_anwm_static_response.v1",
                    "identity": identity,
                    "request_id": request_id,
                    "request_sha256": native.digest(job / "request.json"),
                    "history_sha256": request["history_sha256"],
                    "started_at_unix_s": started,
                    "completed_at_unix_s": time.time(),
                    "wam_inference_invoked": predictions.get("fixture") is not True,
                    "dispatch_allowed": False,
                    "physical_execution_invoked": False,
                    **predictions,
                }
                native.write_json(job / "response.json", result)
                self.send(200, result)
            except Exception as exc:
                error = {"error": str(exc)}
                if job is not None and job.is_dir():
                    native.write_json(job / "failure.json", error)
                self.send(400, error)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--upstream", type=Path, required=True)
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, default=18118)
    p.add_argument("--cpu-between-requests", action="store_true")
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    model = NativeModel(
        args.upstream.resolve(),
        args.checkpoint.resolve(),
        cpu_between_requests=args.cpu_between_requests,
    )
    identity = {
        "schema_version": "ship_anwm_static_service.v1",
        "session_id": uuid4().hex,
        "model_revision": native.MODEL_REVISION,
        "checkpoint_sha256": native.MODEL_SHA256,
        "upstream_revision": native.UPSTREAM_REVISION,
        "vae_revision": native.VAE_REVISION,
        "server_sha256": native.digest(Path(__file__)),
        "helper_sha256": native.digest(Path(native.__file__)),
        "diffusion_steps": 250,
        "load_seconds": model.load_seconds,
        "cpu_between_requests": args.cpu_between_requests,
    }
    native.write_json(args.output / "identity.json", identity)
    print("READY", flush=True)
    HTTPServer(("127.0.0.1", args.port), make_handler(model, identity, args.output)).serve_forever()


if __name__ == "__main__":
    main()
