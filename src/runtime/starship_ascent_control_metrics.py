"""Prospective reference-v3 scoring and independent v6 control-record checks.

This module never calls a controller, reference producer or dynamics step.
Observing a saved state uses the same static vehicle/aerodynamic model: these
are record/command consistency checks, not independent physical qualification.
The caller remains responsible for source/environment pins and runtime spies.
"""
from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import asdict
import hashlib
import json
import math
from numbers import Real
from pathlib import Path

from . import starship_ascent_metrics as base
from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from .starship_ascent import AscentScreenSettings
from .starship_ascent_retry_metrics import PHYSICAL_FAILURES, _availability, _shutdowns


POLICY = "controlled_coast_orbit_v6"
TRACE_SCHEMA = "missionos.starship_ascent_control_trace.v1"
FIELDS = (*saved._STATE_FIELDS, "engine_states", "flap_angles_rad")
PHASES = {"stack_ascent", "ship_ascent", "suborbital_coast", "orbit_insertion", "ascent_screen_shutdown"}
REFERENCE_PATH = "docs/assets/starship-reference-v3-20261018/reference.json"
ORIGINAL_CSV = "docs/assets/starship-flight14-telemetry-20261017/ascent_stack_and_ship.csv"
EXPECTED_OVERRIDES = {80: ("233.0", 2230), 83: ("366.0", 2388), 84: ("405.5", 2441), 88: ("613.0", 2667)}
MAX_TRACE_ROWS = 20_000


def _require(value, message):
    if not value:
        raise ValueError(message)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _equal(actual, expected, message, *, atol=2e-10, rtol=2e-11):
    if isinstance(expected, Real) and not isinstance(expected, bool):
        _require(saved._number(actual) and math.isclose(actual, expected, rel_tol=rtol, abs_tol=atol), message)
    elif type(expected) is dict:
        _require(type(actual) is dict and set(actual) == set(expected), message)
        for key in expected:
            _equal(actual[key], expected[key], message+":"+key, atol=atol, rtol=rtol)
    elif type(expected) in (list, tuple):
        _require(type(actual) in (list, tuple) and len(actual) == len(expected), message)
        for a, e in zip(actual, expected):
            _equal(a, e, message, atol=atol, rtol=rtol)
    else:
        _require(type(actual) is type(expected) and actual == expected, message)


