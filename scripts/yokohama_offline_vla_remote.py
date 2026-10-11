"""One opt-in native warmup and saved image-pair comparison; no vehicle access.

Run under an independent preparation/comparison deadline supervisor. This tool
has no cloud provisioning, download, retry, dispatch or flight capability.
"""

import argparse
import base64
import hashlib
from pathlib import Path
import time
from uuid import uuid4


def write(path, value):
    if __package__:
        from .yokohama_offline_vla_lifecycle import write_json
    else:
        from yokohama_offline_vla_lifecycle import write_json

    write_json(path, value)


def deadline_check(deadline):
    if time.time() >= deadline:
        raise TimeoutError("Stage deadline reached")


def run(base, adapter, inputs, output, preparation_deadline, comparison_deadline):
    from PIL import Image

    if __package__:
        from .yokohama_offline_vla_lifecycle import DurableEvents
        from .ship_aerovla_server import NativeModel
        from . import ship_aerovla as native
        from .yokohama_offline_vla_pair import GENERATION, PROMPT, model_payload, analyze_raw
    else:
        from yokohama_offline_vla_lifecycle import DurableEvents
        from ship_aerovla_server import NativeModel
        import ship_aerovla as native
        from yokohama_offline_vla_pair import GENERATION, PROMPT, model_payload, analyze_raw

    output.mkdir(exist_ok=True)
    events = DurableEvents(output, display=lambda value: print(value, flush=True))
    counts = {
        "warmup_started": 0,
        "warmup_completed": 0,
        "comparison_started": 0,
        "comparison_completed": 0,
    }
    write(output / "counts.json", counts)
    deadline_check(preparation_deadline)
    payloads = []
    for index in (1, 2):
        case = inputs / f"case-{index:02d}"
        payload = model_payload({k: (case / f"{k}.png").read_bytes() for k in ("rgb", "down")})
        if (case / "prompt.txt").read_text() != PROMPT:
            raise ValueError("Prepared prompt changed")
        payloads.append(payload)
    if payloads[0]["images"] == payloads[1]["images"]:
        raise ValueError("Image pair must differ")
    model = NativeModel(base, adapter)  # default: no generation-time action mask
    if model.grammar is not None or model.cpu_between_requests:
        raise ValueError("Unexpected native generation settings")
    if model.torch.__version__ != "2.9.1+cu129" or "L4" not in model.torch.cuda.get_device_name(0):
        raise ValueError("Native runtime differs from qualified Torch/L4 environment")
    deadline_check(preparation_deadline)
    identity = {
        "session_id": uuid4().hex,
        "base_revision": native.BASE_REVISION,
        "adapter_revision": native.ADAPTER_REVISION,
        "weights_sha256": native.WEIGHTS,
        "custom_code_sha256": native.CUSTOM_CODE,
        "adapter_sha256": native.ADAPTER_SHA256,
        "native_weight_and_custom_code_hashes_checked": True,
        "gpu": model.torch.cuda.get_device_name(0),
        "torch_version": model.torch.__version__,
        "generation_settings": GENERATION,
        "prompt": PROMPT,
        "load_seconds": model.load_seconds,
        "dispatch_capability": False,
        "runtime_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parent.glob("*.py")
        },
    }
    write(output / "identity.json", identity)
    counts["warmup_started"] = 1
    write(output / "counts.json", counts)
    warmup, mosaic = model.predict(PROMPT, [Image.new("RGB", (640, 360)) for _ in range(2)])
    write(output / "warmup.json", {**warmup, "purpose": "synthetic_warmup_no_dispatch"})
    (output / "warmup-mosaic.png").write_bytes(mosaic)
    counts["warmup_completed"] = 1
    write(output / "counts.json", counts)
    deadline_check(preparation_deadline)
    ready = time.time()
    write(output / "ready.json", {"ready_unix_s": ready})
    events.emit("model_ready", ready_unix_s=ready)
    # Match the guest supervisor: readiness starts the 45-second comparison allowance.
    deadline = min(comparison_deadline, ready + 45)
    receipts = []
    for index, payload in enumerate(payloads, 1):
        deadline_check(deadline)
        request = {
            "prompt": payload["prompt"],
            "images_base64": {
                k: base64.b64encode(v).decode() for k, v in payload["images"].items()
            },
        }
        write(output / f"request-{index:02d}.json", request)
        counts["comparison_started"] += 1  # persist before invoking, never retry
        write(output / "counts.json", counts)
        import io

        images = [Image.open(io.BytesIO(payload["images"][k])).copy() for k in ("rgb", "down")]
        response, mosaic = model.predict(payload["prompt"], images)
        write(output / f"raw-response-{index:02d}.json", response)
        (output / f"mosaic-{index:02d}.png").write_bytes(mosaic)
        counts["comparison_completed"] += 1
        write(output / "counts.json", counts)
        receipt = {
            "case_index": index,
            "session_id": identity["session_id"],
            "input_images_sha256": {
                k: hashlib.sha256(v).hexdigest() for k, v in payload["images"].items()
            },
            "raw_response": response,
            "analysis": analyze_raw(response["generated_text"]),
        }
        write(output / f"receipt-{index:02d}.json", receipt)
        receipts.append(receipt)
        deadline_check(deadline)
    write(
        output / "result.json",
        {
            "status": "completed_native_saved_image_pair",
            "receipts": receipts,
            "raw_text_differs": receipts[0]["raw_response"]["generated_text"]
            != receipts[1]["raw_response"]["generated_text"],
            "real_model_inference": True,
            "dispatch_authorized": False,
            "WAM_calls": 0,
            "flights": 0,
            "retries": 0,
            "avoidance_performance_qualified": False,
            "operational_response_lease_qualified": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "adapter", "inputs", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--preparation-deadline", type=float, required=True)
    parser.add_argument("--comparison-deadline", type=float, required=True)
    args = parser.parse_args()
    run(
        args.base,
        args.adapter,
        args.inputs,
        args.output,
        args.preparation_deadline,
        args.comparison_deadline,
    )
