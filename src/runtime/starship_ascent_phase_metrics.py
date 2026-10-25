"""Saved evidence checks for separately registered phase-objective ascent.

This checks JSON states, static profile vehicles, orbital identities and the
registered command goal at saved samples. It does not propagate a trajectory,
validate a real vehicle, admit deployment/return, or borrow the v3 certificate.
Source/input hashes, deadlines and complete per-step provenance remain the
runner's responsibility; saved samples cannot prove every unsaved control tick.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from typing import Mapping

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from . import starship_ascent_metrics as original
from .starship_ascent_retry_metrics import _shutdowns, PHYSICAL_FAILURES, EXTRA_PHYSICAL_FAILURES
from .starship_ascent import AscentObjectives, AscentCoastOrbitObjectives, AscentScreenSettings, AscentShutdown, AscentController


POLICY = "phase_objectives_v4"
COAST_ORBIT_POLICY = "coast_orbit_v5"
STATE_FIELDS = (*saved._STATE_FIELDS, "engine_states", "flap_angles_rad")
PHASES = {"stack_ascent", "ship_ascent", "suborbital_coast", "orbit_insertion", "ascent_screen_shutdown"}


def _state(row):
    return dyn.state_from_dict({key: row[key] for key in STATE_FIELDS})


def _same_full(left, right):
    return type(left) is dict and type(right) is dict and all(left.get(k) == right.get(k) for k in STATE_FIELDS)


def _orbit(row):
    return env.orbital_elements(env.State3D(row["time_s"], row["r_eci_m"], row["v_eci_mps"], row["propellant_kg"]))


def _reached(row, perigee):
    orbit = _orbit(row)
    return orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= perigee


def _require(condition, detail):
    saved._require(condition, "$", detail)


def _settings(expected, profile):
    _require(type(expected) is dict, "Explicit registered phase settings are required")
    wanted = {key: value for key, value in expected.items() if key != "duration_s"}
    o = (AscentCoastOrbitObjectives(**wanted["objectives"]) if wanted.get("policy") == COAST_ORBIT_POLICY
         else AscentObjectives(**wanted["objectives"]))
    settings = AscentScreenSettings(wanted["stack_pitch_offset_deg"], wanted["ship_pitch_offset_deg"],
        wanted["finite_mode"], wanted["policy"], tuple(AscentShutdown(**x) for x in wanted["shutdowns"]), o)
    _require(wanted.get("policy") in (POLICY, COAST_ORBIT_POLICY) and settings.record() == wanted,
             "Phase settings differ from explicit registered policy contract")
    AscentController(settings).validate_profile(profile)
    _require(expected.get("duration_s", 1537.) == 1537., "Phase screen horizon must remain1537seconds")
    return wanted, settings


def _command_goal(sample, craft, profile, settings, separation_time, *, stop_request=False):
    """Independently check the high-level target algebra, not torque allocation."""
    command = sample.get("command")
    if command is None:
        return False
    actual = dyn.command_from_dict(command)
    state = _state(sample)
    _require(len(actual.engines) == len(craft.engines), "Command engine inventory differs")
    _require(len(actual.flap_angles_rad) == len(craft.aero_panels), "Command flap inventory differs")
    phase, time = sample["phase"], sample["time_s"]
    up, east, north = env.local_frame(env.State3D(time, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    radial = env.dot(state.v_eci_mps, up)
    tangent_vec = env.add(state.v_eci_mps, env.scale(up, -radial))
    tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
    g, o = profile["guidance"], settings.objectives
    direction, count, throttle = tangent, 0, 0.
    if stop_request:
        # The production loop records an explicit zero-throttle request at a
        # physical/deadline stop before breaking, retaining its former phase.
        # The caller binds this exception to the exact terminal event/state.
        pass
    elif phase == "stack_ascent":
        program = g["pitch_program_time_deg"]
        values = [a if i < 2 else a+settings.stack_pitch_offset_deg for i, (_, a) in enumerate(program)]
        if time <= program[0][0]:
            angle = values[0]
        elif time >= program[-1][0]:
            angle = values[-1]
        else:
            i = next(i for i in range(1, len(program)) if time <= program[i][0])
            fraction = (time-program[i-1][0])/(program[i][0]-program[i-1][0])
            angle = values[i-1]+fraction*(values[i]-values[i-1])
        heading = env.add(env.scale(east, math.sin(math.radians(profile["launch"]["azimuth_deg"]))),
                          env.scale(north, math.cos(math.radians(profile["launch"]["azimuth_deg"]))))
        direction = env.add(env.scale(up, math.sin(math.radians(angle))), env.scale(heading, math.cos(math.radians(angle))))
        count, throttle = 33, .7 if sample["dynamic_pressure_pa"] > g["max_q_pa"] else 1.
    elif phase in ("ship_ascent", "orbit_insertion"):
        _require(state.propellant_kg > profile["ship"]["ascent_reserve_kg"], "Powered command crosses unchanged reserve")
        gate = o.first_burn_perigee_m if phase == "ship_ascent" else o.final_perigee_m
        phase_reached = _reached(sample, gate)
        coast_guide = settings.policy == COAST_ORBIT_POLICY and phase == "ship_ascent"
        if coast_guide:
            orbit = _orbit(sample)
            phase_reached = phase_reached and orbit["apogee_altitude_m"] is not None and orbit["apogee_altitude_m"] >= o.first_burn_apogee_m
            up = env.unit(state.r_eci_m)
            radial = env.dot(state.v_eci_mps, up)
            tangent_vec = env.add(state.v_eci_mps, env.scale(up, -radial))
            tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
        _require(not phase_reached, "Main burn continued after its measured phase gate was achieved")
        _require(time < (g["max_ascent_s"] if phase == "ship_ascent" else 1536.), "Main burn exceeds finite deadline")
        count, throttle = (6 if phase == "ship_ascent" else 1), 1.
        acc = sum(engine.max_thrust_n for i, (engine, status) in enumerate(zip(craft.engines, state.engine_states))
            if i < count and "main" in engine.name and status.available)/sample["mass_kg"]
        _require(acc > 0., "Powered command claims unavailable requested propulsion")
        radius = env.norm(state.r_eci_m)
        reference = None
        if coast_guide:
            rp, ra = env.EARTH_EQUATORIAL_RADIUS_M+o.first_burn_perigee_m, env.EARTH_EQUATORIAL_RADIUS_M+o.first_burn_apogee_m
            a = (rp+ra)/2
            h2 = 2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
            speed2 = env.EARTH_MU_M3_S2*(2/radius-1/a)-h2/radius**2
            vr = math.sqrt(max(0., speed2))
            feedforward = h2/radius**3-env.EARTH_MU_M3_S2/radius**2
            reference = {"schema": "missionos.starship_ascent_coast_reference.v1",
                "frame": "geocentric_radial_inertial", "target_semimajor_axis_m": a,
                "target_angular_momentum_squared_m4_s2": h2,
                "radial_speed_squared_m2_s2": speed2, "desired_radial_velocity_mps": vr,
                "radial_feedforward_acceleration_mps2": feedforward,
                "inside_target_radial_interval": rp <= radius <= ra,
                "actual_state_assigned": False, "future_coast_verified": False,
                "actual_radial_velocity_mps": radial}
        else:
            target_h = o.first_burn_altitude_m if phase == "ship_ascent" else o.insertion_altitude_m
            vr = max(-50., min(800., (target_h-sample["altitude_m"])/g["ascent_altitude_response_s"]))
        radial_acc = env.EARTH_MU_M3_S2/radius**2-env.norm(tangent_vec)**2/radius+(vr-radial)/g["ascent_velocity_response_s"]
        if reference is not None:
            radial_acc += reference["radial_feedforward_acceleration_mps2"]
            reference["requested_radial_thrust_acceleration_mps2"] = radial_acc
            observed = sample.get("controller", {}).get("ascent_orbit_reference")
            _require(type(observed) is dict and set(observed) == set(reference), "Missing exact coast-orbit reference diagnostic")
            for key, expected in reference.items():
                equal = (math.isclose(observed[key], expected, rel_tol=2e-12, abs_tol=1e-8)
                         if type(expected) in (int, float) and saved._number(observed[key]) else observed[key] == expected)
                _require(equal, "Coast-orbit reference differs from saved state and registered target ellipse")
        fraction = max(-.25, min(.95, radial_acc/acc))
        if settings.ship_pitch_offset_deg:
            _require(separation_time is not None, "Pitch offset lacks actual separation")
            ramp = max(0., min(1., (time-separation_time)/10.))
            angle = math.atan2(fraction, math.sqrt(1-fraction*fraction))+math.radians(settings.ship_pitch_offset_deg)*ramp
            fraction = math.sin(max(math.asin(-.25), min(math.asin(.95), angle)))
        direction = env.add(env.scale(up, fraction), env.scale(tangent, math.sqrt(1-fraction*fraction)))
    elif phase == "suborbital_coast":
        _require(time < 1517. and not _reached(sample, o.final_perigee_m),
                 "Coast command ignored restart boundary or an achieved final orbital gate")
    target = sample.get("controller", {}).get("target_q_body_to_eci")
    _require(saved._vector(target, 4) and abs(sum(x*x for x in target)-1) < 1e-8, "Command lacks unit target attitude")
    axis = saved._rotate(target, [0., 0., 1.])
    _require(all(saved._near(a, b, 2e-12) for a, b in zip(axis, direction)), "Saved attitude target differs from registered phase objective")
    for i, (engine, request) in enumerate(zip(craft.engines, actual.engines)):
        if "main" in engine.name:
            enabled = i < count and throttle > 0.
            _require(request.enabled is enabled and request.throttle == (throttle if i < count else 0.),
                     "Main command differs from bounded phase request")
        _require(abs(request.gimbal_x_rad) <= engine.max_gimbal_rad+1e-12
                 and abs(request.gimbal_y_rad) <= engine.max_gimbal_rad+1e-12, "Command exceeds physical gimbal limit")
    return True


def verify_ascent_record(record: Mapping, *, finite_mode=True, expected_settings=None,
                         profile=None, shutdown_schedule=None) -> dict:
    result = {"schema": "missionos.starship_ascent_phase_record_verification.v1", "passed": False, "issues": [],
              "scope": "saved state/phase goal/command consistency; no propagation, real vehicle validity or mission qualification"}
    try:
        _require(finite_mode is True and type(profile) is dict, "Explicit profile and finite mode are required")
        saved._json_tree(record)
        saved._json_tree(expected_settings)
        wanted, settings = _settings(expected_settings, profile)
        o = settings.objectives
        first, last = saved._samples(record.get("samples"), "$.samples", [0], "ship")
        _require(record.get("scenario") == "launch" and first["time_s"] == 0., "Only actual launch-origin phase screens are accepted")
        _require(_same_full(first, record.get("initial_state")) and _same_full(last, record.get("final_state")),
                 "Initial/final full state or actuator history differs")
        outcome = record["outcome"]
        termination = outcome["termination"]
        allowed = PHYSICAL_FAILURES | {"time_limit", "insertion_gate_reached"}
        _require(termination in allowed and termination != "numerical_failure", "Unknown or numerical phase termination")
        _require(last["time_s"] <= 1537. and (termination != "time_limit" or last["time_s"] == 1537.), "Invalid finite horizon")
        _require(outcome.get("duration_s") == last["time_s"] and outcome.get("phase") == last["phase"]
                 and type(outcome.get("integration_steps")) is int and outcome["integration_steps"] > 0, "Duration/phase/step evidence differs")
        for key, expected in (("six_dof_integrated", True), ("attitude_prescribed", False),
                              ("starship_vehicle_validated", False), ("booster_return_invoked", False)):
            _require(outcome.get(key) is expected, "Phase screen claim boundary differs")
        _require(record.get("booster_run") is None and record.get("satellites") == []
                 and outcome.get("payload_released_count") == 0, "Phase screen cannot dispatch payload or recovery")
        events = saved._events(record.get("events"), "$.events", 0., last["time_s"])
        screen = record["ascent_screen"]
        _require(screen.get("settings") == wanted and screen.get("policy") == wanted["policy"]
                 and screen.get("phase_objectives") == wanted["objectives"]
                 and screen.get("final_mission_perigee_gate_m") == profile["guidance"]["target_perigee_m"]
                 and screen.get("phase_objectives_are_registered_engineering_inputs") is True
                 and screen.get("legacy_v3_verification_scope") is False, "Phase settings/objective provenance differs")
        for key in ("mission_dispatch", "return_qualified", "payload_release_authorized", "physical_execution", "propulsion_reference_connected"):
            _require(screen.get(key) is False, "Phase diagnostic cannot grant mission or qualified-return authority")
        schedule = wanted["shutdowns"] if shutdown_schedule is None else shutdown_schedule
        _require(schedule == wanted["shutdowns"] == screen.get("shutdown_schedule"), "Outage registration differs")
        _shutdowns(record, screen, schedule, last["time_s"], saved)
        stages = events.get("stage_separation", [])
        _require(len(stages) <= 1, "Repeated actual separation")
        separation_time = stages[0]["time_s"] if stages else None
        _require(screen.get("separation_time_s") == separation_time, "Separation metadata differs")
        if stages:
            result["actual_separation"] = original._separation(record)
            _require(record["booster_separation_state"]["time_s"] == separation_time, "Separated booster state time differs")
            _state(record["booster_separation_state"])
            for key, bound in (("mass_residual_kg", 1e-5), ("linear_momentum_residual_kg_mps", .1), ("angular_momentum_residual_kg_m2_s", 1.)):
                _require(saved._number(stages[0].get(key)) and abs(stages[0][key]) < bound, "Separation conservation receipt exceeds unchanged bound")
        else:
            _require(record.get("booster_separation_state") is None and termination in PHYSICAL_FAILURES, "Missing launch separation")
        from . import starship_sixdof_mission as mission
        ship, booster = mission.vehicle(profile), mission.vehicle(profile, "booster")
        stack = mission.stack_vehicle(profile, ship, booster)
        craft_by_phase = {key: stack if key == "stack_ascent" else ship for key in PHASES}
        command_count = 0
        previous_fuel = {}
        terminal_stops = events.get("ascent_screen_physical_failure", []) + [
            event for event in events.get("ascent_screen_final_cutoff_command", [])
            if event.get("termination") == "insertion_gate_unreached"]
        for sample in record["samples"]:
            phase = sample["phase"]
            _require(phase in PHASES and (phase == "stack_ascent" or separation_time is not None
                     and sample["time_s"] >= separation_time), "Phase lacks actual separation")
            craft = craft_by_phase[phase]
            state = _state(sample)
            _require(len(state.engine_states) == len(craft.engines) and len(state.flap_angles_rad) == len(craft.aero_panels), "Saved actuator inventory differs from profile")
            body = "stack" if phase == "stack_ascent" else "ship"
            _require(body not in previous_fuel or state.propellant_kg <= previous_fuel[body]+1e-6,
                     "Propellant increased without an actual body separation")
            previous_fuel[body] = state.propellant_kg
            for engine, actuator in zip(craft.engines, state.engine_states):
                _require((not engine.name.startswith("rcs_") or actuator.available is True)
                         and abs(actuator.gimbal_x_rad) <= engine.max_gimbal_rad+1e-12
                         and abs(actuator.gimbal_y_rad) <= engine.max_gimbal_rad+1e-12,
                         "Unregistered actuator unavailability or actual gimbal bound")
            observation = dyn.observe(state, craft)
            for key in ("altitude_m", "mass_kg", "dynamic_pressure_pa"):
                _require(saved._near(sample.get(key), observation[key], 1e-5), "Observation differs from full saved state/profile vehicle")
            stop_request = (sample["time_s"] == last["time_s"]
                and any(event.get("termination") == termination and event["time_s"] == sample["time_s"]
                    and _same_full(event.get("state"), sample) for event in terminal_stops))
            command_count += _command_goal(sample, craft, profile, settings, separation_time,
                                           stop_request=stop_request)
        _require(command_count > 0, "Missing phase command witnesses")
        _require(record["final_vehicle"] == json.loads(json.dumps(asdict(craft_by_phase[last["phase"]]))),
                 "Final vehicle differs from actual profile and retained payload inventory")
        for field in ("altitude_m", "ground_speed_mps"):
            _require(saved._near(outcome.get("final_"+field), last[field], 1e-5), "Final outcome observation differs")
        witnesses = screen["transition_witnesses"]
        _require(type(witnesses) is list, "Missing phase transition witnesses")
        for w in witnesses:
            _require(w.get("state_reset") is False and w.get("state_before") == w.get("state_after")
                     and w.get("state_after_origin") in ("next_physics_tick_before_command", "final_retained_state")
                     and w["time_s"] == w["state_before"]["time_s"], "Transition changes full actual state/actuators")
            _state(w["state_before"])
            _require(any(sample["time_s"] == w["time_s"] and _same_full(sample, w["state_before"])
                         for sample in record["samples"]), "Transition witness is not an actual saved state")
        first_cutoff = screen.get("first_cutoff_measurement")
        cuts = events.get("ascent_screen_first_cutoff_command", [])
        _require(len(cuts) <= 1 and bool(cuts) == (first_cutoff is not None), "First cutoff event/measurement differs")
        if cuts:
            event = cuts[0]
            coast_guide = settings.policy == COAST_ORBIT_POLICY
            first_objective = ({"first_burn_apogee_m": o.first_burn_apogee_m,
                "first_burn_perigee_m": o.first_burn_perigee_m} if coast_guide else {
                "first_burn_altitude_m": o.first_burn_altitude_m, "first_burn_perigee_m": o.first_burn_perigee_m})
            _require(first_cutoff["time_s"] == event["time_s"] and first_cutoff["state"] == event.get("state")
                     and first_cutoff.get("orbit") == _orbit(first_cutoff["state"])
                     and first_cutoff.get("reason") == ("measured_first_burn_coast_orbit_gates" if coast_guide
                        else "measured_first_burn_suborbital_perigee_gate")
                     and first_cutoff.get("objective") == first_objective
                     and _reached(first_cutoff["state"], o.first_burn_perigee_m)
                     and (not coast_guide or first_cutoff["orbit"]["apogee_altitude_m"] >= o.first_burn_apogee_m)
                     and first_cutoff["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"], "First cutoff lacks its measured registered phase objective")
            _require(any(w["time_s"] == event["time_s"] and w["from_phase"] == "ship_ascent"
                     and w["to_phase"] == "suborbital_coast" and w["state_before"] == event["state"] for w in witnesses), "First cutoff lacks continuous actual state witness")
        else:
            _require(termination in PHYSICAL_FAILURES and last["time_s"] <= profile["guidance"]["max_ascent_s"]+.2,
                     "Ascent lacks a physical stop or registered first cutoff")
        final_commands = events.get("ascent_screen_final_cutoff_command", [])
        cutoff = screen.get("cutoff_measurement")
        _require(len(final_commands) <= 1 and bool(final_commands) == (cutoff is not None), "Final cutoff event/measurement differs")
        if last["time_s"] > 1536.:
            _require(len(final_commands) == 1, "Continued beyond finite deadline without a measured final cutoff")
        reached = False
        if final_commands:
            event = final_commands[0]
            reached = _reached(event["state"], o.final_perigee_m)
            _require(cutoff["time_s"] == event["time_s"] and cutoff["state"] == event["state"]
                     and cutoff.get("orbit") == event.get("orbit") == _orbit(event["state"])
                     and cutoff.get("gate_reached") is reached and event["time_s"] <= 1536.
                     and (reached or event["time_s"] == 1536.), "Final cutoff lacks measured unchanged orbital gate")
            _require(event.get("termination") == ("insertion_gate_reached" if reached else "insertion_gate_unreached"), "Final cutoff classification differs")
            _require(any(w["time_s"] == event["time_s"] and w["state_before"] == event["state"]
                     and w["to_phase"] == ("ascent_screen_shutdown" if reached else w["from_phase"]) for w in witnesses), "Final cutoff lacks actual continuity witness")
        restarts = events.get("ascent_screen_restart_command", [])
        _require(len(restarts) <= 1, "Repeated insertion restart")
        for event in restarts:
            _require(event["time_s"] == 1517. and first_cutoff is not None
                     and first_cutoff["time_s"] <= 1517. and not _reached(event["state"], o.final_perigee_m)
                     and any(w["time_s"] == 1517. and w["from_phase"] == "suborbital_coast"
                        and w["to_phase"] == "orbit_insertion" and w["state_before"] == event["state"] for w in witnesses), "Restart ignored achieved orbital goal or exact-time state")
        if last["time_s"] > 1517. and first_cutoff is not None and (cutoff is None or cutoff["time_s"] > 1517.):
            _require(len(restarts) == 1, "Missing due insertion restart")
        _require(screen.get("final_orbit") == _orbit(record["final_state"]), "Final orbit differs from actual state")
        _require(outcome.get("orbit_gate_reached") is reached and screen.get("insertion_gate_reached") is reached, "Insertion claim differs from measured final gate")
        terminal_name = ("time_limit" if termination == "insertion_gate_reached" else
            "ascent_screen_final_cutoff_command" if termination == "insertion_gate_unreached" else
            "ascent_screen_physical_failure" if termination in EXTRA_PHYSICAL_FAILURES | {"ascent_reserve_exhausted"} else termination)
        terminal_events = events.get(terminal_name, [])
        _require(len(terminal_events) == 1 and saved._near(terminal_events[0]["time_s"], last["time_s"]), "Missing actual final physical event")
        contact = termination in {"surface_impact", "low_speed_surface_contact"}
        _require(last.get("contact", False) is contact, "Contact flag differs")
        if contact:
            receipt = outcome.get("contact_receipt")
            _require(type(receipt) is dict and receipt.get("event_time_s") == last["time_s"], "Contact lacks timed physical receipt")
        if termination == "insertion_gate_reached":
            _require(reached and last["time_s"] == 1537. and last["phase"] == "ascent_screen_shutdown", "Accepted gate lacks actual shutdown continuation")
        if termination == "insertion_gate_unreached":
            _require(not reached and last["time_s"] == 1536., "Unreached gate did not stop at finite deadline")
        if termination == "ascent_reserve_exhausted":
            _require(last["propellant_kg"] <= profile["ship"]["ascent_reserve_kg"], "Reserve stop lacks actual crossing")
        result.update(passed=True, termination=termination, physical_failure=termination in PHYSICAL_FAILURES,
            measured_final_insertion_gate_reached=reached, deferred_booster_honestly_recorded=True,
            first_cutoff_time_s=first_cutoff["time_s"] if first_cutoff else None,
            shutdown_continuation_completed=termination == "insertion_gate_reached", verified_command_samples=command_count,
            legacy_v3_qualification_borrowed=False, physical_reexecution=False)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_ascent_phase_record", "detail": str(exc)})
    return result


def verify_phase_objective_record(record, profile, *, expected_objectives, expected_shutdowns, expected_offsets,
                                  expected_policy=POLICY):
    """Worker-facing exact preregistration binding; delegates to one checker."""
    expected = {"finite_mode": True, "policy": expected_policy, "objectives": expected_objectives,
        "shutdowns": expected_shutdowns, **expected_offsets, "duration_s": 1537.}
    return verify_ascent_record(record, expected_settings=expected, profile=profile)
