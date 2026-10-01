"""Host-only, opt-in loopback inference bridge; the simulator stays offline."""

import base64
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .ship_aerovla_live import image_bytes, make_request
from .ship_vla_adapter import content_hash


def connect(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ValueError("AeroVLA requires an explicit HTTP IPv4 loopback tunnel")
    return http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=5)


def request_json(url, method, path, payload=None):
    connection = connect(url)
    try:
        body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        connection.request(method, path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000 or response.status != 200:
            raise ValueError(f"AeroVLA service rejected request: HTTP {response.status}")
        return json.loads(raw)
    finally:
        connection.close()


def check_service(url):
    value = request_json(url, "GET", "/health")
    validate_service_identity(value)
    return value


def validate_service_identity(value):
    from scripts import ship_aerovla as native

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    expected = {
        "schema_version": "ship_aerovla_service.v1",
        "base_revision": native.BASE_REVISION,
        "adapter_revision": native.ADAPTER_REVISION,
        "adapter_sha256": native.ADAPTER_SHA256,
        "upstream_revision": native.UPSTREAM_REVISION,
        "weights_sha256": native.WEIGHTS,
        "custom_code_sha256": native.CUSTOM_CODE,
        "runtime_sha256": {
            name: hashlib.sha256((scripts / name).read_bytes()).hexdigest()
            for name in ("ship_aerovla.py", "ship_aerovla_server.py", "ship_anwm.py")
        },
        "warmup_completed": True,
        "dispatch_capability": False,
    }
    if (
        any(value.get(k) != v for k, v in expected.items())
        or not re.fullmatch(r"[0-9a-f]{32}", value.get("session_id", ""))
        or not value.get("gpu", "").startswith("NVIDIA ")
    ):
        raise ValueError("AeroVLA service identity or reviewed runtime mismatch")


def verify_native_chain(root, config, samples, receipt):
    """Reopen the actual host mailbox and images independently of the permit."""
    from io import BytesIO
    from PIL import Image
    from .runtime_claim_evidence import validate_runtime_invocation_evidence

    validate_service_identity(config["urban"]["aerovla_live"])
    request = json.loads((root / "aerovla-request.json").read_text())
    response = json.loads((root / "aerovla-response.json").read_text())
    inference = receipt["inference"]
    response_text = (root / "aerovla-model-response.json").read_text()
    if json.loads(response_text) != inference["response"]:
        raise ValueError("native_response_artifact_mismatch")
    runtime_evidence = validate_runtime_invocation_evidence(
        {
            **response["runtime_invocation_evidence"],
            "invocation_stdout_preimage": response_text,
            "invocation_stderr_preimage": "",
        }
    )
    if (
        runtime_evidence["run_id"] != config["run_id"]
        or runtime_evidence["execution_scope"] != "sim"
        or runtime_evidence["invocation_exit_code"] != 0
        or runtime_evidence["request_sha256"] != content_hash(inference["request"])
    ):
        raise ValueError("native_runtime_invocation_binding_mismatch")
    if (
        request["request"] != inference["request"]
        or request["created_at_s"] != inference["host_started_s"]
        or response.get("error")
        or response.get("invocation_attempted") is not True
        or response["request_sha256"] != content_hash(inference["request"])
        or response["response"] != inference["response"]
        or receipt["execution"]["input_elapsed_s"] != request["input_elapsed_s"]
    ):
        raise ValueError("native_mailbox_receipt_chain_mismatch")
    row = next(r for r in samples if r["elapsed_s"] == request["input_elapsed_s"])
    mosaic = Image.new("RGB", (224, 448))
    for index, data in enumerate(image_bytes(root, row).values()):
        with Image.open(BytesIO(data)) as image:
            if image.size != (640, 360) or image.mode != "RGB":
                raise ValueError("native_image_format_mismatch")
            mosaic.paste(image.resize((224, 224), Image.Resampling.BICUBIC), (0, 224 * index))
    if hashlib.sha256(mosaic.tobytes()).hexdigest() != inference["response"]["mosaic_rgb_sha256"]:
        raise ValueError("native_actual_model_mosaic_mismatch")
    project = Path(__file__).resolve().parents[2]
    for name, folder in (
        ("ship_aerovla_live.py", "src/runtime"),
        ("ship_aerovla_host.py", "src/runtime"),
        ("ship_aerovla_server.py", "scripts"),
        ("ship_aerovla.py", "scripts"),
        ("ship_anwm.py", "scripts"),
    ):
        expected = config["urban"]["onboard_source_sha256"][name]
        paths = [root / "sources" / name, project / folder / name]
        if name == "ship_aerovla_live.py":
            paths.append(root / name)
        if any(hashlib.sha256(p.read_bytes()).hexdigest() != expected for p in paths):
            raise ValueError("native_source_binding_mismatch")
    return {
        "verified": True,
        "runtime_invocation_evidence": runtime_evidence,
        "request_id": inference["request"]["request_id"],
        "generated_text": inference["response"]["generated_text"],
        "model_inference_seconds": inference["response"]["inference_seconds"],
        "host_exchange_seconds": response["host_exchange_seconds"],
        "input_to_permit_seconds": receipt["execution"]["permit"]["issued_at_s"] - row["elapsed_s"],
    }


def process_native_request(root, config, url):
    request_path = root / "aerovla-request.json"
    response_path = root / "aerovla-response.json"
    if not request_path.exists() or response_path.exists():
        return
    message = json.loads(request_path.read_text())
    request = message["request"]
    result = {"request_sha256": content_hash(request), "invocation_attempted": False}
    started = time.monotonic()
    try:
        rows = [json.loads(line) for line in (root / "telemetry.jsonl").read_text().splitlines()]
        matches = [r for r in rows if r["elapsed_s"] == message["input_elapsed_s"]]
        if len(matches) != 1 or request != make_request(config, matches[0], request["request_id"]):
            raise ValueError("Native request does not match recorded observation")
        payload = {
            "request": request,
            "images_base64": {
                k: base64.b64encode(v).decode("ascii")
                for k, v in image_bytes(root, matches[0]).items()
            },
        }
        result["invocation_attempted"] = True
        invocation_started = datetime.now(timezone.utc).isoformat()
        result["response"] = request_json(url, "POST", "/infer", payload)
        response_text = json.dumps(result["response"], sort_keys=True, allow_nan=False)
        (root / "aerovla-model-response.json").write_text(response_text)
        result["runtime_invocation_evidence"] = {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "http_loopback",
            "invocation_target": "AeroVLA:/infer:" + config["urban"]["aerovla_live"]["session_id"],
            "invocation_started_at": invocation_started,
            "invocation_completed_at": datetime.now(timezone.utc).isoformat(),
            "invocation_exit_code": 0,
            "invocation_stdout_sha256": hashlib.sha256(response_text.encode()).hexdigest(),
            "invocation_stderr_sha256": hashlib.sha256(b"").hexdigest(),
            "run_id": config["run_id"],
            "execution_scope": "sim",
            "request_sha256": content_hash(request),
        }
        if result["response"].get("service") != config["urban"]["aerovla_live"]:
            raise ValueError("Native service changed after plan approval")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
    result["host_exchange_seconds"] = time.monotonic() - started
    temporary = root / "aerovla-response.tmp"
    temporary.write_text(json.dumps(result, allow_nan=False, indent=2))
    temporary.replace(response_path)