def _read_csv(path):
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def load_reference(root, registration):
    """Admit only the four source-reviewed cells, preserving both frozen masks."""
    root = Path(root)
    _require(type(registration) is dict and type(registration.get("input_sha256")) is dict,
             "explicit_reference_registration_required")
    hashes = {}
    for relative, expected in registration["input_sha256"].items():
        path = Path(relative)
        _require(not path.is_absolute() and ".." not in path.parts, "unsafe_registered_reference_path")
        actual = hashlib.sha256((root/path).read_bytes()).hexdigest()
        _require(actual == expected, "reference_input_hash_mismatch:"+relative)
        hashes[relative] = actual
    selected = registration.get("source_reference")
    _require(type(selected) is dict and selected.get("path") == REFERENCE_PATH
             and hashes.get(REFERENCE_PATH) == selected.get("sha256"), "explicit_reference_v3_required")
    manifest = base._read_json(root/REFERENCE_PATH)
    _require(manifest.get("schema") == "missionos.starship_reference_selection.v3"
             and manifest.get("status") == "frozen_for_prospective_registration;not_activated_in_existing_runners",
             "source_reference_v3_contract_mismatch")
    for key in ("immutable_parent", "source_original_csv", "ascent_csv"):
        item = manifest[key]
        _require(hashes.get(item["path"]) == item["sha256"], "source_reference_parent_hash_mismatch")
    parent = base._read_json(root/manifest["immutable_parent"]["path"])
    acceptance = base._read_json(root/parent["acceptance_record"]["path"])
    _require(parent["status"] == "accepted_for_bounded_engineering_evaluation"
             and acceptance["user_reply"] == "はい", "inherited_reference_acceptance_missing")
    fit = registration["fit_data"]
    _require(fit["file"] == manifest["ascent_csv"]["path"] and fit["body"] == "stack_then_ship"
             and fit["elapsed_s_inclusive"] == [0, 491], "reference_v3_fit_scope_changed")
    old_headers, old_rows = _read_csv(root/ORIGINAL_CSV)
    new_headers, new_rows = _read_csv(root/fit["file"])
    _require(old_headers == new_headers and len(old_rows) == len(new_rows), "source_reference_inventory_changed")
    manual = {row["id"]: row for row in _read_csv(root/manifest["manual_audit"]["all30_transcription"])[1]}
    overrides = manifest["field_overrides"]
    _require(len(overrides) == 4 and {row["t_plus_s"] for row in overrides} == set(EXPECTED_OVERRIDES),
             "source_reference_override_inventory_changed")
    declared = {}
    for item in overrides:
        time = item["t_plus_s"]
        old, new = EXPECTED_OVERRIDES[time]
        witness = manual[item["manual_id"]]
        _require(item["field"] == "displayed_speed_kmh" and item["old_processed_value"] == old
                 and item["new_manual_display_value"] == new and int(witness["visible_met_s"]) == time
                 and int(witness["speed_kmh"]) == new and witness["frame_sha256"] == item["frame_sha256"],
                 "manual_display_binding_mismatch")
        declared[time] = item
    changed = []
    for line, (old, new) in enumerate(zip(old_rows, new_rows), 2):
        _require(set(old) == set(new), "source_reference_columns_changed")
        for field in old:
            if old[field] == new[field]:
                continue
            time = float(old["t_plus_s"])
            item = declared.get(time)
            _require(item is not None and field == item["field"] and old[field] == item["old_processed_value"]
                     and float(new[field]) == item["new_manual_display_value"], "unregistered_reference_cell_change")
            changed.append((time, field))
    _require(len(changed) == 4, "registered_reference_cell_missing")
    quarantine = base._read_json(root/fit["quarantine"]["file"])["proposed_field_quarantine"]
    lines = set(fit["quarantine"]["source_lines"])
    _require(len(quarantine) == len(lines) == 11 and {row["line"] for row in quarantine} == lines,
             "quarantine_mask_mismatch")
    q_by_line = {row["line"]: row for row in quarantine}
    altitude, speed, excluded = [], [], []
    rows_in_window = 0
    for line, row in enumerate(new_rows, 2):
        time = float(row["t_plus_s"])
        if not 0 <= time <= 491:
            continue
        rows_in_window += 1
        _require(row["body"] == fit["body"], "reference_body_mismatch")
        for field, destination, factor in (("displayed_altitude_km", altitude, 1000.),
                                            ("displayed_speed_kmh", speed, 1/3.6)):
            if line in q_by_line and field == "displayed_altitude_km":
                q = q_by_line[line]
                _require(q["source"] == ORIGINAL_CSV and q["field"] == field and q["t_plus_s"] == time
                         and q["replacement_value"] is None and row[field] != ""
                         and float(row[field]) == q["stored_value"] == float(old_rows[line-2][field]),
                         "quarantine_source_mismatch")
                excluded.append({"source_line": line, "time_s": time, "field": field})
            elif row[field] != "":
                destination.append({"time_s": time, "observed": base._number(float(row[field]), "reference")*factor,
                                    "source_line": line})
    counts = {"altitude_points": len(altitude), "speed_points": len(speed),
        "altitude_bins": len({int(row["time_s"]//5) for row in altitude}),
        "speed_bins": len({int(row["time_s"]//5) for row in speed})}
    _require(counts == {"altitude_points": 145, "speed_points": 442, "altitude_bins": 36, "speed_bins": 96}
             and rows_in_window == fit["rows_in_window"] == 474 and len(excluded) == 11,
             "frozen_reference_mask_changed")
    return {"schema": "missionos.starship_ascent_reference.v3", "altitude": altitude, "speed": speed,
        "excluded_altitude_fields": excluded, "counts": counts, "input_sha256": hashes,
        "registration": registration, "source_reference": selected, "manual_speed_overrides": changed,
        "speed_frame": "surface_relative_working_assumption", "altitude_datum": "unknown_broadcast_datum"}


def score_ascent(record, reference):
    return base.score_ascent(record, reference)


def _state(row):
    saved._state(row, "$.trace.state")
    _require(all(key in row for key in FIELDS), "trace_full_actuator_state_missing")
    return dyn.state_from_dict({key: row[key] for key in FIELDS})


def _state_fields(row):
    return {key: row[key] for key in FIELDS}


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw*bw-ax*bx-ay*by-az*bz, aw*bx+ax*bw+ay*bz-az*by,
            aw*by-ax*bz+ay*bw+az*bx, aw*bz+ax*by-ay*bx+az*bw)


def _qinv(q):
    return (q[0], -q[1], -q[2], -q[3])


def _qnormalize(q):
    norm = math.hypot(*q)
    return tuple(x/norm for x in q)


def _rotvec(q):
    if ((abs(q[0]) > 1e-14 and q[0] < 0)
            or (abs(q[0]) <= 1e-14 and q[1+max(range(3), key=lambda i: abs(q[i+1]))] < 0)):
        q = tuple(-x for x in q)
    length = math.hypot(*q[1:])
    if length < 1e-14:
        return (0., 0., 0.)
    return tuple(x*(2*math.atan2(length, max(0., q[0])))/length for x in q[1:])


def _qincrement(vector):
    angle = math.hypot(*vector)
    if angle < 1e-14:
        return (1., 0., 0., 0.)
    return (math.cos(angle/2), *(x*math.sin(angle/2)/angle for x in vector))


def _clip(vector, maximum):
    norm = math.hypot(*vector)
    scale = min(1., maximum/max(norm, 1e-300))
    return tuple(x*scale for x in vector), norm > maximum


def _check_reference(row, history, profile):
    """Recompute requested quaternion kinematics without the producer module."""
    receipt = row["controller"]["reference_tracking"]
    state = row["state_before_control"]
    goal = tuple(row["raw_goal_q_body_to_eci"])
    actual = tuple(state["q_body_to_eci"])
    time = row["time_s"]
    previous_q, previous_time, previous_rate, previous_goal = history
    interval = time-previous_time
    maximum_acceleration = profile["guidance"]["max_angular_acceleration_rad_s2"]
    maximum_rate = maximum_acceleration/profile["guidance"]["attitude_frequency_rad_s"]
    first = row["step_index"] == 0
    _require((first and interval == 0) or (not first and 0 < interval <= 1.), "reference_history_interval_invalid")
    error = _rotvec(_qmul(goal, _qinv(previous_q)))
    rotation = _rotvec(_qmul(goal, _qinv(previous_goal)))
    if interval == 0:
        unbounded_rate = rate_limited = rate = previous_rate
        unbounded_goal = goal_rate = closing_rate = (0., 0., 0.)
        propagated, closing_error, closing_bound = previous_q, error, 0.
        unbounded_acceleration = acceleration = (0., 0., 0.)
        goal_clipped = rate_clipped = acceleration_clipped = False
        requested = previous_q
    else:
        unbounded_goal = tuple(x/interval for x in rotation)
        goal_rate, goal_clipped = _clip(unbounded_goal, maximum_rate)
        propagated = _qnormalize(_qmul(_qincrement(tuple(x*interval for x in goal_rate)), previous_q))
        closing_error = _rotvec(_qmul(goal, _qinv(propagated)))
        angle = math.hypot(*closing_error)
        adt = maximum_acceleration*interval
        closing_bound = math.sqrt(adt*adt+2*maximum_acceleration*angle)-adt
        scale = min(1/interval, closing_bound/max(angle, 1e-300))
        closing_rate = tuple(x*scale for x in closing_error)
        unbounded_rate = tuple(a+b for a, b in zip(goal_rate, closing_rate))
        rate_limited, rate_clipped = _clip(unbounded_rate, maximum_rate)
        unbounded_acceleration = tuple((a-b)/interval for a, b in zip(rate_limited, previous_rate))
        acceleration, acceleration_clipped = _clip(unbounded_acceleration, maximum_acceleration)
        rate = tuple(a+interval*b for a, b in zip(previous_rate, acceleration))
        requested = _qnormalize(_qmul(_qincrement(tuple(x*interval for x in rate)), previous_q))
        if sum(a*b for a, b in zip(requested, previous_q)) < 0:
            requested = tuple(-x for x in requested)
    body_rate = saved._rotate(_qinv(actual), rate)
    body_acceleration = saved._rotate(_qinv(actual), acceleration)
    expected = {"schema": "missionos.starship_reference_tracking.v1", "policy_id": "bounded_reference_tracking_v1",
        "time_s": time, "interval_s": interval, "maximum_interval_s": 1.,
        "raw_goal_q_body_to_eci": goal, "requested_q_body_to_eci": requested,
        "previous_requested_q_body_to_eci": previous_q, "previous_time_s": previous_time,
        "previous_raw_goal_q_body_to_eci": previous_goal, "previous_raw_goal_time_s": previous_time,
        "previous_reference_rate_eci_rad_s": previous_rate, "actual_q_body_to_eci": actual,
        "actual_body_rate_rad_s": state["omega_body_rad_s"], "goal_error_rotation_eci_rad": error,
        "raw_goal_rotation_eci_rad": rotation, "unbounded_goal_rate_eci_rad_s": unbounded_goal,
        "goal_rate_eci_rad_s": goal_rate, "goal_rate_clipped": goal_clipped,
        "goal_rate_propagated_request_q_body_to_eci": propagated, "closing_error_rotation_eci_rad": closing_error,
        "closing_rate_bound_rad_s": closing_bound, "closing_reference_rate_eci_rad_s": closing_rate,
        "unbounded_reference_rate_eci_rad_s": unbounded_rate,
        "rate_limited_reference_rate_eci_rad_s": rate_limited, "reference_rate_eci_rad_s": rate,
        "unbounded_reference_acceleration_eci_rad_s2": unbounded_acceleration,
        "reference_acceleration_eci_rad_s2": acceleration, "reference_rate_body_rad_s": body_rate,
        "reference_acceleration_body_rad_s2": body_acceleration, "maximum_reference_rate_rad_s": maximum_rate,
        "maximum_reference_acceleration_rad_s2": maximum_acceleration, "rate_clipped": rate_clipped,
        "acceleration_clipped": acceleration_clipped, "first_update": first,
        "initialized_at_current_time": interval == 0,
        "reference_kinematics": "constant-rate quaternion increments; inertial backward-difference acceleration",
        "closing_rate_law": "v*dt + v^2/(2*amax) <= shortest goal error; causal raw-goal-rate feedforward",
        "request_is_execution": False, "actual_state_assigned": False}
    _equal(receipt, expected, "reference_receipt_mismatch")
    _equal(row["requested_q_body_to_eci"], requested, "bounded_requested_target_mismatch")
    # Keep independently reconstructed history for the next kinematic check.
    # Downstream PD algebra must use its actual saved inputs, already checked
    # above. Reusing the independently rounded reconstruction as a new PD input
    # amplifies small floating-point rate/acceleration differences by the large stack inertia
    # and falsely rejects otherwise exact command receipts. No tolerance changes.
    return ((requested, time, rate, goal), receipt["reference_rate_body_rad_s"],
            receipt["reference_acceleration_body_rad_s2"])


def _check_control(row, craft, profile, reference_rate=None, reference_acceleration=None):
    state = _state(row["state_before_control"])
    target = row["requested_q_body_to_eci"]
    _require(saved._vector(target, 4) and abs(sum(x*x for x in target)-1) < 1e-8, "invalid_requested_quaternion")
    _equal(row["controller"]["target_q_body_to_eci"], target, "controller_target_mismatch")
    error = _qmul(_qinv(state.q_body_to_eci), target)
    if error[0] < 0:
        error = tuple(-x for x in error)
    omega = state.omega_body_rad_s
    g = profile["guidance"]
    wn, damping, maximum = g["attitude_frequency_rad_s"], g["attitude_damping_ratio"], g["max_angular_acceleration_rad_s2"]
    transport = (0., 0., 0.) if reference_rate is None else saved._cross(omega, reference_rate)
    rates = (0., 0., 0.) if reference_rate is None else reference_rate
    accelerations = (0., 0., 0.) if reference_acceleration is None else reference_acceleration
    raw = tuple(2*wn**2*e-2*damping*wn*(w-r)+a-c
                for e, w, r, a, c in zip(error[1:], omega, rates, accelerations, transport))
    limited = tuple(max(-maximum, min(maximum, x)) for x in raw)
    observation = dyn.observe(state, craft)
    inertia = observation["inertia_kg_m2"]
    angular_momentum = [sum(row[j]*omega[j] for j in range(3)) for row in inertia]
    gyro = saved._cross(omega, angular_momentum)
    wanted = [sum(row[j]*limited[j] for j in range(3))+gyro[i] for i, row in enumerate(inertia)]
    pre_fin = [a-b for a, b in zip(wanted, observation["aero_torque_body_nm"])]
    _equal(row["controller"]["requested_torque_body_nm"], pre_fin, "requested_control_torque_mismatch", atol=1e-5)
    if reference_rate is None:
        _require("reference_tracking" not in row["controller"] and "reference_tracking_control" not in row["controller"],
                 "disabled_tracking_receipt_present")
    else:
        expected = {"schema": "missionos.starship_reference_tracking_control.v1",
            "policy_id": "bounded_reference_tracking_v1", "reference_rate_body_rad_s": reference_rate,
            "reference_acceleration_body_rad_s": reference_acceleration, "actual_body_rate_rad_s": omega,
            "transport_term_body_rad_s2": transport, "raw_requested_acceleration_body_rad_s2": raw,
            "limited_requested_acceleration_body_rad_s2": limited, "component_acceleration_limit_rad_s2": maximum,
            "rate_ceiling_rad_s": maximum/wn, "acceleration_clipped": any(a != b for a, b in zip(raw, limited)),
            "reference_tracking_is_execution": False, "requested_rigid_body_torque_body_nm": wanted,
            "actual_aero_torque_body_nm": observation["aero_torque_body_nm"], "pre_fin_torque_body_nm": pre_fin}
        receipt = row["controller"]["reference_tracking_control"]
        _require(type(receipt) is dict and set(receipt) == set(expected), "tracking_control_receipt_fields_mismatch")
        torque_fields = {"requested_rigid_body_torque_body_nm", "actual_aero_torque_body_nm", "pre_fin_torque_body_nm"}
        for key, value in expected.items():
            _equal(receipt[key], value, "tracking_control_receipt_mismatch:"+key,
                   atol=1e-5 if key in torque_fields else 2e-10)


def _raw_request(row, craft, profile, settings, separation_time, *, terminal=False):
    state = _state(row["state_before_control"])
    phase, time = row["phase"], row["time_s"]
    observation = dyn.observe(state, craft)
    up, east, north = env.local_frame(env.State3D(time, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    radial = env.dot(state.v_eci_mps, up)
    tangent_vec = env.add(state.v_eci_mps, env.scale(up, -radial))
    tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
    direction, count, throttle = tangent, 0, 0.
    g, objective = profile["guidance"], settings.objectives
    if not terminal and phase == "stack_ascent":
        program = g["pitch_program_time_deg"]
        values = [value if i < 2 else value+settings.stack_pitch_offset_deg for i, (_, value) in enumerate(program)]
        if time <= program[0][0]:
            angle = values[0]
        elif time >= program[-1][0]:
            angle = values[-1]
        else:
            index = next(i for i in range(1, len(program)) if time <= program[i][0])
            fraction = (time-program[index-1][0])/(program[index][0]-program[index-1][0])
            angle = values[index-1]+fraction*(values[index]-values[index-1])
        heading = env.add(env.scale(east, math.sin(math.radians(profile["launch"]["azimuth_deg"]))),
                          env.scale(north, math.cos(math.radians(profile["launch"]["azimuth_deg"]))))
        direction = env.add(env.scale(up, math.sin(math.radians(angle))), env.scale(heading, math.cos(math.radians(angle))))
        count, throttle = 33, .7 if observation["dynamic_pressure_pa"] > g["max_q_pa"] else 1.
    elif not terminal and phase in ("ship_ascent", "orbit_insertion"):
        _require(state.propellant_kg > profile["ship"]["ascent_reserve_kg"], "powered_below_unchanged_reserve")
        orbit = _orbit(row["state_before_control"])
        achieved = (orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= (
            objective.first_burn_perigee_m if phase == "ship_ascent" else objective.final_perigee_m))
        if phase == "ship_ascent":
            achieved = achieved and orbit["apogee_altitude_m"] >= objective.first_burn_apogee_m
        _require(not achieved and time < (g["max_ascent_s"] if phase == "ship_ascent" else 1536.),
                 "powered_past_measured_phase_gate_or_deadline")
        count, throttle = (6 if phase == "ship_ascent" else 1), 1.
        acceleration = sum(engine.max_thrust_n for index, (engine, status) in enumerate(zip(craft.engines, state.engine_states))
            if index < count and "main" in engine.name and status.available)/observation["mass_kg"]
        _require(acceleration > 0, "powered_without_registered_capacity")
        radius = env.norm(state.r_eci_m)
        feedforward = 0.
        if phase == "ship_ascent":
            up = env.unit(state.r_eci_m)
            radial = env.dot(state.v_eci_mps, up)
            tangent_vec = env.add(state.v_eci_mps, env.scale(up, -radial))
            tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
            rp, ra = env.EARTH_EQUATORIAL_RADIUS_M+objective.first_burn_perigee_m, env.EARTH_EQUATORIAL_RADIUS_M+objective.first_burn_apogee_m
            semimajor = (rp+ra)/2
            momentum2 = 2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
            speed2 = env.EARTH_MU_M3_S2*(2/radius-1/semimajor)-momentum2/radius**2
            desired = math.sqrt(max(0., speed2))
            feedforward = momentum2/radius**3-env.EARTH_MU_M3_S2/radius**2
        else:
            desired = max(-50., min(800., (objective.insertion_altitude_m-observation["altitude_m"])/g["ascent_altitude_response_s"]))
        radial_acc = env.EARTH_MU_M3_S2/radius**2-env.norm(tangent_vec)**2/radius+(desired-radial)/g["ascent_velocity_response_s"]+feedforward
        fraction = max(-.25, min(.95, radial_acc/acceleration))
        apply_bias = settings.ship_pitch_offset_deg and (phase == "ship_ascent" or not settings.control_options.first_burn_only_bias)
        if apply_bias:
            _require(separation_time is not None, "ship_bias_without_actual_separation")
            ramp = max(0., min(1., (time-separation_time)/10.))
            angle = math.atan2(fraction, math.sqrt(1-fraction*fraction))+math.radians(settings.ship_pitch_offset_deg)*ramp
            fraction = math.sin(max(math.asin(-.25), min(math.asin(.95), angle)))
        direction = env.add(env.scale(up, fraction), env.scale(tangent, math.sqrt(1-fraction*fraction)))
    elif not terminal and phase == "suborbital_coast":
        orbit = _orbit(row["state_before_control"])
        achieved = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= objective.final_perigee_m
        _require(time < 1517. and not achieved and state.propellant_kg > profile["ship"]["ascent_reserve_kg"],
                 "coast_ignored_restart_or_final_gate_or_reserve")
    raw = row["raw_goal_q_body_to_eci"]
    _require(saved._vector(raw, 4) and abs(sum(x*x for x in raw)-1) < 1e-8, "invalid_raw_goal_quaternion")
    _equal(saved._rotate(raw, [0., 0., 1.]), env.unit(direction), "raw_guidance_axis_mismatch")
    x = env.add(north, env.scale(env.unit(direction), -env.dot(north, env.unit(direction))))
    if env.norm(x) < 1e-10:
        basis = [float(i == min(range(3), key=lambda j: abs(direction[j]))) for i in range(3)]
        x = env.add(basis, env.scale(env.unit(direction), -env.dot(basis, env.unit(direction))))
    _equal(saved._rotate(raw, [1., 0., 0.]), env.unit(x), "raw_guidance_roll_mismatch")
    command = dyn.command_from_dict(row["command"])
    _require(len(command.engines) == len(craft.engines) and len(command.flap_angles_rad) == len(craft.aero_panels),
             "command_actuator_inventory_mismatch")
    for index, (engine, request) in enumerate(zip(craft.engines, command.engines)):
        if "main" in engine.name:
            _require(request.enabled is (index < count and throttle > 0.)
                     and request.throttle == (throttle if index < count else 0.), "main_command_differs_from_registered_request")
        _require(abs(request.gimbal_x_rad) <= engine.max_gimbal_rad+1e-12
                 and abs(request.gimbal_y_rad) <= engine.max_gimbal_rad+1e-12, "command_exceeds_physical_gimbal")
    _require(all(abs(angle) <= panel.max_deflection_rad+1e-12 for angle, panel in zip(command.flap_angles_rad, craft.aero_panels)),
             "command_exceeds_physical_flap")


def _orbit(state):
    return env.orbital_elements(env.State3D(state["time_s"], state["r_eci_m"], state["v_eci_mps"], state["propellant_kg"]))


def _check_phase_events(record, events, settings, profile):
    screen, outcome = record["ascent_screen"], record["outcome"]
    last = record["final_state"]
    objective = settings.objectives
    witnesses = screen.get("transition_witnesses")
    _require(type(witnesses) is list, "phase_transition_witnesses_missing")
    for witness in witnesses:
        _require(witness.get("state_reset") is False and witness.get("state_before") == witness.get("state_after")
                 and witness.get("state_after_origin") in ("next_physics_tick_before_command", "final_retained_state")
                 and witness["time_s"] == witness["state_before"]["time_s"], "phase_transition_resets_actual_state")
        _state(witness["state_before"])
        _require(any(sample["time_s"] == witness["time_s"] and _state_fields(sample) == witness["state_before"]
                     for sample in record["samples"]), "phase_witness_not_saved_actual_state")
    first = screen.get("first_cutoff_measurement")
    cuts = events.get("ascent_screen_first_cutoff_command", [])
    _require(len(cuts) <= 1 and bool(cuts) == (first is not None), "first_cutoff_event_measurement_mismatch")
    if cuts:
        event, orbit = cuts[0], _orbit(first["state"])
        _require(first["time_s"] == event["time_s"] and first["state"] == event.get("state")
                 and first.get("orbit") == orbit and orbit["status"] == "bound"
                 and orbit["perigee_altitude_m"] >= objective.first_burn_perigee_m
                 and orbit["apogee_altitude_m"] >= objective.first_burn_apogee_m
                 and first.get("reason") == "measured_first_burn_coast_orbit_gates"
                 and first.get("objective") == {"first_burn_apogee_m": objective.first_burn_apogee_m,
                                               "first_burn_perigee_m": objective.first_burn_perigee_m}
                 and first["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"], "first_cutoff_has_no_actual_orbital_gate")
        _require(any(w["time_s"] == event["time_s"] and w["from_phase"] == "ship_ascent"
                     and w["to_phase"] == "suborbital_coast" and w["state_before"] == event["state"] for w in witnesses),
                 "first_cutoff_continuity_witness_missing")
    else:
        _require(outcome["termination"] in PHYSICAL_FAILURES and last["time_s"] <= profile["guidance"]["max_ascent_s"]+.2,
                 "first_cutoff_or_physical_stop_missing")
    final = screen.get("cutoff_measurement")
    commands = events.get("ascent_screen_final_cutoff_command", [])
    _require(len(commands) <= 1 and bool(commands) == (final is not None), "final_cutoff_event_measurement_mismatch")
    reached = False
    if commands:
        event, orbit = commands[0], _orbit(final["state"])
        reached = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= objective.final_perigee_m
        _require(final["time_s"] == event["time_s"] and final["state"] == event["state"]
                 and final.get("orbit") == event.get("orbit") == orbit and final.get("gate_reached") is reached
                 and event["time_s"] <= 1536. and (reached or event["time_s"] == 1536.)
                 and event.get("termination") == ("insertion_gate_reached" if reached else "insertion_gate_unreached"),
                 "final_cutoff_has_no_actual_orbital_gate")
        _require(any(w["time_s"] == event["time_s"] and w["state_before"] == event["state"]
                     and w["to_phase"] == ("ascent_screen_shutdown" if reached else w["from_phase"]) for w in witnesses),
                 "final_cutoff_continuity_witness_missing")
    restarts = events.get("ascent_screen_restart_command", [])
    _require(len(restarts) <= 1, "repeated_orbit_insertion_restart")
    for event in restarts:
        orbit = _orbit(event["state"])
        achieved = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= objective.final_perigee_m
        _require(event["time_s"] == 1517. and first is not None and first["time_s"] <= 1517. and not achieved
                 and any(w["time_s"] == 1517. and w["from_phase"] == "suborbital_coast"
                     and w["to_phase"] == "orbit_insertion" and w["state_before"] == event["state"] for w in witnesses),
                 "restart_lacks_registered_time_or_actual_state")
    if last["time_s"] > 1517. and first is not None and (final is None or final["time_s"] > 1517.):
        _require(len(restarts) == 1, "due_restart_missing")
    _require(screen.get("final_orbit") == _orbit(last) and screen.get("insertion_gate_reached") is reached
             and outcome.get("orbit_gate_reached") is reached, "final_insertion_claim_mismatch")
    termination = outcome["termination"]
    terminal_name = ("time_limit" if termination == "insertion_gate_reached" else
        "ascent_screen_final_cutoff_command" if termination == "insertion_gate_unreached" else
        "ascent_screen_physical_failure" if termination in {"ascent_time_limit_exceeded", "ascent_propulsion_unavailable", "ascent_reserve_exhausted"}
        else termination)
    terminal = events.get(terminal_name, [])
    _require(len(terminal) == 1 and terminal[0]["time_s"] == last["time_s"], "actual_terminal_event_missing")
    if termination == "insertion_gate_reached":
        _require(reached and last["time_s"] == 1537. and outcome["phase"] == "ascent_screen_shutdown", "incomplete_finite_shutdown_continuation")
    if termination == "insertion_gate_unreached":
        _require(not reached and last["time_s"] == 1536., "unreached_final_gate_timing_mismatch")
    if termination == "ascent_reserve_exhausted":
        _require(last["propellant_kg"] <= profile["ship"]["ascent_reserve_kg"], "reserve_stop_without_crossing")
    if termination == "time_limit":
        _require(last["time_s"] == 1537., "unexplained_short_time_limit")
    contact = termination in {"surface_impact", "low_speed_surface_contact"}
    _require(record["samples"][-1].get("contact", False) is contact, "terminal_contact_flag_mismatch")
    if contact:
        _require(type(outcome.get("contact_receipt")) is dict
                 and outcome["contact_receipt"].get("event_time_s") == last["time_s"], "terminal_contact_receipt_missing")
    return reached


def _bridge(previous, current, phase, events, profile, stack, ship, booster):
    """Only declared availability changes and rigid separation may intervene."""
    expected = deepcopy(previous)
    for event in events.get("ascent_screen_engine_unavailable", []):
        if event["time_s"] == expected["time_s"] and event["state_before"] == expected:
            expected = event["state_after"]
    if phase == "ship_ascent" and len(expected["engine_states"]) == len(stack.engines):
        separation = events.get("stage_separation", [])
        _require(len(separation) == 1 and separation[0]["time_s"] == expected["time_s"], "unrecorded_body_transition")
        state = _state(expected)
        parent = dyn.mass_properties(stack, state.propellant_kg)
        child = dyn.mass_properties(ship, profile["ship"]["propellant_kg"])
        origin = (0., 0., profile["geometry"]["booster_length_m"])
        offset = env.add(env.add(origin, child.com_body_m), env.scale(parent.com_body_m, -1))
        rotated = saved._rotate(state.q_body_to_eci, offset)
        rotated_velocity = saved._rotate(state.q_body_to_eci, saved._cross(state.omega_body_rad_s, offset))
        flaps = {panel.name.removeprefix("upper_"): angle for panel, angle in zip(stack.aero_panels, state.flap_angles_rad)}
        expected = {"time_s": state.time_s, "r_eci_m": list(env.add(state.r_eci_m, rotated)),
            "v_eci_mps": list(env.add(state.v_eci_mps, rotated_velocity)), "q_body_to_eci": list(state.q_body_to_eci),
            "omega_body_rad_s": list(state.omega_body_rad_s), "propellant_kg": profile["ship"]["propellant_kg"],
            "engine_states": [asdict(dyn.EngineState()) for _ in ship.engines],
            "flap_angles_rad": [flaps.get(panel.name, 0.) for panel in ship.aero_panels]}
        _equal(current, expected, "rigid_separation_state_mismatch", atol=1e-7)
    else:
        _require(_wire(expected) == _wire(current), "plant_state_chain_or_reference_assignment_mismatch")


def verify_controlled_ascent_record(record, profile, *, expected_settings: AscentScreenSettings):
    """Check every declared control tick; never infer an unsaved tick or grant."""
    result = {"schema": "missionos.starship_ascent_control_record_verification.v1", "passed": False, "issues": [],
        "scope": "full saved state/reference/command consistency; no dynamics reexecution or vehicle qualification",
        "legacy_v3_qualification_borrowed": False, "physical_reexecution": False,
        "mission_authority_granted": False}
    try:
        _require(type(expected_settings) is AscentScreenSettings and expected_settings.policy == POLICY
                 and expected_settings.finite_mode is True and type(profile) is dict, "explicit_v6_settings_required")
        _require(expected_settings.objectives.final_perigee_m == profile["guidance"]["target_perigee_m"], "changed_final_mission_gate")
        wanted = expected_settings.record()
        _require(type(record) is dict, "record_object_required")
        trace = record.get("ascent_control_trace")
        saved._json_tree({key: value for key, value in record.items() if key != "ascent_control_trace"})
        _require(type(trace) is dict and trace.get("schema") == TRACE_SCHEMA and trace.get("settings") == wanted
                 and trace.get("actual_state_assigned") is False and trace.get("mission_authority_granted") is False,
                 "missing_or_unbound_control_trace")
        rows, terminal = trace.get("rows"), trace.get("terminal_commands")
        _require(type(rows) is list and 0 < len(rows) <= MAX_TRACE_ROWS and type(terminal) is list and len(terminal) <= 1,
                 "missing_or_unbounded_control_ticks")
        first, last = saved._samples(record.get("samples"), "$.samples", [0], "ship")
        _require(record.get("scenario") == "launch" and first["time_s"] == 0.
                 and _wire(_state_fields(first)) == _wire(_state_fields(record["initial_state"]))
                 and _wire(_state_fields(last)) == _wire(_state_fields(record["final_state"])), "launch_origin_or_final_state_mismatch")
        outcome, screen = record["outcome"], record["ascent_screen"]
        termination = outcome["termination"]
        _require(termination in PHYSICAL_FAILURES | {"time_limit", "insertion_gate_reached"}
                 and termination != "numerical_failure", "unknown_or_numerical_termination")
        _require(type(outcome.get("integration_steps")) is int and outcome["integration_steps"] == len(rows), "missing_control_ticks")
        _require(outcome["duration_s"] == last["time_s"] <= 1537. and outcome["phase"] == last["phase"], "invalid_finite_horizon")
        for key, value in (("six_dof_integrated", True), ("attitude_prescribed", False),
                           ("starship_vehicle_validated", False), ("booster_return_invoked", False)):
            _require(outcome.get(key) is value, "incorrect_claim_boundary:"+key)
        _require(record.get("booster_run") is None and record.get("satellites") == []
                 and outcome.get("payload_released_count") == 0, "control_screen_cannot_dispatch_mission")
        _require(_wire(screen.get("settings")) == _wire(wanted) and screen.get("policy") == POLICY
                 and screen.get("phase_objectives") == wanted["objectives"]
                 and screen.get("final_mission_perigee_gate_m") == profile["guidance"]["target_perigee_m"]
                 and screen.get("phase_objectives_are_registered_engineering_inputs") is True
                 and screen.get("legacy_v3_verification_scope") is False, "v6_screen_settings_or_authority_mismatch")
        for key in ("mission_dispatch", "return_qualified", "payload_release_authorized", "physical_execution", "propulsion_reference_connected"):
            _require(screen.get(key) is False, "screen_authority_creep:"+key)
        events = saved._events(record.get("events"), "$.events", 0., last["time_s"])
        _require(screen.get("shutdown_schedule") == wanted["shutdowns"], "shutdown_schedule_mismatch")
        _shutdowns(record, screen, wanted["shutdowns"], last["time_s"], saved)
        reached = _check_phase_events(record, events, expected_settings, profile)
        stages = events.get("stage_separation", [])
        _require(len(stages) <= 1, "repeated_separation")
        separation_time = stages[0]["time_s"] if stages else None
        _require(screen.get("separation_time_s") == separation_time, "separation_time_mismatch")
        from .starship_sixdof_mission import vehicle, stack_vehicle
        ship, booster = vehicle(profile), vehicle(profile, "booster")
        stack = stack_vehicle(profile, ship, booster)
        # Production initial_state is the full first observation sample; only
        # these exact physical/actuator fields are the plant-chain anchor.
        previous = _state_fields(record["initial_state"])
        history = (tuple(previous["q_body_to_eci"]), 0., (0., 0., 0.), tuple(previous["q_body_to_eci"]))
        samples_by_key = {(sample["time_s"], sample["phase"]): sample for sample in record["samples"] if sample.get("command") is not None}
        witnessed_samples = set()
        for index, row in enumerate(rows):
            saved._json_tree(row)
            _require(type(row) is dict and type(row.get("step_index")) is int and row["step_index"] == index
                     and row.get("phase") in PHASES and row.get("body_id") == ("stack" if row["phase"] == "stack_ascent" else "ship"),
                     "control_tick_index_phase_or_body_mismatch")
            before, given, returned = row["state_before_control"], row["state_given_to_plant"], row["state_returned_by_plant"]
            state = _state(before)
            _state(given)
            after = _state(returned)
            _require(_wire(before) == _wire(given), "reference_assigned_to_plant_state")
            _require(row["time_s"] == state.time_s and saved._number(row["interval_s"])
                     and 0 < row["interval_s"] <= 1. and math.isclose(after.time_s-state.time_s, row["interval_s"], abs_tol=1e-9),
                     "control_tick_interval_mismatch")
            _bridge(previous, before, row["phase"], events, profile, stack, ship, booster)
            craft = stack if row["phase"] == "stack_ascent" else ship
            _require(len(state.engine_states) == len(craft.engines) and len(state.flap_angles_rad) == len(craft.aero_panels),
                     "actual_actuator_inventory_mismatch")
            _require(len(after.engine_states) == len(craft.engines) and len(after.flap_angles_rad) == len(craft.aero_panels),
                     "returned_actuator_inventory_mismatch")
            kind = "booster" if row["phase"] == "stack_ascent" else "ship"
            _availability(before, kind, wanted["shutdowns"])
            _availability(returned, kind, wanted["shutdowns"], before_event=True)
            for snapshot in (state, after):
                _require(all((not engine.name.startswith("rcs_") or actuator.available is True)
                    and abs(actuator.gimbal_x_rad) <= engine.max_gimbal_rad+1e-12
                    and abs(actuator.gimbal_y_rad) <= engine.max_gimbal_rad+1e-12
                    for engine, actuator in zip(craft.engines, snapshot.engine_states)), "actual_actuator_exceeds_registered_limit")
                _require(all(abs(angle) <= panel.max_deflection_rad+1e-12 for angle, panel in zip(snapshot.flap_angles_rad, craft.aero_panels)),
                         "actual_flap_exceeds_registered_limit")
            command = dyn.command_from_dict(row["command"])
            powered = any(request.throttle > 0 and "main" in engine.name for engine, request in zip(craft.engines, command.engines))
            altitude = env.ecef_to_geodetic(tuple(state.r_eci_m))[2]
            expected_interval = profile["integration"]["powered_dt_s"] if powered or altitude < 100000. else profile["integration"]["coast_dt_s"]
            expected_interval = min(1537.-state.time_s, expected_interval)
            for boundary in sorted([1517., 1536.]+[item["time_s"] for item in wanted["shutdowns"]]):
                if state.time_s < boundary < state.time_s+expected_interval:
                    expected_interval = boundary-state.time_s
                    break
            _equal(row.get("requested_interval_s"), expected_interval, "requested_tick_interval_changed", atol=1e-12)
            _require(row["interval_s"] <= expected_interval+1e-9, "plant_advanced_beyond_requested_interval")
            if row["interval_s"] < expected_interval-1e-9:
                _require(index == len(rows)-1 and termination in {"surface_impact", "low_speed_surface_contact"}
                         and outcome["contact_receipt"]["event_time_s"] == after.time_s, "unexplained_partial_physics_tick")
            _require(after.propellant_kg <= state.propellant_kg+1e-6, "propellant_increased_during_step")
            _raw_request(row, craft, profile, expected_settings, separation_time)
            diagnostic = row["controller"]["ascent_control"]
            expected_diagnostic = {"schema": "missionos.starship_ascent_control_request.v1",
                "raw_goal_q_body_to_eci": row["raw_goal_q_body_to_eci"],
                "requested_q_body_to_eci": row["requested_q_body_to_eci"], "phase": row["phase"],
                **asdict(expected_settings.control_options)}
            _equal(diagnostic, expected_diagnostic, "ascent_control_request_receipt_mismatch")
            reference_rate = reference_acceleration = None
            if expected_settings.control_options.moving_reference_tracking:
                history, reference_rate, reference_acceleration = _check_reference(row, history, profile)
            else:
                _equal(row["requested_q_body_to_eci"], row["raw_goal_q_body_to_eci"], "untracked_target_changed")
            _check_control(row, craft, profile, reference_rate, reference_acceleration)
            key = (state.time_s, row["phase"])
            if key in samples_by_key:
                sample = samples_by_key[key]
                _require(_wire(_state_fields(sample)) == _wire(before) and sample["command"] == row["command"]
                         and sample["controller"] == row["controller"], "saved_sample_differs_from_actual_control_tick")
                witnessed_samples.add(key)
            previous = returned
        _require(_wire(previous) == _wire(record["final_state"]), "last_control_tick_differs_from_final_state")
        for item in terminal:
            saved._json_tree(item)
            _require(item.get("integrated") is False and item.get("reference_updated") is False
                     and item["time_s"] == last["time_s"] and item["state"] == record["final_state"], "invalid_terminal_command")
            angular = termination == "angular_rate_envelope_exceeded"
            if angular:
                _require(item["reason"] == termination and math.hypot(*item["state"]["omega_body_rad_s"]) > 5.
                         and any(event["time_s"] == item["time_s"] for event in events.get(termination, [])),
                         "angular_stop_lacks_actual_rate_or_direct_event")
            else:
                _require(any(event["time_s"] == item["time_s"] and event.get("termination") == item["reason"] == termination
                             for event in events.get("ascent_screen_physical_failure", [])+events.get("ascent_screen_final_cutoff_command", [])),
                         "terminal_command_lacks_actual_stop_event")
            converted = {**item, "state_before_control": item["state"],
                         "requested_q_body_to_eci": item["raw_goal_q_body_to_eci"]}
            craft = stack if item["phase"] == "stack_ascent" else ship
            _raw_request(converted, craft, profile, expected_settings, separation_time, terminal=not angular)
            _check_control(converted, craft, profile)
            key = (item["time_s"], item["phase"])
            _require(key in samples_by_key and samples_by_key[key]["command"] == item["command"]
                     and samples_by_key[key]["controller"] == item["controller"], "terminal_sample_missing")
            witnessed_samples.add(key)
        _require(witnessed_samples == set(samples_by_key), "saved_command_sample_has_no_actual_tick")
        for sample in record["samples"]:
            craft = stack if sample["phase"] == "stack_ascent" else ship
            observation = dyn.observe(_state(sample), craft)
            # saved._samples above independently recomputes ground speed from
            # ECI velocity; observe() without wind reports air_speed instead.
            for key in ("altitude_m", "mass_kg", "dynamic_pressure_pa"):
                _equal(sample[key], observation[key], "saved_observation_mismatch:"+key, atol=1e-5)
        _require(record["final_vehicle"] == json.loads(json.dumps(asdict(stack if last["phase"] == "stack_ascent" else ship))),
                 "final_vehicle_differs_from_profile")
        result.update(passed=True, termination=termination, physical_failure=termination in PHYSICAL_FAILURES,
            verified_control_ticks=len(rows), verified_command_samples=len(witnessed_samples),
            terminal_command_count=len(terminal), state_assignment_runtime_proven=False,
            measured_final_insertion_gate_reached=reached,
            deferred_booster_honestly_recorded=True)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_ascent_control_record", "detail": str(exc)})
    return result
