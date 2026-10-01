"""Opt-in local VLM action/forecast experiment; no native flight-VLA/WAM claim.

The same observed frames and numeric image measurements go to both roles. The
forecast role predicts an exogenous obstacle while the aircraft holds. It is
not an action-conditioned video world model. Outputs carry no approval power.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen


MODEL = "gemma4:26b"
MODEL_DIGEST = "5571076f3d70050487b26b341705799e0ab29b808164f90d20d4cf84f699d251"
ENDPOINT = "http://127.0.0.1:11434"
MODEL_POLICIES = ("onboard_vlm_action", "onboard_vlm_forecast")
DEADLINE_S = 20.0
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["wait", "detour"]},
        "predicted_clearance_seconds": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["action", "predicted_clearance_seconds", "reason"],
    "additionalProperties": False,
}
PROMPT = """You propose a route for a simulated parcel drone holding at an urban entry.
You have no authority to approve, dispatch, or claim delivery. All attached
images are chronological observations from the same onboard camera. The red
obstacle crosses a known mapped plane; the supplied image-derived east positions
have already been corrected with the aircraft's PX4 position and attitude.
Use only these observations. You do not know the scripted future or case label.
The direct corridor is blocked until the obstacle centre is at east >= 14 metres.
The alternative preapproved detour adds 22.1666667 seconds to the direct path.
Infer whether motion continues or slows to a stop. Choose wait when clearance
is expected within the detour cost, otherwise detour. The forecast is seconds
from the last observation until east >=14; use 120 for no credible clearance
within 120 seconds. Return JSON with action, predicted_clearance_seconds, and
a short reason. A rule will require new image-observed clearance before direct
flight regardless of this proposal. Forecasting is about the obstacle while
the aircraft holds; it does not establish route safety or delivery success.
"""


def _json_call(path, payload=None, *, timeout=3):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(ENDPOINT + path, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def verify_local_model():
    models = _json_call("/api/tags")["models"]
    if not any(m.get("name") == MODEL and m.get("digest") == MODEL_DIGEST for m in models):
        raise ValueError("The explicitly pinned local vision model is unavailable")
    capabilities = _json_call("/api/show", {"model": MODEL}).get("capabilities", [])
    if "vision" not in capabilities:
        raise ValueError("Pinned model has no declared vision capability")
    return {"model": MODEL, "digest": MODEL_DIGEST, "capabilities": capabilities}


def interpret_proposal(proposal, policy, *, elapsed_s):
    if policy not in MODEL_POLICIES or not 0 <= elapsed_s <= DEADLINE_S:
        raise ValueError("Model proposal is unknown or expired")
    if not isinstance(proposal, dict) or set(proposal) != set(SCHEMA["required"]):
        raise ValueError("Model output schema mismatch")
    if proposal["action"] not in ("wait", "detour"):
        raise ValueError("Model action is not in the approved alternatives")
    forecast = proposal["predicted_clearance_seconds"]
    if (
        type(forecast) not in (int, float)
        or not math.isfinite(forecast)
        or not 0 <= forecast <= 120
    ):
        raise ValueError("Model clearance forecast is invalid")
    if not isinstance(proposal["reason"], str) or not 1 <= len(proposal["reason"]) <= 1600:
        raise ValueError("Model explanation is invalid")
    action = proposal["action"]
    if policy == "onboard_vlm_forecast":
        # Waiting during inference is real mission time. Expired predictions do
        # not grant direct passage: image clearance remains mandatory.
        action = "wait" if max(0, forecast - elapsed_s) <= 22.1666667 else "detour"
    return {
        "action": action,
        "policy": policy,
        "observation_source": "onboard_rgb_px4_ego_mapped_plane",
        "model_proposal": proposal,
        "inference_elapsed_s": elapsed_s,
        "vision_language_model_invoked": True,
        "native_flight_vla_invoked": False,
        "action_conditioned_wam_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
    }


def propose(root, frames, history, policy):
    verify_local_model()
    observed = [
        {
            "seconds_from_first": r["observed_at_s"] - history[0]["observed_at_s"],
            "east_m": r["obstacle_x_m"],
        }
        for r in history
    ]
    request = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": PROMPT + "\nObserved history: " + json.dumps(observed),
                "images": [
                    base64.b64encode((root / r["onboard_frame"]["file"]).read_bytes()).decode()
                    for r in frames
                ],
            }
        ],
        "format": SCHEMA,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "seed": 73, "num_predict": 240, "num_ctx": 8192},
        "keep_alive": "20m",
    }
    request_bytes = json.dumps(request, sort_keys=True).encode()
    (root / "model-request.json").write_bytes(request_bytes)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    receipt = {
        "model": MODEL,
        "model_digest": MODEL_DIGEST,
        "started_at": started_at,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
    }
    try:
        response = _json_call("/api/chat", request, timeout=DEADLINE_S)
        elapsed = time.monotonic() - started
        response_bytes = json.dumps(response, sort_keys=True).encode()
        (root / "model-response.json").write_bytes(response_bytes)
        receipt.update(
            response_sha256=hashlib.sha256(response_bytes).hexdigest(), elapsed_s=elapsed
        )
        if (
            response.get("done") is not True
            or response.get("done_reason") != "stop"
            or response.get("model") != MODEL
        ):
            raise ValueError("Model completion is missing, truncated or mismatched")
        decision = interpret_proposal(
            json.loads(response["message"]["content"]), policy, elapsed_s=elapsed
        )
        receipt["status"] = "proposal_only"
        return decision
    except Exception as exc:
        receipt.update(
            status="rejected",
            elapsed_s=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        (root / "model-invocation.json").write_text(json.dumps(receipt, indent=2))


def verify_model_artifacts(root, frames, history, policy, decision):
    """Check recorded inference without silently rerunning the model."""
    receipt = json.loads((root / "model-invocation.json").read_text())
    request_bytes, response_bytes = (
        (root / "model-request.json").read_bytes(),
        (root / "model-response.json").read_bytes(),
    )
    if (
        receipt.get("model_digest") != MODEL_DIGEST
        or receipt.get("status") != "proposal_only"
        or receipt.get("request_sha256") != hashlib.sha256(request_bytes).hexdigest()
        or receipt.get("response_sha256") != hashlib.sha256(response_bytes).hexdigest()
    ):
        raise ValueError("Model invocation artifact binding mismatch")
    request, response = json.loads(request_bytes), json.loads(response_bytes)
    expected_images = [
        base64.b64encode((root / r["onboard_frame"]["file"]).read_bytes()).decode() for r in frames
    ]
    expected_history = [
        {
            "seconds_from_first": r["observed_at_s"] - history[0]["observed_at_s"],
            "east_m": r["obstacle_x_m"],
        }
        for r in history
    ]
    message = request["messages"]
    if message != [
        {
            "role": "user",
            "content": PROMPT + "\nObserved history: " + json.dumps(expected_history),
            "images": expected_images,
        }
    ]:
        raise ValueError("Model input differs from matched observations")
    if (
        request.get("model") != MODEL
        or response.get("model") != MODEL
        or response.get("done_reason") != "stop"
        or response.get("done") is not True
        or request.get("format") != SCHEMA
        or request.get("think") is not False
        or request.get("stream") is not False
        or request.get("options")
        != {"temperature": 0, "seed": 73, "num_predict": 240, "num_ctx": 8192}
    ):
        raise ValueError("Pinned model completion mismatch")
    expected = interpret_proposal(
        json.loads(response["message"]["content"]), policy, elapsed_s=receipt["elapsed_s"]
    )
    if expected != decision:
        raise ValueError("Dispatched choice differs from recorded model proposal")
    return {
        "verified": True,
        "model": MODEL,
        "model_digest": MODEL_DIGEST,
        "inference_elapsed_s": receipt["elapsed_s"],
    }
