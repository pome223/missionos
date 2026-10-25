"""Saved-only direct checks of ground-to-return state-insertion missions.

This new explicit scope preserves the strict healthy v5 checker. It checks real
ascent prefix rows in the whole record, not a fabricated stopped diagnostic run.
No controller, dynamics integration, forecast or hosted model is invoked.
"""
from dataclasses import asdict
import math
import re

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from . import starship_ascent_state_metrics as prefix
from .starship_ascent import (AscentCoastOrbitObjectives, AscentScreenSettings,
    AscentControlOptions, AscentShutdown, StateInsertionOptions)
from .starship_experimental_continuation import development_settings
from .starship_experimental_mission_metrics import (STATE_FIELDS, FORBIDDEN, digest,
    _require, _same, _json, _vehicle_for_sample, _clock, _verify_booster)
from .starship_retained_return import TRIMMED_POLICY_ID


def _settings(expected, profile):
    from .starship_state_mission import ExperimentalStateMissionAscentSettings
    for shutdowns in ((), (AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.))):
        candidate = ExperimentalStateMissionAscentSettings(AscentScreenSettings(policy="state_insertion_v7",
            shutdowns=shutdowns, objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
            control_options=AscentControlOptions(True, False), insertion_options=StateInsertionOptions()))
        if type(expected) is dict and digest(expected) == digest(candidate.record()):
            candidate.validate_profile(profile)
            return candidate
    raise ValueError("explicit_frozen_state_mission_settings_required")


def _check_availability(sample, kind, settings):
    prefix._availability(sample, kind, settings.ascent.record()["shutdowns"])
    main_count = 33 if kind == "booster" else 6
    _require(all(row.get("available") is True for row in sample["engine_states"][main_count:]),
        "Unregistered RCS engine outage")
    return True


def _initial_ground(record, profile):
    """Independently reconstruct only the declared surface initial state."""
    from .starship_sixdof_mission import vehicle, stack_vehicle
    craft = stack_vehicle(profile, vehicle(profile), vehicle(profile, "booster"))
    altitude = dyn.mass_properties(craft, craft.propellant_capacity_kg).com_body_m[2]+profile["launch"]["release_base_altitude_m"]
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
        altitude, craft.propellant_capacity_kg)
    up, _, north = env.local_frame(point)
    state = dyn.state_from_dict({k: record["initial_state"][k] for k in STATE_FIELDS})
    _require(state.time_s == 0. and state.r_eci_m == point.r and state.v_eci_mps == point.v
        and state.propellant_kg == craft.propellant_capacity_kg
        and env.norm(env.add(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), env.scale(up, -1.))) < 1e-10
        and env.norm(env.add(dyn.rotate(state.q_body_to_eci, (1., 0., 0.)), env.scale(north, -1.))) < 1e-10
        and env.norm(env.add(dyn.rotate(state.q_body_to_eci, state.omega_body_rad_s), (0., 0., -env.EARTH_ROTATION_RAD_S))) < 1e-12,
        "Whole state mission must start at the unchanged profile ground state")
    _require(len(state.engine_states) == len(craft.engines) and len(state.flap_angles_rad) == len(craft.aero_panels)
        and all(angle == 0. for angle in state.flap_angles_rad), "Ground actuator inventory differs")
    for engine, actual in zip(craft.engines, state.engine_states):
        wanted = dyn.EngineState(throttle=1. if profile["launch"]["main_engines_initially_spooled"] and "main" in engine.name else 0.)
        _require(asdict(actual) == asdict(wanted), "Ground actuator spool or availability changed")


