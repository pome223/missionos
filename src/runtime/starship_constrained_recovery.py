"""Development return candidate executed through the existing finite 6DOF plant.

Point-model shooting and ZEM/ZEV requests never assign a physical trajectory.
This separate experiment has no production scenario or MissionOS authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import math
import time

from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_attitude_reference import ConditionedGeographicFrame
from .starship_booster_control import control_coast_stopping_distance, control_with_measured_tvc, reallocate_measured_tvc
from .starship_booster_recovery import _tower_observation, _vector_burn_preview, _slew_axis, _trim_entry_candidate
from .starship_booster_catch import configuration as catch_configuration
from .starship_sixdof_booster import _configuration, _navigation, _sample_booster, _landing_engine_demand, _coast_control_profile
from .starship_sixdof_contact import find_contact, hull_clearance
from .starship_sixdof_mission import _attitude, vehicle

POLICY_ID = "constrained_return_development_v8"
TRANSPORT_POLICY_ID = "constrained_return_development_v10"
CONFIG = {
    "maximum_duration_s": 1200., "alignment_deg": 15., "velocity_tolerance_mps": 12.,
    "rate_settle_limit_rad_s": .003, "rate_settle_max_s": 30.,
    "preview_interval_s": .5, "landing_height_margin_m": 500.,
    "maximum_landing_engines": 13, "finite_fin_regularization": .05,
    "recording": "every_macrostep", "production_policy_admitted": False,
    "boostback_reference": "finite_shooting_fixed_axis_impulse",
    "entry_bank_reference": "held_site_bearing",
    "landing_allocation": "opposing_gimballed_pair_when_feasible",
    "maximum_boostback_forecast_calls": 48,
    "coast_attitude_preparation": "powered_upright",
    "coast_preparation_alignment_deg": 5., "minimum_boostback_macrostep_s": .001,
    "fixed_entry_bank_angle_deg": 35., "entry_preparation_altitude_m": 80000.,
    "trajectory_objective": "post_prepare_point_coast_intercept", "terminal_corridor_hold": True,
    "finite_fin_policy": "finite_moment_priority_fins_v1",
    "entry_pretrim": {
        "policy_id": "finite_entry_pretrim_v1", "maximum_dynamic_pressure_pa": 100.,
        "actual_fin_tolerance_deg": .1, "probe_fractions": [.2, .4, .6, .8, 1.],
        "maximum_transition_probes": 5, "high_q_unprepared_policy": "finite_regularized_fins_v1",
        "latch_rule": "latest_accepted_measured_governed_current_flow_trim_at_low_q",
    },
    "reference_tracking": {
        "policy_id": "bounded_reference_tracking_v1", "scope": "entry_and_landing",
        "feedback_scope": "entry_q_above_100_and_all_landing",
        "initial_anchor": "previous_commanded_target_zero_reference_rate",
        "historyless_fixture_anchor": "same_time_actual_pose_zero_reference_rate",
        "rate_limit_source": "guidance.max_angular_acceleration_rad_s2/attitude_frequency_rad_s",
        "acceleration_limit_source": "guidance.max_angular_acceleration_rad_s2",
        "law": "causal_raw_goal_rate_plus_stopping_distance_closing_with_radial_rate_and_acceleration_limits",
    },
}


def physical_guidance_configuration(development_transport_prepare_roll=False):
    """Version only the opted-in request law; retain default configuration bits."""
    if type(development_transport_prepare_roll) is not bool:
        raise ValueError("invalid_transport_prepare_roll_mode")
    result = deepcopy(CONFIG)
    if development_transport_prepare_roll:
        result.update(transport_prepare_roll=True,
            powered_prepare_reference={"policy_id": "parallel_transport_deferred_geographic_roll_v1",
                "scope": "powered_entry_prepare_and_command_off_tail",
                "geographic_roll_reacquisition": "after_four_tau_main_shutdown_tail_in_coast",
                "shutdown_tail_tau_multiplier": 4., "thrust_axis": "local_up",
                "actual_state_assigned": False})
    return result


def balanced_main_pair(state, booster, required_force_n, maximum_count=13):
    """Select an available geometric opposing pair within real throttle limits.

    The centre-engine prefix is asymmetric when only two are lit. No mass,
    minimum throttle, gimbal limit or arrival geometry is changed here.
    """
    for i in range(maximum_count):
        for j in range(i+1, maximum_count):
            a, b = booster.engines[i], booster.engines[j]
            if not state.engine_states[i].available or not state.engine_states[j].available:
                continue
            if a.max_gimbal_rad <= 0 or b.max_gimbal_rad <= 0 or abs(a.max_thrust_n-b.max_thrust_n) > 1e-8:
                continue
            if env.norm(env.add(a.position_body_m[:2]+(0.,), b.position_body_m[:2]+(0.,))) > 1e-8:
                continue
            capacity = a.max_thrust_n+b.max_thrust_n
            throttle = required_force_n/capacity
            if max(a.min_throttle, b.min_throttle) <= throttle <= 1.:
                return (i, j), throttle
    return None


def nonvertical_corridor_ready(arrival):
    """Measured horizontal/attitude gates before requesting final descent."""
    limits = arrival["limits"]
    return (math.hypot(*arrival["position_error_enu_m"][:2]) <= limits["horizontal_position_m"]
            and all(math.hypot(*p["relative_velocity_enu_mps"][:2]) <= limits["pin_horizontal_speed_mps"]
                    for p in arrival["pins"])
            and arrival["tilt_deg"] <= limits["attitude_angle_deg"]
            and arrival["body_x_east_angle_deg"] <= limits["attitude_angle_deg"]
            and arrival["body_rate_rad_s"] <= limits["body_rate_rad_s"]
            and arrival["propellant_kg"] >= limits["propellant_reserve_kg"])


def landing_actuation_demand(state, booster, axis, requested_force_n):
    """Separate positive braking projection from the finite attitude slew."""
    signed = float(env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), axis))
    if signed <= 0.:
        return 3, .4, None, {"requested_axis_thrust_alignment": signed,
            "landing_alignment_slew": {"active": True, "translational_thrust_admitted": False,
                                       "signed_projection": signed}}
    required = requested_force_n/max(.2, signed)
    count, throttle = _landing_engine_demand(state, booster, required, CONFIG["maximum_landing_engines"])
    pair = balanced_main_pair(state, booster, required, CONFIG["maximum_landing_engines"])
    if pair is not None:
        count, throttle = 2, pair[1]
    return count, throttle, pair, {"requested_axis_thrust_alignment": signed}


def simulate_constrained_recovery(profile, initial_state, catch_config, *, duration_s=None,
        development_actual_planning=False, actual_planning_artifact_sink=None,
        actual_planning_baseline_reference=None, _resume_context=None,
        _forecast_request=None, _capture_context=False, development_transport_prepare_roll=False,
        actual_planning_baseline_binding=None, frozen_boostback_bundle=None,
        development_landing_decision=None, _forecast_landing_delay_s=None, _forecast_landing_local=False):
    """Execute one return from exact inherited state, preserving failed outcomes."""
    from .starship_constrained_guidance import boostback_plan, boostback_force, entry_axis, landing_force
    from .starship_actual_recovery_shooting import digest as shooting_digest

    profile, catch_config = deepcopy(profile), catch_configuration(deepcopy(catch_config))
    if (type(development_actual_planning) is not bool or type(_capture_context) is not bool
            or type(development_transport_prepare_roll) is not bool or type(_forecast_landing_local) is not bool):
        raise ValueError("invalid_actual_recovery_planning_mode")
    if development_actual_planning and development_transport_prepare_roll:
        from .starship_actual_recovery_shooting import validate_transport_baseline
        validate_transport_baseline(actual_planning_baseline_reference, actual_planning_baseline_binding,
                                    profile, catch_config, initial_state=initial_state)
    if development_actual_planning and (not callable(actual_planning_artifact_sink) or _resume_context is not None):
        raise ValueError("actual_planning_requires_artifact_sink_and_live_origin")
    if (_resume_context is None) != (_forecast_request is None):
        raise ValueError("continuation_requires_context_and_forecast_request")
    forecasting = _forecast_request is not None
    if _forecast_landing_local:
        from .starship_landing_decision import CONFIG as LANDING_CONFIG
        if (not forecasting or _forecast_request.get("duration_s") != LANDING_CONFIG["maximum_horizon_s"]
                or _forecast_request.get("maximum_integration_steps") != LANDING_CONFIG["maximum_steps_per_call"]):
            raise ValueError("local_landing_requires_frozen_clock_and_step_budget")
    if _forecast_landing_delay_s is not None and (not forecasting or type(_forecast_landing_delay_s) not in (int, float)
            or _forecast_landing_delay_s not in (0., CONFIG["preview_interval_s"])):
        raise ValueError("scheduled_landing_is_a_bounded_forecast_action")
    if _forecast_landing_delay_s is not None and _resume_context.get("context", {}).get("phase") != "recovery_entry_coast":
        raise ValueError("scheduled_landing_requires_coast_context")
    if frozen_boostback_bundle is not None:
        if development_actual_planning or forecasting or development_transport_prepare_roll is not True:
            raise ValueError("frozen_boostback_requires_transport_only_actual_execution")
        from .starship_landing_decision import validate_bundle
        validate_bundle(frozen_boostback_bundle, profile, catch_config, initial_state)
    if development_landing_decision is not None and frozen_boostback_bundle is None:
        raise ValueError("actual_landing_decision_requires_frozen_boostback_bundle")
    if development_landing_decision is not None:
        from .starship_landing_decision import validate_live_decision, validate_baseline_binding
        from .starship_actual_recovery_shooting import validate_context
        origin = development_landing_decision.get("origin_context") if type(development_landing_decision) is dict else None
        if type(origin) is not dict:
            raise ValueError("actual_landing_decision_requires_declared_origin")
        validate_live_decision(development_landing_decision, origin)
        if origin["context"]["phase"] != "recovery_entry_coast":
            raise ValueError("scheduled_landing_requires_coast_context")
        validate_context(origin, profile, catch_config, physical_guidance_configuration(True))
        validate_baseline_binding(development_landing_decision["baseline_binding"], origin, profile, catch_config)
        if any(development_landing_decision["baseline_binding"][key] != frozen_boostback_bundle[key]
                for key in ("source_run_sha256", "source_map_sha256")):
            raise ValueError("actual_landing_decision_and_frozen_plans_source_mismatch")
    state = dyn.state_from_dict(initial_state)
    booster = vehicle(profile, "booster")
    dyn.observe(state, booster)
    config = _configuration(profile)
    duration = CONFIG["maximum_duration_s"] if duration_s is None else duration_s
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 1200:
        raise ValueError("invalid_constrained_recovery_duration")
    start = state.time_s
    if start+min(.1, duration) <= start or start+duration <= start:
        raise ValueError("constrained_recovery_clock_must_advance")
    _, east, _, _, _, _ = _navigation(state, profile)
    reference = ConditionedGeographicFrame(state.q_body_to_eci, start,
        maximum_roll_rate_rad_s=profile["guidance"]["max_angular_acceleration_rad_s2"]/
        profile["guidance"]["attitude_frequency_rad_s"])
    phase, termination = "recovery_boostback_slew", "time_limit"
    plan, refreshed, burn_start, settle_start = None, False, None, None
    prepare_tail_start = None
    samples, points, events, plans = [], [], [], []
    previous_entry = None
    entry_pretrim_prepared_at = None
    reference_tracker = None
    next_preview, preview, contact, handoff, steps = start, None, None, None, 0
    prior_command_reference = None
    actual_planning_receipt = None
    forecast_failure = None
    controller_error = None
    final_context_error = None
    forecast_clock_start = time.monotonic() if forecasting else None
    forecast_deadline = None
    forecast_step_limit = None
    absolute_deadline = start+duration
    loop_end_time_s = absolute_deadline
    scheduled_landing_onset = (state.time_s+_forecast_landing_delay_s if _forecast_landing_delay_s is not None else None)
    decision_origin_checked, scheduled_landing_applied = False, False
    physical_configuration = physical_guidance_configuration(development_transport_prepare_roll)
    if forecasting:
        from .starship_actual_recovery_shooting import (
            validate_context, validate_forecast_request, restore_references, physical_configuration_from_context,
        )
        stored_configuration = physical_configuration_from_context(_resume_context)
        stored_flag = stored_configuration.get("transport_prepare_roll", False)
        if development_transport_prepare_roll and stored_flag is not True:
            raise ValueError("continuation_transport_policy_mismatch")
        development_transport_prepare_roll = stored_flag
        physical_configuration = physical_guidance_configuration(stored_flag)
        validate_context(_resume_context, profile, catch_config, physical_configuration)
        validate_forecast_request(_forecast_request, _resume_context)
        if _resume_context["state"] != deepcopy(initial_state):
            raise ValueError("continuation_state_mismatch")
        ctx = deepcopy(_resume_context["context"])
        start, absolute_deadline = ctx["start_time_s"], ctx["deadline_s"]
        phase, plan, refreshed = ctx["phase"], ctx["plan"], ctx["refreshed"]
        burn_start, settle_start = ctx["burn_start_s"], ctx["settle_start_s"]
        prepare_tail_start = ctx.get("prepare_tail_start_s")
        previous_entry = ctx["previous_entry_axis_enu"]
        entry_pretrim_prepared_at = ctx["entry_pretrim_prepared_at_s"]
        next_preview, preview = ctx["next_preview_s"], ctx["braking_preview"]
        prior_command_reference = ctx["prior_command_reference"]
        reference, reference_tracker = restore_references(ctx, profile)
        forecast_deadline = _forecast_request["wall_deadline_monotonic_s"]
        forecast_step_limit = _forecast_request["maximum_integration_steps"]
        loop_end_time_s = min(absolute_deadline, state.time_s+_forecast_request["duration_s"])
    policy_id = ("constrained_return_development_v11" if development_actual_planning and development_transport_prepare_roll else
                 TRANSPORT_POLICY_ID if development_transport_prepare_roll else
                 "constrained_return_development_v9" if development_actual_planning else POLICY_ID)
    if development_landing_decision is not None or _forecast_landing_delay_s is not None:
        policy_id = "constrained_return_development_v12"
    run_configuration = deepcopy(physical_configuration)
    if development_actual_planning:
        from .starship_actual_recovery_shooting import actual_planning_configuration
        run_configuration.update(trajectory_objective="actual_finite_sixdof_same_time_arrival",
                                 actual_planning=actual_planning_configuration(development_transport_prepare_roll))
    if frozen_boostback_bundle is not None:
        run_configuration["frozen_boostback"] = {"bundle_sha256": shooting_digest(frozen_boostback_bundle),
            "runtime_short_forecast_calls": 0, "runtime_actual_forecast_calls": 0}
    if development_landing_decision is not None or _forecast_landing_delay_s is not None:
        from .starship_landing_decision import CONFIG as LANDING_CONFIG
        run_configuration["landing_onset_decision"] = deepcopy(LANDING_CONFIG)

    def controller_context():
        from .starship_actual_recovery_shooting import capture_context
        return capture_context(state, profile, catch_config, physical_configuration, phase=phase, start_time_s=start,
            deadline_s=absolute_deadline, plan=plan, refreshed=refreshed, burn_start_s=burn_start,
            settle_start_s=settle_start, previous_entry_axis_enu=previous_entry,
            entry_pretrim_prepared_at_s=entry_pretrim_prepared_at, next_preview_s=next_preview,
            braking_preview=preview, prior_command_reference=prior_command_reference,
            conditioned_reference=reference, reference_tracker=reference_tracker,
            **({"prepare_tail_start_s": prepare_tail_start} if development_transport_prepare_roll else {}))

    def event(name, **fields):
        events.append({"event": name, "time_s": state.time_s, "state": asdict(state), **fields})

    event("constrained_return_start", prediction_is_execution=False)
    try:
        while state.time_s < loop_end_time_s-1e-9:
            if forecasting and (steps >= forecast_step_limit or time.monotonic() >= forecast_deadline):
                termination = "forecast_step_budget_exhausted" if steps >= forecast_step_limit else "forecast_wall_budget_exhausted"
                break
            observed = dyn.observe(state, booster)
            up, east, north, velocity, displacement, distance = _navigation(state, profile)
            position = [-env.dot(displacement, east), -env.dot(displacement, north), observed["altitude_m"]]
            local_v = [env.dot(velocity, a) for a in (east, north, up)]
            mass = observed["mass_kg"]
            gravity = env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2
            axis, count, throttle = up, 0, 0.
            diagnostic = {"guidance_origin": policy_id, "return_site_distance_m": distance,
                          "vertical_speed_mps": local_v[2]}
            trim, main_pair = None, None
            if development_landing_decision is not None and not decision_origin_checked:
                origin_time = development_landing_decision["origin_time_s"]
                if state.time_s >= origin_time-1e-9:
                    from .starship_landing_decision import validate_live_decision
                    validate_live_decision(development_landing_decision, controller_context())
                    decision_origin_checked = True
                    scheduled_landing_onset = development_landing_decision["scheduled_onset_s"]
                    event("landing_onset_decision_context_matched", decision_sha256=shooting_digest(development_landing_decision),
                          origin_state_sha256=development_landing_decision["origin_state_sha256"],
                          origin_operative_context_sha256=development_landing_decision["origin_operative_context_sha256"],
                          actual_controller_context=controller_context(),
                          runtime_preview_calls=0, prediction_is_execution=False)
            if phase.startswith("recovery_boostback"):
                if plan is None:
                    plan = (deepcopy(frozen_boostback_bundle["initial_plan"]) if frozen_boostback_bundle is not None else
                        boostback_plan(position, local_v, mass, state.propellant_kg, booster, profile, catch_config,
                                      full_panel_vector=True))
                    plans.append({"time_s": state.time_s, "state": asdict(state), "plan": plan})
                    event("constrained_boostback_plan", plan=plan)
                force, detail = boostback_force(local_v, plan["target_velocity_enu_mps"], mass, gravity, profile,
                    burn_axis_enu=plan["burn_axis_enu"])
                diagnostic.update(detail)
                axis = env.unit(tuple(sum(force[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
                aligned = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), axis) >= math.cos(math.radians(CONFIG["alignment_deg"]))
                if aligned and not refreshed:
                    if frozen_boostback_bundle is not None:
                        from .starship_landing_decision import validate_refresh
                        refreshed = True
                        validate_refresh(frozen_boostback_bundle, controller_context())
                        plan = deepcopy(frozen_boostback_bundle["refreshed_plan"])
                        event("frozen_boostback_plan_applied", bundle_sha256=shooting_digest(frozen_boostback_bundle),
                              refresh_state_sha256=frozen_boostback_bundle["refresh_state_sha256"],
                              refresh_operative_context_sha256=frozen_boostback_bundle["refresh_operative_context_sha256"],
                              runtime_short_forecast_calls=0, runtime_actual_forecast_calls=0)
                    else:
                        plan = boostback_plan(position, local_v, mass, state.propellant_kg, booster, profile, catch_config,
                            full_panel_vector=True)
                        from .starship_boostback_shooting import refine_boostback_plan
                        short_arguments = {"development_transport_prepare_roll": True} if development_transport_prepare_roll else {}
                        plan = refine_boostback_plan(state, profile, catch_config, plan, reference=reference, **short_arguments)
                    plans.append({"time_s": state.time_s, "state": asdict(state), "plan": plan})
                    event("constrained_boostback_plan_refreshed", plan=plan)
                    refreshed = True
                    if development_actual_planning:
                        from .starship_actual_recovery_shooting import refine_actual_recovery_plan
                        snapshot = controller_context()
                        plan, actual_planning_receipt = refine_actual_recovery_plan(snapshot, profile, catch_config, plan,
                            artifact_sink=actual_planning_artifact_sink,
                            baseline_reference=actual_planning_baseline_reference,
                            **({"baseline_binding": actual_planning_baseline_binding} if development_transport_prepare_roll else {}))
                        plans.append({"time_s": state.time_s, "state": asdict(state), "plan": plan})
                        event("actual_finite_sixdof_plan_selected", plan=plan, receipt=actual_planning_receipt)
                    force, detail = boostback_force(local_v, plan["target_velocity_enu_mps"], mass, gravity, profile,
                        burn_axis_enu=plan["burn_axis_enu"])
                    diagnostic.update(detail)
                    axis = env.unit(tuple(sum(force[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
                    aligned = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), axis) >= math.cos(math.radians(CONFIG["alignment_deg"]))
                error = env.norm(tuple(plan["target_velocity_enu_mps"][i]-local_v[i] for i in range(3)))
                along_error = sum((plan["target_velocity_enu_mps"][i]-local_v[i])*plan["burn_axis_enu"][i] for i in range(3))
                cutoff = (state.propellant_kg <= config["landing_reserve_kg"] or burn_start is not None and
                          (along_error <= CONFIG["velocity_tolerance_mps"] or state.time_s-burn_start >= config["boostback_max_burn_s"]))
                if cutoff:
                    phase, settle_start = "recovery_powered_entry_prepare", state.time_s
                    event("constrained_boostback_cutoff", velocity_error_mps=error, along_axis_velocity_error_mps=along_error,
                          cutoff_basis="fuel_guard" if state.propellant_kg <= config["landing_reserve_kg"] else
                          "along_axis_impulse" if along_error <= CONFIG["velocity_tolerance_mps"] else "time_guard")
                elif aligned:
                    if burn_start is None:
                        burn_start = state.time_s
                        event("boostback_ignition", requested_engine_count=33)
                    phase = "recovery_boostback_burn"
                    count, throttle = _landing_engine_demand(state, booster, env.norm(force), 33)
                else:
                    count, throttle = 3, .4
            if phase == "recovery_powered_entry_prepare":
                count, throttle = 3, .4
                axis = up
                tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
                if tilt <= CONFIG["coast_preparation_alignment_deg"] and env.norm(state.omega_body_rad_s) < CONFIG["rate_settle_limit_rad_s"]:
                    phase, count, throttle = "recovery_entry_coast", 0, 0.
                    if development_transport_prepare_roll:
                        phase, prepare_tail_start = "recovery_entry_shutdown_tail", state.time_s
                    event("powered_coast_attitude_prepared", tilt_deg=tilt)
                elif state.time_s-settle_start > config["boostback_max_slew_s"]:
                    termination = "rate_settle_failed"
                    break
            if phase == "recovery_entry_shutdown_tail":
                axis, count, throttle = up, 0, 0.
                tail_duration = 4*max(engine.throttle_time_constant_s for engine in booster.engines[:profile["booster"]["engine_count"]])
                diagnostic["prepare_shutdown_tail"] = {"started_at_s": prepare_tail_start,
                    "duration_s": tail_duration, "elapsed_s": state.time_s-prepare_tail_start,
                    "main_commands_off": True, "actual_throttle_assigned": False}
                tail_tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
                tail_rate = env.norm(state.omega_body_rad_s)
                if (state.time_s-prepare_tail_start >= tail_duration-1e-9
                        and tail_tilt <= CONFIG["coast_preparation_alignment_deg"]
                        and tail_rate < CONFIG["rate_settle_limit_rad_s"]):
                    phase = "recovery_entry_coast"
                    diagnostic.pop("prepare_shutdown_tail", None)
                    event("powered_coast_shutdown_tail_complete", duration_s=tail_duration,
                          actual_tilt_deg=tail_tilt, actual_body_rate_rad_s=tail_rate,
                          actual_throttle_assigned=False)
                elif state.time_s-settle_start > config["boostback_max_slew_s"]:
                    termination = "rate_settle_failed"
                    break
            if (scheduled_landing_onset is not None and not scheduled_landing_applied and phase == "recovery_entry_coast"
                    and state.time_s >= scheduled_landing_onset-1e-9):
                scheduled_landing_applied = True
                phase = "recovery_landing_13"
                event("landing_onset_ignition_requested", scheduled_onset_s=scheduled_landing_onset,
                      forecast_action_delay_s=_forecast_landing_delay_s,
                      requested_engine_count=13, actual_state_assigned=False)
            if phase == "recovery_entry_coast":
                local_axis, detail = entry_axis(position, local_v, profile, previous_axis=previous_entry,
                    mass_kg=mass, propellant_kg=state.propellant_kg, vehicle=booster, com_body_m=observed["com_body_m"],
                    bank_heading_enu=plan["bank_heading_enu"], bank_sign=plan["bank_sign"],
                    fixed_bank_angle_deg=CONFIG["fixed_entry_bank_angle_deg"],
                    preparation_altitude_m=CONFIG["entry_preparation_altitude_m"])
                rate = profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]
                interval = .1 if observed["altitude_m"] < 100000. else .25
                governor_before = previous_entry or (0., 0., 1.)
                local_axis = _slew_axis(governor_before, local_axis, rate*interval)
                previous_entry = local_axis
                axis = env.unit(tuple(sum(local_axis[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
                diagnostic.update(detail)
                if local_v[2] < 0:
                    if state.time_s >= next_preview:
                        preview = _vector_burn_preview(state, booster, observed, profile, CONFIG["maximum_landing_engines"])
                        next_preview = state.time_s+CONFIG["preview_interval_s"]
                    diagnostic["braking_prediction"] = preview
                    _, _, alpha = _coast_control_profile(profile, observed, include_spooled_tvc=True)
                    # A guidance estimate of fin authority, not achieved torque or
                    # a bound proving the physical attitude will finish the turn.
                    inertia = max(sum(abs(x) for x in row) for row in observed["inertia_kg_m2"])
                    fin_moment = sum(observed["dynamic_pressure_pa"]*panel.area_m2*panel.normal_coefficient*
                        abs(panel.position_body_m[2]-observed["com_body_m"][2])*math.sin(panel.max_deflection_rad)
                        for panel in booster.aero_panels if panel.name.startswith("grid_fin_"))
                    alpha = min(profile["guidance"]["max_angular_acceleration_rad_s2"], alpha+fin_moment/inertia)
                    prospective = _tower_observation(state, booster, profile, catch_config)
                    aero_world = dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"])
                    prospective_force, _ = landing_force(prospective["position_error_enu_m"],
                        min(prospective["pin_height_above_support_m"]), local_v,
                        [env.dot(aero_world, a)/mass for a in (east, north, up)],
                        mass, state.propellant_kg, profile, catch_config)
                    landing_local_axis = env.unit(prospective_force)
                    landing_world_axis = env.unit(tuple(sum(landing_local_axis[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
                    turn = math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), landing_world_axis))))
                    response_delay = 2/(profile["guidance"]["attitude_damping_ratio"]*profile["guidance"]["attitude_frequency_rad_s"])
                    turn_time = 2*math.sqrt(turn/max(alpha, 1e-12))+response_delay
                    if preview["burn_fuel_feasible"] and observed["altitude_m"] <= preview["estimated_stopping_height_m"]+CONFIG["landing_height_margin_m"]+max(0., -local_v[2])*turn_time:
                        local_axis = _slew_axis(governor_before, landing_local_axis, rate*interval)
                        previous_entry = local_axis
                        axis = env.unit(tuple(sum(local_axis[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
                        diagnostic["landing_attitude_preparation"] = {"estimated_turn_time_s": turn_time,
                            "requested_axis_enu": list(landing_local_axis), "target_is_execution": False}
                    if (development_landing_decision is None and _forecast_landing_delay_s is None
                            and observed["altitude_m"] <= preview["estimated_stopping_height_m"]+CONFIG["landing_height_margin_m"]+catch_config["support_height_m"]):
                        phase = "recovery_landing_13"
                        event("landing_stage_requested", requested_engine_count=13, prediction=preview)
                # Recompute feedforward for the governed axis actually requested,
                # rather than retaining trim for a discarded ideal bank candidate.
                if phase == "recovery_entry_coast" and local_v[2] < 0:
                    governed_force, governed_trim = _trim_entry_candidate(local_v, local_axis,
                        observed["atmosphere"]["density_kg_m3"], booster.aero_panels, observed["com_body_m"],
                        2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"],
                        profile["guidance"]["max_q_pa"])
                    diagnostic.update(governed_trim)
                    diagnostic.update(governed_entry_axis_enu=list(local_axis),
                        predicted_force_enu_n=list(governed_force), entry_reference_rate_rad_s=rate)
            if phase.startswith("recovery_landing"):
                arrival = _tower_observation(state, booster, profile, catch_config)
                if arrival["eligible"]:
                    termination = "catch_handoff"
                    handoff = {"eligible": True, "time_s": state.time_s, "state": asdict(state),
                               "observation": arrival, "limits": arrival["limits"]}
                    break
                aero = dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"])
                aero_local = [env.dot(aero, a)/mass for a in (east, north, up)]
                force, detail = landing_force(arrival["position_error_enu_m"], min(arrival["pin_height_above_support_m"]),
                    local_v, aero_local, mass, state.propellant_kg, profile, catch_config)
                diagnostic.update(detail, arrival_observation=arrival)
                clear = min(arrival["pin_height_above_support_m"])
                if clear < 100.:
                    from .starship_terminal_guidance import terminal_horizontal_request
                    horizontal, terminal_detail = terminal_horizontal_request(state, arrival, profile, catch_config, gravity)
                    force[:2] = [mass*(horizontal[i]-aero_local[i]) for i in (0, 1)]
                    horizontal_limit = force[2]*math.tan(math.radians(catch_config["terminal_max_tilt_deg"]))
                    scale = min(1., horizontal_limit/max(math.hypot(*force[:2]), 1e-30))
                    force[:2] = [value*scale for value in force[:2]]
                    diagnostic["terminal_horizontal_feedback"] = terminal_detail
                    diagnostic["requested_force_enu_n"] = force
                if clear < 100. and not nonvertical_corridor_ready(arrival) and state.propellant_kg >= arrival["limits"]["propellant_reserve_kg"]:
                    target_clearance = catch_config["initial_pin_clearance_m"]+2*catch_config["arm_half_width_m"]
                    desired_vertical = max(-20., min(20., -(clear-target_clearance)/catch_config["terminal_position_tau_s"]))
                    force[2] = mass*max(.5, gravity+(desired_vertical-local_v[2])/catch_config["terminal_velocity_tau_s"]-aero_local[2])
                    horizontal_limit = force[2]*math.tan(math.radians(catch_config["terminal_max_tilt_deg"]))
                    scale = min(1., horizontal_limit/max(math.hypot(*force[:2]), 1e-30))
                    force[:2] = [value*scale for value in force[:2]]
                    capacity = CONFIG["maximum_landing_engines"]*profile["booster"]["engine_thrust_n"]
                    scale = min(1., capacity/max(env.norm(force), 1e-30))
                    force = [value*scale for value in force]
                    diagnostic["terminal_corridor_hold"] = {"active": True, "target_pin_clearance_m": target_clearance,
                        "observed_nonvertical_ready": False, "state_assigned": False}
                    diagnostic["requested_force_enu_n"] = force
                axis = env.unit(tuple(sum(force[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
                count, throttle, main_pair, demand_detail = landing_actuation_demand(state, booster, axis, env.norm(force))
                diagnostic.update(demand_detail)
            preferred = _attitude(axis, east)
            if development_transport_prepare_roll and phase in ("recovery_powered_entry_prepare", "recovery_entry_shutdown_tail"):
                target = reference.target(axis, east, preferred, time_s=state.time_s, force_bridge=True,
                                          defer_geographic_reacquisition=True)
                diagnostic["prepare_attitude_reference"] = deepcopy(reference.diagnostics)
            else:
                target = reference.target(axis, east, preferred, time_s=state.time_s)
            if phase.startswith("recovery_landing") and env.norm(observed["thrust_force_body_n"]) > 1.:
                trim = dyn.quaternion_from_two_vectors(env.unit(observed["thrust_force_body_n"]), (0., 0., 1.))
                target = dyn.normalize_quaternion(dyn.quaternion_multiply(target, trim))
            tracking_arguments = {}
            if phase == "recovery_entry_coast" or phase.startswith("recovery_landing"):
                from .starship_reference_tracking import ReferenceTracker
                if reference_tracker is None:
                    # Carry the last actual commanded reference across the phase
                    # boundary; a raw new goal must not become an instantaneous
                    # reference jump or an assigned physical attitude.
                    if prior_command_reference is not None:
                        reference_tracker = ReferenceTracker(profile, prior_command_reference["quaternion"],
                            prior_command_reference["time_s"])
                    elif points:
                        anchor = points[-1]
                        reference_tracker = ReferenceTracker(profile,
                            anchor["navigation"]["target_q_body_to_eci"], anchor["time_s"])
                    else:
                        # An independently initialized short fixture may enter
                        # this phase on its first sample. Anchor its REQUEST to
                        # its actual observed pose without inventing past motion.
                        reference_tracker = ReferenceTracker(profile, state.q_body_to_eci, state.time_s)
                target, tracking = reference_tracker.update(target, state.q_body_to_eci,
                    state.omega_body_rad_s, state.time_s)
                diagnostic["reference_tracking"] = tracking["receipt"]
                if phase.startswith("recovery_landing") or observed["dynamic_pressure_pa"] > 100.:
                    tracking_arguments = {key: tracking[key] for key in
                        ("reference_rate_body_rad_s", "reference_acceleration_body_rad_s")}
                if phase == "recovery_entry_coast" and local_v[2] < 0:
                    requested_axis = dyn.rotate(target, (0., 0., 1.))
                    requested_local_axis = [env.dot(requested_axis, a) for a in (east, north, up)]
                    governed_force, governed_trim = _trim_entry_candidate(local_v, requested_local_axis,
                        observed["atmosphere"]["density_kg_m3"], booster.aero_panels, observed["com_body_m"],
                        2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"],
                        profile["guidance"]["max_q_pa"])
                    diagnostic.update(governed_trim)
                    diagnostic.update(governed_entry_axis_enu=requested_local_axis,
                        predicted_force_enu_n=list(governed_force))
            nominal_step = .1 if observed["altitude_m"] < 100_000. or count else .25
            remaining_time = loop_end_time_s-state.time_s
            step = min(nominal_step, remaining_time)
            if _forecast_landing_local:
                # Local source-bound prefix only: IEEE clock roundoff must not
                # create a different actuator-rate-limited final command.
                from .starship_landing_decision import local_macrostep
                step = local_macrostep(nominal_step, remaining_time)
            if development_transport_prepare_roll and phase == "recovery_entry_shutdown_tail":
                step = min(step, .1)
                remaining_tail = prepare_tail_start+tail_duration-state.time_s
                if remaining_tail > 1e-9:
                    step = min(step, remaining_tail)
            if phase.startswith("recovery_boostback"):
                from .starship_boostback_shooting import boostback_macro_step
                step, step_receipt = boostback_macro_step(state, booster, observed, profile,
                    plan["target_velocity_enu_mps"], plan["burn_axis_enu"], maximum_step_s=step)
                diagnostic["boostback_macrostep"] = step_receipt
            finite_fins = phase == "recovery_entry_coast" or phase.startswith("recovery_landing")
            if phase == "recovery_entry_coast" and observed["dynamic_pressure_pa"] <= 100.:
                command, detail = control_coast_stopping_distance(state, booster, target, profile, control_interval_s=step)
                from .starship_entry_pretrim import prepare_entry_fins
                declared_trim = diagnostic.get("predicted_trim_flap_angles_rad")
                reference_kind = ("governed_current_flow_trim" if local_v[2] < 0 and
                    diagnostic.get("governed_entry_axis_enu") is not None and declared_trim is not None else
                    "future_descending_entry_prediction" if declared_trim is not None else "missing_reference")
                command, pretrim = prepare_entry_fins(state, booster, command, detail, profile,
                    declared_trim, interval_s=step)
                diagnostic["entry_pretrim"] = pretrim
                diagnostic["entry_pretrim_reference_kind"] = reference_kind
                # Recheck at every low-q sample: an old/ascent trim cannot certify
                # the different branch required at the actual entry boundary.
                entry_pretrim_prepared_at = (state.time_s if reference_kind == "governed_current_flow_trim"
                    and pretrim["status"] == "accepted" and pretrim["prepared"] is True else None)
            else:
                command, detail = control_with_measured_tvc(state, booster, target, throttle, count, profile,
                    use_flaps=finite_fins, development_fin_allocation=finite_fins, control_interval_s=step,
                    trim_angles_rad=diagnostic.get("predicted_trim_flap_angles_rad") if phase == "recovery_entry_coast" else None,
                    development_fin_policy=CONFIG["finite_fin_policy"] if finite_fins and entry_pretrim_prepared_at is not None
                    else "finite_regularized_fins_v1", **tracking_arguments)
            diagnostic.update(detail)
            diagnostic["entry_pretrim_prepared_at_s"] = entry_pretrim_prepared_at
            if main_pair is not None:
                selected, throttle = main_pair
                engines = list(command.engines)
                for index in range(33):
                    engines[index] = replace(engines[index], enabled=index in selected,
                        throttle=throttle if index in selected else 0.)
                command = dyn.Command6DOF(tuple(engines), command.flap_angles_rad)
                command, diagnostic = reallocate_measured_tvc(state, booster, command, diagnostic, profile, observed=observed)
                diagnostic["selected_main_engine_indices"] = list(selected)
            if trim is not None:
                diagnostic["net_thrust_trim_quaternion"] = list(trim)
            samples.append(_sample_booster(state, booster, phase, command, diagnostic))
            points.append({"time_s": state.time_s, "phase": phase, "state": asdict(state), "command": asdict(command),
                           "com_rate_body_mps": list(observed["com_rate_body_mps"]), "navigation": diagnostic})
            prior_command_reference = {"quaternion": list(diagnostic["target_q_body_to_eci"]), "time_s": state.time_s}
            if env.norm(state.omega_body_rad_s) > 5.:
                termination = "angular_rate_envelope_exceeded"
                break
            if observed["altitude_m"] < 300.+env.norm(velocity)*step:
                state, receipt = find_contact(state, booster, command, step, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])
                if receipt["contact"]:
                    contact, termination = receipt, "surface_contact"
                    steps += 1
                    break
            else:
                state = dyn.step(state, booster, command, step)
            steps += 1
    except Exception as exc:
        if not forecasting and development_landing_decision is None:
            raise
        forecast_failure = type(exc).__name__
        controller_error = forecast_failure
        termination = "forecast_controller_exception" if forecasting else "landing_decision_execution_exception"
    final = _sample_booster(state, booster, phase)
    if points and points[-1]["time_s"] == state.time_s:
        samples[-1] = final
        points[-1]["command"] = None
        points[-1]["navigation"] = {}
    else:
        samples.append(final)
        points.append({"time_s": state.time_s, "phase": phase, "state": asdict(state), "command": None,
                       "com_rate_body_mps": final["com_rate_body_mps"], "navigation": {}})
    arrival = _tower_observation(state, booster, profile, catch_config)
    if handoff is None:
        handoff = {"eligible": False, "time_s": state.time_s, "state": None,
                   "observation": arrival, "limits": arrival["limits"]}
    event(termination)
    result = {"scenario": "booster_return", "body_id": "booster", "guidance_policy": policy_id,
        "initial_state": deepcopy(initial_state), "booster_separation_state": deepcopy(initial_state),
        "final_state": asdict(state), "samples": samples, "events": events, "contact": contact,
        "recovery_record": {"schema": "missionos.starship_constrained_recovery.v1", "policy_id": policy_id,
            "input_separation_state": deepcopy(initial_state), "guidance_configuration": run_configuration,
            "checkpoints": points, "plans": plans, "handoff": handoff, "requested_duration_s": duration_s,
            "resolved_duration_s": duration, "production_policy_admitted": False,
            "physical_execution": False, "missionos_dispatch": False},
        "outcome": {"termination": termination, "phase": phase, "start_time_s": start, "end_time_s": state.time_s,
            "duration_s": state.time_s-start, "integration_steps": steps,
            "final_ground_speed_mps": final["ground_speed_mps"], "final_hull_clearance_m":
            hull_clearance(state, booster, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])["signed_clearance_m"],
            "handoff_reached": handoff["eligible"], "physical_execution": False, "mission_completed": False}}
    if actual_planning_receipt is not None:
        result["recovery_record"]["actual_planning_receipt"] = actual_planning_receipt
    if frozen_boostback_bundle is not None:
        result["recovery_record"]["frozen_boostback_bundle"] = deepcopy(frozen_boostback_bundle)
    if development_landing_decision is not None or _forecast_landing_delay_s is not None:
        result["recovery_record"]["landing_onset_decision"] = (deepcopy(development_landing_decision) if development_landing_decision is not None else
            {"schema": "missionos.starship_landing_onset_forecast_action.v1", "delay_s": _forecast_landing_delay_s,
             "origin_time_s": initial_state["time_s"], "scheduled_onset_s": scheduled_landing_onset,
             "runtime_preview_calls": 0, "actual_state_assigned": False})
    if development_landing_decision is not None:
        result["recovery_record"]["execution_failure"] = controller_error
    if forecasting:
        try:
            result["final_controller_context"] = controller_context()
        except Exception as exc:
            # A post-loop context/serialization failure must not erase the
            # already executed physical trajectory. It is a failed forecast,
            # not a resumable or selectable result.
            final_context_error = type(exc).__name__
            forecast_failure = final_context_error
            result["final_controller_context"] = None
    elif _capture_context:
        result["final_controller_context"] = controller_context()
    if forecasting:
        result["forecast_metadata"] = {"schema": "missionos.starship_actual_recovery_forecast.v1",
            "origin_context_sha256": _forecast_request["origin_context_sha256"],
            "request": deepcopy(_forecast_request), "failure": forecast_failure,
            "final_context_error": final_context_error,
            "controller_error": controller_error,
            "wall_seconds": time.monotonic()-forecast_clock_start,
            "prediction_complete": forecast_failure is None and termination in ("catch_handoff", "surface_contact"),
            "prediction_is_execution": False, "production_policy_admitted": False}
        if _forecast_landing_local:
            result["forecast_metadata"]["landing_local_clock"] = {
                "roundoff_tolerance_s": LANDING_CONFIG["clock_roundoff_tolerance_s"],
                "maximum_horizon_s": LANDING_CONFIG["maximum_horizon_s"],
                "maximum_integration_steps": LANDING_CONFIG["maximum_steps_per_call"],
                "macrostep_preserved_within_clock_roundoff_only": True}
    return result
