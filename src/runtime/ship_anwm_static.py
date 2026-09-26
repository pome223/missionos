"""Native ANWM candidate views for a stationary, mapped synthetic urban scene.

Predictions propose a route only. Fresh image validation and the existing
executor authorize motion separately. This is not a moving-object forecast.
"""

from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4
import numpy as np
from PIL import Image
from scripts import ship_anwm as native

POLICY = "onboard_anwm_static"
CONTRACT = {
    "schema_version": "ship_anwm_static_contract.v2",
    "target_association": "past_rgbd_cross_section.v1",
    "position_error_limit_m": 5.0,
    "stationarity_limit_m": 1.5,
    "host_inference_limit_s": 75.0,
    "revalidation_age_limit_s": 2.0,
    "direct_centre_clearance_m": 14.0,
    "candidate_agreement_limit_m": 5.0,
    "scope": "stationary candidate-view prediction; dynamic forecast timing not qualified",
    "dispatch_allowed": False,
}


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def endpoint(url):
    value = urlparse(url)
    if (
        value.scheme != "http"
        or value.hostname != "127.0.0.1"
        or not value.port
        or value.path
        or value.query
        or value.fragment
        or value.username
        or value.password
    ):
        raise ValueError("ANWM endpoint must be explicit IPv4 loopback")
    return url


def check_service(url):
    with urllib.request.urlopen(endpoint(url) + "/health", timeout=5) as response:
        identity = json.load(response)
    expected = {
        "schema_version": "ship_anwm_static_service.v1",
        "model_revision": native.MODEL_REVISION,
        "checkpoint_sha256": native.MODEL_SHA256,
        "upstream_revision": native.UPSTREAM_REVISION,
        "vae_revision": native.VAE_REVISION,
        "server_sha256": native.digest(Path(native.__file__).with_name("ship_anwm_server.py")),
        "helper_sha256": native.digest(Path(native.__file__)),
        "diffusion_steps": 250,
    }
    if any(identity.get(k) != v for k, v in expected.items()) or not identity.get("session_id"):
        raise ValueError("ANWM service identity differs from reviewed source")
    return {"identity": identity, "contract": CONTRACT}


def warm_spans(image):
    """Read substantial warm regions, without assigning object identity."""
    rgb = np.asarray(image)
    if rgb.shape != (224, 224, 3) or rgb.dtype != np.uint8:
        raise ValueError("Expected 224px RGB prediction")
    p = rgb.astype(np.int16)
    warm = (p[:, :, 0] > 80) & (p[:, :, 0] - p[:, :, 1] >= 30) & (p[:, :, 0] - p[:, :, 2] >= 45)
    columns = np.flatnonzero(warm[56:168].mean(axis=0) >= 0.55)
    spans = [v for v in np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1) if len(v) >= 6]
    spans.sort(key=len, reverse=True)
    return [[int(v[0]), int(v[-1])] for v in spans]


def obstacle_interval(image, reference=None):
    """Associate the forecast with a past RGBD target, never a future observation.

    The legacy unassociated reader remains available for diagnosis. Association
    uses a half-width search margin, a 2x scale bound and >= 0.5 interval IoU.
    Every substantial region touching that search window competes: a second
    plausible region, missing target, merged object or clipping still rejects.
    Regions elsewhere are not certified free space or classified as harmless.
    """
    spans = warm_spans(image)
    if reference is not None:
        lo, hi = reference
        if not all(math.isfinite(v) for v in reference) or not 0 < lo < hi < 223:
            raise ValueError("Past target projection missing or clipped")
        width = hi - lo + 1
        associated = [s for s in spans if s[1] >= lo - width / 2 and s[0] <= hi + width / 2]
        if len(associated) != 1:
            raise ValueError("Predicted target missing or ambiguous in association window")
        left, right = associated[0]
        intersection = max(0, min(hi, right) - max(lo, left) + 1)
        union = max(hi, right) - min(lo, left) + 1
        if (
            left == 0
            or right == 223
            or not 0.5 <= (right - left + 1) / width <= 2
            or intersection / union < 0.5
        ):
            raise ValueError("Predicted target clipped, displaced or shape-inconsistent")
        return associated[0]
    if (
        not spans
        or spans[0][0] == 0
        or spans[0][-1] == 223
        or (len(spans) > 1 and spans[1][1] - spans[1][0] + 1 >= (spans[0][1] - spans[0][0] + 1) / 2)
    ):
        raise ValueError("Main predicted obstacle missing, clipped or ambiguous")
    return [int(spans[0][0]), int(spans[0][-1])]


