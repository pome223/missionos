"""Read-only retrospective constraint audit of a bound native AeroVLA result."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import time

import numpy as np
from PIL import Image

from scripts import ship_aerovla
from scripts.ship_anwm import NED_FROM_ENU, digest, rotation
from src.runtime.ship_vla_adapter import assess_proposal, bind_proposal, entry_limits


def audit_native_proposal(run_dir, model_input, model_output, *, now_s=None):
    root, inputs, outputs = map(Path, (run_dir, model_input, model_output))
    request_path, result_path = inputs / "request.json", outputs / "result.json"
    request, result = (json.loads(p.read_text()) for p in (request_path, result_path))
    config = json.loads((root / "config.json").read_text())
    capture_path = root / "native-capture/capture.json"
    capture = json.loads(capture_path.read_text())
    if (
        result.get("schema_version") != "ship_aerovla_invocation.v1"
        or result.get("request_sha256") != digest(request_path)
        or result.get("runtime_sha256") != digest(Path(ship_aerovla.__file__))
        or result.get("base_revision") != ship_aerovla.BASE_REVISION
        or result.get("adapter_revision") != ship_aerovla.ADAPTER_REVISION
        or result.get("adapter_sha256") != ship_aerovla.ADAPTER_SHA256
        or result.get("upstream_revision") != ship_aerovla.UPSTREAM_REVISION
        or result.get("vla_inference_invoked") is not True
        or any(
            result.get(k) is not False for k in ("dispatch_invoked", "physical_execution_invoked")
        )
    ):
        raise ValueError("Native invocation identity or no-dispatch contract mismatch")
    # Reproduce the historical preparation from raw, hash-checked sensor assets.
    # Only frame 15 is used; outcome frames are never passed to the guard.
    with tempfile.TemporaryDirectory(prefix="ship-vla-audit-") as directory:
        prepared = Path(directory) / "prepared"
        ship_aerovla.prepare(capture_path, prepared)
        if json.loads((prepared / "request.json").read_text()) != request:
            raise ValueError("Native request does not reproduce from captured observation")
        images = []
        for key in ("rgb", "down"):
            if digest(inputs / f"{key}.png") != request["images_sha256"][key]:
                raise ValueError("Native input image hash mismatch")
            with (
                Image.open(inputs / f"{key}.png") as image,
                Image.open(prepared / f"{key}.png") as expected,
            ):
                if (
                    image.mode != "RGB"
                    or image.size != expected.size
                    or image.tobytes() != expected.tobytes()
                ):
                    raise ValueError("Native input image differs from captured pixels")
                images.append(image.resize((224, 224), Image.Resampling.BICUBIC))
    mosaic_path = outputs / "actual-mosaic.png"
    if digest(mosaic_path) != result["mosaic_sha256"]:
        raise ValueError("Native mosaic hash mismatch")
    expected = Image.new("RGB", (224, 448))
    for index, image in enumerate(images):
        expected.paste(image, (0, index * 224))
    with Image.open(mosaic_path) as actual:
        if (
            actual.mode != "RGB"
            or actual.size != expected.size
            or actual.tobytes() != expected.tobytes()
        ):
            raise ValueError("Native mosaic pixels differ from request")
    frame = capture["frames"][15]
    position = NED_FROM_ENU @ np.asarray(frame["vehicle_position_enu_m"])
    body_to_ned = NED_FROM_ENU @ rotation(frame["vehicle_quaternion_wxyz"]) @ np.diag([1, -1, -1])
    heading = math.atan2(-body_to_ned[0, 1], body_to_ned[0, 0])
    stamp = max(frame["received_at_unix_s"].values())
    envelope = {
        **{k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")},
        "schema_version": "ship_vla_envelope.v1",
        "execution_scope": "sim",
        "inspection_authorized": False,
        "approval_ref": None,
        "clock_id": "archive_unix:" + config["run_id"],
        "frame_id": "gazebo_ned:" + config["run_id"],
        "expires_at_s": stamp + config["timeout_s"],
        "limits": entry_limits(config["urban"]),
    }
    observation = {
        **{
            k: envelope[k]
            for k in ("run_id", "world_sha256", "plan_sha256", "clock_id", "frame_id")
        },
        "phase": "urban_entry",
        "observed_at_s": stamp,
        "pose_age_s": 0.0,
        "image_age_s": 0.0,
        "image_sha256": request["images_sha256"]["rgb"],
        "position_ned_m": position.tolist(),
        "heading_ned_rad": heading,
        # The native request does not carry PX4 hold/velocity/reset evidence.
        # Placeholders cannot pass the hold gate; no live state is invented.
        "velocity_ned_mps": [0.0, 0.0, 0.0],
        "position_valid": False,
        "nav_state": None,
        "arming_state": None,
        "reset_counters": [0, 0, 0],
        "unavailable_fields": ["velocity_ned_mps", "nav_state", "arming_state", "reset_counters"],
        "state_evidence": "archived_gazebo_geometry_only",
    }
    proposal = bind_proposal(result["generated_text"], observation, envelope)
    assessment = assess_proposal(
        proposal,
        observation,
        observation,
        envelope,
        now_s=time.time() if now_s is None else now_s,
        archived=True,
    )
    return {
        "schema_version": "ship_vla_native_audit.v1",
        "assessment_scope": "retrospective_constraints_only",
        "run_id": config["run_id"],
        "case": config["urban"]["case"],
        "original_plan_contains_guard_envelope": "vla_guard_limits" in config["urban"],
        "request_sha256": digest(request_path),
        "native_result_sha256": digest(result_path),
        "capture_sha256": digest(capture_path),
        "config_sha256": digest(root / "config.json"),
        "native_runtime_sha256": result["runtime_sha256"],
        "guard_runtime_sha256": digest(Path(__file__).with_name("ship_vla_adapter.py")),
        "audit_runtime_sha256": digest(Path(__file__)),
        "source_frame_sha256": hashlib.sha256(
            json.dumps(frame, sort_keys=True).encode()
        ).hexdigest(),
        "native_generated_text": result["generated_text"],
        "envelope": envelope,
        "observation": observation,
        "proposal": proposal,
        "assessment": assessment,
        "input_pixel_binding_verified": True,
        "model_inference_rerun": False,
        "dispatch_invoked": False,
        "physical_execution_invoked": False,
        "limitations": [
            "new diagnostic envelope was not part of the historical approved plan",
            "Gazebo geometry is not a fresh PX4 estimator observation",
            "file bindings do not independently rerun or attest the GPU invocation",
            "passing nominal geometry would not establish obstacle clearance or flight improvement",
        ],
    }