def _run_state_basics(record, profile):
    """Keep the common strict contact path; admit explicit new physical stops."""
    termination = record.get("outcome", {}).get("termination")
    if termination in saved._TERMINATIONS:
        return saved._run_basics(record, "$", [0], profile)
    allowed = prefix.PHYSICAL_FAILURES | {"state_insertion_settled_gate_unreached"}
    _require(termination in allowed, "Unknown state mission termination")
    first, last = saved._samples(record.get("samples"), "$.samples", [0], "ship", wind_profile=profile)
    outcome = record["outcome"]
    _require(_same(first, record.get("initial_state")) and _same(last, record.get("final_state"))
        and outcome.get("six_dof_integrated") is True and outcome.get("attitude_prescribed") is False
        and outcome.get("starship_vehicle_validated") is False and outcome.get("contact_receipt") is None
        and last.get("contact") is False and type(outcome.get("integration_steps")) is int
        and outcome["integration_steps"] > 0 and outcome.get("phase") == last["phase"]
        and saved._near(outcome.get("duration_s"), last["time_s"]-first["time_s"])
        and saved._near(outcome.get("final_altitude_m"), last["altitude_m"])
        and saved._near(outcome.get("final_ground_speed_mps"), last["ground_speed_mps"])
        and saved._number(outcome.get("max_altitude_m"))
        and outcome["max_altitude_m"] >= max(sample["altitude_m"] for sample in record["samples"])-1e-5,
        "Physical negative lost its actual finite state or observation")
    events = saved._events(record.get("events"), "$.events", first["time_s"], last["time_s"])
    name = termination if termination == "state_insertion_settled_gate_unreached" else "ascent_screen_physical_failure"
    stops = events.get(name, [])
    _require(len(stops) == 1 and stops[0]["time_s"] == last["time_s"]
        and (name == termination or stops[0].get("termination") == termination),
        "New physical stop lacks its exact actual final event")
    return first, last, outcome, events, None


def _trace_clock_binding(record):
    """Each saved ascent integration is one actual shared-clock Ship step."""
    rows = record["ascent_control_trace"]["rows"]
    clock = [row for row in record["concurrent_execution"]["tick_order"] if row["body_id"] == "ship"
        and row["next_time_s"] > row["time_s"]]
    _require(record["outcome"]["integration_steps"] == len(clock), "Whole integration count differs from actual clock")
    ascent_clock = [row for row in clock if row.get("phase") in prefix.PHASES]
    _require(len(ascent_clock) == len(rows) and all(
        actual["time_s"] == row["time_s"] and actual["phase"] == row["phase"]
        and actual["next_time_s"] == row["state_returned_by_plant"]["time_s"]
        for actual, row in zip(ascent_clock, rows)), "Actual ascent trace differs from concurrent execution steps")


