"""Source-bound proposals and deterministic rules for Go2 exception supervision.

No LLM, credential or hardware dependency belongs in this module. The host judge
proposes one action; this boundary revalidates the observation and the operator's
mission envelope against fresh simulator state before the executor can act.
"""

from hashlib import sha256
import json
import math
from pathlib import Path
import time


ACTIONS = {"wait", "reroute", "return_home", "request_operator"}
MAX_DECISIONS = 3
MAX_WAIT_SIM_S = 20.0


def supervision_envelope():
    return {
        "maximum_decisions": MAX_DECISIONS,
        "maximum_wait_sim_s": MAX_WAIT_SIM_S,
        "maximum_judgment_hold_wall_s": 35,
        "actions": sorted(ACTIONS),
    }


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def guard_decision(request, response, current, *, waits_used):
    """Return reasons, never reinterpret an invalid proposal as an allowed action."""
    reasons = []
    proposal = response.get("proposal", {})
    if response.get("request_sha256") != digest(request):
        reasons.append("observation_binding_mismatch")
    if response.get("judge_status") != "valid":
        reasons.append("judge_not_valid")
    if not isinstance(proposal, dict) or set(proposal) != {
        "observation_id",
        "action",
        "wait_seconds",
        "rationale",
    }:
        return reasons + ["proposal_schema_invalid"]
    if proposal["observation_id"] != request["observation_id"]:
        reasons.append("observation_id_mismatch")
    action, seconds = proposal["action"], proposal["wait_seconds"]
    if not isinstance(action, str) or action not in ACTIONS:
        reasons.append("action_outside_approved_catalog")
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(seconds)
    ):
        reasons.append("wait_not_finite")
    elif action == "wait":
        if not 1 <= seconds <= MAX_WAIT_SIM_S - waits_used:
            reasons.append("wait_budget_exceeded")
    elif seconds != 0:
        reasons.append("unexpected_wait_parameter")
    if not isinstance(proposal["rationale"], str) or not 1 <= len(proposal["rationale"]) <= 600:
        reasons.append("rationale_invalid")
    for key in ("telemetry_fresh", "base_stable", "geofence_satisfied", "heartbeat_alive"):
        if current.get(key) is not True:
            reasons.append(key)
    if (
        current.get("operator_cancel_requested")
        or current.get("safety_violation_observed")
        or current.get("obstacle_contacts")
    ):
        reasons.append("operating_state_changed")
    xy, previous = current.get("ground_truth_xy"), request["state"].get("ground_truth_xy")
    if xy is None or previous is None or math.dist(xy, previous) > 0.25:
        reasons.append("origin_drift_exceeded")
    if current.get("map_revision") != request["state"].get("map_revision"):
        reasons.append("map_changed_since_observation")
    if action == "reroute" and not current.get("delivery_route_available"):
        reasons.append("delivery_route_unavailable")
    if action == "return_home" and (
        request["leg"] != "outbound" or not current.get("home_route_available")
    ):
        reasons.append("return_outside_envelope_or_unavailable")
    return reasons


class FileSupervisor:
    """Simulator-side IPC. Hold physics at real-time pace while the host judges."""

    def __init__(self, folder: Path, emit):
        self.folder, self.emit = folder / "supervision", emit
        self.folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, request, client):
        name = request["observation_id"]
        (self.folder / f"{name}.request.json").write_text(json.dumps(request))
        client.phase = "Supervisor judging"
        self.emit(dict(event="go2_supervision_requested", request=request))
        path = self.folder / f"{name}.response.json"
        deadline = time.monotonic() + 35
        while not path.exists() and time.monotonic() < deadline:
            if client.cancel_requested():
                break
            started = time.monotonic()
            client.wait(0.1)
            time.sleep(max(0, 0.1 - (time.monotonic() - started)))
        if not path.exists() or time.monotonic() >= deadline:
            return {
                "request_sha256": digest(request),
                "judge_status": "unavailable",
                "proposal": {},
            }
        result = json.loads(path.read_text())
        client.wait(0.1)  # Fresh measurement after receipt, not a pre-inference snapshot.
        return result
