#!/usr/bin/env python3
"""Actual flight-adapted AeroVLA inference on bound ship camera observations.

Outputs are proposals only. This tool has no PX4 connection or dispatch path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

import numpy as np
from PIL import Image

if __package__:
    from .ship_anwm import NED_FROM_ENU, asset, digest, rotation, write_json
else:
    from ship_anwm import NED_FROM_ENU, asset, digest, rotation, write_json

BASE_REVISION = "47a0ec7fc4ec123775a391911046cf33cf9ed83f"
ADAPTER_REVISION = "196f2f3253b69df6e90ac10b6ae041c7b3a9569e"
UPSTREAM_REVISION = "2c5ae0987a484ab92f00dd9d9ed493cb3e98e492"
WEIGHTS = {
    "model-00001-of-00003.safetensors": "10d8636256018712c5e5c823d12e22b5797f99bb721bd123bf6bf2379892be85",
    "model-00002-of-00003.safetensors": "2050b14f21d48904d269f48d5a980fecea87cd7b36641d9b0f015e72d1fe216a",
    "model-00003-of-00003.safetensors": "ea65305a1577f36f721965bf84c8caec0a948ce7ce84d754701637376c531fef",
}
ADAPTER_SHA256 = "05b11cf66d2a7bba9af3fefc5afa9e9af4fe1f2a567ddeca4fca73192060b846"
CUSTOM_CODE = {
    "configuration_prismatic.py": "68cc5ae34f1b46af3168d8d479cb81bb776965653453fd904aa8eefb6c8f9f68",
    "modeling_prismatic.py": "9ce241c5ca09a4bed73654d0ca509baaff0fc5887d0bdd62e360ca8254f7794a",
    "processing_prismatic.py": "06563586bbc73d36501a14a231a3174904ede830d33174d0c625ece8e931ef1e",
}


class ActionGrammar:
    """Constrain generation syntax, leaving all native bins and LAND available.

    This is a generation-time mask, never a repair of returned model text.
    Numeric motion, clearance, approval and terminal-proposal guards still apply.
    The pinned Llama vocabulary is validated before any prediction is made.
    """

    policy = "aerovla_action_grammar.v1"

    def __init__(self, tokenizer):
        vocab = tokenizer.get_vocab()
        pieces = {**{str(i): str(i) for i in range(10)}, "▁": " ", "L": "L", "AND": "AND"}
        self.pieces = {vocab[k]: text for k, text in pieces.items()}
        self.eos = tokenizer.eos_token_id
        self.numbers = {str(i) for i in range(99)}
        if self.eos in self.pieces or len(self.pieces) != len(pieces):
            raise ValueError("Unsupported action-grammar vocabulary")
        for text in [*sorted(self.numbers), "0 49 98", "LAND", "98 49 0 LAND"]:
            ids = []
            for part in re.findall(r"AND|.", text):
                ids.append(vocab["▁" if part == " " else part])
            if tokenizer.decode(ids, skip_special_tokens=False).strip() != text:
                raise ValueError("Unsupported action-grammar token decoding")

    def valid_prefix(self, text, *, complete=False):
        # At most one leading separator; no repeated separators or leading zeroes.
        if text.startswith(" "):
            text = text[1:]
        if text in ("", "L", "LAND"):
            return text == "LAND" if complete else True
        parts = text.split(" ")
        if len(parts) > 4 or any(p not in self.numbers for p in parts[:-1]):
            return False
        last = parts[-1]
        if len(parts) == 4:
            return last == "LAND" if complete else "LAND".startswith(last)
        if complete:
            return len(parts) == 3 and last in self.numbers
        return any(n.startswith(last) for n in self.numbers)

    def allowed(self, generated):
        if any(token not in self.pieces for token in generated):
            raise ValueError("Invalid generated action prefix")
        text = "".join(self.pieces[token] for token in generated)
        if not self.valid_prefix(text):
            raise ValueError("Invalid generated action prefix")
        allowed = [token for token, part in self.pieces.items() if self.valid_prefix(text + part)]
        if self.valid_prefix(text, complete=True):
            allowed.append(self.eos)
        if not allowed:
            raise ValueError("No valid action continuation")
        return sorted(allowed)

    def bind(self, input_length):
        def allowed_tokens(batch_id, tokens):
            if batch_id != 0:
                raise ValueError("Action grammar requires a single observation")
            return self.allowed(tokens.tolist()[input_length:])

        return allowed_tokens


def parse_proposal(text):
    # Unlike the upstream convenience parser, malformed text must not become a
    # zero-action / LAND instruction, and out-of-range bins are not clamped.
    value = text.strip().replace("</s>", "").replace("<pad>", "").strip()
    if value in ("LAND", "<LAND>"):
        return {"kind": "land_proposal", "dispatch_allowed": False}
    stop_proposed = value.endswith(" LAND")
    if stop_proposed:
        value = value[:-5].strip()
    match = re.fullmatch(
        r"(?:\[\s*)?(\d{1,2})\s*[, ]\s*(\d{1,2})\s*[, ]\s*(\d{1,2})(?:\s*\])?", value
    )
    if not match:
        return {"kind": "rejected", "reason": "malformed_action_bins", "dispatch_allowed": False}
    bins = [int(v) for v in match.groups()]
    if any(v > 98 for v in bins):
        return {"kind": "rejected", "reason": "action_bin_out_of_range", "dispatch_allowed": False}
    fwd, down, yaw = bins[0] / 98 * 5, bins[1] / 98 * 10 - 5, bins[2] / 98 * 2.2 - 1.1
    return {
        "kind": "motion_proposal",
        "stop_proposed": stop_proposed or bins == [0, 49, 49],
        "bins": bins,
        "forward_m": fwd,
        "down_m": down,
        "yaw_delta_rad": yaw,
        "dispatch_allowed": False,
        "upstream_controller_translates_horizontal": abs(yaw) < 0.25,
        "requested_body_frd_delta_m_rad": [fwd * math.cos(yaw), fwd * math.sin(yaw), down, yaw],
        "nominal_action_duration_s": None,
    }


def prepare(capture, output):
    source = json.loads(capture.read_text())
    if source.get("schema_version") != "ship_anwm_capture.v1" or source.get(
        "history_indices"
    ) != list(range(16)):
        raise ValueError("A bound native observation is required")
    frame = source["frames"][15]
    config = json.loads((capture.parent.parent / "config.json").read_text())
    if any(config[k] != source[k] for k in ("run_id", "world_sha256", "plan_sha256")):
        raise ValueError("Mission and observation differ")
    position = NED_FROM_ENU @ np.asarray(frame["vehicle_position_enu_m"])
    body_to_ned = NED_FROM_ENU @ rotation(frame["vehicle_quaternion_wxyz"]) @ np.diag([1, -1, -1])
    target = np.array([config["goal_north_m"], 0, -0.05])
    relative = body_to_ned.T @ (target - position)
    angle = math.degrees(math.atan2(relative[1], relative[0]))
    if abs(angle) <= 15:
        direction = "straight ahead "
    elif 15 < angle <= 60:
        direction = "forward-right "
    elif -60 <= angle < -15:
        direction = "forward-left "
    elif 60 < angle <= 120:
        direction = "to your right "
    elif -120 <= angle < -60:
        direction = "to your left "
    else:
        direction = "to your right rear " if angle > 0 else "to your left rear "
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for key in ("rgb", "down"):
        data = asset(capture.parent, frame["assets"][key])
        image = np.frombuffer(data, np.uint8).reshape(360, 640, 3)
        path = output / f"{key}.png"
        Image.fromarray(image).save(path)
        hashes[key] = digest(path)
    request = {
        "schema_version": "ship_aerovla_request.v1",
        "run_id": source["run_id"],
        "world_sha256": source["world_sha256"],
        "plan_sha256": source["plan_sha256"],
        "source_frame_index": 15,
        "simulation_time_ns": frame["simulation_time_ns"],
        "source_frame_sha256": hashlib.sha256(
            json.dumps(frame, sort_keys=True).encode()
        ).hexdigest(),
        "images_sha256": hashes,
        "prompt": f"<image>\nFly {direction}and find the target. The delivery pad on the ground at the end of the corridor.\nAction: ",
        "direction_source": "approved_goal_and_simulator_ego_pose",
        "future_ground_truth_used": False,
        "dispatch_allowed": False,
    }
    write_json(output / "request.json", request)


def run(request_path, output, base, adapter):
    request = json.loads(request_path.read_text())
    if (
        request.get("schema_version") != "ship_aerovla_request.v1"
        or request.get("dispatch_allowed") is not False
        or request.get("future_ground_truth_used") is not False
    ):
        raise ValueError("Unsupported VLA proposal request")
    images = []
    for key in ("rgb", "down"):
        path = request_path.parent / f"{key}.png"
        if digest(path) != request["images_sha256"][key]:
            raise ValueError("VLA input image changed")
        with Image.open(path) as image:
            if image.size != (640, 360) or image.mode != "RGB":
                raise ValueError("Unexpected native VLA image")
            images.append(image.resize((224, 224), Image.Resampling.BICUBIC))
    for name, sha in WEIGHTS.items():
        if digest(base / name) != sha:
            raise ValueError("Unreviewed base model weights")
    for name, sha in CUSTOM_CODE.items():
        if digest(base / name) != sha:
            raise ValueError("Unreviewed custom model code")
    if digest(adapter / "adapter_model.safetensors") != ADAPTER_SHA256:
        raise ValueError("Unreviewed flight adapter")
    import torch
    from transformers import AutoImageProcessor, AutoModelForVision2Seq, AutoTokenizer
    from peft import PeftModel

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("CUDA bfloat16 required")
    output.mkdir(parents=True, exist_ok=False)
    mosaic = Image.new("RGB", (224, 448))
    mosaic.paste(images[0], (0, 0))
    mosaic.paste(images[1], (0, 224))
    mosaic.save(output / "actual-mosaic.png")
    started = time.time()
    tokenizer = AutoTokenizer.from_pretrained(base, trust_remote_code=True, local_files_only=True)
    processor = AutoImageProcessor.from_pretrained(
        base, trust_remote_code=True, local_files_only=True
    )
    model = AutoModelForVision2Seq.from_pretrained(
        base,
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        trust_remote_code=True,
        local_files_only=True,
    )
    model.resize_token_embeddings(len(tokenizer))
    model = PeftModel.from_pretrained(model, adapter, is_trainable=False).to("cuda").eval()
    inputs = tokenizer([request["prompt"]], return_tensors="pt", padding=True)
    inputs["pixel_values"] = processor(images=mosaic, return_tensors="pt")["pixel_values"].to(
        model.dtype
    )
    inputs = {k: v.to("cuda") for k, v in inputs.items()}
    torch.cuda.synchronize()
    loaded = time.time()
    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        tokens = model.generate(
            **inputs, max_new_tokens=20, do_sample=False, eos_token_id=[tokenizer.eos_token_id]
        )
    torch.cuda.synchronize()
    completed = time.time()
    generated = tokens[0, inputs["input_ids"].shape[1] :]
    text = tokenizer.decode(generated, skip_special_tokens=False)
    write_json(
        output / "result.json",
        {
            "schema_version": "ship_aerovla_invocation.v1",
            "request_sha256": digest(request_path),
            "runtime_sha256": digest(Path(__file__)),
            "base_revision": BASE_REVISION,
            "adapter_revision": ADAPTER_REVISION,
            "adapter_sha256": ADAPTER_SHA256,
            "upstream_revision": UPSTREAM_REVISION,
            "load_seconds": loaded - started,
            "inference_seconds": completed - loaded,
            "started_at_unix_s": started,
            "completed_at_unix_s": completed,
            "mosaic_sha256": digest(output / "actual-mosaic.png"),
            "pixel_values_shape": list(inputs["pixel_values"].shape),
            "input_token_ids": inputs["input_ids"].tolist(),
            "generated_token_ids": generated.tolist(),
            "generated_text": text,
            "proposal": parse_proposal(text),
            "gpu": torch.cuda.get_device_name(0),
            "torch_version": torch.__version__,
            "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(),
            "vla_inference_invoked": True,
            "dispatch_invoked": False,
            "flight_improvement_established": False,
            "physical_execution_invoked": False,
        },
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("prepare", "run"))
    p.add_argument("--capture", type=Path)
    p.add_argument("--request", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--base", type=Path)
    p.add_argument("--adapter", type=Path)
    a = p.parse_args()
    if a.command == "prepare":
        prepare(a.capture, a.output)
    else:
        run(a.request, a.output, a.base, a.adapter)
    print(json.dumps({"status": "completed", "command": a.command}))