def _ascent(record, profile, settings, metadata, events):
    """Directly check the real closed ascent prefix and its actual handoff."""
    from .starship_sixdof_mission import vehicle
    specification = settings.ascent
    wanted = specification.record()
    trace = record.get("ascent_control_trace")
    _require(type(trace) is dict and trace.get("schema") == prefix.TRACE_SCHEMA
        and trace.get("settings") == wanted and trace.get("actual_state_assigned") is False
        and trace.get("mission_authority_granted") is False, "Missing or unbound actual v7 ascent trace")
    rows = trace.get("rows")
    _require(type(rows) is list and rows and type(metadata.get("ascent_trace_steps")) is int
        and len(rows) == metadata["ascent_trace_steps"], "Closed ascent trace length differs")
    _initial_ground(record, profile)
    stages = events.get("stage_separation", [])
    _require(len(stages) <= 1 and metadata.get("separation_time_s") == (stages[0]["time_s"] if stages else None),
        "Ascent separation metadata differs")
    if stages:
        for key, bound in (("mass_residual_kg", 1e-5), ("linear_momentum_residual_kg_mps", .1),
                           ("angular_momentum_residual_kg_m2_s", 1.)):
            _require(saved._number(stages[0].get(key)) and abs(stages[0][key]) < bound,
                "Separation conservation receipt exceeds unchanged bounds")
    prefix._shutdowns(record, metadata, wanted["shutdowns"], record["final_state"]["time_s"], saved)
    witnesses = metadata.get("transition_witnesses")
    allowed = {("ship_ascent", "suborbital_coast"), ("suborbital_coast", "orbit_insertion"),
        ("suborbital_coast", "ascent_screen_shutdown"), ("orbit_insertion", "ascent_screen_shutdown"),
        ("ascent_screen_shutdown", "orbital_coast")}
    _require(type(witnesses) is list, "Missing actual phase witnesses")
    for witness in witnesses:
        same_phase_failure = witness.get("from_phase") == witness.get("to_phase")
        _require(((witness.get("from_phase"), witness.get("to_phase")) in allowed or same_phase_failure)
            and witness.get("state_reset") is False and witness.get("state_before") == witness.get("state_after")
            and witness.get("state_after_origin") in ("next_physics_tick_before_command", "final_retained_state")
            and witness["time_s"] == witness["state_before"]["time_s"]
            and any(sample["time_s"] == witness["time_s"] and _same(sample, witness["state_before"])
                for sample in record["samples"]), "Phase transition changes or loses actual finite state")
    first, cutoff, settled = (metadata.get(key) for key in ("first_cutoff_measurement", "cutoff_measurement", "settled_measurement"))
    cuts, finals, restarts, grants = (events.get(key, []) for key in ("ascent_screen_first_cutoff_command",
        "ascent_screen_final_cutoff_command", "ascent_screen_restart_command", "state_insertion_settled_handoff"))
    _require(len(cuts) <= 1 and bool(cuts) == (first is not None) and len(finals) <= 1
        and bool(finals) == (cutoff is not None) and len(restarts) <= 1 and len(grants) <= 1,
        "Repeated or missing ascent gate event")
    if first is not None:
        orbit = prefix._orbit(first["state"])
        _require(first["time_s"] == cuts[0]["time_s"] == first["state"]["time_s"]
            and first["state"] == cuts[0].get("state") and first.get("orbit") == orbit
            and orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= specification.objectives.first_burn_perigee_m
            and orbit["apogee_altitude_m"] >= specification.objectives.first_burn_apogee_m
            and first["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"]
            and first.get("reason") == "measured_first_burn_coast_orbit_gates"
            and first.get("objective") == {"first_burn_apogee_m": specification.objectives.first_burn_apogee_m,
                "first_burn_perigee_m": specification.objectives.first_burn_perigee_m}
            and any(w["from_phase"] == "ship_ascent" and w["to_phase"] == "suborbital_coast"
                and w["state_before"] == first["state"] for w in witnesses), "First cutoff lacks measured gates or continuity")
    restart_time = None
    for event in restarts:
        restart_time = event["time_s"]
        geometry = prefix._geometry(event["state"], specification)
        _require(first is not None and first["time_s"] <= restart_time < specification.insertion_options.maximum_restart_time_s
            and geometry["ready"] and geometry["target_speed"] > geometry["tangent_speed"]
            and event["state"]["engine_states"][0]["available"] is True
            and event["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"]
            and any(w["time_s"] == restart_time and w["from_phase"] == "suborbital_coast"
                and w["to_phase"] == "orbit_insertion" and w["state_before"] == event["state"] for w in witnesses),
            "Insertion restart lacks actual apoapsis geometry and alignment")
    _require(metadata.get("insertion_restart_time_s") == restart_time, "Insertion restart clock differs")
    cutoff_reached = False
    if cutoff is not None:
        orbit = prefix._orbit(cutoff["state"])
        cutoff_reached = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= 220000.
        _require(cutoff_reached and first is not None and cutoff["time_s"] == finals[0]["time_s"] == cutoff["state"]["time_s"]
            and cutoff["state"] == finals[0].get("state") and cutoff.get("orbit") == finals[0].get("orbit") == orbit
            and cutoff.get("gate_reached") is True and finals[0].get("termination") == "insertion_gate_reached"
            and cutoff["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"]
            and (restart_time is None or restart_time <= cutoff["time_s"] <= restart_time+specification.insertion_options.maximum_burn_duration_s)
            and any(w["time_s"] == cutoff["time_s"] and w["state_before"] == cutoff["state"]
                and w["to_phase"] == "ascent_screen_shutdown" for w in witnesses), "Cutoff lacks actual bound orbit or finite shutdown transition")
    expected_settle = cutoff["time_s"]+specification.insertion_options.shutdown_settle_s if cutoff_reached else None
    _require(metadata.get("insertion_shutdown_until_s") == expected_settle, "Actual finite shutdown deadline differs")
    reached = False
    if settled is not None:
        state = dyn.state_from_dict(settled["state"])
        orbit = prefix._orbit(settled["state"])
        reached = bool(orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= 220000.
            and orbit["apogee_altitude_m"] <= 600000. and state.propellant_kg >= 75000.
            and env.norm(state.omega_body_rad_s) <= 5. and all(row.available for row in state.engine_states[:3])
            and len(metadata.get("shutdown_receipts", [])) == len(specification.shutdowns)
            and all(state.engine_states[int(row.engine_name.rsplit("_", 1)[1])].available is False
                for row in specification.shutdowns if row.engine_name.startswith("ship_")))
        _require(cutoff_reached and settled["time_s"] == state.time_s == expected_settle
            and settled.get("orbit") == orbit and settled.get("gate_reached") is reached
            and settled.get("vehicle") == _json(asdict(vehicle(profile, payload_count=26))),
            "Settled measurement differs from actual tail, profile, or independent gate")
        endpoint, endpoint_vehicle, endpoint_phase = settled["state"], settled["vehicle"], "ascent_screen_shutdown"
    else:
        _require(not cutoff_reached or record["final_state"]["time_s"] < expected_settle,
            "Completed shutdown omitted its actual settled measurement")
        endpoint, endpoint_vehicle, endpoint_phase = record["final_state"], record["final_vehicle"], record["outcome"]["phase"]
    _require(endpoint["time_s"] <= 4100. and rows[-1]["state_returned_by_plant"] == endpoint,
        "Closed ascent trace does not reach its exact actual endpoint")
    prefix_samples = [sample for sample in record["samples"] if sample["phase"] in prefix.PHASES]
    _require(prefix_samples and all(sample["time_s"] <= endpoint["time_s"] for sample in prefix_samples)
        and any(sample["time_s"] == endpoint["time_s"] and _same(sample, endpoint) for sample in prefix_samples),
        "Ascent prefix samples extend past or omit its actual endpoint")
    command_count = prefix._verify_control_trace(trace=trace, samples=prefix_samples, initial_state=record["initial_state"],
        final_state=endpoint, final_vehicle=endpoint_vehicle, last_phase=endpoint_phase, profile=profile,
        settings=specification, screen=metadata, events=events, termination=record["outcome"]["termination"],
        contact_receipt=record["outcome"].get("contact_receipt"))
    schedule = None if not reached else {"final_gate_time_s": cutoff["time_s"],
        "first_release_time_s": cutoff["time_s"]+45., "return_request_time_s": cutoff["time_s"]+1800.}
    _require(metadata.get("schedule") == schedule and bool(grants) is reached
        and record["outcome"].get("orbit_gate_reached") is reached,
        "Settled gate or cutoff-relative schedule claim differs")
    if reached:
        event = grants[0]
        _require(metadata.get("insertion_vehicle") == settled["vehicle"]
            and event["time_s"] == endpoint["time_s"] and event.get("state") == endpoint
            and event.get("state_reset") is False
            and all(event.get(k) == value for k, value in schedule.items())
            and any(w["from_phase"] == "ascent_screen_shutdown" and w["to_phase"] == "orbital_coast"
                and w["state_before"] == endpoint for w in witnesses), "Settled handoff resets actual state or shifts schedule")
    else:
        _require(metadata.get("insertion_vehicle") is None and not events.get("payload_released")
            and not events.get("return_requested"), "Unreached insertion dispatched a mission")
        if settled is not None:
            _require(record["outcome"]["termination"] == "state_insertion_settled_gate_unreached"
                and record["final_state"] == endpoint and len(events.get("state_insertion_settled_gate_unreached", [])) == 1,
                "Rejected settled gate lacks honest terminal evidence")
        else:
            termination = record["outcome"]["termination"]
            _require(termination in prefix.PHYSICAL_FAILURES | {"time_limit"},
                "Unsettled prefix lacks a declared physical stop")
            state, geometry = endpoint, prefix._geometry(endpoint, specification) if endpoint_phase != "stack_ascent" else None
            if termination == "ascent_reserve_exhausted":
                _require(state["propellant_kg"] <= profile["ship"]["ascent_reserve_kg"], "Reserve stop lacks crossing")
            elif termination == "ascent_time_limit_exceeded":
                _require(state["time_s"] >= profile["guidance"]["max_ascent_s"], "Ascent deadline stop is early")
            elif termination == "state_insertion_deadline":
                _require(first is not None and restart_time is None and state["time_s"] == specification.insertion_options.maximum_restart_time_s,
                    "Insertion deadline stop lacks actual deadline")
            elif termination == "state_insertion_burn_limit":
                _require(restart_time is not None and state["time_s"] == restart_time+specification.insertion_options.maximum_burn_duration_s,
                    "Burn-limit stop lacks actual burn boundary")
            elif termination == "state_insertion_window_missed":
                _require(geometry is not None and restart_time is None and geometry["radial"] <= 0.
                    and (geometry["radial"] < -specification.insertion_options.maximum_radial_speed_mps or geometry["gap"] > 2000.),
                    "Missed-window stop lacks actual geometry")
            elif termination == "state_insertion_unavailable":
                _require(geometry is not None and (state["engine_states"][0]["available"] is False
                    or geometry["orbit"]["status"] != "bound" or restart_time is None and (not geometry["capacity"]
                        or geometry["ready"] and geometry["target_speed"] <= geometry["tangent_speed"])),
                    "Unavailable stop lacks actual incapacity")
    return reached, command_count


