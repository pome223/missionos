#!/usr/bin/env python3
"""Opt-in loopback AeroVLA service; loaded once, no aircraft connection."""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image

if __package__:
    from . import ship_aerovla as native
else:
    import ship_aerovla as native


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class NativeModel:
    def __init__(self, base, adapter, *, constrain_action_format=False, inspection_level_flight=False):
        if inspection_level_flight and not constrain_action_format:
            raise ValueError("Inspection level flight requires action-format constraints")
        for name, sha in {**native.WEIGHTS, **native.CUSTOM_CODE}.items():
            if native.digest(base / name) != sha:
                raise ValueError("Unreviewed base weights or custom code")
        if native.digest(adapter / "adapter_model.safetensors") != native.ADAPTER_SHA256:
            raise ValueError("Unreviewed flight adapter")
        import torch
        from transformers import AutoImageProcessor, AutoModelForVision2Seq, AutoTokenizer
        from peft import PeftModel

        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError("CUDA bfloat16 required")
        started = time.monotonic()
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(
            base, trust_remote_code=True, local_files_only=True
        )
        self.grammar = (
            native.ActionGrammar(self.tokenizer, vertical_bins=(47, 51) if inspection_level_flight else None)
            if constrain_action_format else None
        )
        self.processor = AutoImageProcessor.from_pretrained(
            base, trust_remote_code=True, local_files_only=True
        )
        self.model = AutoModelForVision2Seq.from_pretrained(
            base,
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
            local_files_only=True,
        )
        self.model.resize_token_embeddings(len(self.tokenizer))
        self.model = (
            PeftModel.from_pretrained(self.model, adapter, is_trainable=False).to("cuda").eval()
        )
        torch.cuda.synchronize()
        self.load_seconds = time.monotonic() - started

    def predict(self, prompt, images):
        torch = self.torch
        mosaic = Image.new("RGB", (224, 448))
        for i, image in enumerate(images):
            mosaic.paste(image.resize((224, 224), Image.Resampling.BICUBIC), (0, 224 * i))
        buffer = BytesIO()
        mosaic.save(buffer, format="PNG")
        started = time.time()
        inputs = self.tokenizer([prompt], return_tensors="pt", padding=True)
        inputs["pixel_values"] = self.processor(images=mosaic, return_tensors="pt")[
            "pixel_values"
        ].to(self.model.dtype)
        inputs = {k: v.to("cuda") for k, v in inputs.items()}
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            tokens = self.model.generate(
                **inputs,
                max_new_tokens=20,
                do_sample=False,
                eos_token_id=[self.tokenizer.eos_token_id],
                prefix_allowed_tokens_fn=(
                    self.grammar.bind(inputs["input_ids"].shape[1]) if self.grammar else None
                ),
            )
        torch.cuda.synchronize()
        completed = time.time()
        generated = tokens[0, inputs["input_ids"].shape[1] :]
        return {
            "generated_text": self.tokenizer.decode(generated, skip_special_tokens=False),
            "generated_token_ids": generated.tolist(),
            "input_token_ids": inputs["input_ids"].tolist(),
            "pixel_values_shape": list(inputs["pixel_values"].shape),
            "mosaic_sha256": hashlib.sha256(buffer.getvalue()).hexdigest(),
            "mosaic_rgb_sha256": hashlib.sha256(mosaic.tobytes()).hexdigest(),
            "started_at_unix_s": started,
            "completed_at_unix_s": completed,
            "inference_seconds": completed - started,
            "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(),
        }, buffer.getvalue()


def decode_request(payload):
    if set(payload) != {"request", "images_base64"}:
        raise ValueError("Unexpected request fields")
    request = payload["request"]
    expected = {
        "schema_version",
        "run_id",
        "world_sha256",
        "plan_sha256",
        "request_id",
        "input_row_sha256",
        "observed_at_s",
        "sensor_stamp_s",
        "images_sha256",
        "prompt",
        "direction_source",
        "future_ground_truth_used",
        "dispatch_allowed",
    }
    if (
        set(request) != expected
        or request["schema_version"] != "ship_aerovla_live_request.v1"
        or request["dispatch_allowed"] is not False
        or request["future_ground_truth_used"] is not False
        or request["direction_source"] != "approved_goal_and_px4_ego_pose"
        or not isinstance(request["prompt"], str)
        or not 1 <= len(request["prompt"]) <= 500
        or set(payload["images_base64"]) != {"rgb", "down"}
        or set(request["images_sha256"]) != {"rgb", "down"}
    ):
        raise ValueError("Unsupported live request")
    images, data = [], {}
    for key in ("rgb", "down"):
        data[key] = base64.b64decode(payload["images_base64"][key], validate=True)
        if hashlib.sha256(data[key]).hexdigest() != request["images_sha256"][key]:
            raise ValueError("Image hash mismatch")
        with Image.open(BytesIO(data[key])) as image:
            if image.size != (640, 360) or image.mode != "RGB" or image.format != "PNG":
                raise ValueError("Unexpected dual-view image")
            images.append(image.copy())
    return request, images, data


