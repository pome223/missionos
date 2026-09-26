"""Compose separately verified native model chains in one observed flight."""

import json
import math

CONTRACT = {
    "schema_version": "ship_native_integration.v1",
    "sequence": "native_vla_motion_then_new_stationary_wam_history_then_route",
    "history_after_vla_handoff": True,
    "fixture_fallback_allowed": False,
    "dispatch_allowed": False,
}


def verify_integration(root, config, samples, events, vla, onboard):
    urban = config["urban"]
    wam = (onboard or {}).get("anwm_verification") or {}
    if (
        urban.get("native_integration") != CONTRACT
        or not urban.get("aerovla_live")
        or not urban.get("anwm_static")
        or urban.get("policy") != "onboard_anwm_static"
        or "stationary_obstacle_east_m" not in urban
        or not vla
        or vla.get("verified") is not True
        or vla.get("vla_inference_invoked") is not True
        or not (vla.get("native_inference_chain") or {}).get("verified")
        or vla.get("mission_motion_resumed") is not True
        or wam.get("verified") is not True
        or wam.get("wam_invoked") is not True
        or (onboard or {}).get("reasons")
    ):
        raise ValueError("Both native chains must independently verify")
    names = (
        "vla_execution_observed",
        "native_history_started",
        "native_history_captured",
        "urban_decision",
        "anwm_dispatch_revalidated",
        "urban_requested",
        "vla_mission_resumed",
    )
    named = {}
    for name in names:
        found = [e for e in events if e["event"] == name]
        if len(found) != 1 or found[0].get("run_id") != config["run_id"]:
            raise ValueError("Native integration event missing or mixed across runs")
        named[name] = found[0]
    times = [e["elapsed_s"] for e in named.values()]
    if not all(math.isfinite(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Native integration stage order invalid")
    matches = [
        r
        for r in samples
        if r["elapsed_s"] == named["native_history_started"]["source_sample_elapsed_s"]
    ]
    if len(matches) != 1:
        raise ValueError("Native capture start observation missing")
    anchor = matches[0]
    if (
        not times[0] <= anchor["elapsed_s"] <= times[1]
        or anchor.get("nav_state") != 4
        or anchor.get("arming_state") != 2
        or anchor.get("run_id") != config["run_id"]
        or anchor.get("world_sha256") != config["world_sha256"]
    ):
        raise ValueError("WAM history did not start after native LOITER handoff")
    capture = json.loads((root / "native-capture/capture.json").read_text())
    if any(capture[k] != config[k] for k in ("run_id", "world_sha256", "plan_sha256")):
        raise ValueError("Native history belongs to another flight")
    stamps = [f["simulation_time_ns"] for f in capture["frames"]]
    anchor_stamp = anchor["onboard_frame"]["sensor_stamp_s"] * 1e9
    if (
        len(stamps) != 20
        or not math.isfinite(anchor_stamp)
        or stamps[0] <= anchor_stamp
        or any(not 246_000_000 <= b - a <= 254_000_000 for a, b in zip(stamps, stamps[1:]))
    ):
        raise ValueError("WAM history predates handoff or lacks continuous 4 Hz samples")
    decision = named["urban_decision"]["decision"]
    if decision.get("wam_invoked") is not True or decision.get("policy") != urban["policy"]:
        raise ValueError("Native WAM prediction was not used for the route")
    return {
        "verified": True,
        "run_id": config["run_id"],
        "sequence": CONTRACT["sequence"],
        "native_vla_request_id": vla["native_inference_chain"]["request_id"],
        "native_wam_request_id": decision["anwm"]["request_id"],
        "new_history_after_vla_handoff": True,
        "vla_observed_displacement_m": vla["observed_displacement_m"],
        "wam_prediction_error_m": wam["maximum_prediction_position_error_m"],
        "stage_times_s": dict(zip(names, times)),
        "physical_execution_invoked": False,
    }