def _verify_state_physical_mission(record, profile, *, expected_settings, expected_source_sha256,
        expected_site, expected_goal, expected_return_sites, expected_duration_s,
        booster_policy="fixed_v1", booster_verifier=None, execution_binding=None):
    """Verify saved evidence; a valid impact remains a physical negative."""
    result = {"schema": "missionos.experimental_state_physical_mission_verification.v1", "passed": False,
        "issues": [], "physical_success": False, "ship_physical_success": False,
        "booster_physical_success": False, "deployment_completed": False,
        "old_certificate_admitted": False, "qualification_refreshed": False,
        "ai_control_completed": False, "all_features_completed": False, "physical_execution": False,
        "scope": "saved source/state/command/inventory/clock/contact consistency; no independent physical model"}
    try:
        settings = _settings(expected_settings, profile)
        _require(type(expected_source_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", expected_source_sha256)
            and type(expected_duration_s) in (int, float) and math.isfinite(expected_duration_s)
            and 3336. < expected_duration_s <= min(7500., profile["integration"]["max_duration_s"]),
            "Explicit registered source identity and absolute horizon are required")
        saved._json_tree({key: value for key, value in record.items() if key != "ascent_control_trace"})
        _require(type(expected_return_sites) is dict and type(expected_return_sites.get("sites")) is list
            and expected_site in expected_return_sites["sites"], "Actual booster site is not in the explicitly bound catalog")
        _require(record.get("scenario") == "experimental_physical_mission"
            and not any(key in record for key in FORBIDDEN)
            and record.get("development_return_qualification") == development_settings(),
            "Experimental whole mission borrowed a diagnostic, qualification or actor scope")
        wanted_execution = execution_binding if execution_binding is not None else {
            "schema": "missionos.experimental_state_physical_mission_execution.v1",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "settings_sha256": digest(expected_settings)}
        _require(record.get("experimental_mission_execution") == wanted_execution,
            "Runner source/profile/settings binding differs")
        metadata = record["experimental_mission_ascent"]
        for policy, field in (("availability_water_return_v1", "booster_outage_control"),
                ("availability_energy_water_return_v2", "booster_energy_control"),
                ("availability_terminal_recovery_v1", "booster_terminal_recovery")):
            _require(booster_policy == policy or field not in metadata,
                "Booster scope cannot borrow another availability-aware control")
        _require(metadata.get("schema") == "missionos.experimental_state_mission_ascent.v1"
            and digest(metadata.get("settings")) == digest(expected_settings)
            and type(metadata.get("requested_absolute_duration_s")) in (int, float)
            and metadata["requested_absolute_duration_s"] == expected_duration_s
            and metadata.get("shutdown_schedule") == settings.ascent.record()["shutdowns"]
            and metadata.get("final_mission_perigee_gate_m") == profile["guidance"]["target_perigee_m"]
            and metadata.get("booster_policy") == booster_policy
            and metadata.get("booster_splashdown_goal") == expected_goal
            and metadata.get("booster_return_sites") == expected_return_sites,
            "Experimental ascent settings, input goals or finite horizon differ")
        for key, expected in (("engineering_objectives_not_real_flight_identification", True),
            ("shared_production_deployment_and_return_loop", True), ("concurrent_actual_separation", True),
            ("qualified_return", False), ("mission_authority_granted", False), ("physical_execution", False),
            ("state_reset", False), ("all_features_completed", False)):
            _require(metadata.get(key) is expected, "Experimental mission claim boundary changed")
        _require(type(metadata.get("native_model_calls")) is int and metadata["native_model_calls"] == 0,
            "This physical cohort cannot claim hosted-model work")
        first, last, outcome, events, speed = _run_state_basics(record, profile)
        _require(first["time_s"] == 0. and first["phase"] == "stack_ascent"
            and _same(first, record["initial_state"]) and _same(last, record["final_state"])
            and outcome["termination"] != "numerical_failure"
            and last["time_s"] <= expected_duration_s and outcome["integration_steps"] > 0,
            "Whole mission lacks actual time0 history, full actuator state or valid termination")
        if outcome["termination"] == "time_limit":
            _require(last["time_s"] == expected_duration_s, "Time-limit run ended before its registered horizon")
        reached, command_count = _ascent(record, profile, settings, metadata, events)
        budget = [len(record["samples"])]
        saved._payloads(record, profile, "$", budget, events, reached)
        count, releases = outcome["payload_released_count"], events.get("payload_released", [])
        schedule = metadata.get("schedule")
        _require(not releases or schedule is not None and releases[0]["time_s"] >= schedule["first_release_time_s"],
            "Actual payload release precedes measured insertion-relative schedule")
        previous, vehicles = {}, {}
        for sample in record["samples"]:
            signature = (sample["phase"] == "stack_ascent", sum(e["time_s"] < sample["time_s"]
                or e["time_s"] == sample["time_s"] and _same(sample, e["receipt"]["parent_after_state"]) for e in releases))
            if signature not in vehicles:
                vehicles[signature] = _vehicle_for_sample(record, profile, sample, releases)
            craft = vehicles[signature]
            state = dyn.state_from_dict({k: sample[k] for k in STATE_FIELDS})
            command = (dyn.command_from_dict(sample["command"]) if sample.get("command") is not None else
                dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad))
            dyn._validate_pair(state, craft, command)
            _require(len(state.engine_states) == len(craft.engines) and len(state.flap_angles_rad) == len(craft.aero_panels)
                and _check_availability(sample, "booster" if sample["phase"] == "stack_ascent" else "ship", settings), "Unregistered engine availability or actuator inventory")
            body = signature[0]
            _require(body not in previous or state.propellant_kg <= previous[body]+1e-6,
                "Propellant increased without an actual body separation")
            previous[body] = state.propellant_kg
            observation = dyn.observe(state, craft)
            for key in ("altitude_m", "mass_kg", "com_z_m"):
                actual = observation["com_body_m"][2] if key == "com_z_m" else observation[key]
                _require(saved._near(sample.get(key), actual, 1e-5), "Saved observation differs from actual inventory/profile vehicle")
        final_craft = _vehicle_for_sample(record, profile, last, releases)
        _require(record.get("final_vehicle") == _json(asdict(final_craft)), "Final vehicle lost retained mass or changed actual capability")
        returns = events.get("return_requested", [])
        _require(len(returns) <= 1, "Repeated physical return request")
        if returns:
            _require(schedule is not None and 0 <= returns[0]["time_s"]-schedule["return_request_time_s"]
                <= profile["integration"]["coast_dt_s"]+1e-7
                and (not releases or releases[-1]["time_s"] <= returns[0]["time_s"]),
                "Return timing differs from actual insertion or releases continued after return")
        if schedule is not None and last["time_s"] > schedule["return_request_time_s"]+profile["integration"]["coast_dt_s"]+1e-7:
            _require(len(returns) == 1, "Missing due physical return request")
        retained = record.get("retained_return", {})
        _require(retained.get("policy_id") == TRIMMED_POLICY_ID and retained.get("landing_verified") is False
            and retained.get("starship_vehicle_validated") is False, "Experimental return policy or claim differs")
        if returns:
            activation = retained.get("activation", {})
            _require(activation.get("time_s") == returns[0]["time_s"]
                and activation.get("payload_retained_count") == 26-count
                and activation.get("payload_retained_mass_kg") == (26-count)*profile["payload"]["mass_each_kg"],
                "Return activation changed actual retained inventory")
        saved._return_allocation_scope(record, development_settings(), "$", profile)
        booster = record.get("booster_run")
        separation = record.get("booster_separation_state")
        _require(outcome.get("booster_return_invoked") is (booster is not None)
            and bool(events.get("stage_separation")) is (booster is not None), "Separation and actual booster invocation differ")
        booster_success = False
        if booster is not None:
            if booster_verifier is None:
                _verify_booster(booster, separation, profile, expected_site, expected_goal, expected_duration_s, budget,
                    availability_check=lambda sample, kind: _check_availability(sample, kind, settings))
            else:
                booster_verifier(booster, separation, profile, expected_site, expected_goal, expected_duration_s, budget,
                    availability_check=lambda sample, kind: _check_availability(sample, kind, settings))
            from .starship_splashdown_verifier import verify_splashdown
            water = verify_splashdown(booster, expected_goal)
            _require(water["passed"], "Independent saved water-contact geometry rejected")
            booster_success = water["controlled_water_entry_envelope_met"]
            result["booster"] = water
        _clock(record, profile, expected_duration_s)
        _trace_clock_binding(record)
        tilt, envelope = None, False
        rate = env.norm(last["omega_body_rad_s"])
        receipt = outcome.get("contact_receipt")
        if receipt is not None:
            axis = dyn.rotate(last["q_body_to_eci"], (0., 0., 1.))
            tilt = math.degrees(math.acos(max(-1., min(1., env.dot(axis, receipt["surface_normal_eci"])))))
            envelope = (not receipt["initial_overlap"] and receipt["surface_normal_speed_mps"] <= 0
                and speed <= 5. and tilt <= 5. and rate <= .02
                and last["propellant_kg"] >= profile["ship"]["return_reserve_kg"])
        ship_success = bool(reached and count == 26 and returns and envelope)
        result.update(passed=True, physical_success=bool(ship_success and booster_success),
            ship_physical_success=ship_success, booster_physical_success=booster_success,
            deployment_completed=count == 26, payload_released_count=count, retained_count=26-count,
            measured_final_insertion_gate_reached=reached, verified_ascent_command_samples=command_count,
            verified_ascent_control_ticks=metadata["ascent_trace_steps"],
            termination=outcome["termination"], contact_point_speed_mps=speed, final_tilt_deg=tilt,
            final_propellant_kg=last["propellant_kg"], return_requested=bool(returns), physical_reexecution=False)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_experimental_state_whole_mission", "detail": str(exc)})
    return result


def verify_state_physical_mission(record, profile, *, expected_settings, expected_source_sha256,
        expected_site, expected_goal, expected_return_sites, expected_duration_s):
    """Strict original v7 whole scope; no alternate Booster policy admission."""
    return _verify_state_physical_mission(record, profile, expected_settings=expected_settings,
        expected_source_sha256=expected_source_sha256, expected_site=expected_site,
        expected_goal=expected_goal, expected_return_sites=expected_return_sites,
        expected_duration_s=expected_duration_s)