def projected_target_interval(arrays, target_pose):
    """Project the last observed red cross-section using its measured depth.

    Only the bound model input history is read. No forecast/projection returned
    by the service, mapped obstacle coordinate, or post-inference truth is used.
    The stationary-scene admission and later independent checks remain required.
    """
    from src.runtime.ship_onboard import observed_obstacle_pixel_center

    rgb, depth = arrays["rgb"][-1], arrays["depth"][-1]
    _, v = observed_obstacle_pixel_center(rgb.astype(float))
    columns = np.flatnonzero(native.red_mask(rgb)[int(v)])
    if len(columns) < 20 or len(columns) / (columns[-1] - columns[0] + 1) < 0.9:
        raise ValueError("Past target cross-section ambiguous")
    z = depth[int(v), columns]
    if not np.all(np.isfinite(z) & (z > 0) & (z <= 500)):
        raise ValueError("Past target depth incomplete")
    k = arrays["intrinsics"]
    rays = np.stack(
        ((columns - k[0, 2]) / k[0, 0], np.full(len(z), (v - k[1, 2]) / k[1, 1]), np.ones(len(z)))
    )
    world = arrays["poses"][-1][:3, :3] @ (rays * z) + arrays["poses"][-1][:3, 3, None]
    camera = target_pose[:3, :3].T @ (world - target_pose[:3, 3, None])
    if not np.isfinite(camera).all() or np.any(camera[2] <= 0):
        raise ValueError("Past target outside candidate camera")
    u = camera[0] / camera[2] * k[0, 0] + k[0, 2]
    resized = (u - 80 + 0.5) * 224 / 480 - 0.5
    return [float(resized.min()), float(resized.max())]


def mapped_center(image, pose, intrinsics, plane_north_m, reference=None):
    interval = obstacle_interval(image, reference)
    # Exact inverse pixel-centre convention of the native 480x360 crop/resize.
    u = ((sum(interval) / 2) + 0.5) * 480 / 224 + 80 - 0.5
    ray = pose[:3, :3] @ np.array([(u - intrinsics[0, 2]) / intrinsics[0, 0], 0.0, 1.0])
    if ray[0] <= 0.8 or not 60 <= plane_north_m - pose[0, 3] <= 120:
        raise ValueError("Prediction outside mapped camera envelope")
    return {
        "interval_px": interval,
        "obstacle_x_m": float(pose[1, 3] + ray[1] * (plane_north_m - pose[0, 3]) / ray[0]),
    }


def decode_forecasts(response, request_path, config):
    request, arrays = native.validate(request_path)
    if (
        response.get("schema_version") != "ship_anwm_static_response.v1"
        or response.get("identity") != config["urban"]["anwm_static"]["identity"]
        or response.get("request_sha256") != native.digest(request_path)
        or response.get("history_sha256") != request["history_sha256"]
        or response.get("wam_inference_invoked") is not True
        or response.get("dispatch_allowed") is not False
        or response.get("physical_execution_invoked") is not False
    ):
        raise ValueError("Native WAM invocation binding mismatch")
    forecasts = response["forecasts"]
    if [f["candidate"] for f in forecasts] != request["candidates"]:
        raise ValueError("Native WAM candidates changed")
    decoded = []
    for forecast in forecasts:
        entry = forecast["files"]["prediction"]
        data = base64.b64decode(entry["png_base64"], validate=True)
        if hash_bytes(data) != entry["sha256"]:
            raise ValueError("Prediction image hash mismatch")
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise ValueError("Invalid prediction format")
            pose = native.lateral_pose(arrays["poses"][-1], forecast["candidate"]["delta"][1])
            reference = projected_target_interval(arrays, pose)
            decoded.append(
                {
                    "candidate": forecast["candidate"]["id"],
                    "image_sha256": entry["sha256"],
                    "association": {
                        "source": CONTRACT["target_association"],
                        "history_sha256": request["history_sha256"],
                        "reference_interval_px": reference,
                        "warm_intervals_px": warm_spans(image),
                    },
                    **mapped_center(
                        image,
                        pose,
                        arrays["intrinsics"],
                        config["urban"]["obstacle_north_m"] - 4,
                        reference,
                    ),
                }
            )
    return decoded, arrays


