"""Offline Ship return forecast from a saved post-deployment coast state.

This development tool uses the same return controller, sampling and plant
stepping as the flight loop. Saved-suffix comparison detects regressions. It
does not dispatch, extend the registered certificate, estimate hidden state,
model waiting endurance, or establish real vehicle accuracy.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
import json
import math
import time

from . import starship_physics as env, starship_sixdof as dyn
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_ship_return import ReturnController, control_interval, record_sample, advance_plant
from .starship_sixdof_mission import vehicle, point_state, _sample

STATE_KEYS = tuple(f.name for f in fields(dyn.State6DOF))
SNAPSHOT_SCHEMA = "missionos.ship_return_prediction_origin.v1"
WINDOW_SCHEMA = "missionos.ship_return_opportunity_window.v1"


def opportunity_window(scheduled_return_time_s):
    """Fixed original deadline, never a rolling extension or runtime grant."""
    return {"schema": WINDOW_SCHEMA, "scheduled_return_time_s": scheduled_return_time_s,
            "maximum_delay_s": 6000.}


def snapshot(sample, *, retained_count):
    """Capture only a coast state; all actuators are required, never reset.

    Coast has no prepared entry trim or transported return frame. Resumption
    from entry/terminal phases requires controller history and is unsupported.
    """
    if sample.get("phase") != "orbital_coast":
        raise ValueError("only_post_deployment_orbital_coast_supported")
    return {"schema": SNAPSHOT_SCHEMA, "basis": "saved_plant_state_development_only",
            "retained_count": retained_count, "state": {k: deepcopy(sample[k]) for k in STATE_KEYS}}


def validate_origin(origin, profile, return_time_s, duration_s, *, window=None):
    if (set(origin) != {"schema", "basis", "retained_count", "state"}
            or origin["schema"] != SNAPSHOT_SCHEMA
            or origin["basis"] != "saved_plant_state_development_only"
            or type(origin["retained_count"]) is not int
            or not 0 <= origin["retained_count"] <= profile["payload"]["count"]
            or set(origin["state"]) != set(STATE_KEYS)):
        raise ValueError("invalid_prediction_origin")
    json.dumps(origin, allow_nan=False)
    state = dyn.state_from_dict(origin["state"])
    craft = vehicle(profile, payload_count=origin["retained_count"])
    if (len(state.engine_states) != len(craft.engines)
            or len(state.flap_angles_rad) != len(craft.aero_panels)
            or abs(sum(x*x for x in state.q_body_to_eci)-1.) > 1e-8):
        raise ValueError("complete_actuator_and_unit_attitude_state_required")
    latest, horizon = state.time_s+180., 4000.
    if window is not None:
        if (not isinstance(window, dict)
                or set(window) != {"schema", "scheduled_return_time_s", "maximum_delay_s"}
                or window["schema"] != WINDOW_SCHEMA or window["maximum_delay_s"] != 6000.
                or type(window["scheduled_return_time_s"]) not in (int, float)
                or not math.isfinite(window["scheduled_return_time_s"])
                or not state.time_s <= window["scheduled_return_time_s"] <= state.time_s+180.):
            raise ValueError("invalid_fixed_opportunity_window")
        latest, horizon = window["scheduled_return_time_s"]+6000., 10000.
    if (type(return_time_s) not in (float, int) or not math.isfinite(return_time_s)
            or not state.time_s <= return_time_s <= latest
            or type(duration_s) not in (float, int) or not math.isfinite(duration_s)
            or not return_time_s-state.time_s < duration_s <= horizon):
        raise ValueError("prediction_time_outside_development_bounds")
    orbit = env.orbital_elements(point_state(state))
    if orbit["status"] != "bound" or dyn.observe(state, craft)["altitude_m"] < 150000.:
        raise ValueError("orbital_coast_origin_required")
    return state, craft


def forecast(origin, profile, *, return_time_s, duration_s=3600., window=None):
    """Advance finite 6DOF under one time candidate; retain failed outcomes.

    No payload release is allowed after the snapshot. The requested time is a
    lower bound: the original integrator's first control tick at/after it wins.
    Limits apply to this development tool, not a grant of flight authority.
    """
    before = deepcopy(origin)
    p = deepcopy(profile)
    s, v = validate_origin(origin, p, return_time_s, duration_s, window=window)
    geometry = p["geometry"]
    started, end = time.monotonic(), s.time_s+duration_s
    controller = ReturnController(TRIMMED_POLICY_ID, return_time_s,
        p["payload"]["count"]-origin["retained_count"], allocation_enabled=True)
    termination, contact, steps = "time_limit", None, 0
    samples, events = [], []
    next_sample = s.time_s

    def event(name, detail="", **data):
        events.append({"time_s": s.time_s, "event": name, "detail": detail, **data})

    while s.time_s < end-1e-8:
        observed = dyn.observe(s, v)
        altitude = observed["altitude_m"]
        step = controller.update(s, v, p, samples, event)
        s = step.state
        if step.transition_only:
            continue
        command, diagnostics = controller.command(step, v, p, altitude)
        next_sample = record_sample(s, v, controller.phase, command, diagnostics, samples, next_sample, p)
        if env.norm(s.omega_body_rad_s) > 5:
            termination = "angular_rate_envelope_exceeded"
            event(termination)
            break
        dt = min(end-s.time_s, control_interval(p, step.throttle, altitude))
        try:
            s, receipt = advance_plant(s, v, command, dt, geometry["ship_length_m"],
                geometry["radius_m"], altitude, observed["air_speed_mps"])
            if receipt is not None and receipt["contact"]:
                contact = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
                event(termination)
                steps += 1
                break
        except (ValueError, OverflowError, FloatingPointError) as error:
            termination = "numerical_failure"
            event(termination, detail=f"{type(error).__name__}: {error}")
            break
        steps += 1
    samples.append(_sample(s, v, controller.phase))
    if origin != before:
        raise AssertionError("prediction_mutated_origin")
    return {"schema": "missionos.ship_return_prediction.v1", "origin": before,
            "opportunity_window": deepcopy(window),
            "return_time_s": return_time_s, "horizon_s": duration_s,
            "events": events, "samples": samples, "final_state": asdict(s),
            "return_controller": controller.to_dict(),
            "outcome": {"termination": termination, "contact_receipt": contact, "integration_steps": steps},
            "wall_time_s": time.monotonic()-started, "prediction_is_execution": False,
            "runtime_admission": False, "observation_uncertainty_qualified": False,
            "recovery_area_qualified": False, "waiting_endurance_qualified": False}
