"""JSON contract for a captured entry-coast controller context, without dynamics."""
from __future__ import annotations

from hashlib import sha256
import json
import math

SCHEMA = "missionos.starship_landing_context.v1"
DELAYS = (None, 0., 2., 4.)  # None is the unchanged legacy landing trigger.
MAX_HORIZON_S = 90.


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _axis(value, size=3):
    return type(value) is list and len(value) == size and all(_number(x) for x in value)


def validate(snapshot, profile, config):
    if type(snapshot) is not dict or set(snapshot) != {
            "schema", "state", "context", "profile_sha256", "catch_profile_sha256", "deadline_s"}:
        raise ValueError("invalid_landing_context")
    state, ctx = snapshot["state"], snapshot["context"]
    if (snapshot["schema"] != SCHEMA or type(state) is not dict or type(ctx) is not dict
            or set(ctx) != {"phase", "previous_axis", "entry_axis", "next_entry_update_s", "entry_diagnostic", "reference"}
            or snapshot["profile_sha256"] != digest(profile) or snapshot["catch_profile_sha256"] != digest(config)):
        raise ValueError("invalid_landing_context")
    now, deadline, ref = state.get("time_s"), snapshot["deadline_s"], ctx["reference"]
    if (not _number(now) or not _number(deadline) or not now < deadline <= now+1200
            or ctx["phase"] != "recovery_entry_coast"
            or not _axis(ctx["entry_axis"]) or abs(sum(x*x for x in ctx["entry_axis"])-1) > 1e-8
            or ctx["previous_axis"] is not None and (
                not _axis(ctx["previous_axis"]) or abs(sum(x*x for x in ctx["previous_axis"])-1) > 1e-8)
            or not _number(ctx["next_entry_update_s"]) or not now-.25000001 <= ctx["next_entry_update_s"] <= now+1.00000001
            or type(ctx["entry_diagnostic"]) is not dict
            or type(ref) is not dict or set(ref) != {"quaternion", "time_s", "maximum_roll_rate_rad_s", "bridging"}
            or not _axis(ref["quaternion"], 4) or abs(sum(x*x for x in ref["quaternion"])-1) > 1e-8
            or type(ref["bridging"]) is not bool or not _number(ref["time_s"])
            or not now-.25000001 <= ref["time_s"] <= now
            or not _number(ref["maximum_roll_rate_rad_s"]) or ref["maximum_roll_rate_rad_s"] <= 0
            or ref["maximum_roll_rate_rad_s"] != profile["guidance"]["attitude_frequency_rad_s"]):
        raise ValueError("invalid_landing_context")
    if len(json.dumps(snapshot, allow_nan=False)) > 32768:
        raise ValueError("oversized_landing_context")


def capture(state, reference, profile, config, *, deadline_s, previous_axis, entry_axis,
            next_entry_update_s, entry_diagnostic):
    snapshot = {"schema": SCHEMA, "state": state, "deadline_s": deadline_s,
        "profile_sha256": digest(profile), "catch_profile_sha256": digest(config),
        "context": {"phase": "recovery_entry_coast", "previous_axis": previous_axis, "entry_axis": entry_axis,
            "next_entry_update_s": next_entry_update_s, "entry_diagnostic": entry_diagnostic,
            "reference": {"quaternion": reference.frame.quaternion, "time_s": reference.time_s,
                "maximum_roll_rate_rad_s": reference.maximum_roll_rate_rad_s, "bridging": reference.bridging}}}
    # The retained JSON is the input to both forecast and checker, not a live
    # reference to caller memory. This changes no physical/control value.
    snapshot = json.loads(json.dumps(snapshot, allow_nan=False))
    validate(snapshot, profile, config)
    return snapshot


def request(snapshot, profile, config, delay_s, duration_s):
    validate(snapshot, profile, config)
    if (delay_s is not None and (not _number(delay_s) or delay_s not in DELAYS[1:])
            or not _number(duration_s) or not 0 < duration_s <= MAX_HORIZON_S
            or snapshot["state"]["time_s"]+duration_s > snapshot["deadline_s"]+1e-9
            or delay_s is not None and delay_s >= duration_s):
        raise ValueError("invalid_landing_forecast_budget")
    return {"schema": "missionos.starship_landing_forecast_request.v1", "origin_sha256": digest(snapshot),
            "delay_s": delay_s, "duration_s": duration_s,
            "prediction_is_execution": False, "production_policy_admitted": False}