def interpret(decoded, current_x, elapsed_s):
    positions = [d["obstacle_x_m"] for d in decoded]
    if (
        len(positions) != 2
        or not all(math.isfinite(x) for x in [*positions, current_x, elapsed_s])
        or not 0 <= elapsed_s <= CONTRACT["host_inference_limit_s"]
    ):
        raise ValueError("Prediction or response time outside contract")
    if max(positions) - min(positions) > CONTRACT["candidate_agreement_limit_m"]:
        raise ValueError("Candidate view positions are inconsistent")
    if max(abs(x - current_x) for x in positions) > CONTRACT["position_error_limit_m"]:
        raise ValueError("Prediction exceeds absolute position error bound")
    return {
        "action": "wait"
        if min(positions) - CONTRACT["position_error_limit_m"]
        >= CONTRACT["direct_centre_clearance_m"]
        else "detour",
        "policy": POLICY,
        "observation_source": "native_anwm_candidate_views_and_fresh_onboard_validation",
        "vla_invoked": False,
        "wam_invoked": True,
        "dispatch_allowed": False,
        "inference_elapsed_s": elapsed_s,
        "anwm": {
            "contract": CONTRACT,
            "predictions": decoded,
            "revalidated_obstacle_x_m": current_x,
            "maximum_position_error_m": max(abs(x - current_x) for x in positions),
        },
    }


def scene_observations(root, config, rows):
    from src.runtime.ship_onboard import observe_image

    if len(rows) < 3 or any(b["elapsed_s"] - a["elapsed_s"] > 1.5 for a, b in zip(rows, rows[1:])):
        raise ValueError("Stationary scene monitoring has gaps")
    if any(
        r.get("nav_state") != 4 or r.get("arming_state") != 2 or not r.get("position_valid")
        for r in rows
    ):
        raise ValueError("Armed stable hold was not maintained")
    observations = [observe_image(root, r, config["urban"]["obstacle_north_m"] - 4) for r in rows]
    if any(
        b["sensor_stamp_s"] <= a["sensor_stamp_s"] for a, b in zip(observations, observations[1:])
    ):
        raise ValueError("WAM hold requires continuously advancing images")
    values = [o["obstacle_x_m"] for o in observations]
    if max(values) - min(values) > CONTRACT["stationarity_limit_m"]:
        raise ValueError("Observed obstacle moved outside stationary contract")
    if max(math.dist(r["local_ned"], rows[0]["local_ned"]) for r in rows) > 0.5:
        raise ValueError("Ego drift outside stationary contract")
    return observations


