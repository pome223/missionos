"""Optional CPU pad forecasts may request waiting, never create entry authority.

This module is also copied to the isolated aircraft executor. It has no model,
GPU, simulator or Mission Assurance dependencies.
"""

from __future__ import annotations
import math


def summarize(config, request, receipt):
    policy = config["world"].get("pad_state_advisory")
    if not policy:
        raise ValueError("Pad advisory was not enabled")
    if (
        receipt.get("schema") != "missionos.pad-state-advisory.v1"
        or receipt.get("request_id") != request["request_id"]
        or receipt.get("model_sha256") != policy["weights_sha256"]
        or receipt.get("camera_entity") != policy["camera_entity"]
        or receipt.get("history_sha256") != (request.get("pad_camera_history") or {}).get("sha256")
        or receipt.get("flight_authority_created") is not False
    ):
        raise ValueError("Unbound auxiliary forecast")
    if receipt.get("status") == "unavailable":
        return "unavailable_fallback_to_current_rules"
    forecast = receipt.get("forecast", {})
    if (
        receipt.get("status") != "computed"
        or forecast.get("schema") != "missionos.pad-state-forecast.v1"
        or type(forecast.get("supported")) is not bool
        or any(
            forecast.get(k) is not False
            for k in (
                "flight_admitted",
                "dispatch_invoked",
                "action_conditioning_verified",
                "interval_collision_verified",
            )
        )
    ):
        raise ValueError("Invalid auxiliary model boundary")
    stamp = forecast.get("input_last_stamp_ns")
    now = request["observations"][-1]["sim_s"]
    if type(stamp) is not int or not math.isfinite(now) or not 0 <= now - stamp / 1e9 <= 1:
        return "stale_fallback_to_current_rules"
    entries = forecast.get("forecasts", [])
    if len(entries) != 17:
        raise ValueError("Incomplete sampled forecast")
    for i, item in enumerate(entries):
        if (
            item.get("stamp_ns") != stamp + i * 250_000_000
            or item.get("offset_s") != i / 4
            or item.get("state") not in {"occupied", "clear", "unknown"}
        ):
            raise ValueError("Invalid forecast clock or state")
    if not forecast["supported"]:
        if any(f["state"] != "unknown" for f in entries):
            raise ValueError("Unsupported observation cannot declare occupancy")
        return "unknown_fallback_to_current_rules"
    if policy.get("reentry_risk_advisory", False):
        try:
            from src.runtime.yokohama_pad_reentry_risk import possible_reentry
        except ModuleNotFoundError:
            from yokohama_pad_reentry_risk import possible_reentry
        if possible_reentry(config, request, forecast):
            return "possible_reentry_reobserve"
    future = [f["state"] for f in entries if f["stamp_ns"] / 1e9 > now]
    if "occupied" in future:
        return "future_occupancy_reobserve"
    if not future or "unknown" in future:
        return "unknown_fallback_to_current_rules"
    return "sampled_clear_still_requires_current_rules"


def selected_action(config, request, receipt, baseline):
    signal = summarize(config, request, receipt)
    if config["world"]["pad_state_advisory"]["mode"] == "assist" and signal in {
        "future_occupancy_reobserve",
        "possible_reentry_reobserve",
    }:
        return "wait_at_current_hold"
    return baseline
