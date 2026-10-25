"""Bounded stage-1 candidate screen, never runtime fault-return admission.

Shared production phase machine and finite actuators; no new force or pose reset.
Truth-state capability is an offline experimental input, not an AI observation.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
import time

from . import starship_physics as env, starship_sixdof as dyn
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_return_prediction import validate_origin
from .starship_ship_return import ReturnController, advance_plant, control_interval, record_sample
from .starship_sixdof_mission import _sample


def apply_fault(state, craft, fault):
    """Persistent physical capability change, identical for both procedures."""
    if fault is None:
        return state, craft
    if fault["kind"] == "landing_engine_out":
        i = fault["engine_index"]
        if type(i) is not int or i not in (0, 1, 2) or fault["thrust_factor"] != 1.:
            raise ValueError("unsupported_engine_loss")
        engines = list(state.engine_states)
        engines[i] = replace(engines[i], available=False)
        return replace(state, engine_states=tuple(engines)), craft
    if fault["kind"] == "landing_engine_derate":
        factor = fault["thrust_factor"]
        if factor not in (.8, .6, .4) or fault["engine_index"] is not None:
            raise ValueError("unsupported_derate")
        return state, replace(craft, engines=tuple(
            replace(e, max_thrust_n=e.max_thrust_n*factor) if i < 3 else e
            for i, e in enumerate(craft.engines)))
    raise ValueError("unsupported_fault_family")


def capability_profile(profile, craft):
    p = deepcopy(profile)
    # All derated engines share one predeclared factor. No invented performance.
    p["ship"]["engine_thrust_n"] = craft.engines[0].max_thrust_n
    return p


class AvailableCapacityController(ReturnController):
    """Reuse the phase machine; adapt capability and physical engine selection."""

    def update(self, s, v, p, samples, event):
        return super().update(s, v, capability_profile(p, v), samples, event)

    def command(self, step, craft, profile, altitude):
        healthy = [i for i in range(3) if step.state.engine_states[i].available]
        inactive = [i for i in range(3) if i not in healthy]
        order = healthy+inactive+list(range(3, len(craft.engines)))
        active_count = min(step.count, len(healthy))
        p = capability_profile(profile, craft)
        actual = p["ship"]["engine_thrust_n"]
        axis = dyn.rotate(step.state.q_body_to_eci, (0., 0., 1.))
        up = env.unit(step.state.r_eci_m)
        flip = self.phase == "landing_burn" and env.dot(axis, up) <= .5
        demand_thrust = profile["ship"]["engine_thrust_n"] if self.phase == "deorbit_burn" or flip else actual
        requested = step.count*step.throttle*demand_thrust
        throttle = (min(1., max(.4, requested/(active_count*actual)))
                    if active_count and step.throttle > 0 else 0.)
        # This permutation changes control slots, never physical actuator identity.
        mapped_state = replace(step.state, engine_states=tuple(step.state.engine_states[i] for i in order))
        mapped_craft = replace(craft, engines=tuple(craft.engines[i] for i in order))
        mapped_step = replace(step, state=mapped_state, count=active_count, throttle=throttle)
        command, diagnostic = super().command(mapped_step, mapped_craft, p, altitude)
        restored = [None]*len(order)
        for slot, physical in enumerate(order):
            restored[physical] = command.engines[slot]
        diagnostic["fault_return_allocation"] = {
            "candidate": "available_capacity_return_v1",
            "selected_physical_engine_indices": healthy[:active_count] if throttle else [],
            "requested_total_main_thrust_n": requested,
            "commanded_total_main_thrust_n": active_count*throttle*actual,
            "capacity_saturated": bool(requested > active_count*actual+1e-8),
            "actual_main_engine_thrust_limit_n": actual,
            "pose_or_force_assigned": False, "runtime_admitted": False}
        return replace(command, engines=tuple(restored)), diagnostic


def static_two_engine_capacity(origin, profile):
    """Necessary thrust and axial-lever trim estimates; not controllability proof."""
    from .starship_sixdof_mission import vehicle
    craft = vehicle(profile, payload_count=origin["retained_count"])
    gravity = env.EARTH_MU_M3_S2/env.EARTH_RADIUS_M**2 if hasattr(env, "EARTH_RADIUS_M") else env.STANDARD_GRAVITY_MPS2
    rows = []
    for missing in range(3):
        pair = [i for i in range(3) if i != missing]
        for fuel in (28000., 62000., origin["state"]["propellant_kg"]):
            props = dyn.mass_properties(craft, fuel)
            thrust = sum(craft.engines[i].max_thrust_n for i in pair)
            minimum = sum(craft.engines[i].max_thrust_n*craft.engines[i].min_throttle for i in pair)
            centroid = [sum(craft.engines[i].position_body_m[a] for i in pair)/2 for a in range(3)]
            axial = abs(props.com_body_m[2]-centroid[2])
            transverse = math.hypot(props.com_body_m[0]-centroid[0], props.com_body_m[1]-centroid[1])
            trim = math.atan2(transverse, axial)
            limit = min(craft.engines[i].max_gimbal_rad for i in pair)
            remaining = max(0., limit-trim)
            transverse_inertia = max(props.inertia_kg_m2[0][0], props.inertia_kg_m2[1][1])
            rows.append({"missing_engine_index": missing, "surviving_engine_indices": pair,
                "fuel_kg": fuel, "mass_kg": props.mass_kg, "maximum_thrust_n": thrust,
                "minimum_two_engine_thrust_n": minimum,
                "maximum_thrust_to_weight": thrust/(props.mass_kg*gravity),
                "minimum_two_engine_thrust_to_weight": minimum/(props.mass_kg*gravity),
                "common_gimbal_trim_estimate_deg": math.degrees(trim),
                "gimbal_limit_deg": math.degrees(limit),
                "pitch_yaw_acceleration_upper_estimate_rad_s2": minimum*math.sin(remaining)*axial/transverse_inertia,
                "static_thrust_and_trim_necessary_checks_pass": thrust > props.mass_kg*gravity and trim < limit,
                "sixdof_attitude_margin_proven": False})
    return {"schema": "missionos.ship_two_engine_capacity_screen.v1", "rows": rows,
        "assumptions": ["existing generic engine geometry/performance", "surface gravity estimate",
            "equal-thrust axial-lever trim only; not roll/aero/lag controllability",
            "two minimum-throttle engines can exceed weight; terminal selection may use one surviving engine"],
        "runtime_admission": False, "physical_vehicle_identified": False}


def rollout(origin, profile, return_time_s, fault, candidate, *, wall_limit_s, cohort_deadline):
    before = deepcopy(origin)
    original, nominal = validate_origin(origin, profile, return_time_s, 3600.)
    state, craft = apply_fault(original, nominal, fault)
    initial = asdict(state)
    kind = ReturnController if candidate == "original_plan" else AvailableCapacityController
    if candidate not in ("original_plan", "available_capacity_return_v1"):
        raise ValueError("unregistered_candidate")
    controller = kind(TRIMMED_POLICY_ID, return_time_s,
                      profile["payload"]["count"]-origin["retained_count"], allocation_enabled=True)
    started = time.monotonic()
    deadline = min(started+wall_limit_s, cohort_deadline)
    end = state.time_s+3600.
    samples, events = [], []
    contact = None
    termination, error, steps = "time_limit", None, 0
    next_sample = state.time_s

    def event(name, detail="", **data):
        events.append({"time_s": state.time_s, "event": name, "detail": detail, **data})

    try:
        while state.time_s < end-1e-8:
            if time.monotonic() >= deadline:
                termination = "execution_wall_budget"
                break
            obs = dyn.observe(state, craft)
            altitude = obs["altitude_m"]
            step = controller.update(state, craft, profile, samples, event)
            state = step.state
            if step.transition_only:
                continue
            command, diagnostic = controller.command(step, craft, profile, altitude)
            next_sample = record_sample(state, craft, controller.phase, command, diagnostic,
                                       samples, next_sample, profile)
            if env.norm(state.omega_body_rad_s) > 5:
                termination = "angular_rate_envelope_exceeded"
                break
            dt = min(end-state.time_s, control_interval(profile, step.throttle, altitude))
            state, receipt = advance_plant(state, craft, command, dt,
                profile["geometry"]["ship_length_m"], profile["geometry"]["radius_m"], altitude, obs["air_speed_mps"])
            steps += 1
            if receipt is not None and receipt["contact"]:
                contact = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
                break
    except (ValueError, OverflowError, FloatingPointError, InterruptedError) as exc:
        termination, error = "numerical_or_control_failure", {"type": type(exc).__name__, "detail": str(exc)}
    samples.append(_sample(state, craft, controller.phase))
    if before != origin:
        raise AssertionError("mutated_origin")
    return {"schema": "missionos.ship_fault_stage1_rollout.v1", "origin": before,
        "fault": deepcopy(fault), "faulted_initial_state": initial,
        "actual_engine_limits_n": [e.max_thrust_n for e in craft.engines],
        "candidate": candidate, "return_time_s": return_time_s,
        "events": events, "samples": samples, "final_state": asdict(state),
        "outcome": {"termination": termination, "contact_receipt": contact, "integration_steps": steps},
        "error": error, "wall_time_s": time.monotonic()-started,
        "fault_domain_status": "unverified", "no_fault_certificate_used_for_admission": False,
        "runtime_admission": False, "model_calls": 0, "physical_execution": False}


def verify_record(record, expected_origin, expected_profile, fault, candidate, limits):
    """No propagator/controller calls; recompute record and contact relationships."""
    from .starship_sixdof_mission import vehicle
    checks = {}
    metrics = None
    try:
        original = dyn.state_from_dict(expected_origin["state"])
        craft = vehicle(expected_profile, payload_count=expected_origin["retained_count"])
        # Reconstruct expected capabilities independently, not from model output.
        initial = json.loads(json.dumps(asdict(original), allow_nan=False))
        thrusts = [e.max_thrust_n for e in craft.engines]
        if fault and fault["kind"] == "landing_engine_out":
            initial["engine_states"][fault["engine_index"]]["available"] = False
        elif fault and fault["kind"] == "landing_engine_derate":
            for i in range(3):
                thrusts[i] *= fault["thrust_factor"]
        samples = record["samples"]
        checks["same_frozen_origin"] = record["origin"] == expected_origin
        checks["fault_bound"] = record["fault"] == fault
        checks["candidate_bound"] = record["candidate"] == candidate
        checks["faulted_initial_state_bound"] = record["faulted_initial_state"] == initial
        checks["physical_engine_limits_bound"] = record["actual_engine_limits_n"] == thrusts
        checks["initial_sample_matches"] = all(samples[0][k] == v for k, v in initial.items())
        checks["time_advances"] = len(samples) > 1 and samples[-1]["time_s"] > samples[0]["time_s"] and all(
            a["time_s"] <= b["time_s"] for a, b in zip(samples, samples[1:]))
        checks["fuel_nonincreasing"] = all(b["propellant_kg"] <= a["propellant_kg"]+1e-7
                                            for a, b in zip(samples, samples[1:]))
        checks["availability_never_restored"] = all(
            [e["available"] for e in s["engine_states"]] == [e["available"] for e in initial["engine_states"]]
            for s in samples)
        checks["finite_physical_pose"] = all(abs(sum(x*x for x in s["q_body_to_eci"])-1) < 1e-8
            and all(math.isfinite(x) for key in ("r_eci_m", "v_eci_mps", "omega_body_rad_s") for x in s[key]) for s in samples)
        checks["observed_main_thrust_matches_capability"] = all(all(
            math.isclose(s["main_engine_thrust_n"][i], thrusts[i]*s["engine_states"][i]["throttle"]
                if s["engine_states"][i]["available"] and s["propellant_kg"] > 0 else 0., rel_tol=1e-9, abs_tol=1e-6)
            for i in range(6)) for s in samples)
        checks["no_qualified_admission_claim"] = record["runtime_admission"] is False and record["no_fault_certificate_used_for_admission"] is False
        checks["no_numerical_or_budget_failure"] = record["error"] is None and record["outcome"]["termination"] not in (
            "numerical_or_control_failure", "execution_wall_budget", "time_limit", "angular_rate_envelope_exceeded")
        contact = record["outcome"]["contact_receipt"]
        checks["terminal_contact_retained"] = contact is not None and contact["contact"] is True
        if contact is not None:
            x, y, _ = contact["point_eci_m"]
            surface = (-env.EARTH_ROTATION_RAD_S*y, env.EARTH_ROTATION_RAD_S*x, 0.)
            speed = math.sqrt(sum((a-b)**2 for a, b in zip(contact["point_velocity_eci_mps"], surface)))
            w, x, y, z = contact["q_body_to_eci"]
            axis = (2*(x*z+w*y), 2*(y*z-w*x), 1-2*(x*x+y*y))
            tilt = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(axis, contact["surface_normal_eci"]))))))
            final = record["final_state"]
            checks["contact_kinematics"] = math.isclose(speed, contact["surface_relative_speed_mps"], rel_tol=1e-10, abs_tol=1e-8)
            checks["contact_is_final_state"] = contact["event_time_s"] == final["time_s"] and contact["propellant_kg"] == final["propellant_kg"] and contact["q_body_to_eci"] == final["q_body_to_eci"]
            metrics = {"time_s": contact["event_time_s"], "speed_mps": speed,
                "tilt_deg": tilt, "body_rate_rad_s": math.sqrt(sum(x*x for x in final["omega_body_rad_s"])),
                "reserve_kg": contact["propellant_kg"]}
        valid = all(checks.values())
        accepted = bool(valid and metrics and all(metrics[k] <= limits[k] for k in ("speed_mps", "tilt_deg", "body_rate_rad_s"))
                        and metrics["reserve_kg"] >= limits["reserve_kg"])
        return {"record_valid": valid, "checks": checks, "contact": metrics, "contact_limits_met": accepted,
            "sample_count": len(samples), "sixdof_record_consistency_only": True,
            "independent_physics_validation": False, "runtime_admission": False, "fault_domain_status": "unverified"}
    except (KeyError, ValueError, TypeError, IndexError, OverflowError) as exc:
        return {"record_valid": False, "checks": checks, "contact": metrics,
            "contact_limits_met": False, "error": f"{type(exc).__name__}: {exc}", "runtime_admission": False}