def propose(root, config, frames, history, url):
    from src.runtime.ship_onboard import observed_obstacle_pixel_center

    prepared = root / "anwm-prepared"
    native.prepare(root / "native-capture/capture.json", prepared)
    request_path = prepared / "input/request.json"
    request, arrays = native.validate(request_path)
    plane = config["urban"]["obstacle_north_m"] - 4
    initial = []
    for rgb, pose in zip(arrays["rgb"], arrays["poses"]):
        # Raw observations share the visibility-aware pixel decoder with fresh
        # onboard RGB. Generated views keep their separate calibrated palette.
        u, v = observed_obstacle_pixel_center(rgb.astype(float))
        k = arrays["intrinsics"]
        ray = pose[:3, :3] @ np.array([(u - k[0, 2]) / k[0, 0], (v - k[1, 2]) / k[1, 1], 1])
        if ray[0] <= 0.8 or not 60 <= plane - pose[0, 3] <= 120:
            raise ValueError("Raw camera history outside mapped envelope")
        initial.append(float(pose[1, 3] + ray[1] * (plane - pose[0, 3]) / ray[0]))
    if (
        max(initial) - min(initial) > CONTRACT["stationarity_limit_m"]
        or abs(initial[-1] - history[-1]["obstacle_x_m"]) > CONTRACT["stationarity_limit_m"]
    ):
        raise ValueError("Native history does not show the same stationary obstacle")
    request_id = uuid4().hex
    payload = json.dumps(
        {
            "request_id": request_id,
            "request_base64": base64.b64encode(request_path.read_bytes()).decode(),
            "history_base64": base64.b64encode(
                (prepared / "input/history.npz").read_bytes()
            ).decode(),
        },
        sort_keys=True,
    ).encode()
    (root / "anwm-http-request.json").write_bytes(payload)
    started_at = datetime.now(timezone.utc).isoformat()
    begin = time.monotonic()
    error = None
    try:
        req = urllib.request.Request(
            endpoint(url) + "/infer", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=75) as reply:
            raw = reply.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("Oversized WAM response")
        (root / "anwm-http-response.json").write_bytes(raw)
        response = json.loads(raw)
        if response.get("request_id") != request_id:
            raise ValueError("Native WAM request ID mismatch")
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        elapsed = time.monotonic() - begin
        native.write_json(
            root / "anwm-http-invocation.json",
            {
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": elapsed,
                "request_sha256": hash_bytes(payload),
                "response_sha256": native.digest(root / "anwm-http-response.json")
                if (root / "anwm-http-response.json").exists()
                else None,
                "request_id": request_id,
                "url": url,
                "error": error,
                "run_id": config["run_id"],
            },
        )
    decoded, _ = decode_forecasts(response, request_path, config)
    rows = [json.loads(line) for line in (root / "telemetry.jsonl").read_text().splitlines()]
    rows = [r for r in rows if r["elapsed_s"] >= frames[0]["elapsed_s"]]
    observations = scene_observations(root, config, rows)
    if abs(initial[-1] - observations[-1]["obstacle_x_m"]) > CONTRACT["stationarity_limit_m"]:
        raise ValueError("Native history no longer matches current scene")
    decision = interpret(decoded, observations[-1]["obstacle_x_m"], elapsed)
    decision["anwm"].update(
        response_sha256=hash_bytes(raw),
        request_id=request_id,
        monitored_samples=[r["elapsed_s"] for r in rows],
        latest_observed_at_s=observations[-1]["observed_at_s"],
    )
    return decision


