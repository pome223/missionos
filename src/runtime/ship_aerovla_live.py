"""Fresh dual-view inference contract. No cloud credentials or PX4 transport."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
from uuid import uuid4

if __package__:
    from .ship_vla_adapter import content_hash, snapshot_from_px4
else:
    from ship_vla_adapter import content_hash, snapshot_from_px4


def goal_prompt(config, row):
    """Approved mapped goal and observed PX4 attitude only; no obstacle truth."""
    w, x, y, z = row["attitude_q"]
    delta = [
        config["goal_north_m"] - row["local_ned"][0],
        -row["local_ned"][1],
        -0.05 - row["local_ned"][2],
    ]
    forward = sum(
        a * b
        for a, b in zip([1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)], delta)
    )
    right = sum(
        a * b
        for a, b in zip([2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w)], delta)
    )
    angle = math.degrees(math.atan2(right, forward))
    if abs(angle) <= 15:
        direction = "straight ahead"
    elif 15 < angle <= 60:
        direction = "forward-right"
    elif -60 <= angle < -15:
        direction = "forward-left"
    elif 60 < angle <= 120:
        direction = "to your right"
    elif -120 <= angle < -60:
        direction = "to your left"
    else:
        direction = "to your right rear" if angle > 0 else "to your left rear"
    return (
        f"<image>\nFly {direction} and find the target. "
        "The delivery pad on the ground at the end of the corridor.\nAction: "
    )


def make_request(config, row, request_id):
    snapshot_from_px4(row, config)
    forward = row["onboard_frame"]
    down = forward["paired_down"]
    if (
        not re.fullmatch(r"[0-9a-f]{32}", request_id)
        or forward["sensor_stamp_s"] != down["sensor_stamp_s"]
        or any(
            type(f["received_age_s"]) not in (int, float) or not 0 <= f["received_age_s"] <= 0.6
            for f in (forward, down)
        )
    ):
        raise ValueError("native_dual_view_not_fresh_or_aligned")
    return {
        "schema_version": "ship_aerovla_live_request.v1",
        **{k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")},
        "request_id": request_id,
        "input_row_sha256": content_hash(row),
        "observed_at_s": row["elapsed_s"],
        "sensor_stamp_s": forward["sensor_stamp_s"],
        "images_sha256": {"rgb": forward["sha256"], "down": down["sha256"]},
        "prompt": goal_prompt(config, row),
        "direction_source": "approved_goal_and_px4_ego_pose",
        "future_ground_truth_used": False,
        "dispatch_allowed": False,
    }


def validate_inference(config, row, inference, now_s):
    request, response = inference["request"], inference["response"]
    if request != make_request(config, row, request["request_id"]):
        raise ValueError("native_request_not_bound_to_observation")
    if (
        response.get("schema_version") != "ship_aerovla_live_invocation.v1"
        or response.get("request_sha256") != content_hash(request)
        or response.get("service") != config["urban"]["aerovla_live"]
        or response.get("vla_inference_invoked") is not True
        or response.get("dispatch_invoked") is not False
        or response.get("physical_execution_invoked") is not False
        or response.get("input_images_sha256") != request["images_sha256"]
        or not isinstance(response.get("generated_text"), str)
        or not response.get("generated_token_ids")
        or response.get("pixel_values_shape") != [1, 6, 224, 224]
    ):
        raise ValueError("native_response_identity_or_invocation_mismatch")
    started, completed = inference["host_started_s"], inference["host_completed_s"]
    if (
        not all(type(v) in (int, float) and math.isfinite(v) for v in (started, completed, now_s))
        or not row["elapsed_s"] <= started <= completed <= now_s
    ):
        raise ValueError("native_inference_clock_order")
    # The existing two-second guard also checks PX4 pose and forward-image age.
    # Check the paired downward camera and actual host round trip separately.
    age = now_s - row["elapsed_s"] + row["onboard_frame"]["paired_down"]["received_age_s"]
    if age > config["urban"]["vla_guard_limits"]["max_observation_age_s"]:
        raise ValueError("native_downward_observation_stale")
    return response["generated_text"]


def image_bytes(root, row):
    frames = {"rgb": row["onboard_frame"], "down": row["onboard_frame"]["paired_down"]}
    result = {}
    for key, frame in frames.items():
        path = (Path(root) / frame["file"]).resolve()
        if not path.is_relative_to(Path(root).resolve()):
            raise ValueError("native_image_outside_run")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != frame["sha256"]:
            raise ValueError("native_image_hash_mismatch")
        result[key] = data
    return result


class LiveBridge:
    """Single-use mailbox. Keep observing LOITER while the host invokes the GPU."""

    def __init__(self, root, config):
        self.root, self.config = Path(root), config

    def infer(self, row, tick, clock):
        request = make_request(self.config, row, uuid4().hex)
        message = {"request": request, "input_elapsed_s": row["elapsed_s"], "created_at_s": clock()}
        temp = self.root / "aerovla-request.tmp"
        temp.write_text(json.dumps(message, allow_nan=False))
        temp.replace(self.root / "aerovla-request.json")
        deadline = clock() + 5
        response_path = self.root / "aerovla-response.json"
        while clock() < deadline:
            tick()
            if response_path.exists():
                value = json.loads(response_path.read_text())
                if value.get("request_sha256") != content_hash(request):
                    raise ValueError("native_mailbox_response_mismatch")
                if value.get("error"):
                    raise RuntimeError("native_inference_failed:" + value["error"])
                # Host Unix/monotonic clocks are deliberately not used as the
                # flight clock. These are bounds measured by the worker.
                return {
                    "request": request,
                    "response": value["response"],
                    "host_started_s": message["created_at_s"],
                    "host_completed_s": clock(),
                }
            time.sleep(0.01)
        raise TimeoutError("native_inference_timeout")
