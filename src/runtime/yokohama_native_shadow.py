"""One recorded VLA/WAM HTTP pair with no vehicle or lifecycle authority.

This preserves native image-forecast semantics. It is deliberately not an
adapter for the goal fixture's synthetic clearance booleans or live dispatch.
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time

import numpy as np
from PIL import Image

from scripts import ship_anwm
from scripts.ship_anwm import rotation
from scripts.yokohama_decision_host import DecisionHost, validate_native_services
from scripts.yokohama_decision_worker import require_city_request
from src.runtime.yokohama_native import digest, load_capture, read_asset
from src.runtime.ship_aerovla_host import validate_service_identity
from src.runtime.yokohama_shadow_http import exchange

REPO = Path(__file__).resolve().parents[2]
SERVICE_SOURCES = (
    "ship_aerovla_server.py", "ship_aerovla.py", "ship_anwm.py",
    "ship_anwm_server.py", "yokohama_appearance.py", "yokohama_wam_profile.py",
)
PLAN_FIELDS = {
    "schema_version", "approval_ref", "execution_mode", "config", "cycle",
    "next_target_world_xyz_m", "vla", "wam", "collision_map", "source_sha256",
    "services", "ports", "limits",
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("shadow_invalid_fields:" + label)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _check_sources(source_hashes):
    for name, expected in source_hashes.items():
        if _sha((REPO / "scripts" / name).read_bytes()) != expected:
            raise ValueError("shadow_service_source_changed:" + name)


def _bound_file(root, entry):
    _exact(entry, {"file", "sha256"}, "file")
    if not isinstance(entry["file"], str) or not entry["file"]:
        raise ValueError("shadow_invalid_file")
    path = root / entry["file"]
    if Path(entry["file"]).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("shadow_file_outside_input_root")
    data = path.read_bytes()
    if _sha(data) != entry["sha256"]:
        raise ValueError("shadow_input_hash_mismatch")
    return path, data


def _prepare(plan_path):
    path = Path(plan_path).resolve()
    raw = path.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("shadow_plan_too_large")
    plan = json.loads(raw)
    _exact(plan, PLAN_FIELDS, "plan")
    if (plan["schema_version"] != "yokohama_native_shadow_plan.v1"
            or plan["execution_mode"] not in {"mock_http", "native_shadow"}
            or not isinstance(plan["approval_ref"], str) or not plan["approval_ref"]
            or type(plan["cycle"]) is not int or plan["cycle"] not in {1, 2}):
        raise ValueError("shadow_unapproved_scope")
    limits = plan["limits"]
    _exact(limits, {"max_vla_requests", "max_wam_requests", "request_timeout_s", "total_timeout_s"}, "limits")
    if (any(type(limits[k]) is not int or limits[k] != 1
            for k in ("max_vla_requests", "max_wam_requests"))
            or not _finite(limits["request_timeout_s"])
            or not 0 < limits["request_timeout_s"] <= 75
            or not _finite(limits["total_timeout_s"])
            or not 0 < limits["total_timeout_s"] <= 180):
        raise ValueError("shadow_requires_one_bounded_pair")
    _exact(plan["ports"], {"vla", "wam"}, "ports")
    if (any(type(p) is not int or not 1024 <= p <= 65535 for p in plan["ports"].values())
            or len(set(plan["ports"].values())) != 2):
        raise ValueError("shadow_requires_distinct_loopback_ports")
    _exact(plan["source_sha256"], SERVICE_SOURCES, "sources")
    _check_sources(plan["source_sha256"])
    config = plan["config"]
    if config["decisions"].get("wam_profile", "legacy") not in {"legacy", "motion-v4"}:
        raise ValueError("shadow_unknown_model_profile")
    validate_native_services(config, plan["services"], plan["source_sha256"])
    mock = plan["execution_mode"] == "mock_http"
    if any((identity.get("fixture") is True) != mock for identity in plan["services"].values()):
        raise ValueError("shadow_service_execution_mode_mismatch")
    if not mock:
        validate_service_identity(plan["services"]["vla"])
        wam = plan["services"]["wam"]
        if (wam.get("schema_version") != "ship_anwm_static_service.v1"
                or re.fullmatch(r"[0-9a-f]{32}", wam.get("session_id", "")) is None
                or wam.get("model_revision") != ship_anwm.MODEL_REVISION
                or wam.get("vae_revision") != ship_anwm.VAE_REVISION
                or wam.get("diffusion_steps") != 250):
            raise ValueError("shadow_unreviewed_native_wam_identity")
    expected_next = {1: "01-D2", 2: "02-D3"}[plan["cycle"]]
    stages = [s for s in config["flight_stages"] if s["name"] == expected_next]
    if len(stages) != 1 or stages[0]["target_world_xyz_m"] != plan["next_target_world_xyz_m"]:
        raise ValueError("shadow_goal_outside_approved_stage")
    _, collision = _bound_file(path.parent, plan["collision_map"])
    # Parse now; the exact approved bytes are copied to the private output later.
    if json.loads(collision).get("type") != "FeatureCollection":
        raise ValueError("shadow_invalid_collision_map")
    bound_map = config["world"].get("source_sha256", {}).get("collision-footprints.geojson")
    if (not mock or bound_map is not None) and bound_map != plan["collision_map"]["sha256"]:
        raise ValueError("shadow_map_not_bound_to_recorded_world")
    captures = {}
    for model in ("vla", "wam"):
        entry = plan[model]
        _exact(entry, {"capture", "observation"}, model)
        capture_path, capture_bytes = _bound_file(path.parent, entry["capture"])
        frozen_record = json.loads(capture_bytes)
        record, arrays = load_capture(
            capture_path, appearance=config["decisions"].get("wam_profile") == "motion-v4"
        )
        if record != frozen_record:
            raise ValueError("shadow_capture_changed_while_loading")
        row = entry["observation"]
        if ((not mock or "run_id" in row) and row.get("run_id") != config["run_id"]
                or (not mock or "world_sha256" in row)
                and row.get("world_sha256") != config["world"]["world_sha256"]):
            raise ValueError("shadow_recorded_run_or_world_mismatch")
        require_city_request(config, {
            "operation": model, "cycle": plan["cycle"], "observation": row,
        })
        if (row.get("position_valid") is not True
                or not all(_finite(x) for x in [row["wall_s"], row["sim_s"], row["heading_ned_rad"],
                                               row["battery_fraction"], *row["velocity_ned"]])
                or len(row["velocity_ned"]) != 3
                or math.hypot(*row["velocity_ned"]) > 0.3
                or not 0.2 <= row["battery_fraction"] <= 1
                or not 0 <= row["sim_s"] - arrays["stamps_ns"][-1] / 1e9 <= 2):
            raise ValueError("shadow_invalid_recorded_hold")
        last_pose = record["frames"][-1]["pose"]
        if (math.dist(last_pose["xyz"], row["vehicle"]["xyz"]) > 0.5
                or not np.isfinite(row["vehicle"]["quat_wxyz"]).all()
                or len(row["vehicle"]["quat_wxyz"]) != 4):
            raise ValueError("shadow_recorded_pose_mismatch")
        observed_rotation = rotation(row["vehicle"]["quat_wxyz"])
        captured_rotation = rotation(last_pose["quat_wxyz"])
        if np.arccos(np.clip((np.trace(observed_rotation.T @ captured_rotation) - 1) / 2, -1, 1)) > 0.03:
            raise ValueError("shadow_recorded_camera_orientation_mismatch")
        images = {
            key: read_asset(capture_path.parent, record["frames"][-1]["assets"][sensor + "_png"])
            for key, sensor in (("rgb", "onboard_rgb"), ("down", "down_rgb"))
        }
        for data in images.values():
            with Image.open(io.BytesIO(data)) as image:
                if image.mode != "RGB" or image.size != (640, 360) or image.format != "PNG":
                    raise ValueError("shadow_invalid_recorded_dual_view")
        captures[model] = (capture_path, record, arrays, images)
    vla_row, wam_row = (plan[m]["observation"] for m in ("vla", "wam"))
    if (plan["vla"]["capture"] == plan["wam"]["capture"]
            or captures["wam"][1]["frames"][0]["stamp_ns"] <= captures["vla"][1]["frames"][-1]["stamp_ns"]
            or wam_row["wall_s"] <= vla_row["wall_s"]
            or wam_row["sim_s"] <= vla_row["sim_s"]
            or math.dist(wam_row["vehicle"]["xyz"], vla_row["vehicle"]["xyz"]) > 0.5
            or abs(math.remainder(wam_row["heading_ned_rad"] - vla_row["heading_ned_rad"], 2 * math.pi)) > 0.03
            or wam_row.get("reset_counters") != vla_row.get("reset_counters")):
        raise ValueError("shadow_requires_new_recorded_wam_history")
    return plan, captures, collision, _sha(raw)


def preflight(plan_path):
    """Validate only local recordings and pins; never contact a service."""
    plan, _, _, plan_hash = _prepare(plan_path)
    return {
        "valid": True, "plan_sha256": plan_hash, "execution_mode": plan["execution_mode"],
        "input_mode": "recorded_replay_not_live_observation", "limits": plan["limits"],
        "dispatch_allowed": False, "network_contacted": False,
    }


def run_shadow(plan_path, output_dir, *, approved=False):
    """Attach to separately owned services for one diagnostic pair; never dispatch.

    Caller approval covers inference only. Cloud creation, warmup, remote stop
    and deletion must be separately owned and bounded outside this process.
    """
    if approved is not True:
        raise PermissionError("--approve-shadow is required before output or HTTP")
    plan, captures, collision, plan_hash = _prepare(plan_path)
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    bundle = root / "frozen-map"
    bundle.mkdir()
    (bundle / "collision-footprints.geojson").write_bytes(collision)
    (root / "plan.json").write_text(json.dumps(plan, indent=2, allow_nan=False) + "\n")
    clock_start = time.monotonic()
    deadline = clock_start + plan["limits"]["total_timeout_s"]
    counts = {"vla": 0, "wam": 0}
    health = {"vla": 0, "wam": 0}
    calls = []

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("shadow_total_time_budget_exhausted")
        return value

    def bounded_exchange(port, path, payload=None, timeout=75):
        _check_sources(plan["source_sha256"])
        model = next((m for m, p in plan["ports"].items() if p == port), None)
        if model is None or path not in {"health", "infer"}:
            raise ValueError("shadow_unapproved_endpoint")
        if path == "health":
            if payload is not None or health[model] != 0 or any(counts.values()):
                raise ValueError("shadow_health_reuse")
            health[model] += 1
        else:
            if counts[model] or (model == "wam" and counts["vla"] != 1):
                raise ValueError("shadow_inference_budget_or_order")
            if model == "vla":
                expected = {k: _sha(v) for k, v in captures["vla"][3].items()}
                actual = {k: _sha(base64.b64decode(v, validate=True))
                          for k, v in payload["images_base64"].items()}
                if actual != expected or payload["request"]["images_sha256"] != expected:
                    raise ValueError("shadow_transmitted_image_changed")
            counts[model] += 1
        started = time.monotonic()
        item = {"model": model, "path": path, "payload_sha256": digest(payload), "completed": False}
        calls.append(item)
        try:
            response = exchange(port, path, payload, timeout=min(timeout, plan["limits"]["request_timeout_s"], remaining()))
            (root / f"http-{model}-{path}-response.json").write_text(
                json.dumps(response, indent=2, allow_nan=False) + "\n"
            )
            remaining()
            if path == "infer" and (
                response.get("physical_execution_invoked") is not False
                or (plan["execution_mode"] == "native_shadow" and response.get("fixture") is True)
                or (model == "wam" and response.get("request_id") != payload["request_id"])
            ):
                raise ValueError("shadow_response_authority_or_fixture_mismatch")
            item.update(completed=True, response_sha256=digest(response))
            return response
        finally:
            item["elapsed_s"] = time.monotonic() - started

    class FrozenShadowHost(DecisionHost):
        def capture(self, message):
            model = message["operation"]
            if (model not in captures or message["capture"] != plan[model]["capture"]
                    or message["observation"] != plan[model]["observation"]):
                raise ValueError("shadow_unbound_capture_message")
            path, record, arrays, _ = captures[model]
            return path, deepcopy(record), {k: v.copy() for k, v in arrays.items()}

    host = FrozenShadowHost(
        root, deepcopy(plan["config"]), bundle, "native",
        {m + "_port": p for m, p in plan["ports"].items()},
        detached=True, capture_root=Path(plan_path).resolve().parent,
        http_exchange=bounded_exchange, mock_http=plan["execution_mode"] == "mock_http",
    )
    failure, proposal, assessment, local_close = None, None, None, None
    try:
        host.attach_existing_services(expected_services=plan["services"], source_hashes=plan["source_sha256"])
        for model in ("vla", "wam"):
            remaining()
            message = {
                "operation": model, "run_id": plan["config"]["run_id"],
                "config_sha256": digest(plan["config"]), "cycle": plan["cycle"],
                **deepcopy(plan[model]),
            }
            if model == "vla":
                message["next_target_world_xyz_m"] = plan["next_target_world_xyz_m"]
            else:
                message["vla"] = proposal
            output = root / model
            output.mkdir()
            (output / "message.json").write_text(json.dumps(message, indent=2) + "\n")
            value = getattr(host, model)(message, output)
            remaining()
            (output / "assessment.json").write_text(json.dumps(value, indent=2) + "\n")
            if model == "vla":
                proposal = value
            else:
                assessment = value
    except Exception as exc:
        failure = f"{type(exc).__name__}:{exc}"
    finally:
        local_close = host.close()
    verified = failure is None and proposal is not None and assessment is not None
    result = {
        "schema_version": "yokohama_native_shadow_result.v1",
        "status": "completed" if verified else "blocked", "failure": failure,
        "execution_mode": plan["execution_mode"], "approval_ref": plan["approval_ref"],
        "input_mode": "recorded_replay_not_live_observation", "plan_sha256": plan_hash,
        "source_sha256": plan["source_sha256"], "model_contract_verified": verified,
        "native_inference_verified": verified and plan["execution_mode"] == "native_shadow",
        "visible_structure_consistent": verified and assessment["passed"] is True,
        "model_requests": sum(counts.values()), "vla_requests": counts["vla"], "wam_requests": counts["wam"],
        "calls": calls, "elapsed_s": time.monotonic() - clock_start,
        "local_revocation": local_close, "externally_owned_services": True,
        "remote_cleanup_verified": False, "dispatch_allowed": False,
        "flight_authority_created": False, "physical_execution_invoked": False,
        "goal_completion_verified": False, "collision_prediction_verified": False,
    }
    (root / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