def verify_artifacts(root, config, decision, samples, events):
    from src.runtime.ship_onboard import observe_image

    response_path = root / "anwm-http-response.json"
    response = json.loads(response_path.read_text())
    receipt = json.loads((root / "anwm-http-invocation.json").read_text())
    payload = json.loads((root / "anwm-http-request.json").read_text())
    request_path = root / "anwm-prepared/input/request.json"
    if (
        receipt["run_id"] != config["run_id"]
        or receipt["error"] is not None
        or receipt["response_sha256"] != native.digest(response_path)
        or receipt["request_sha256"] != native.digest(root / "anwm-http-request.json")
        or payload["request_id"] != response["request_id"]
        or receipt["request_id"] != response["request_id"]
    ):
        raise ValueError("WAM HTTP evidence mismatch")
    for key, path in [
        ("request_base64", request_path),
        ("history_base64", request_path.with_name("history.npz")),
    ]:
        if base64.b64decode(payload[key], validate=True) != path.read_bytes():
            raise ValueError("WAM request input mismatch")
    decoded, arrays = decode_forecasts(response, request_path, config)
    request = json.loads(request_path.read_text())
    if any(request[k] != config[k] for k in ("run_id", "world_sha256", "plan_sha256")):
        raise ValueError("WAM input mission binding mismatch")
    capture_path = root / "native-capture/capture.json"
    capture = json.loads(capture_path.read_text())
    if request["source_history_sha256"] != hash_bytes(
        json.dumps(capture["frames"][:16], sort_keys=True).encode()
    ):
        raise ValueError("WAM source history binding mismatch")
    for i, frame in enumerate(capture["frames"][:16]):
        rgb = np.frombuffer(
            native.asset(capture_path.parent, frame["assets"]["rgb"]), np.uint8
        ).reshape(360, 640, 3)
        depth = np.frombuffer(
            native.asset(capture_path.parent, frame["assets"]["depth"]), "<f4"
        ).reshape(360, 640)
        depth = np.where(np.isfinite(depth) & (depth > 0) & (depth <= 500), depth, 0)
        if (
            not np.array_equal(rgb, arrays["rgb"][i])
            or not np.array_equal(depth, arrays["depth"][i])
            or not np.array_equal(native.camera_pose(frame), arrays["poses"][i])
            or frame["simulation_time_ns"] != arrays["stamps_ns"][i]
        ):
            raise ValueError("WAM inputs differ from captured observations")
    by_time = {r["elapsed_s"]: r for r in samples}
    decision_event = next(e for e in events if e["event"] == "urban_decision")
    vision_request = json.loads((root / "vision-request-1.json").read_text())
    monitored = decision["anwm"]["monitored_samples"]
    if not monitored or monitored[-1] > decision_event["elapsed_s"]:
        raise ValueError("WAM validation uses future observations")
    required = [
        r["elapsed_s"]
        for r in samples
        if vision_request["frames"][0]["elapsed_s"] <= r["elapsed_s"] <= monitored[-1]
    ]
    if monitored != required:
        raise ValueError("WAM stationary monitoring omitted observations")
    selected = [by_time[t] for t in monitored]
    observations = scene_observations(root, config, selected)
    expected = interpret(decoded, observations[-1]["obstacle_x_m"], receipt["elapsed_s"])
    expected["anwm"].update(
        response_sha256=native.digest(response_path),
        request_id=response["request_id"],
        monitored_samples=[r["elapsed_s"] for r in selected],
        latest_observed_at_s=observations[-1]["observed_at_s"],
    )
    if decision != expected:
        raise ValueError("WAM decision is not reproducible")
    checks = [e for e in events if e["event"] == "anwm_dispatch_revalidated"]
    if len(checks) != 1:
        raise ValueError("WAM dispatch revalidation missing")
    event = checks[0]
    row = by_time[event["sample_elapsed_s"]]
    current = observe_image(root, row, config["urban"]["obstacle_north_m"] - 4)
    dispatched = next(e for e in events if e["event"] == "urban_requested")
    if (
        not 0 <= dispatched["elapsed_s"] - row["elapsed_s"] <= 2
        or abs(current["obstacle_x_m"] - observations[-1]["obstacle_x_m"])
        > CONTRACT["stationarity_limit_m"]
        or event["response_sha256"] != native.digest(response_path)
    ):
        raise ValueError("WAM was not freshly revalidated at dispatch")
    # Independent truth is used here only, not in model input or route selection.
    errors = [abs(p["obstacle_x_m"] - row["urban_obstacle"]["xyz"][0]) for p in decoded]
    if max(errors) > CONTRACT["position_error_limit_m"]:
        raise ValueError("Independent WAM position validation exceeds absolute bound")
    from src.runtime.runtime_claim_evidence import validate_runtime_invocation_evidence

    invocation = validate_runtime_invocation_evidence(
        {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "http_loopback",
            "invocation_target": endpoint(receipt["url"]) + "/infer:EmbodiedCity/ANWM",
            "invocation_started_at": receipt["started_at"],
            "invocation_completed_at": receipt["completed_at"],
            "invocation_exit_code": 0,
            "invocation_stdout_sha256": native.digest(response_path),
            "invocation_stdout_preimage": response_path.read_text(),
            "invocation_stderr_sha256": hash_bytes(b""),
            "invocation_stderr_preimage": "",
            "run_id": config["run_id"],
            "execution_scope": "sim",
            "request_sha256": receipt["request_sha256"],
        }
    )
    return {
        "verified": True,
        "runtime_invocation_evidence": invocation,
        "maximum_prediction_position_error_m": max(errors),
        "inference_elapsed_s": receipt["elapsed_s"],
        "stationary_candidate_view_only": True,
        "wam_invoked": True,
    }
