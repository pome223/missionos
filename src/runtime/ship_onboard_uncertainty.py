"""Initial-only uncertainty filter for the existing camera-based urban scene.

This is a new runtime adaptation, not the two-dynamic-route CPU protocol. It
uses the existing approved, static detour and its geometric time estimate.
No actor identity, scripted motion, future truth or unobserved second gate is
manufactured as a policy observation.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np

from src.runtime.ship_urban_decision import choose_urban_action


CONTRACT = {
    "schema_version": "ship_onboard_initial_gate.v1",
    "rms_limit_m": 0.1,
    "brake_after_s": [0, 2, 4, 6, 8],
    "brake_decelerations_mps2": [0.3, 0.6],
    "sample_s": 0.5,
    "speed_bound_mps": 4.0,
    "clearance_x_m": 14.0,
    "reserve_s": 1.0,
    "reference_policy": "onboard_stopping",
    "timing_basis": "mapped extra path 170m / requested airspeed + 8s turns; not measured flight time",
    "fallback": "retain stopping-aware rule; direct still needs two fresh clear images",
    "scope": "initial hold only; static mapped detour; no change while flying",
}


def _position(parameters, seconds):
    x, v, a = parameters
    bound = CONTRACT["speed_bound_mps"]
    v = max(-bound, min(bound, v))
    cap_time = max(0.0, ((bound if a > 0 else -bound) - v) / a) if a else math.inf
    first = min(seconds, cap_time)
    end_v = max(-bound, min(bound, v + a * first))
    return x + v * first + a * first**2 / 2 + end_v * (seconds - first), end_v


def _clearance_time(parameters, *, after=None, deceleration=None, horizon):
    consecutive = 0
    for tick in range(int(horizon / CONTRACT["sample_s"]) + 1):
        t = tick * CONTRACT["sample_s"]
        if after is None or t <= after:
            x = _position(parameters, t)[0]
        else:
            x, v = _position(parameters, after)
            duration = min(t - after, abs(v) / deceleration)
            sign = 1 if v > 0 else -1 if v < 0 else 0
            x += v * duration - sign * deceleration * duration**2 / 2
        consecutive = consecutive + 1 if x >= CONTRACT["clearance_x_m"] else 0
        if consecutive >= 2:
            return t
    return None


def choose_initial_gate(points, baseline, *, airspeed_mps):
    # Strict scalar contract; image metadata are bound separately by the caller.
    choose_urban_action(points, "constant_velocity", airspeed_mps=airspeed_mps)
    if len(points) < 5:
        raise ValueError("Initial uncertainty filter requires five observed frames")
    if baseline.get("policy") != "onboard_stopping" or baseline.get("action") not in (
        "wait",
        "detour",
    ):
        raise ValueError("Uncertainty filter requires the stopping-aware baseline")
    t = np.array([v["observed_at_s"] - points[-1]["observed_at_s"] for v in points])
    y = np.array([v["obstacle_x_m"] for v in points])
    fits = {}
    for degree, label in ((1, "cv"), (2, "ca")):
        c = np.polynomial.polynomial.polyfit(t, y, degree)
        fits[label] = {
            "parameters": [float(c[0]), float(c[1]), float(2 * c[2]) if degree == 2 else 0.0],
            "rms_m": float(np.sqrt(np.mean((np.polynomial.polynomial.polyval(t, c) - y) ** 2))),
        }
    horizon = baseline["estimated_extra_detour_s"]
    record = {
        "contract": CONTRACT,
        "history_sha256": hashlib.sha256(
            json.dumps(points, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "observed_through_s": points[-1]["observed_at_s"],
        "baseline_action": baseline["action"],
        "fits": fits,
        "horizon_s": horizon,
        "action_changed": False,
        "dispatch_allowed": False,
    }

    def result(reason, action=None):
        action = baseline["action"] if action is None else action
        return {
            **baseline,
            "policy": "onboard_uncertainty",
            "action": action,
            "uncertainty_gate": {
                **record,
                "reason": reason,
                "action_changed": action != baseline["action"],
            },
        }

    if fits["ca"]["rms_m"] > CONTRACT["rms_limit_m"]:
        return result("poor_fit_retain_baseline")
    nominal = _clearance_time(fits["ca"]["parameters"], horizon=horizon)
    record["nominal_clearance_s"] = nominal
    if baseline["action"] == "wait" and nominal is not None:
        return result("baseline_predicted_feasible")
    predictions = [nominal]
    if fits["cv"]["rms_m"] <= CONTRACT["rms_limit_m"]:
        predictions.append(_clearance_time(fits["cv"]["parameters"], horizon=horizon))
    for after in CONTRACT["brake_after_s"]:
        for deceleration in CONTRACT["brake_decelerations_mps2"]:
            predictions.append(
                _clearance_time(
                    fits["ca"]["parameters"],
                    after=after,
                    deceleration=deceleration,
                    horizon=horizon,
                )
            )
    record["predicted_clearance_s"] = predictions
    if baseline["action"] == "wait" and all(v is None for v in predictions):
        return result("all_hypotheses_miss_wait_window", "detour")
    if baseline["action"] == "detour" and all(
        v is not None and v <= horizon - CONTRACT["reserve_s"] for v in predictions
    ):
        return result("all_hypotheses_favor_wait", "wait")
    return result("hypotheses_disagree_retain_baseline")
