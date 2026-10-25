"""Independent saved-water record checks; never replays a physical forecast.

Consistency and the terminal water envelope are separate results. A valid
impact is retained as a negative result. Coarse planning receipts are checked
against saved actual capability and arithmetic, not treated as qualification.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import math

STATE_FIELDS = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
                "propellant_kg", "engine_states", "flap_angles_rad")
COMMANDS = {"boostback": "booster_boostback_ignition_command",
            "boostback_cutoff": "booster_boostback_cutoff_command", "landing": "booster_landing_burn_requested"}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _near(a, b, *, tolerance=1e-6):
    return (type(a) in (int, float) and type(b) in (int, float)
            and math.isfinite(a) and math.isfinite(b) and abs(a-b) <= tolerance*(1+abs(b)))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _state(row, craft, flags, dynamics):
    value = dynamics.state_from_dict({k: row[k] for k in STATE_FIELDS})
    _require([e.available for e in value.engine_states] == flags, "water_inherited_availability_changed")
    dynamics._validate_pair(value, craft,
        dynamics.Command6DOF(tuple(dynamics.EngineCommand() for _ in craft.engines), value.flap_angles_rad))
    return value


def _same_state(left, right):
    return all(left.get(k) == right.get(k) for k in STATE_FIELDS)


def _plans(metadata, record, profile, craft, flags, site, goal, dynamics, geometry, configuration):
    from .starship_sixdof_booster import _navigation, _coast_control_profile
    plans = metadata["plans"]
    _require(type(plans) is list and 1 <= len(plans) <= 2 and metadata["plan_count"] == len(plans), "water_plan_budget_changed")
    totals = {"point_coast_forecast_count": 0, "coarse_burn_estimate_count": 0, "coarse_burn_steps": 0}
    for ordinal, receipt in enumerate(plans, 1):
        _require(receipt["schema"] == "missionos.starship_water_return_plan.v1"
                 and receipt["ordinal"] == ordinal and receipt["nested_sixdof_forecasts"] == 0
                 and receipt["mission_authority_granted"] is False and receipt["prediction_is_execution"] is False,
                 "water_plan_claim_or_ordinal_changed")
        state = _state(receipt["origin_state"], craft, flags, dynamics)
        _require(receipt["time_s"] == state.time_s
                 and record["initial_state"]["time_s"] <= state.time_s <= record["final_state"]["time_s"], "water_plan_time_outside_actual_flight")
        observed = dynamics.observe(state, craft)
        up, east, north, velocity, displacement, _ = _navigation(state, profile, return_site=site)
        position = [-geometry.dot(displacement, east), -geometry.dot(displacement, north), observed["altitude_m"]]
        local_velocity = [geometry.dot(velocity, axis) for axis in (east, north, up)]
        _require(_near(receipt["origin_mass_kg"], observed["mass_kg"])
                 and all(_near(x, y) for x, y in zip(receipt["origin_position_enu_m"], position))
                 and all(_near(x, y) for x, y in zip(receipt["origin_velocity_enu_mps"], local_velocity))
                 and len(receipt["origin_position_enu_m"]) == len(receipt["origin_velocity_enu_mps"]) == 3,
                 "water_plan_origin_geometry_or_mass_changed")
        _require(receipt["return_site_sha256"] == site.sha256
                 and receipt["minimum_terminal_propellant_kg"] == goal.minimum_propellant_kg
                 and receipt["boostback_fuel_floor_kg"] == configuration["landing_reserve_kg"]
                 and receipt["maximum_boostback_burn_s"] == configuration["boostback_max_burn_s"], "water_plan_goal_or_boostback_guard_changed")
        _, _, alpha = _coast_control_profile(profile, observed)
        _require(_near(receipt["coarse_axis_slew_rate_bound_rad_s"], math.sqrt(2*alpha*math.pi)), "water_coarse_slew_authority_changed")
        capacity = []
        for i, (engine, actual) in enumerate(zip(craft.engines, state.engine_states)):
            if engine.name.startswith("rcs_") or not actual.available:
                continue
            capacity.append({"index": i, "name": engine.name, "maximum_force_n": engine.max_thrust_n,
                "maximum_flow_kg_s": engine.max_thrust_n/(engine.isp_s*geometry.STANDARD_GRAVITY_MPS2),
                "throttle": float(actual.throttle), "tau_s": engine.throttle_time_constant_s,
                "rate_per_s": engine.throttle_rate_per_s})
        _require(receipt["engine_capacity"] == capacity, "water_plan_actual_engine_capacity_changed")
        force = sum(c["maximum_force_n"] for c in capacity)
        flow = sum(c["maximum_flow_kg_s"] for c in capacity)
        _require(_near(receipt["available_main_thrust_n"], force)
                 and _near(receipt["available_main_mass_flow_kg_s"], flow), "water_plan_available_force_or_flow_changed")
        burns, forecasts, plan = receipt["burn_estimates"], receipt["point_coast_forecasts"], receipt["plan"]
        _require(len(burns) == len(forecasts) == receipt["coarse_burn_estimate_count"]
                 == receipt["point_coast_forecast_count"] == 15, "water_point_forecast_budget_changed")
        _require(plan["prediction_is_execution"] is False and len(plan["candidates"]) == 5
                 and plan["selection_rule"] == "contact_budget_first_else_best_effort_surplus", "water_candidate_scope_changed")
        gravity = geometry.EARTH_MU_M3_S2/geometry.norm(state.r_eci_m)**2
        mass, fuel = observed["mass_kg"], state.propellant_kg
        dry, steps = mass-fuel, 0
        exhaust = force/max(flow, 1e-30) if force > 0 else profile["booster"]["engine_isp_s"]*geometry.STANDARD_GRAVITY_MPS2
        for candidate_index, target_vertical in enumerate((200., 400., 600., 800., 1000.)):
            fall_time = (target_vertical+math.sqrt(target_vertical**2+19.6*position[2]))/9.8
            horizontal = [-position[i]/max(30., fall_time) for i in (0, 1)]
            for correction in range(3):
                index = candidate_index*3+correction
                burn, prediction = burns[index], forecasts[index]
                elapsed, remaining = burn["elapsed_s"], burn["remaining_fuel_kg"]
                _require(type(burn["steps"]) is int and 0 <= burn["steps"] <= 1000
                         and 0 <= elapsed <= configuration["boostback_max_burn_s"]+1e-8
                         and elapsed <= .1*burn["steps"]+1e-7, "water_coarse_burn_budget_changed")
                _require(burn["minimum_remaining_fuel_kg"] == configuration["landing_reserve_kg"]
                         and burn["maximum_elapsed_s"] == configuration["boostback_max_burn_s"]
                         and min(fuel, configuration["landing_reserve_kg"])-1e-6 <= remaining <= fuel+1e-6
                         and _near(burn["remaining_mass_kg"], dry+remaining)
                         and _near(burn["propellant_used_kg"], fuel-remaining)
                         and fuel-remaining <= flow*elapsed+1e-5, "water_coarse_burn_mass_or_guard_changed")
                _require(burn["prediction_is_execution"] is False and burn["thrust_direction_and_tvc_modelled"] is False,
                         "water_coarse_burn_claim_changed")
                required = math.sqrt(sum((horizontal[i]-local_velocity[i])**2 for i in (0, 1))
                    +(target_vertical-local_velocity[2]+gravity*elapsed)**2)
                _require(_near(burn["requested_impulse_velocity_mps"], required), "water_coarse_burn_input_changed")
                _require(0 <= burn["available_impulse_velocity_mps"] <= force/max(dry, 1.)*elapsed+1e-5,
                         "water_coarse_burn_exceeds_actual_capacity")
                reached = burn["requested_impulse_reached"]
                _require(type(reached) is bool, "water_coarse_burn_feasibility_not_boolean")
                if reached:
                    _require(burn["termination"] == "requested_impulse_reached"
                             and remaining > configuration["landing_reserve_kg"]+1e-6
                             and elapsed < configuration["boostback_max_burn_s"]-1e-12
                             and burn["available_impulse_velocity_mps"] >= required,
                             "water_coarse_burn_claims_impulse_beyond_guard")
                else:
                    _require(burn["termination"] in ("reserve_limited", "time_limited", "no_available_thrust", "step_limit"), "water_unknown_burn_termination")
                    if burn["termination"] == "reserve_limited":
                        _require(remaining <= configuration["landing_reserve_kg"]+1e-6, "water_false_reserve_stop")
                    elif burn["termination"] == "time_limited":
                        _require(elapsed >= configuration["boostback_max_burn_s"]-1e-12, "water_false_time_stop")
                    elif burn["termination"] == "no_available_thrust":
                        _require(force == 0, "water_false_capacity_stop")
                    else:
                        _require(burn["steps"] == 1000, "water_false_step_stop")
                _require(type(prediction["elapsed_s"]) in (int, float) and 0 <= prediction["elapsed_s"] <= 600.
                         and len(prediction["position_enu_m"]) == len(prediction["velocity_enu_mps"]) == 3,
                         "water_point_coast_horizon_or_vector_changed")
                if correction < 2:
                    for i in (0, 1):
                        horizontal[i] -= prediction["position_enu_m"][i]/max(30., prediction["elapsed_s"])
                steps += burn["steps"]
            candidate = plan["candidates"][candidate_index]
            after_stop = burn["remaining_mass_kg"]*math.exp(-(geometry.norm(prediction["velocity_enu_mps"])+60.)/exhaust)-dry
            _require(all(_near(x, y) for x, y in zip(candidate["target_velocity_enu_mps"], [*horizontal, target_vertical]))
                     and len(candidate["target_velocity_enu_mps"]) == 3
                     and _near(candidate["predicted_post_burn_fuel_kg"], burn["remaining_mass_kg"]-dry)
                     and _near(candidate["predicted_point_model_after_arrest_fuel_kg"], after_stop)
                     and candidate["point_model_contact_propellant_reserve_kg"] == goal.minimum_propellant_kg
                     and candidate["point_model_contact_budget_met"] is bool(after_stop >= goal.minimum_propellant_kg and reached)
                     and candidate["prediction"] == prediction and candidate["coarse_burn_estimate"] == burn,
                     "water_candidate_receipt_arithmetic_changed")
        feasible = [c for c in plan["candidates"] if c["point_model_contact_budget_met"]]
        selected = max(feasible or plan["candidates"], key=lambda c: c["predicted_point_model_after_arrest_fuel_kg"]-c["point_model_contact_propellant_reserve_kg"])
        _require(plan["selected"] == selected and plan["selected_contact_budget_met"] is selected["point_model_contact_budget_met"]
                 and plan["contact_budget_candidate_count"] == len(feasible)
                 and receipt["status"] == ("coarse_candidate_only" if feasible else "no_feasible_return_plan"), "water_plan_selection_or_status_changed")
        _require(receipt["coarse_burn_steps"] == steps, "water_burn_step_total_changed")
        for key in totals:
            totals[key] += receipt[key]
    _require(all(metadata[key] == value for key, value in totals.items()), "water_total_planning_budget_changed")


def _coupled_scope(record, profile, craft, flags, site, goal, dynamics, geometry, configuration, events):
    from .starship_booster_guidance import WATER_POLICY_ID, WaterReturnSettings
    metadata = record["water_return"]
    _require(metadata["schema"] == "missionos.starship_coupled_water_return.v1"
             and metadata["policy_id"] == WATER_POLICY_ID and metadata["settings"] == WaterReturnSettings().record()
             and metadata["minimum_terminal_propellant_kg"] == goal.minimum_propellant_kg
             and metadata["return_site_sha256"] == site.sha256, "water_settings_or_goal_changed")
    for key in ("capture_allowed", "mission_authority_granted", "physics_limits_changed", "actual_state_assigned", "qualification_refreshed"):
        _require(metadata[key] is False, "water_invalid_planning_authority_claim")
    _require(metadata["nested_sixdof_forecasts"] == 0, "water_unregistered_nested_physics_forecast")
    _plans(metadata, record, profile, craft, flags, site, goal, dynamics, geometry, configuration)
    witnesses = metadata["command_witnesses"]
    _require(set(witnesses) == set(COMMANDS), "water_command_witness_set_changed")
    for name, event_name in COMMANDS.items():
        found, witness = events.get(event_name, []), witnesses[name]
        _require(len(found) <= 1 and bool(found) == (witness is not None), "water_command_event_witness_presence_changed")
        if witness is None:
            continue
        state = _state(witness["state"], craft, flags, dynamics)
        command = dynamics.command_from_dict(witness["command"])
        dynamics._validate_pair(state, craft, command)
        count, throttle = witness["requested_main_count"], witness["requested_throttle"]
        _require(witness["time_s"] == state.time_s == found[0]["time_s"]
                 and type(count) is int and 0 <= count <= (13 if name == "landing" else 33)
                 and type(throttle) in (int, float) and 0 <= throttle <= 1., "water_command_clock_or_group_changed")
        for index, target in enumerate(command.engines[:33]):
            _require(target.enabled is (index < count and throttle > 0.)
                     and target.throttle == (throttle if index < count else 0.), "water_command_group_or_throttle_changed")
    return metadata["plan_count"], metadata["point_coast_forecast_count"]


def _verify_record(record, profile, actual_ascent, *, ascent_record_bytes, expected_ascent_sha256,
                   expected_source_sha256, expected_site, expected_goal, fixed_mode):
    """Validate exact saved handoff/capability/geometry without any plant step."""
    result = {"schema": "missionos.starship_water_verification.v1", "passed": False, "issues": [],
        "water_entry_accepted": False, "nested_sixdof_forecasts": 0,
        "scope": "saved source/handoff/actual capability/receipt arithmetic/contact geometry; no independent physics model",
        "catch_verified": False, "qualification_refreshed": False, "physical_execution": False}
    try:
        from . import starship_sixdof as dynamics, starship_sixdof_verifier as saved, starship_physics as geometry
        from .starship_sixdof_mission import vehicle
        from .starship_sixdof_booster import _configuration
        from .starship_booster_guidance import WATER_POLICY_ID
        from .starship_return_sites import ReturnSite, validate_return_site
        from .starship_splashdown import SplashdownGoal
        from .starship_splashdown_verifier import verify_splashdown
        saved._json_tree(record)
        saved._json_tree(actual_ascent)
        _require(type(ascent_record_bytes) is bytes
                 and sha256(ascent_record_bytes).hexdigest() == expected_ascent_sha256
                 and json.loads(ascent_record_bytes) == actual_ascent, "water_ascent_source_bytes_changed")
        _require(record.get("water_execution") == {"schema": "missionos.starship_water_execution.v1",
            "ascent_record_sha256": expected_ascent_sha256, "source_sha256": expected_source_sha256,
            "profile_sha256": digest(profile)}, "water_execution_source_binding_changed")
        site, goal = ReturnSite.from_dict(expected_site), SplashdownGoal.from_dict(expected_goal)
        validate_return_site(site, profile, terminal_goal="model_surface_contact")
        goal.validate_site(site)
        _require(record.get("outcome", {}).get("termination") != "numerical_failure",
                 "water_numerical_failure_is_invalid_not_a_physical_negative")
        first, last, outcome, events, _ = saved._run_basics(record, "$.water", [0], profile, body_id="booster")
        origin = actual_ascent["booster_separation_state"]
        _require(record.get("scenario") == "booster_return" and record.get("body_id") == "booster"
                 and record.get("guidance_policy") == ("fixed_v1" if fixed_mode else WATER_POLICY_ID), "water_policy_or_body_changed")
        _require(record.get("booster_separation_state") == origin and _same_state(first, origin)
                 and _same_state(record["initial_state"], origin) and _same_state(last, record["final_state"]), "water_actual_handoff_or_final_state_changed")
        _require(record.get("active_return_site") == expected_site and record.get("return_site_sha256") == site.sha256
                 and record.get("capture_inhibited") is True, "water_surface_site_binding_changed")
        for key in ("booster_fidelity", "recovery_record", "catch_record", "booster_catch_run", "mission_director",
                    "trouble_control", "development_coast", "development_coast_from_separation", "return_qualification_sha256"):
            _require(record.get(key) is None, "water_borrowed_execution_or_catch_scope")
        configuration = _configuration(profile)
        _require(record["guidance_configuration"] == configuration, "water_frozen_configuration_changed")
        craft = vehicle(profile, "booster")
        _require(digest(record["final_vehicle"]) == digest(asdict(craft)), "water_vehicle_or_wind_changed")
        flags = [e["available"] for e in origin["engine_states"]]
        previous = None
        for sample in record["samples"]:
            state = _state(sample, craft, flags, dynamics)
            _require(_near(sample["mass_kg"], craft.dry_mass_kg+state.propellant_kg)
                     and _near(sample["altitude_m"], geometry.ecef_to_geodetic(state.r_eci_m)[2]), "water_saved_mass_or_altitude_changed")
            if previous is not None:
                _require(sample["propellant_kg"] <= previous["propellant_kg"]+1e-8
                         and (sample["time_s"] != previous["time_s"] or _same_state(sample, previous)), "water_fuel_increase_or_zero_time_reset")
            previous = sample
        _require(first["time_s"] <= last["time_s"] <= first["time_s"]+1200.+1e-7
                 and _near(outcome["start_time_s"], first["time_s"])
                 and _near(outcome["end_time_s"], last["time_s"]), "water_horizon_or_clock_changed")
        if outcome["termination"] == "time_limit":
            _require(_near(last["time_s"]-first["time_s"], 1200.), "water_early_nonphysical_horizon_stop")
        for key in ("physical_execution", "catch_verified", "landing_hardware_validated"):
            _require(outcome.get(key) is False, "water_invalid_execution_or_catch_claim")
        profile_envelope = saved._booster_envelope(record, profile, last, "$.water")
        _require(record.get("divert_destination_reached") is profile_envelope, "water_profile_envelope_echo_changed")
        peak = record["runner_booster_peak_witness"]["state"]
        _state(peak, craft, flags, dynamics)
        _require(first["time_s"] <= peak["time_s"] <= last["time_s"]
                 and _near(outcome["max_altitude_m"], geometry.ecef_to_geodetic(tuple(peak["r_eci_m"]))[2])
                 and outcome["max_altitude_m"] >= max(s["altitude_m"] for s in record["samples"])-1e-5,
                 "water_actual_peak_witness_changed")
        if fixed_mode:
            _require(record.get("water_return") is None, "fixed_water_cannot_borrow_coupled_planning_scope")
            for event_name in COMMANDS.values():
                _require(len(events.get(event_name, [])) <= 1, "fixed_water_repeated_phase_request")
            plan_count, forecast_count = 0, 0
        else:
            plan_count, forecast_count = _coupled_scope(record, profile, craft, flags, site, goal,
                dynamics, geometry, configuration, events)
        _require(outcome["boostback_started"] is bool(events.get(COMMANDS["boostback"]))
                 and outcome["coast_started"] is bool(events.get(COMMANDS["boostback_cutoff"])), "water_actual_phase_flags_changed")
        water = verify_splashdown(record, expected_goal)
        _require(water["passed"], "water_independent_contact_check_failed:"+str(water["issues"]))
        result.update(passed=True, water_entry_accepted=water["controlled_water_entry_envelope_met"],
            profile_contact_envelope_met=profile_envelope, termination=outcome["termination"],
            exact_separation_handoff=True, inherited_availability_preserved=True,
            plan_count=plan_count, point_coast_forecast_count=forecast_count,
            verification_policy="fixed_water_v1" if fixed_mode else "coupled_water_v1")
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, ZeroDivisionError) as exc:
        result["issues"].append({"code": "invalid_water_record", "detail": str(exc)})
    except Exception as exc:
        # Common verifier's private _Invalid carries a structured issue; keep
        # that exact diagnostic while avoiding an integration retry.
        if hasattr(exc, "issue"):
            result["issues"].append(exc.issue)
        else:
            raise
    return result



def verify_record(record, profile, actual_ascent, *, ascent_record_bytes, expected_ascent_sha256,
                  expected_source_sha256, expected_site, expected_goal):
    """Coupled policy only, including its strict per-plan capability receipts."""
    return _verify_record(record, profile, actual_ascent, ascent_record_bytes=ascent_record_bytes,
        expected_ascent_sha256=expected_ascent_sha256, expected_source_sha256=expected_source_sha256,
        expected_site=expected_site, expected_goal=expected_goal, fixed_mode=False)


def verify_fixed_water_record(record, profile, actual_ascent, *, ascent_record_bytes, expected_ascent_sha256,
                              expected_source_sha256, expected_site, expected_goal):
    """Explicit fixed_v1 water regression; no coupled/catch planning is allowed.

    Source, actual inherited state, frozen vehicle/capability, sample/contact
    geometry and strict water limits are identical to the common checks. The
    fixed controller performs no point/coarse forecast and grants no authority.
    """
    return _verify_record(record, profile, actual_ascent, ascent_record_bytes=ascent_record_bytes,
        expected_ascent_sha256=expected_ascent_sha256, expected_source_sha256=expected_source_sha256,
        expected_site=expected_site, expected_goal=expected_goal, fixed_mode=True)
