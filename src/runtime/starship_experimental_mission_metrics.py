"""Saved-only checks for the explicit healthy v5 experimental whole mission.

Record consistency, simulated recovery and hosted-model work are different
facts. This checker never advances a plant, runs a forecast, calls a provider,
or makes an old empirical return certificate apply to the new ascent.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import math
import re

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from . import starship_ascent_phase_metrics as phase_checks
from .starship_ascent import AscentCoastOrbitObjectives, AscentScreenSettings, ExperimentalMissionAscentSettings
from .starship_experimental_continuation import development_settings
from .starship_retained_return import TRIMMED_POLICY_ID


STATE_FIELDS = phase_checks.STATE_FIELDS
ASCENT_PHASES = {"stack_ascent", "ship_ascent", "suborbital_coast", "orbit_insertion"}
FORBIDDEN = ("ascent_screen", "experimental_ascent_continuation", "return_qualification_sha256",
    "mission_director", "booster_mission_director", "visual_observation", "trouble", "trouble_control",
    "supervision", "deployment_permission", "booster_recovery", "booster_catch_run")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _require(condition, detail):
    saved._require(condition, "$", detail)


def _same(left, right):
    return type(left) is dict and type(right) is dict and all(left.get(k) == right.get(k) for k in STATE_FIELDS)


def _json(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _settings(expected, profile):
    wanted = ExperimentalMissionAscentSettings(AscentScreenSettings(policy="coast_orbit_v5",
        objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.)))
    wanted.validate_profile(profile)
    _require(type(expected) is dict and digest(expected) == digest(wanted.record()),
        "Explicit settings must identify the frozen healthy R2 engineering law")
    return wanted


def _ascent(record, profile, settings, metadata, events):
    """Check real whole-mission evidence directly, without a diagnostic adapter."""
    from .starship_sixdof_mission import vehicle
    stages = events.get("stage_separation", [])
    _require(len(stages) <= 1 and metadata.get("separation_time_s") == (stages[0]["time_s"] if stages else None),
        "Actual separation event and mission metadata differ")
    if stages:
        for key, bound in (("mass_residual_kg", 1e-5), ("linear_momentum_residual_kg_mps", .1),
                           ("angular_momentum_residual_kg_m2_s", 1.)):
            _require(saved._number(stages[0].get(key)) and abs(stages[0][key]) < bound,
                "Separation conservation receipt exceeds the unchanged bound")
    separation_time = stages[0]["time_s"] if stages else None
    witnesses = metadata.get("transition_witnesses")
    _require(type(witnesses) is list, "Missing same-state ascent transitions")
    allowed = {("ship_ascent", "suborbital_coast"), ("suborbital_coast", "orbit_insertion"),
               ("orbit_insertion", "orbital_coast"), ("suborbital_coast", "orbital_coast")}
    for witness in witnesses:
        _require((witness.get("from_phase"), witness.get("to_phase")) in allowed
            and witness.get("state_reset") is False and witness.get("state_before") == witness.get("state_after")
            and witness.get("state_after_origin") in ("next_physics_tick_before_command", "final_retained_state")
            and witness.get("time_s") == witness["state_before"]["time_s"],
            "An ascent transition changed full state or finite actuator history")
        dyn.state_from_dict(witness["state_before"])
        _require(any(sample["time_s"] == witness["time_s"] and _same(sample, witness["state_before"])
            for sample in record["samples"]), "Ascent transition is absent from actual saved samples")
    first, cutoff = metadata.get("first_cutoff_measurement"), metadata.get("cutoff_measurement")
    first_events = events.get("ascent_screen_first_cutoff_command", [])
    final_events = events.get("ascent_screen_final_cutoff_command", [])
    restarts = events.get("ascent_screen_restart_command", [])
    gates = events.get("orbit_cutoff_command", [])
    _require(len(first_events) <= 1 and bool(first_events) == (first is not None)
        and len(final_events) <= 1 and bool(final_events) == (cutoff is not None)
        and len(restarts) <= 1 and len(gates) <= 1, "Repeated or missing measured ascent command")
    o = settings.ascent.objectives
    if first is not None:
        measured = phase_checks._orbit(first["state"])
        _require(first["time_s"] == first_events[0]["time_s"] == first["state"]["time_s"]
            and first["state"] == first_events[0].get("state") and first.get("orbit") == measured
            and first.get("reason") == "measured_first_burn_coast_orbit_gates"
            and first.get("objective") == {"first_burn_apogee_m": o.first_burn_apogee_m,
                "first_burn_perigee_m": o.first_burn_perigee_m}
            and measured["status"] == "bound" and measured["perigee_altitude_m"] >= o.first_burn_perigee_m
            and measured["apogee_altitude_m"] >= o.first_burn_apogee_m
            and first["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"],
            "First cutoff does not meet both registered measured orbit objectives")
        _require(any(w["from_phase"] == "ship_ascent" and w["to_phase"] == "suborbital_coast"
            and w["state_before"] == first["state"] for w in witnesses), "First cutoff lacks continuous state witness")
    for event in restarts:
        _require(first is not None and first["time_s"] <= 1517. and event["time_s"] == 1517.
            and not phase_checks._reached(event["state"], o.final_perigee_m)
            and any(w["from_phase"] == "suborbital_coast" and w["to_phase"] == "orbit_insertion"
                and w["time_s"] == 1517. and w["state_before"] == event["state"] for w in witnesses),
            "Restart lacks its exact scheduled time and actual state")
    reached = False
    if cutoff is not None:
        measured = phase_checks._orbit(cutoff["state"])
        reached = measured["status"] == "bound" and measured["perigee_altitude_m"] >= o.final_perigee_m
        _require(cutoff["time_s"] == final_events[0]["time_s"] == cutoff["state"]["time_s"]
            and cutoff["state"] == final_events[0].get("state")
            and cutoff.get("orbit") == final_events[0].get("orbit") == measured
            and cutoff.get("gate_reached") is reached and cutoff["time_s"] <= 1536.
            and final_events[0].get("termination") == ("insertion_gate_reached" if reached else "insertion_gate_unreached"),
            "Final gate does not follow the measured unchanged perigee threshold")
    _require(record["outcome"].get("orbit_gate_reached") is reached and bool(gates) is reached,
        "Whole-mission orbital claim differs from measured final gate")
    if reached:
        _require(first is not None and separation_time is not None, "Final insertion lacks earlier actual first cutoff and separation")
        gate = gates[0]
        schedule = {"final_gate_time_s": cutoff["time_s"],
            "first_release_time_s": cutoff["time_s"]+profile["guidance"]["orbit_release_delay_s"],
            "return_request_time_s": cutoff["time_s"]+profile["guidance"]["coast_before_return_s"]}
        _require(gate["time_s"] == cutoff["time_s"] and gate.get("state") == cutoff["state"]
            and gate.get("orbit") == cutoff["orbit"] and gate.get("state_reset") is False
            and all(gate.get(k) == v for k, v in schedule.items()) and metadata.get("schedule") == schedule
            and metadata.get("insertion_vehicle") == _json(asdict(vehicle(profile, payload_count=26))),
            "Actual gate state, retained vehicle or insertion-relative schedule differs")
        _require(any(w["to_phase"] == "orbital_coast" and w["state_before"] == cutoff["state"] for w in witnesses),
            "Final gate lacks a same-state transition into the mission")
    else:
        _require(metadata.get("schedule") is None and metadata.get("insertion_vehicle") is None,
            "Unreached insertion cannot manufacture a mission schedule")
    if first is not None and record["final_state"]["time_s"] > 1517. and (cutoff is None or cutoff["time_s"] > 1517.):
        _require(len(restarts) == 1, "Missing due insertion restart")
    from .starship_sixdof_mission import stack_vehicle
    ship = vehicle(profile)
    stack = stack_vehicle(profile, ship, vehicle(profile, "booster"))
    commands = 0
    for sample in record["samples"]:
        if sample["phase"] in ASCENT_PHASES:
            craft = stack if sample["phase"] == "stack_ascent" else ship
            stop = any(event.get("state") is not None and _same(sample, event["state"])
                for event in events.get("ascent_screen_physical_failure", []))
            commands += phase_checks._command_goal(sample, craft, profile, settings.ascent,
                separation_time, stop_request=stop)
    _require(commands > 0, "Missing actual ascent command witnesses")
    return reached, commands


def _vehicle_for_sample(record, profile, sample, releases):
    from .starship_sixdof_mission import stack_vehicle, vehicle
    if sample["phase"] == "stack_ascent":
        return stack_vehicle(profile, vehicle(profile), vehicle(profile, "booster"))
    already = sum(event["time_s"] < sample["time_s"] or event["time_s"] == sample["time_s"]
        and _same(sample, event["receipt"]["parent_after_state"]) for event in releases)
    return vehicle(profile, payload_count=26-already)


def _clock(record, profile, expected_duration):
    clock = record["concurrent_execution"]
    bound = max(profile["integration"]["powered_dt_s"], profile["integration"]["coast_dt_s"])
    _require(clock.get("schema") == "missionos.ship_booster_clock.v1"
        and clock.get("absolute_deadline_s") == expected_duration and clock.get("maximum_tick_s") == bound
        and clock.get("posthoc_ordering") is False and clock.get("physical_execution") is False
        and clock.get("observers_run_before_each_body_step") is True
        and clock.get("satellite_propagation") == "existing_postprocessed_orbit_only",
        "Concurrent mission clock differs from registered physical boundary")
    rows, bodies = clock.get("tick_order"), {}
    _require(type(rows) is list and bool(rows), "Missing executed physical tick order")
    for index, row in enumerate(rows):
        _require(type(row.get("sequence")) is int and row["sequence"] == index
            and row.get("body_id") in ("ship", "booster")
            and saved._number(row.get("time_s")) and saved._number(row.get("next_time_s"))
            and 0 <= row["time_s"] <= row["next_time_s"] <= row["time_s"]+bound+1e-8
            and (index == 0 or row["time_s"] >= rows[index-1]["time_s"]-1e-9),
            "Invalid or posthoc-reordered actual physical tick")
        bodies.setdefault(row["body_id"], []).append(row)
    separated = record.get("booster_separation_state") is not None
    _require(set(bodies) == ({"ship", "booster"} if separated else {"ship"})
        and bodies["ship"][0]["time_s"] == 0. and clock.get("separation_state_preserved") is separated,
        "Concurrent bodies do not match actual separation")
    for body, own in bodies.items():
        final = record["final_state"] if body == "ship" else record["booster_run"]["final_state"]
        _require(own[-1]["next_time_s"] == final["time_s"] <= expected_duration
            and all(a["next_time_s"] == b["time_s"] for a, b in zip(own, own[1:])),
            "Actual body history or final physical clock is discontinuous")
    pending = {body: own[0]["time_s"] for body, own in bodies.items()}
    remaining = {body: len(own) for body, own in bodies.items()}
    lead = 0.
    for row in rows:
        body = row["body_id"]
        _require(row["time_s"] == min(pending.values()), "A body advanced before the pending common clock")
        remaining[body] -= 1
        if remaining[body]:
            pending[body] = row["next_time_s"]
        else:
            del pending[body]
        if len(pending) == 2 and row["time_s"] >= bodies["booster"][0]["time_s"]:
            lead = max(lead, max(pending.values())-min(pending.values()))
    _require(lead <= bound+1e-8 and saved._near(lead, clock.get("maximum_observed_body_lead_s"), 1e-8),
        "Recorded body-lead bound differs from actual execution order")
    if separated:
        _require(bodies["booster"][0]["time_s"] == record["booster_separation_state"]["time_s"],
            "Concurrent booster did not start at actual separation")


def verify_experimental_physical_mission(record, profile, *, expected_settings, expected_source_sha256,
        expected_site, expected_goal, expected_return_sites, expected_duration_s):
    """Verify saved evidence; a valid impact remains a physical negative."""
    result = {"schema": "missionos.experimental_physical_mission_verification.v1", "passed": False,
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
        saved._json_tree(record)
        _require(type(expected_return_sites) is dict and type(expected_return_sites.get("sites")) is list
            and expected_site in expected_return_sites["sites"], "Actual booster site is not in the explicitly bound catalog")
        _require(record.get("scenario") == "experimental_physical_mission"
            and not any(key in record for key in FORBIDDEN)
            and record.get("development_return_qualification") == development_settings(),
            "Experimental whole mission borrowed a diagnostic, qualification or actor scope")
        _require(record.get("experimental_mission_execution") == {
            "schema": "missionos.experimental_physical_mission_execution.v1",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "settings_sha256": digest(expected_settings)}, "Runner source/profile/settings binding differs")
        metadata = record["experimental_mission_ascent"]
        _require(metadata.get("schema") == "missionos.experimental_mission_ascent.v1"
            and digest(metadata.get("settings")) == digest(expected_settings)
            and type(metadata.get("requested_absolute_duration_s")) in (int, float)
            and metadata["requested_absolute_duration_s"] == expected_duration_s
            and metadata.get("shutdown_schedule") == metadata.get("shutdown_receipts") == []
            and metadata.get("final_mission_perigee_gate_m") == profile["guidance"]["target_perigee_m"]
            and metadata.get("booster_policy") == "fixed_v1"
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
        first, last, outcome, events, speed = saved._run_basics(record, "$", [0], profile)
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
                and all(engine.available is True for engine in state.engine_states), "Unregistered engine availability or actuator inventory")
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
            _verify_booster(booster, separation, profile, expected_site, expected_goal, expected_duration_s, budget)
            from .starship_splashdown_verifier import verify_splashdown
            water = verify_splashdown(booster, expected_goal)
            _require(water["passed"], "Independent saved water-contact geometry rejected")
            booster_success = water["controlled_water_entry_envelope_met"]
            result["booster"] = water
        _clock(record, profile, expected_duration_s)
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
            termination=outcome["termination"], contact_point_speed_mps=speed, final_tilt_deg=tilt,
            final_propellant_kg=last["propellant_kg"], return_requested=bool(returns), physical_reexecution=False)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_experimental_whole_mission", "detail": str(exc)})
    return result


def _verify_booster(booster, separation, profile, site, goal, horizon, budget, *, availability_check=None,
                    expected_guidance_policy="fixed_v1", checkpoint_required=True):
    from .starship_sixdof_mission import vehicle
    from .starship_sixdof_booster import _configuration
    from .starship_return_sites import ReturnSite, validate_return_site
    from .starship_splashdown import SplashdownGoal
    actual_site = ReturnSite.from_dict(site)
    validate_return_site(actual_site, profile, terminal_goal="model_surface_contact")
    SplashdownGoal.from_dict(goal).validate_site(actual_site)
    first, last, outcome, _, _ = saved._run_basics(booster, "$.booster_run", budget, profile, "booster")
    _require(booster.get("scenario") == "booster_return" and booster.get("body_id") == "booster"
        and booster.get("guidance_policy") == expected_guidance_policy and booster.get("booster_separation_state") == separation
        and _same(first, separation) and _same(booster.get("initial_state"), separation)
        and _same(last, booster.get("final_state")) and outcome["termination"] != "numerical_failure",
        "Booster did not inherit all actual separation and finite actuator state")
    _require(booster.get("active_return_site") == site and booster.get("return_site_sha256") == actual_site.sha256
        and booster.get("capture_inhibited") is True and booster.get("guidance_configuration") == _configuration(profile),
        "Booster target or frozen guidance configuration changed")
    repair_scopes = {
        "availability_water_return_v1": ("booster_outage_control", "booster_outage_execution"),
        "availability_energy_water_return_v2": ("booster_energy_control", "booster_energy_execution"),
        "availability_terminal_recovery_v1": ("booster_terminal_recovery", "booster_terminal_recovery_execution"),
    }
    extras = tuple(key for policy, keys in repair_scopes.items() if policy != expected_guidance_policy for key in keys)
    for key in (*FORBIDDEN, *extras, "water_return", "booster_fidelity", "recovery_record", "catch_record",
                "development_coast", "development_coast_from_separation"):
        _require(key not in booster, "Booster borrowed an unregistered actor, planner or catch scope")
    craft = vehicle(profile, "booster")
    _require(booster.get("final_vehicle") == _json(asdict(craft)), "Booster capability/profile changed")
    previous = separation["propellant_kg"]
    for sample in booster["samples"]:
        state = dyn.state_from_dict({k: sample[k] for k in STATE_FIELDS})
        command = (dyn.command_from_dict(sample["command"]) if sample.get("command") is not None else
            dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad))
        dyn._validate_pair(state, craft, command)
        _require(len(state.engine_states) == len(craft.engines) and len(state.flap_angles_rad) == len(craft.aero_panels)
            and (all(e.available is True for e in state.engine_states) if availability_check is None
                 else availability_check(sample, "booster"))
            and state.propellant_kg <= previous+1e-6, "Booster availability, inventory or fuel continuity changed")
        previous = state.propellant_kg
        observation = dyn.observe(state, craft)
        _require(saved._near(sample["mass_kg"], observation["mass_kg"], 1e-5)
            and saved._near(sample["altitude_m"], observation["altitude_m"], 1e-5), "Booster saved mass or altitude differs")
    if checkpoint_required:
        _require(booster.get("shared_clock_checkpoint") == {"absolute_deadline_s": horizon,
            "unfinished": outcome["termination"] == "time_limit", "state_reset": False}, "Booster shared-clock boundary differs")
        if outcome["termination"] == "time_limit":
            _require(last["time_s"] == min(horizon, separation["time_s"]+_configuration(profile)["max_duration_s"]),
                "Booster time limit ended before its registered horizon")
    else:
        _require("shared_clock_checkpoint" not in booster, "Separated suffix cannot claim a shared launch clock")
        if outcome["termination"] == "time_limit":
            _require(last["time_s"] == horizon, "Suffix ended before its explicit elapsed horizon")
    for key in ("catch_verified", "landing_hardware_validated", "physical_execution"):
        _require(outcome.get(key) is False, "Booster cannot claim catch, hardware validity or physical execution")
    saved._booster_envelope(booster, profile, last, "$.booster_run")
    peak = booster["runner_booster_peak_witness"]
    _require(peak.get("origin") == "all_yielded_physics_ticks_and_final_state; no extra plant step"
        and peak.get("not_independent_physics_reexecution") is True
        and first["time_s"] <= peak["state"]["time_s"] <= last["time_s"]
        and saved._near(outcome["max_altitude_m"], env.ecef_to_geodetic(peak["state"]["r_eci_m"])[2], 1e-5),
        "Booster peak lacks its saved actual-tick witness")
    dyn.state_from_dict(peak["state"])
