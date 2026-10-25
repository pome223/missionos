"""Step-2 propulsion capability observation; a numeric estimate, never an AI decision.

A synthetic accelerometer, engine throttle telemetry and the existing fuel gauge
feed a numeric landing-engine capability estimator. Its bounded uncertainty,
telemetry latency and consequence previews are packaged as a MissionOS request
for a later decision. Step 2 produces no executable decision and calls no model.

The plant's true capability never enters the estimator, the request or the
controller. It is used only to advance the plant and in offline evaluation.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
import time

import numpy as np

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_ship_capability_matrix as matrix
from . import starship_sixdof_mission as mission
from .starship_mission_director import fuel_sensor
from .starship_ship_return import advance_plant, control_interval, record_sample

SENSOR = {"schema": "missionos.ship_capability_sensor_model.v1",
    "accelerometer_frame": "body", "sample_basis": "each powered control step",
    "noise_sigma_mps2": .02, "bias_sigma_mps2": .005, "telemetry_latency_s": 1.,
    "engine_telemetry": "per-engine availability and throttle state; no thrust or chamber pressure",
    "mass_source": "nominal vehicle model with existing fuel gauge", "gauge_error_bound_kg": 100.,
    "basis": "engineering test settings, not Starship sensor specifications"}
ESTIMATOR = {"schema": "missionos.ship_capability_estimator.v1",
    "minimum_main_throttle_sum": .9, "minimum_samples": 10, "statistical_multiple": 3.,
    "bias_multiple": 3., "model_allowance": .01, "segment_gap_s": .15,
    "burn_data_window_after_cutoff_s": 2.}
REQUEST = {"schema": "missionos.ship_capability_request.v1", "deadline_after_issue_s": 900.,
    "deep_reasoning_minimum_window_s": 150.}
ACTIONS = ("continue_nominal_assumption", "collect_status", "adopt_estimated_capability")
EFFECTS = {
    "continue_nominal_assumption": "Keep the nominal landing-engine capability assumption in the return controller. No control change.",
    "collect_status": "Wait for the complete deorbit-burn telemetry (including any later ignition segment) and reassess with the recomputed estimate. No corrective action.",
    "adopt_estimated_capability": "After deorbit cutoff, replace only the return controller's assumed landing-engine capability with the numeric point estimate named by estimate_id. Plant, guidance code and contact limits are unchanged. Requires an independent check before execution.",
}
FACTOR_RANGE = (.3, 1.2)
MAIN = (0, 1, 2)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


def model_vehicle(nominal, factor):
    """Nominal vehicle with the three landing-engine limits scaled by one factor."""
    if type(factor) not in (int, float) or not math.isfinite(factor) or not FACTOR_RANGE[0] <= factor <= FACTOR_RANGE[1]:
        raise ValueError("capability_factor_outside_model_range")
    return replace(nominal, engines=tuple(
        replace(e, max_thrust_n=e.max_thrust_n*factor) if i in MAIN else e
        for i, e in enumerate(nominal.engines)))


class SwitchAudit(matrix.BoundaryAudit):
    """Step-1.5 boundary audit plus one recorded change of the assumed model."""

    def __init__(self, nominal, model, plant):
        super().__init__(nominal, model, plant)
        self.initial_assumed_limits = list(self.assumed_limits)
        self.switches = []

    def switch(self, time_s, model, basis):
        if self.switches:
            raise ValueError("capability_switch_already_applied")
        self.assumed_limits = [e.max_thrust_n for e in model.engines]
        self.switches.append({"time_s": time_s, "row_index": len(self.rows),
            "engine_limits_n": list(self.assumed_limits), "basis": deepcopy(basis)})

    def to_dict(self):
        return {**super().to_dict(), "initial_assumed_engine_limits_n": self.initial_assumed_limits,
                "switches": deepcopy(self.switches)}


class SwitchingController(matrix.SeparatedController):
    """The step-1.5 separated controller; only its model vehicle may change, once."""

    def __init__(self, nominal, model, profile, return_time_s, released_count, audit):
        if [e.max_thrust_n for e in model.engines] != [e.max_thrust_n for e in nominal.engines]:
            raise ValueError("initial_model_must_be_nominal")
        super().__init__(model, profile, return_time_s, released_count, audit)
        self.nominal, self.switch = nominal, None

    def apply_switch(self, time_s, factor, basis):
        if self.switch is not None:
            raise ValueError("capability_switch_already_applied")
        if self.controller.phase != "ballistic_return" or self.controller.record.get("status") != "active":
            raise ValueError("capability_switch_outside_registered_window")
        self.model = model_vehicle(self.nominal, factor)
        self.audit.switch(time_s, self.model, basis)
        self.switch = {"time_s": time_s, "factor": factor, "basis": deepcopy(basis)}

    def rebind_audit(self, plant):
        """A prediction branch gets a fresh audit bound to its own plant object."""
        audit = SwitchAudit(self.nominal, self.model, plant)
        if self.switch is not None:
            raise ValueError("rebind_before_switch_only")
        self.audit = audit
        return audit


def specific_force_row(state, plant, phase):
    """Truth row for the offline sensor model; never shown to control or AI."""
    o = dyn.observe(state, plant)
    force = env.add(o["thrust_force_body_n"], o["aero_force_body_n"])
    return {"time_s": state.time_s, "phase": phase,
        "true_specific_force_body_mps2": [x/o["mass_kg"] for x in force],
        "engine_throttle_states": [e.throttle for e in state.engine_states],
        "engine_available": [e.available for e in state.engine_states],
        "propellant_kg": state.propellant_kg}


def propagate(state, plant, controller, profile, *, end_time_s, deadline, before_update=None, on_step=None,
              step_boundary_s=None):
    """The step-1.5 rollout loop with read-only/once-only hooks. No state is assigned."""
    samples, events = [], []
    contact, termination, error, steps = None, "time_limit", None, 0
    next_sample = state.time_s

    def event(name, detail="", **data):
        events.append({"time_s": state.time_s, "event": name, "detail": detail, **data})

    try:
        while state.time_s < end_time_s-1e-8:
            if time.monotonic() >= deadline:
                termination = "execution_wall_budget"
                break
            if before_update is not None and before_update(state, controller, events) is False:
                termination = "registered_stop"
                break
            obs = dyn.observe(state, plant)
            altitude = obs["altitude_m"]
            step = controller.update(state, samples, event)
            state = step.state
            if step.transition_only:
                continue
            command, diagnostic = controller.command(step, altitude)
            next_sample = record_sample(state, plant, controller.phase, command, diagnostic,
                                       samples, next_sample, profile)
            if env.norm(state.omega_body_rad_s) > 5:
                termination = "angular_rate_envelope_exceeded"
                break
            dt = min(end_time_s-state.time_s, control_interval(profile, step.throttle, altitude))
            # Optional new-cohort actuation boundary; existing callers retain
            # precisely their original grid when this argument is omitted.
            if step_boundary_s is not None and state.time_s < step_boundary_s-1e-9:
                dt = min(dt, step_boundary_s-state.time_s)
            controller.audit.inspect("plant", state, plant)
            if on_step is not None:
                on_step(state, plant, controller)
            state, receipt = advance_plant(state, plant, command, dt,
                profile["geometry"]["ship_length_m"], profile["geometry"]["radius_m"], altitude, obs["air_speed_mps"])
            steps += 1
            if receipt is not None and receipt["contact"]:
                contact = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
                break
    except (ValueError, OverflowError, FloatingPointError, InterruptedError) as exc:
        termination, error = "numerical_or_control_failure", {"type": type(exc).__name__, "detail": str(exc)}
    samples.append(mission._sample(state, plant, controller.phase))
    return {"state": state, "samples": samples, "events": events, "termination": termination,
            "contact": contact, "error": error, "steps": steps}


def cutoff_time(events):
    times = [e["time_s"] for e in events if e["event"] == "deorbit_cutoff_command"]
    return times[0] if times else None


def measure(rows, factor_index, seed):
    """Apply the registered accelerometer model to truth rows for one seed."""
    rng = np.random.default_rng([20261014, factor_index, seed])
    bias = rng.normal(0., SENSOR["bias_sigma_mps2"], 3)
    measured = []
    for row in rows:
        noise = rng.normal(0., SENSOR["noise_sigma_mps2"], 3)
        measured.append({"time_s": row["time_s"], "phase": row["phase"],
            "measured_specific_force_body_mps2": [float(a+b+c) for a, b, c in
                zip(row["true_specific_force_body_mps2"], bias, noise)],
            "engine_throttle_states": list(row["engine_throttle_states"]),
            "engine_available": list(row["engine_available"]),
            "fuel_gauge_kg": fuel_sensor(row["propellant_kg"], row["time_s"], "ship")})
    return measured


def prepare_telemetry(telemetry, nominal):
    """Per-row nominal-model expectation; uses gauge mass and throttle telemetry only."""
    rows = []
    for row in telemetry:
        throttles = [row["engine_throttle_states"][i] for i in MAIN]
        prepared = {"time_s": row["time_s"], "accepted": bool(all(row["engine_available"][i] for i in MAIN)
            and sum(throttles) >= ESTIMATOR["minimum_main_throttle_sum"])}
        if prepared["accepted"]:
            mass = dyn.mass_properties(nominal, row["fuel_gauge_kg"]).mass_kg
            main = sum(t*nominal.engines[i].max_thrust_n*nominal.engines[i].direction_body[2]
                       for i, t in zip(MAIN, throttles))/mass
            # Other engines are not under estimation: nominal throttle-state force.
            other = sum(row["engine_throttle_states"][i]*e.max_thrust_n*e.direction_body[2]
                        for i, e in enumerate(nominal.engines) if i not in MAIN)/mass
            measured = row["measured_specific_force_body_mps2"][2]
            prepared.update(mass_estimate_kg=mass, expected_main_mps2=main, expected_other_mps2=other,
                            measured_mps2=measured, ratio=(measured-other)/main)
        rows.append(prepared)
    return rows


def estimate(prepared, observed_at_s):
    """Numeric capability estimate from telemetry that has arrived by observed_at_s."""
    latency = SENSOR["telemetry_latency_s"]
    arrived = [r for r in prepared if r["time_s"]+latency <= observed_at_s+1e-9]
    accepted = [r for r in arrived if r["accepted"]]
    n = len(accepted)
    result = {"schema": ESTIMATOR["schema"], "observed_at_s": observed_at_s,
        "data_through_s": max((r["time_s"] for r in arrived), default=None),
        "telemetry_latency_s": latency, "accepted_samples": n, "anomaly": False,
        "factor_estimate": None, "band": None, "half_width": None, "half_width_terms": None,
        "expected_main_acceleration_mps2": None, "measured_axial_acceleration_mps2": None,
        "acceleration_deficit_mps2": None, "segments": []}
    if n >= 2:
        ratios = np.array([r["ratio"] for r in accepted])
        expected = float(np.mean([r["expected_main_mps2"] for r in accepted]))
        mass = float(np.mean([r["mass_estimate_kg"] for r in accepted]))
        factor = float(np.mean(ratios))
        terms = {"statistical": ESTIMATOR["statistical_multiple"]*float(np.std(ratios, ddof=1))/math.sqrt(n),
            "accelerometer_bias": ESTIMATOR["bias_multiple"]*SENSOR["bias_sigma_mps2"]/expected,
            "mass_gauge": factor*SENSOR["gauge_error_bound_kg"]/mass,
            "model_allowance": ESTIMATOR["model_allowance"]}
        half = sum(terms.values())
        segments, current = [], [accepted[0]]
        for row in accepted[1:]:
            if row["time_s"]-current[-1]["time_s"] > ESTIMATOR["segment_gap_s"]:
                segments.append(current)
                current = []
            current.append(row)
        segments.append(current)
        main_measured = float(np.mean([r["measured_mps2"]-r["expected_other_mps2"] for r in accepted]))
        result.update(factor_estimate=factor, band=[factor-half, factor+half], half_width=half,
            half_width_terms=terms, expected_main_acceleration_mps2=expected,
            measured_axial_acceleration_mps2=main_measured,
            acceleration_deficit_mps2=expected-main_measured,
            anomaly=bool(n >= ESTIMATOR["minimum_samples"] and abs(factor-1.) > half),
            segments=[{"start_s": s[0]["time_s"], "end_s": s[-1]["time_s"], "samples": len(s),
                       "factor_estimate": float(np.mean([r["ratio"] for r in s]))} for s in segments])
    result["estimate_id"] = digest({k: v for k, v in result.items() if k != "estimate_id"})
    return result


def first_detection(prepared, step_times):
    """Earliest control-step time at which the registered anomaly rule holds."""
    latency = SENSOR["telemetry_latency_s"]
    arrivals = sorted({r["time_s"]+latency for r in prepared if r["accepted"]})
    for arrival in arrivals:
        issued = next((t for t in step_times if t >= arrival-1e-9), None)
        if issued is None:
            return None
        current = estimate(prepared, issued)
        if current["anomaly"]:
            return current
    return None


def observation(state, plant, phase):
    """The existing ideal kinematic channel plus gauge and engine availability."""
    o = dyn.observe(state, plant)
    return {"time_s": state.time_s, "phase": phase, "altitude_m": o["altitude_m"],
        "speed_mps": env.norm(state.v_eci_mps), "body_rate_rad_s": env.norm(state.omega_body_rad_s),
        "fuel_gauge_kg": fuel_sensor(state.propellant_kg, state.time_s, "ship"),
        "landing_engines_available": [state.engine_states[i].available for i in MAIN]}


def build_request(request_id, row, current, trigger, *, burn_complete, previews=None):
    """Non-executable step-2 request; every number comes from telemetry or previews."""
    allowed = ["continue_nominal_assumption"]
    if not burn_complete:
        allowed.append("collect_status")
    if current["anomaly"]:
        allowed.append("adopt_estimated_capability")
    issued = row["time_s"]
    deadline = issued+REQUEST["deadline_after_issue_s"]
    request = {"schema": REQUEST["schema"], "request_id": request_id, "trigger": trigger,
        "issued_at_s": issued, "deadline_s": deadline, "observation": deepcopy(row),
        "capability_estimate": deepcopy(current), "burn_data_complete": burn_complete,
        "allowed_actions": allowed, "effects": {a: EFFECTS[a] for a in allowed},
        "previews": deepcopy(previews), "deep_reasoning_allowed": deadline-issued >= REQUEST["deep_reasoning_minimum_window_s"],
        "executable": False, "authority": "step2_observation_only_no_dispatch"}
    validate_request(request)
    return request


def validate_request(request):
    keys = {"schema", "request_id", "trigger", "issued_at_s", "deadline_s", "observation",
            "capability_estimate", "burn_data_complete", "allowed_actions", "effects", "previews",
            "deep_reasoning_allowed", "executable", "authority"}
    if type(request) is not dict or set(request) != keys or request["schema"] != REQUEST["schema"]:
        raise ValueError("invalid_capability_request")
    current = request["capability_estimate"]
    if (current.get("estimate_id") != digest({k: v for k, v in current.items() if k != "estimate_id"})
            or request["executable"] is not False or request["trigger"] not in ("propulsion_capability_anomaly", "burn_data_complete")
            or not request["issued_at_s"] < request["deadline_s"]
            or current["observed_at_s"] != request["issued_at_s"]
            or any(a not in ACTIONS for a in request["allowed_actions"])
            or set(request["effects"]) != set(request["allowed_actions"])
            or ("collect_status" in request["allowed_actions"]) == request["burn_data_complete"]
            or ("adopt_estimated_capability" in request["allowed_actions"]) != current["anomaly"]):
        raise ValueError("inconsistent_capability_request")
    forbidden = ("actual", "truth", "true_", "seed", "plant_factor_actual")
    def scan(value, path=""):
        if isinstance(value, dict):
            for k, v in value.items():
                if any(word in k for word in forbidden):
                    raise ValueError("truth_or_seed_in_request:"+path+k)
                scan(v, path+k+".")
        elif isinstance(value, list):
            for v in value:
                scan(v, path)
    scan(request)
    return request


def public_input(request):
    """What a later model sees: current observations, estimate, previews and effects only."""
    request = validate_request(request)
    current = request["capability_estimate"]
    evidence = {"altitude_m": request["observation"]["altitude_m"],
        "fuel_gauge_kg": request["observation"]["fuel_gauge_kg"],
        "capability_factor_estimate": current["factor_estimate"],
        "capability_band_low": current["band"][0] if current["band"] else None,
        "capability_band_high": current["band"][1] if current["band"] else None,
        "expected_main_acceleration_mps2": current["expected_main_acceleration_mps2"],
        "measured_axial_acceleration_mps2": current["measured_axial_acceleration_mps2"],
        "acceleration_deficit_mps2": current["acceleration_deficit_mps2"],
        "accepted_samples": current["accepted_samples"], "telemetry_latency_s": current["telemetry_latency_s"],
        "burn_segments": len(current["segments"])}
    for action, preview in (request["previews"] or {}).items():
        for point in preview["points"]:
            label = f"{action}@{point['label']}"
            evidence[f"{label}.contact_speed_mps"] = point["contact_speed_mps"]
            evidence[f"{label}.reserve_kg"] = point["reserve_kg"]
            evidence[f"{label}.limits_met"] = point["limits_met"]
    return {"observation": request["observation"], "trigger": request["trigger"],
        "capability_estimate": {k: current[k] for k in ("estimate_id", "factor_estimate", "band", "half_width_terms",
            "accepted_samples", "data_through_s", "telemetry_latency_s", "segments", "anomaly")},
        "previews": request["previews"], "allowed_actions": request["allowed_actions"],
        "effects": request["effects"], "deadline_s": request["deadline_s"],
        "deep_reasoning_allowed": request["deep_reasoning_allowed"], "evidence": evidence,
        "scope": ("Experimental simulation. The capability estimate is a numeric tool output from a synthetic "
                  "accelerometer, engine throttle telemetry and a quantized fuel gauge. Previews are predictions "
                  "using estimated, not known, capability. Faulted returns are not qualified. Step 2 request: "
                  "not executable.")}


def fixture_decision(request):
    """Deterministic transport fixture; explicitly not AI evidence."""
    request = validate_request(request)
    if "collect_status" in request["allowed_actions"]:
        action = "collect_status"
    elif "adopt_estimated_capability" in request["allowed_actions"]:
        action = "adopt_estimated_capability"
    else:
        action = "continue_nominal_assumption"
    return {"request_id": request["request_id"], "request_sha256": digest(request), "mode": "fixture",
            "action": action, "model_inference_invoked": False, "executable": False}


def contact_metrics(contact, final):
    x, y, _ = contact["point_eci_m"]
    surface = (-env.EARTH_ROTATION_RAD_S*y, env.EARTH_ROTATION_RAD_S*x, 0.)
    speed = math.sqrt(sum((a-b)**2 for a, b in zip(contact["point_velocity_eci_mps"], surface)))
    w, x, y, z = contact["q_body_to_eci"]
    axis = (2*(x*z+w*y), 2*(y*z-w*x), 1-2*(x*x+y*y))
    tilt = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(axis, contact["surface_normal_eci"]))))))
    return {"time_s": contact["event_time_s"], "speed_mps": speed, "tilt_deg": tilt,
            "body_rate_rad_s": math.sqrt(sum(v*v for v in final["omega_body_rad_s"])),
            "reserve_kg": contact["propellant_kg"]}


def verify_record(record, limits):
    """Record consistency and contact gate for a switch-capable rollout.

    Recomputes thrust/limit relations and audited routing from the record's own
    declared plant factor and switch schedule; makes no propagator call.
    """
    checks, metrics = {}, None
    try:
        nominal_limits = record["nominal_engine_limits_n"]
        actual = [x*record["plant_factor"] if i in MAIN else x for i, x in enumerate(nominal_limits)]
        audit = record["control_capability_audit"]
        switches = audit["switches"]
        expected_switch = record["switch"]
        checks["plant_limits_bound"] = audit["actual_engine_limits_n"] == actual and record["plant_engine_limits_n"] == actual
        checks["initial_model_nominal"] = audit["initial_assumed_engine_limits_n"] == nominal_limits
        if expected_switch is None:
            checks["switch_schedule_bound"] = switches == []
            split, switched = len(audit["all_boundary_calls"]), None
        else:
            switched = [x*expected_switch["factor"] if i in MAIN else x for i, x in enumerate(nominal_limits)]
            checks["switch_schedule_bound"] = (len(switches) == 1 and switches[0]["engine_limits_n"] == switched
                and switches[0]["time_s"] == expected_switch["time_s"])
            split = switches[0]["row_index"] if switches else -1
        rows = audit["all_boundary_calls"]
        checks["complete_control_trace_hash"] = audit["trace_sha256"] == sha256(matrix.encoded(rows)).hexdigest()
        def assumed_for(index):
            return nominal_limits if index < split else switched
        controls = [(i, r) for i, r in enumerate(rows) if r["boundary"] != "plant"]
        plants = [r for r in rows if r["boundary"] == "plant"]
        checks["all_control_calls_use_scheduled_model"] = bool(controls) and all(
            r["engine_limits_n"] == assumed_for(i) and r["is_actual_plant_object"] is False for i, r in controls)
        checks["all_profiles_use_scheduled_model"] = all(r["profile_main_limit_n"] == assumed_for(i)[0]
            for i, r in controls if r["boundary"] in ("capability_profile", "tvc_control"))
        checks["all_physical_steps_use_plant"] = bool(plants) and all(
            r["engine_limits_n"] == actual and r["is_actual_plant_object"] is True for r in plants)
        counts = {k: sum(r["boundary"] == k for r in rows) for k in ("update", "command", "capability_profile", "tvc_control", "plant")}
        steps = record["outcome"]["integration_steps"]
        checks["complete_boundary_coverage"] = (counts["plant"] == counts["command"] == counts["tvc_control"] == steps
            and counts["capability_profile"] == counts["update"]+counts["command"] and len(rows) == sum(counts.values()))
        samples = record["samples"]
        checks["initial_sample_matches"] = all(samples[0][k] == v for k, v in record["initial_state"].items())
        checks["time_advances"] = len(samples) > 1 and all(a["time_s"] <= b["time_s"] for a, b in zip(samples, samples[1:]))
        checks["fuel_nonincreasing"] = all(b["propellant_kg"] <= a["propellant_kg"]+1e-7 for a, b in zip(samples, samples[1:]))
        checks["finite_physical_pose"] = all(abs(sum(x*x for x in s["q_body_to_eci"])-1) < 1e-8 and all(
            math.isfinite(x) for key in ("r_eci_m", "v_eci_mps", "omega_body_rad_s") for x in s[key]) for s in samples)
        switch_time = switches[0]["time_s"] if switches else math.inf
        thrust_valid, witnesses = True, 0
        for s in samples:
            witness = s.get("retained_return") is not None or s.get("entry_trim") is not None
            witnesses += witness
            limits_used = (switched if witness and s["time_s"] >= switch_time else nominal_limits) if witness else actual
            for i in range(6):
                e = s["engine_states"][i]
                expected = limits_used[i]*e["throttle"] if e["available"] and s["propellant_kg"] > 0 else 0.
                thrust_valid &= math.isclose(s["main_engine_thrust_n"][i], expected, rel_tol=1e-9, abs_tol=1e-6)
        checks["observed_main_thrust_matches_capability"] = thrust_valid
        checks["no_numerical_or_budget_failure"] = record["error"] is None and record["outcome"]["termination"] in (
            "surface_impact", "low_speed_surface_contact")
        contact = record["outcome"]["contact_receipt"]
        checks["terminal_contact_retained"] = contact is not None and contact["contact"] is True
        if contact is not None:
            metrics = contact_metrics(contact, record["final_state"])
            final = record["final_state"]
            checks["contact_kinematics"] = math.isclose(metrics["speed_mps"], contact["surface_relative_speed_mps"], rel_tol=1e-10, abs_tol=1e-8)
            checks["contact_is_final_state"] = (contact["event_time_s"] == final["time_s"]
                and contact["propellant_kg"] == final["propellant_kg"] and contact["q_body_to_eci"] == final["q_body_to_eci"])
        valid = all(checks.values())
        accepted = bool(valid and metrics and all(metrics[k] <= limits[k] for k in ("speed_mps", "tilt_deg", "body_rate_rad_s"))
                        and metrics["reserve_kg"] >= limits["reserve_kg"])
        return {"record_valid": valid, "checks": checks, "contact": metrics, "contact_limits_met": accepted,
                "controller_witness_samples": witnesses, "runtime_admission": False, "fault_domain_status": "unverified"}
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        return {"record_valid": False, "checks": checks, "contact": metrics, "contact_limits_met": False,
                "error": f"{type(exc).__name__}: {exc}", "runtime_admission": False}


def continuation_matches(full, branch):
    """Physics equality of a snapshot continuation; step counts are reconciled, not equated.

    A branch starts at its snapshot, so its integration count excludes the
    prefix. The prefix count comes from the full record's audited plant calls.
    """
    start = branch["initial_state"]["time_s"]
    plants = [r for r in full["control_capability_audit"]["all_boundary_calls"] if r["boundary"] == "plant"]
    prefix = sum(r["time_s"] < start for r in plants)
    later = [s for s in full["samples"] if s["time_s"] >= start]
    times = {s["time_s"] for s in branch["samples"]}
    shared_full = [s for s in later if s["time_s"] in times]
    shared_branch = [s for s in branch["samples"] if s["time_s"] in {s["time_s"] for s in later}]
    def key(rows):
        return [(r["time_s"], r["boundary"], r["engine_limits_n"], r["profile_main_limit_n"]) for r in rows]
    checks = {"final_state_equal": full["final_state"] == branch["final_state"],
        "termination_equal": full["outcome"]["termination"] == branch["outcome"]["termination"],
        "contact_receipt_equal": full["outcome"]["contact_receipt"] == branch["outcome"]["contact_receipt"],
        "events_after_snapshot_equal": [e for e in full["events"] if e["time_s"] >= start] == branch["events"],
        "shared_samples_equal_in_order": bool(shared_full) and shared_full == shared_branch,
        "audit_rows_after_snapshot_equal": key(r for r in full["control_capability_audit"]["all_boundary_calls"]
            if r["time_s"] >= start) == key(branch["control_capability_audit"]["all_boundary_calls"]),
        "integration_steps_reconciled": branch["outcome"]["integration_steps"]+prefix == full["outcome"]["integration_steps"]}
    return {"exact": all(checks.values()), "checks": checks, "prefix_integration_steps": prefix,
            "shared_sample_count": len(shared_full)}


def record(kind, initial_state, plant, nominal, controller, run, *, plant_factor, switch, wall_time_s, extra=None):
    return {"schema": "missionos.ship_fault_step2_rollout.v1", "kind": kind,
        "initial_state": initial_state, "plant_factor": plant_factor,
        "plant_engine_limits_n": [e.max_thrust_n for e in plant.engines],
        "nominal_engine_limits_n": [e.max_thrust_n for e in nominal.engines],
        "switch": deepcopy(switch), "events": run["events"], "samples": run["samples"],
        "final_state": asdict(run["state"]),
        "outcome": {"termination": run["termination"], "contact_receipt": run["contact"], "integration_steps": run["steps"]},
        "error": run["error"], "wall_time_s": wall_time_s, "control_capability_audit": controller.audit.to_dict(),
        "fault_domain_status": "unverified", "runtime_admission": False, "model_calls": 0,
        "physical_execution": False, **(extra or {})}
