"""Offline Ship return forecast from a saved post-deployment coast state.

This development tool reuses the finite plant/controller and mirrors the v4
return phase machine. Suffix comparison must detect drift in that mirror. It
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
from .starship_attitude_reference import ConditionedGeographicFrame
from .starship_entry_trim import prepare_entry_trim, entry_preferred, MAXIMUM_ROLL_RATE_RAD_S
from .starship_retained_return import terminal_budget
from .starship_sixdof_contact import find_contact, hull_clearance
from .starship_sixdof_mission import vehicle, point_state, control, _attitude, _sample

STATE_KEYS = tuple(f.name for f in fields(dyn.State6DOF))
SNAPSHOT_SCHEMA = "missionos.ship_return_prediction_origin.v1"


def snapshot(sample, *, retained_count):
    """Capture only a coast state; all actuators are required, never reset.

    Coast has no prepared entry trim or transported return frame. Resumption
    from entry/terminal phases requires controller history and is unsupported.
    """
    if sample.get("phase") != "orbital_coast":
        raise ValueError("only_post_deployment_orbital_coast_supported")
    return {"schema": SNAPSHOT_SCHEMA, "basis": "saved_plant_state_development_only",
            "retained_count": retained_count, "state": {k: deepcopy(sample[k]) for k in STATE_KEYS}}


def validate_origin(origin, profile, return_time_s, duration_s):
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
    if (type(return_time_s) not in (float, int) or not math.isfinite(return_time_s)
            or not state.time_s <= return_time_s <= state.time_s+180.
            or type(duration_s) not in (float, int) or not math.isfinite(duration_s)
            or not return_time_s-state.time_s < duration_s <= 4000.):
        raise ValueError("prediction_time_outside_development_bounds")
    orbit = env.orbital_elements(point_state(state))
    if orbit["status"] != "bound" or dyn.observe(state, craft)["altitude_m"] < 150000.:
        raise ValueError("orbital_coast_origin_required")
    return state, craft


def forecast(origin, profile, *, return_time_s, duration_s=3600.):
    """Advance finite 6DOF under one time candidate; retain failed outcomes.

    No payload release is allowed after the snapshot. The requested time is a
    lower bound: the original integrator's first control tick at/after it wins.
    Limits apply to this development tool, not a grant of flight authority.
    """
    before = deepcopy(origin)
    p = deepcopy(profile)
    s, v = validate_origin(origin, p, return_time_s, duration_s)
    g, geometry = p["guidance"], p["geometry"]
    started, end = time.monotonic(), s.time_s+duration_s
    phase, termination = "orbital_coast", "time_limit"
    prepared, frame, contact = None, None, None
    trim_attempts, next_trim, steps = 0, 0., 0
    samples, events = [], []
    next_sample = s.time_s

    def event(name, **data):
        events.append({"time_s": s.time_s, "event": name, **data})

    while s.time_s < end-1e-8:
        ps, observed = point_state(s), dyn.observe(s, v)
        altitude = observed["altitude_m"]
        up, east, north = env.local_frame(ps)
        radial = env.dot(s.v_eci_mps, up)
        tangent = env.add(s.v_eci_mps, env.scale(up, -radial))
        tangent = env.unit(tangent) if env.norm(tangent) > 1 else east
        target, throttle, count = _attitude(tangent, north), 0., 0
        if phase == "orbital_coast":
            if s.time_s >= return_time_s:
                phase = "deorbit_slew"
                event("return_requested")
                continue
        elif phase in ("deorbit_slew", "deorbit_burn"):
            target = _attitude(env.scale(tangent, -1), north)
            axis = dyn.rotate(s.q_body_to_eci, (0., 0., 1.))
            if env.dot(axis, env.scale(tangent, -1)) > math.cos(math.radians(g["deorbit_max_alignment_deg"])):
                if phase == "deorbit_slew":
                    phase = "deorbit_burn"
                    event("deorbit_ignition_command")
                throttle, count = g["deorbit_throttle"], 3
            orbit = env.orbital_elements(ps)
            if orbit["perigee_altitude_m"] <= g["deorbit_perigee_m"] or s.propellant_kg <= p["ship"]["return_reserve_kg"]:
                phase = "ballistic_return"
                event("deorbit_cutoff_command")
                continue
        elif phase == "ballistic_return":
            flow = env.unit(env.air_relative_velocity(ps))
            lift_up = env.add(up, env.scale(flow, -env.dot(up, flow)))
            lift_up = env.unit(lift_up) if env.norm(lift_up) > 1e-8 else north
            alpha = math.radians(g["entry_alpha_deg"])
            axis = env.add(env.scale(flow, math.cos(alpha)), env.scale(lift_up, math.sin(alpha)))
            target = _attitude(axis, env.scale(north, -1))
            if prepared is None and trim_attempts < 3 and s.time_s >= next_trim and observed["dynamic_pressure_pa"] > 1e-12:
                actual_axis = dyn.rotate(s.q_body_to_eci, (0., 0., 1.))
                if env.dot(actual_axis, axis) > math.cos(math.radians(5.)) and env.norm(s.omega_body_rad_s) < .02:
                    candidate = prepare_entry_trim(s, v, axis, flow, target)
                    trim_attempts += 1
                    next_trim = s.time_s+30.
                    if candidate["status"] == "prepared":
                        prepared = candidate
                        samples.append(_sample(s, v, phase))
                        event("entry_trim_prepared", preparation=prepared)
            preferred, reference = entry_preferred(axis, flow, target, prepared or {})
            if frame is None:
                frame = ConditionedGeographicFrame(target, s.time_s, maximum_roll_rate_rad_s=MAXIMUM_ROLL_RATE_RAD_S)
            target = frame.target(axis, reference, preferred, time_s=s.time_s, force_bridge=True)
            budget = terminal_budget(_sample(s, v, phase), p)
            if budget["trigger"]:
                event("retained_return_terminal_trigger", budget=budget)
                phase = "landing_burn"
                event("flip_and_landing_command")
                continue
        elif phase == "landing_burn":
            air_velocity = env.air_relative_velocity(ps)
            vertical = env.dot(air_velocity, up)
            clearance = hull_clearance(s, v, geometry["ship_length_m"], geometry["radius_m"])["signed_clearance_m"]
            desired = -max(g["landing_target_speed_mps"], min(100., max(0., clearance)/g["landing_height_response_s"]))
            acceleration = env.norm(env.gravity_acceleration(s.r_eci_m))+(desired-vertical)/g["landing_velocity_response_s"]
            horizontal = env.add(air_velocity, env.scale(up, -vertical))
            lateral = env.scale(horizontal, -1/g["landing_horizontal_response_s"])
            lateral_limit = max(0., acceleration)*math.tan(math.radians(g["landing_max_tilt_deg"]))
            if env.norm(lateral) > lateral_limit:
                lateral = env.scale(lateral, lateral_limit/env.norm(lateral))
            axis = env.add(env.scale(up, max(.1, acceleration)), lateral)
            target = _attitude(axis, north)
            if frame is not None:
                target = frame.target(axis, north, target, time_s=s.time_s,
                                      force_bridge=True, defer_geographic_reacquisition=True)
            tilt = env.dot(dyn.rotate(s.q_body_to_eci, (0., 0., 1.)), up)
            if tilt > .5:
                force = max(0., acceleration)*observed["mass_kg"]/tilt
                count = max(1, min(3, math.ceil(force/p["ship"]["engine_thrust_n"])))
                throttle = max(.4, min(1., force/(count*p["ship"]["engine_thrust_n"])))
            else:
                throttle, count = g["flip_min_throttle"], 3
        bounded = phase in ("ballistic_return", "landing_burn")
        interval = p["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100000 else p["integration"]["coast_dt_s"]
        command, diagnostics = control(s, v, target, throttle, count, p, use_flaps=bounded,
            development_fin_allocation=bounded, control_interval_s=interval,
            trim_angles_rad=prepared["trim_angles_rad"] if prepared else None,
            development_entry_preposition=bounded and phase == "ballistic_return" and prepared is not None,
            development_fin_policy="finite_moment_priority_fins_v1" if bounded else "finite_regularized_fins_v1")
        angular_sample_due = bool(samples and env.norm(s.omega_body_rad_s)*(s.time_s-samples[-1]["time_s"]) > .2)
        if s.time_s >= next_sample-1e-9 or angular_sample_due or not samples or samples[-1]["phase"] != phase:
            samples.append(_sample(s, v, phase, command, diagnostics))
            next_sample = s.time_s+p["integration"]["sample_interval_s"]
        if env.norm(s.omega_body_rad_s) > 5:
            termination = "angular_rate_envelope_exceeded"
            event(termination)
            break
        dt = min(end-s.time_s, interval)
        try:
            if altitude < max(2000., geometry["ship_length_m"]+2*observed["air_speed_mps"]*dt):
                s, receipt = find_contact(s, v, command, dt, geometry["ship_length_m"], geometry["radius_m"])
                if receipt["contact"]:
                    contact = receipt
                    termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
                    event(termination)
                    steps += 1
                    break
            else:
                s = dyn.step(s, v, command, dt)
        except (ValueError, OverflowError, FloatingPointError) as error:
            termination = "numerical_failure"
            event(termination, detail=f"{type(error).__name__}: {error}")
            break
        steps += 1
    samples.append(_sample(s, v, phase))
    if origin != before:
        raise AssertionError("prediction_mutated_origin")
    return {"schema": "missionos.ship_return_prediction.v1", "origin": before,
            "return_time_s": return_time_s, "horizon_s": duration_s,
            "events": events, "samples": samples, "final_state": asdict(s),
            "outcome": {"termination": termination, "contact_receipt": contact, "integration_steps": steps},
            "wall_time_s": time.monotonic()-started, "prediction_is_execution": False,
            "runtime_admission": False, "observation_uncertainty_qualified": False,
            "recovery_area_qualified": False, "waiting_endurance_qualified": False}
