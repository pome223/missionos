"""Independent checks of the opt-in state-based second insertion record.

Saved actuator/reference/command consistency only: no propagation, controller,
mission permission or vehicle qualification. Legacy v6 checks remain unchanged.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from .starship_ascent import AscentScreenSettings
from .starship_ascent_control_metrics import (
    PHASES, TRACE_SCHEMA, _availability, _bridge, _check_control, _check_reference,
    _equal, _orbit, _raw_request as _first_burn_request, _require, _shutdowns,
    _state, _state_fields, _wire,
)
from .starship_ascent_retry_metrics import PHYSICAL_FAILURES as LEGACY_FAILURES

POLICY = "state_insertion_v7"
MAX_TRACE_ROWS = 40_000
HORIZON_S = 4100.
STATE_FAILURES = {"state_insertion_deadline", "state_insertion_window_missed",
                  "state_insertion_unavailable", "state_insertion_burn_limit"}
PHYSICAL_FAILURES = LEGACY_FAILURES | STATE_FAILURES


def _geometry(value, settings):
    """Current osculating geometry; not a future-state prediction."""
    s = _state(value)
    up = env.unit(s.r_eci_m)
    radial = env.dot(s.v_eci_mps, up)
    tangential = env.add(s.v_eci_mps, env.scale(up, -radial))
    _require(env.norm(tangential) > 1., "state_insertion_tangent_undefined")
    tangent = env.unit(tangential)
    nose = dyn.rotate(s.q_body_to_eci, (0., 0., 1.))
    error = math.degrees(math.acos(max(-1., min(1., env.dot(nose, tangent)))))
    orbit = _orbit(value)
    apogee = orbit.get("apogee_altitude_m")
    gap = (env.EARTH_EQUATORIAL_RADIUS_M+apogee-env.norm(s.r_eci_m)) if apogee is not None else None
    o, objective = settings.insertion_options, settings.objectives
    bound = orbit["status"] == "bound"
    reached = bound and orbit["perigee_altitude_m"] >= objective.final_perigee_m
    capacity = bound and apogee >= objective.final_perigee_m+objective.minimum_apogee_headroom_m
    geometry_ready = (capacity and not reached and -o.maximum_radial_speed_mps <= radial <= 0.
                      and gap <= 2000.)
    ready = (geometry_ready and error <= o.maximum_attitude_error_deg
             and env.norm(s.omega_body_rad_s) <= o.maximum_body_rate_rad_s)
    rp = env.EARTH_EQUATORIAL_RADIUS_M+objective.final_perigee_m
    target_speed = math.sqrt(2*env.EARTH_MU_M3_S2*rp/(env.norm(s.r_eci_m)*(env.norm(s.r_eci_m)+rp)))
    return {"orbit": orbit, "radial": radial, "tangent": tangent, "gap": gap,
            "radius": env.norm(s.r_eci_m), "tangent_speed": env.norm(tangential), "target_speed": target_speed,
            "attitude_error_deg": error, "reached": reached, "capacity": capacity, "ready": ready}


def _check_diagnostic(row, craft, settings, geometry):
    s, o = _state(row["state_before_control"]), settings.insertion_options
    window = (geometry["capacity"] and -o.maximum_radial_speed_mps <= geometry["radial"] <= 0.
              and geometry["gap"] <= 2000.)
    expected = {"schema": "missionos.starship_state_insertion_reference.v1", "time_s": s.time_s,
        "frame": "geocentric_radial_inertial", "radius_m": geometry["radius"],
        "radial_velocity_mps": geometry["radial"], "tangential_velocity_mps": geometry["tangent_speed"],
        "tangent_eci": geometry["tangent"], "target_ellipse_tangent_velocity_mps": geometry["target_speed"],
        "tangential_velocity_error_mps": geometry["target_speed"]-geometry["tangent_speed"],
        "orbit_status": geometry["orbit"].get("status"),
        "perigee_altitude_m": geometry["orbit"].get("perigee_altitude_m"),
        "apogee_altitude_m": geometry["orbit"].get("apogee_altitude_m"),
        "apoapsis_radius_gap_m": geometry["gap"], "orbit_has_insertion_headroom": geometry["capacity"],
        "near_apoapsis_window": bool(window), "attitude_error_deg": geometry["attitude_error_deg"],
        "body_rate_rad_s": env.norm(s.omega_body_rad_s),
        "aligned": geometry["attitude_error_deg"] <= o.maximum_attitude_error_deg
            and env.norm(s.omega_body_rad_s) <= o.maximum_body_rate_rad_s,
        "engine_available": bool(craft.engines and s.engine_states and s.engine_states[0].available),
        "actual_state_assigned": False, "finite_burn_feasibility_proven": False}
    _equal(row["controller"]["state_insertion_reference"], expected, "state_insertion_diagnostic_mismatch")


def _actuator_step_limits(first, last, craft, interval):
    for engine, before, after in zip(craft.engines, first.engine_states, last.engine_states):
        _require(abs(after.throttle-before.throttle) <= engine.throttle_rate_per_s*interval+1e-8,
                 "actual_throttle_rate_limit_exceeded")
        _require(max(abs(after.gimbal_x_rad-before.gimbal_x_rad), abs(after.gimbal_y_rad-before.gimbal_y_rad))
                 <= engine.gimbal_rate_rad_s*interval+1e-8, "actual_gimbal_rate_limit_exceeded")
    for panel, before, after in zip(craft.aero_panels, first.flap_angles_rad, last.flap_angles_rad):
        _require(abs(after-before) <= panel.deflection_rate_rad_s*interval+1e-8,
                 "actual_flap_rate_limit_exceeded")


def _raw_request(row, craft, profile, settings, separation_time, *, terminal=False):
    phase = row["phase"]
    if phase in ("stack_ascent", "ship_ascent"):
        return _first_burn_request(row, craft, profile, settings, separation_time, terminal=terminal)
    s = _state(row["state_before_control"])
    geometry = _geometry(row["state_before_control"], settings)
    if not terminal:
        _check_diagnostic(row, craft, settings, geometry)
    if not terminal and phase != "ascent_screen_shutdown":
        _require(s.propellant_kg > profile["ship"]["ascent_reserve_kg"], "powered_or_coasting_below_reserve")
        _require(geometry["orbit"]["status"] == "bound", "state_insertion_powered_or_coasting_on_unbound_orbit")
        _require(not geometry["reached"], "ignored_actual_insertion_gate")
        _require(craft.engines[0].name == "ship_main_0" and s.engine_states[0].available,
                 "state_insertion_without_selected_main")
        if phase == "suborbital_coast":
            _require(geometry["capacity"], "state_coast_without_insertion_headroom")
            _require(s.time_s < settings.insertion_options.maximum_restart_time_s,
                     "state_coast_ignored_restart_deadline")
            _require(not geometry["ready"], "state_coast_ignored_ready_restart_window")
            if geometry["radial"] <= 0.:
                _require(geometry["radial"] >= -settings.insertion_options.maximum_radial_speed_mps
                         and geometry["capacity"] and geometry["gap"] <= 2000.,
                         "state_coast_ignored_missed_window")
    raw = row["raw_goal_q_body_to_eci"]
    _require(saved._vector(raw, 4) and abs(sum(x*x for x in raw)-1) < 1e-8, "invalid_raw_goal_quaternion")
    direction = geometry["tangent"]
    _equal(saved._rotate(raw, [0., 0., 1.]), direction, "state_raw_guidance_axis_mismatch")
    _, _, north = env.local_frame(env.State3D(s.time_s, s.r_eci_m, s.v_eci_mps, s.propellant_kg))
    x = env.add(north, env.scale(direction, -env.dot(north, direction)))
    if env.norm(x) < 1e-10:
        basis = [float(i == min(range(3), key=lambda j: abs(direction[j]))) for i in range(3)]
        x = env.add(basis, env.scale(direction, -env.dot(basis, direction)))
    _equal(saved._rotate(raw, [1., 0., 0.]), env.unit(x), "state_raw_guidance_roll_mismatch")
    command = dyn.command_from_dict(row["command"])
    _require(len(command.engines) == len(craft.engines) and len(command.flap_angles_rad) == len(craft.aero_panels),
             "command_actuator_inventory_mismatch")
    count = int(not terminal and phase == "orbit_insertion")
    throttle = craft.engines[0].min_throttle if count else 0.
    for index, (engine, request) in enumerate(zip(craft.engines, command.engines)):
        if "main" in engine.name:
            _require(request.enabled is (index < count and throttle > 0.)
                     and request.throttle == (throttle if index < count else 0.),
                     "main_command_differs_from_state_insertion_request")
        _require(abs(request.gimbal_x_rad) <= engine.max_gimbal_rad+1e-12
                 and abs(request.gimbal_y_rad) <= engine.max_gimbal_rad+1e-12, "command_exceeds_physical_gimbal")
    _require(all(abs(angle) <= panel.max_deflection_rad+1e-12 for angle, panel in zip(command.flap_angles_rad, craft.aero_panels)),
             "command_exceeds_physical_flap")


def _check_phase_events(record, events, settings, profile):
    screen, outcome = record["ascent_screen"], record["outcome"]
    last, objective, options = record["final_state"], settings.objectives, settings.insertion_options
    witnesses = screen.get("transition_witnesses")
    _require(type(witnesses) is list, "phase_transition_witnesses_missing")
    for witness in witnesses:
        _require(witness.get("state_reset") is False and witness.get("state_before") == witness.get("state_after")
                 and witness.get("state_after_origin") in ("next_physics_tick_before_command", "final_retained_state")
                 and witness["time_s"] == witness["state_before"]["time_s"], "phase_transition_resets_actual_state")
        _state(witness["state_before"])
        _require(any(sample["time_s"] == witness["time_s"] and _state_fields(sample) == witness["state_before"]
                     for sample in record["samples"]), "phase_witness_not_saved_actual_state")
    first, cuts = screen.get("first_cutoff_measurement"), events.get("ascent_screen_first_cutoff_command", [])
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
    restarts = events.get("ascent_screen_restart_command", [])
    _require(len(restarts) <= 1, "repeated_state_insertion_restart")
    restart_time = None
    for event in restarts:
        restart_time = event["time_s"]
        geometry = _geometry(event["state"], settings)
        _require(first is not None and first["time_s"] <= restart_time < options.maximum_restart_time_s
                 and geometry["ready"] and geometry["target_speed"] > geometry["tangent_speed"]
                 and event["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"]
                 and event["state"]["engine_states"][0]["available"] is True
                 and any(w["time_s"] == restart_time and w["from_phase"] == "suborbital_coast"
                     and w["to_phase"] == "orbit_insertion" and w["state_before"] == event["state"] for w in witnesses),
                 "restart_without_measured_state_window")
    _require(screen.get("insertion_restart_time_s") == restart_time, "restart_clock_mismatch")
    final, commands = screen.get("cutoff_measurement"), events.get("ascent_screen_final_cutoff_command", [])
    _require(len(commands) <= 1 and bool(commands) == (final is not None), "final_cutoff_event_measurement_mismatch")
    reached = False
    if commands:
        event, orbit = commands[0], _orbit(final["state"])
        reached = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= objective.final_perigee_m
        _require(final["time_s"] == event["time_s"] and final["state"] == event["state"]
                 and final.get("orbit") == event.get("orbit") == orbit and final.get("gate_reached") is reached
                 and reached and first is not None and final["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"]
                 and event.get("termination") == "insertion_gate_reached", "final_cutoff_has_no_actual_orbital_gate")
        if restart_time is not None:
            _require(restart_time <= event["time_s"] <= restart_time+options.maximum_burn_duration_s,
                     "cutoff_after_registered_burn_limit")
        _require(any(w["time_s"] == event["time_s"] and w["state_before"] == event["state"]
                     and w["to_phase"] == "ascent_screen_shutdown" for w in witnesses), "final_cutoff_continuity_witness_missing")
    expected_end = final["time_s"]+options.shutdown_settle_s if reached else None
    _require(screen.get("insertion_shutdown_until_s") == expected_end, "shutdown_end_clock_mismatch")
    _require(screen.get("final_orbit") == _orbit(last) and screen.get("insertion_gate_reached") is reached
             and outcome.get("orbit_gate_reached") is reached, "final_insertion_claim_mismatch")
    termination = outcome["termination"]
    if termination in STATE_FAILURES:
        _require(first is not None and outcome["phase"] in {"suborbital_coast", "orbit_insertion"},
                 "state_insertion_failure_outside_insertion_phases")
    terminal_name = ("time_limit" if termination == "insertion_gate_reached" else
        "ascent_screen_physical_failure" if termination in {"ascent_time_limit_exceeded", "ascent_propulsion_unavailable", "ascent_reserve_exhausted"}|STATE_FAILURES else termination)
    terminal = events.get(terminal_name, [])
    _require(len(terminal) == 1 and terminal[0]["time_s"] == last["time_s"], "actual_terminal_event_missing")
    if termination == "insertion_gate_reached":
        _require(reached and last["time_s"] == expected_end and outcome["phase"] == "ascent_screen_shutdown",
                 "incomplete_finite_shutdown_continuation")
    elif termination == "ascent_reserve_exhausted":
        _require(last["propellant_kg"] <= profile["ship"]["ascent_reserve_kg"], "reserve_stop_without_crossing")
    elif termination == "state_insertion_deadline":
        _require(restart_time is None and last["time_s"] == options.maximum_restart_time_s, "restart_deadline_mismatch")
    elif termination == "state_insertion_burn_limit":
        _require(restart_time is not None and last["time_s"] == restart_time+options.maximum_burn_duration_s,
                 "burn_limit_clock_mismatch")
    elif termination == "state_insertion_window_missed":
        geometry = _geometry(record["final_state"], settings)
        _require(restart_time is None and geometry["radial"] <= 0.
                 and (geometry["radial"] < -options.maximum_radial_speed_mps or geometry["gap"] > 2000.),
                 "window_stop_without_actual_missed_window")
    elif termination == "state_insertion_unavailable":
        geometry = _geometry(record["final_state"], settings)
        _require(record["final_state"]["engine_states"][0]["available"] is False
                 or geometry["orbit"]["status"] != "bound"
                 or restart_time is None and (not geometry["capacity"]
                    or geometry["ready"] and geometry["target_speed"] <= geometry["tangent_speed"]),
                 "unavailable_stop_without_actual_incapacity")
    elif termination == "time_limit":
        _require(last["time_s"] == HORIZON_S and not reached, "unexplained_short_time_limit")
    contact = termination in {"surface_impact", "low_speed_surface_contact"}
    _require(record["samples"][-1].get("contact", False) is contact, "terminal_contact_flag_mismatch")
    if contact:
        _require(type(outcome.get("contact_receipt")) is dict
                 and outcome["contact_receipt"].get("event_time_s") == last["time_s"], "terminal_contact_receipt_missing")
    return reached


def _verify_control_trace(*, trace, samples, initial_state, final_state, final_vehicle, last_phase,
                          profile, settings, screen, events, termination, contact_receipt=None):
    """Check an actual closed ascent prefix, without inventing a stopped flight.

    The caller independently binds the prefix endpoint and phase events. A whole
    mission may pass its real prefix samples and trace, never a diagnostic run.
    """
    rows, terminal = trace["rows"], trace["terminal_commands"]
    _require(type(rows) is list and 0 < len(rows) <= MAX_TRACE_ROWS
             and type(terminal) is list and len(terminal) <= 1, "missing_or_unbounded_control_ticks")
    _require(all(sample["phase"] in PHASES for sample in samples), "non_ascent_prefix_sample")
    wanted = settings.record()
    stages = events.get("stage_separation", [])
    separation_time = stages[0]["time_s"] if stages else None
    from .starship_sixdof_mission import vehicle, stack_vehicle
    ship, booster = vehicle(profile), vehicle(profile, "booster")
    stack = stack_vehicle(profile, ship, booster)
    # Production initial_state is the full first observation sample; only
    # these exact physical/actuator fields are the plant-chain anchor.
    previous = _state_fields(initial_state)
    previous_phase = "stack_ascent"
    history = (tuple(previous["q_body_to_eci"]), 0., (0., 0., 0.), tuple(previous["q_body_to_eci"]))
    samples_by_key = {(sample["time_s"], sample["phase"]): sample for sample in samples if sample.get("command") is not None}
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
        if row["phase"] != previous_phase and previous_phase != "stack_ascent":
            _require(any(w["time_s"] == state.time_s and w["from_phase"] == previous_phase
                and w["to_phase"] == row["phase"] and w["state_before"] == before
                for w in screen["transition_witnesses"]), "phase_tick_change_without_actual_witness")
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
        horizon = screen["insertion_shutdown_until_s"] if row["phase"] == "ascent_screen_shutdown" else HORIZON_S
        expected_interval = min(horizon-state.time_s, expected_interval)
        boundaries = [settings.insertion_options.maximum_restart_time_s]
        if screen["insertion_restart_time_s"] is not None:
            boundaries.append(screen["insertion_restart_time_s"]+settings.insertion_options.maximum_burn_duration_s)
        for boundary in sorted(boundaries+[item["time_s"] for item in wanted["shutdowns"]]):
            if state.time_s < boundary < state.time_s+expected_interval:
                expected_interval = boundary-state.time_s
                break
        _equal(row.get("requested_interval_s"), expected_interval, "requested_tick_interval_changed", atol=1e-12)
        _require(row["interval_s"] <= expected_interval+1e-9, "plant_advanced_beyond_requested_interval")
        if row["interval_s"] < expected_interval-1e-9:
            _require(index == len(rows)-1 and termination in {"surface_impact", "low_speed_surface_contact"}
                     and contact_receipt["event_time_s"] == after.time_s, "unexplained_partial_physics_tick")
        _require(after.propellant_kg <= state.propellant_kg+1e-6, "propellant_increased_during_step")
        _actuator_step_limits(state, after, craft, row["interval_s"])
        if row["phase"] == "orbit_insertion":
            restart = screen["insertion_restart_time_s"]
            _require(restart is not None and restart <= state.time_s < restart+settings.insertion_options.maximum_burn_duration_s,
                     "burn_without_actual_restart_or_past_limit")
        _raw_request(row, craft, profile, settings, separation_time)
        diagnostic = row["controller"]["ascent_control"]
        expected_diagnostic = {"schema": "missionos.starship_ascent_control_request.v1",
            "raw_goal_q_body_to_eci": row["raw_goal_q_body_to_eci"],
            "requested_q_body_to_eci": row["requested_q_body_to_eci"], "phase": row["phase"],
            **asdict(settings.control_options)}
        _equal(diagnostic, expected_diagnostic, "ascent_control_request_receipt_mismatch")
        reference_rate = reference_acceleration = None
        if settings.control_options.moving_reference_tracking:
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
        previous, previous_phase = returned, row["phase"]
    _require(_wire(previous) == _wire(final_state), "last_control_tick_differs_from_final_state")
    for item in terminal:
        saved._json_tree(item)
        _require(item.get("integrated") is False and item.get("reference_updated") is False
                 and item["time_s"] == final_state["time_s"] and item["state"] == final_state, "invalid_terminal_command")
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
        _raw_request(converted, craft, profile, settings, separation_time, terminal=not angular)
        _check_control(converted, craft, profile)
        key = (item["time_s"], item["phase"])
        _require(key in samples_by_key and samples_by_key[key]["command"] == item["command"]
                 and samples_by_key[key]["controller"] == item["controller"], "terminal_sample_missing")
        witnessed_samples.add(key)
    _require(witnessed_samples == set(samples_by_key), "saved_command_sample_has_no_actual_tick")
    for sample in samples:
        craft = stack if sample["phase"] == "stack_ascent" else ship
        observation = dyn.observe(_state(sample), craft)
        # saved._samples above independently recomputes ground speed from
        # ECI velocity; observe() without wind reports air_speed instead.
        for key in ("altitude_m", "mass_kg", "dynamic_pressure_pa"):
            _equal(sample[key], observation[key], "saved_observation_mismatch:"+key, atol=1e-5)
    _require(final_vehicle == json.loads(json.dumps(asdict(stack if last_phase == "stack_ascent" else ship))),
             "final_vehicle_differs_from_profile")
    return len(witnessed_samples)


def verify_state_insertion_record(record, profile, *, expected_settings: AscentScreenSettings):
    """Check every declared control tick; never infer an unsaved tick or grant."""
    result = {"schema": "missionos.starship_state_insertion_record_verification.v1", "passed": False, "issues": [],
        "scope": "full saved state/reference/command consistency; no dynamics reexecution or vehicle qualification",
        "legacy_v3_qualification_borrowed": False, "physical_reexecution": False,
        "mission_authority_granted": False}
    try:
        _require(type(expected_settings) is AscentScreenSettings and expected_settings.policy == POLICY
                 and expected_settings.finite_mode is True and type(profile) is dict, "explicit_v7_settings_required")
        _require(expected_settings.insertion_options is not None and expected_settings.control_options.first_burn_only_bias is True, "explicit_state_insertion_options_required")
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
        _require(screen.get("registered_duration_s") == HORIZON_S, "registered_horizon_changed")
        termination = outcome["termination"]
        _require(termination in PHYSICAL_FAILURES | {"time_limit", "insertion_gate_reached"}
                 and termination != "numerical_failure", "unknown_or_numerical_termination")
        _require(type(outcome.get("integration_steps")) is int and outcome["integration_steps"] == len(rows), "missing_control_ticks")
        _require(outcome["duration_s"] == last["time_s"] <= HORIZON_S and outcome["phase"] == last["phase"], "invalid_finite_horizon")
        for key, value in (("six_dof_integrated", True), ("attitude_prescribed", False),
                           ("starship_vehicle_validated", False), ("booster_return_invoked", False)):
            _require(outcome.get(key) is value, "incorrect_claim_boundary:"+key)
        _require(record.get("booster_run") is None and record.get("satellites") == []
                 and outcome.get("payload_released_count") == 0, "control_screen_cannot_dispatch_mission")
        _require(_wire(screen.get("settings")) == _wire(wanted) and screen.get("policy") == POLICY
                 and screen.get("phase_objectives") == wanted["objectives"]
                 and screen.get("final_mission_perigee_gate_m") == profile["guidance"]["target_perigee_m"]
                 and screen.get("phase_objectives_are_registered_engineering_inputs") is True
                 and screen.get("legacy_v3_verification_scope") is False, "v7_screen_settings_or_authority_mismatch")
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
        witnessed_count = _verify_control_trace(trace=trace, samples=record["samples"],
            initial_state=record["initial_state"], final_state=record["final_state"],
            final_vehicle=record["final_vehicle"], last_phase=last["phase"], profile=profile,
            settings=expected_settings, screen=screen, events=events, termination=termination,
            contact_receipt=outcome.get("contact_receipt"))
        settled = _orbit(record["final_state"])
        result.update(passed=True, termination=termination, physical_failure=termination in PHYSICAL_FAILURES,
            measured_settled_insertion_gate_reached=settled["status"] == "bound" and settled["perigee_altitude_m"] >= expected_settings.objectives.final_perigee_m,
            final_propellant_above_unchanged_reserve=record["final_state"]["propellant_kg"] >= profile["ship"]["ascent_reserve_kg"],
            verified_control_ticks=len(rows), verified_command_samples=witnessed_count,
            terminal_command_count=len(terminal), state_assignment_runtime_proven=False,
            measured_final_insertion_gate_reached=reached,
            deferred_booster_honestly_recorded=True)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_ascent_control_record", "detail": str(exc)})
    return result