def make_handler(model, identity, root, *, exit_after_request=False):
    seen = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send_json(
                200 if self.path == "/health" else 404,
                identity if self.path == "/health" else {"error": "not_found"},
            )

        def do_POST(self):
            job = None
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/infer" or not 0 < length <= 2_000_000:
                    raise ValueError("Unsupported request path or size")
                payload = json.loads(self.rfile.read(length))
                request, images, raw = decode_request(payload)
                request_hash = digest(request)
                if request["request_id"] in seen:
                    raise ValueError("Repeated inference request")
                seen.add(request["request_id"])
                # Use a host-generated hash as the directory, never a model/user path.
                job = root / request_hash
                job.mkdir(exist_ok=False)
                (job / "request.json").write_text(json.dumps(request, indent=2))
                for key, data in raw.items():
                    (job / f"{key}.png").write_bytes(data)
                prediction, mosaic = model.predict(request["prompt"], images)
                value = {
                    "schema_version": "ship_aerovla_live_invocation.v1",
                    "request_sha256": request_hash,
                    "service": identity,
                    "input_images_sha256": request["images_sha256"],
                    **prediction,
                    "vla_inference_invoked": True,
                    "dispatch_invoked": False,
                    "physical_execution_invoked": False,
                }
                (job / "actual-mosaic.png").write_bytes(mosaic)
                (job / "result.json").write_text(json.dumps(value, indent=2))
                self.send_json(200, value)
                if exit_after_request:
                    # Finish the HTTP response before releasing this process's
                    # CUDA allocation. A new flight needs a newly pinned service.
                    self.wfile.flush()
                    raise SystemExit(0)
            except Exception as exc:
                error = {"error": f"{type(exc).__name__}:{exc}"}
                if job:
                    (job / "error.json").write_text(json.dumps(error))
                self.send_json(400, error)

    return Handler


def serve(base, adapter, output, port, *, exit_after_request=False, constrain_action_format=False,
          inspection_level_flight=False):
    output.mkdir(parents=True, exist_ok=False)
    model = NativeModel(base, adapter, constrain_action_format=constrain_action_format,
                        inspection_level_flight=inspection_level_flight)
    warmup, mosaic = model.predict(
        "<image>\nFly straight ahead and find the target.\nAction: ",
        [Image.new("RGB", (640, 360)) for _ in range(2)],
    )
    (output / "warmup.json").write_text(
        json.dumps({**warmup, "purpose": "synthetic_service_warmup_no_flight"}, indent=2)
    )
    (output / "warmup-mosaic.png").write_bytes(mosaic)
    identity = {
        "schema_version": "ship_aerovla_service.v1",
        "session_id": uuid4().hex,
        "base_revision": native.BASE_REVISION,
        "adapter_revision": native.ADAPTER_REVISION,
        "adapter_sha256": native.ADAPTER_SHA256,
        "upstream_revision": native.UPSTREAM_REVISION,
        "weights_sha256": native.WEIGHTS,
        "custom_code_sha256": native.CUSTOM_CODE,
        "runtime_sha256": {
            p.name: native.digest(p)
            for p in (
                Path(__file__),
                Path(native.__file__),
                Path(native.__file__).with_name("ship_anwm.py"),
            )
        },
        "gpu": model.torch.cuda.get_device_name(0),
        "torch_version": model.torch.__version__,
        "load_seconds": model.load_seconds,
        "warmup_completed": True,
        "dispatch_capability": False,
        "exit_after_request": exit_after_request,
        "decoding_policy": model.grammar.policy if model.grammar else "unconstrained_greedy.v1",
        "inspection_level_flight": inspection_level_flight,
        "vertical_bin_range": [47, 51] if inspection_level_flight else [0, 98],
    }
    (output / "service.json").write_text(json.dumps(identity, indent=2))
    server = HTTPServer(
        ("127.0.0.1", port),
        make_handler(model, identity, output, exit_after_request=exit_after_request),
    )
    print(
        json.dumps({"status": "ready", "port": port, "session_id": identity["session_id"]}),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18117)
    parser.add_argument("--exit-after-request", action="store_true")
    parser.add_argument("--constrain-action-format", action="store_true")
    parser.add_argument("--inspection-level-flight", action="store_true")
    args = parser.parse_args()
    if args.inspection_level_flight and not args.constrain_action_format:
        parser.error("--inspection-level-flight requires --constrain-action-format")
    serve(
        args.base, args.adapter, args.output, args.port,
        exit_after_request=args.exit_after_request,
        constrain_action_format=args.constrain_action_format,
        inspection_level_flight=args.inspection_level_flight,
    )
