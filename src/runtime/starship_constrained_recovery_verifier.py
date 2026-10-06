"""Independent persisted-record checks for isolated constrained return trials.

No guidance, dynamics or control producer is imported. These checks reconstruct
material-point arrival and elementary kinematics from saved states. They do not
replay the integrator, certify the prediction model or award launch execution.
"""
from __future__ import annotations

from bisect import bisect_left
import hashlib
import json
import math

from .starship_booster_catch_verifier import verify_catch
from .starship_booster_recovery_verifier import (
    _A, _E2, _ROTATION, _Invalid, _PHASES, _TERMINATIONS, _arrival, _command, _compare_vector,
    _continuity, _fin_allocation, _flow_and_centroid, _json, _near, _norm,
    _number, _observe_handoff, _prediction_origin, _profile_and_config,
    _require, _same_state, _same_value, _state, _vector, _cross, _rotate,
)
from .starship_sixdof_verifier import _contact, _Invalid as _ContactInvalid
from .starship_actual_recovery_planning_verifier import (
    CONFIG as _ACTUAL_CONFIG, CONFIG_V1 as _ACTUAL_CONFIG_V1,
    SCHEMA as _ACTUAL_SCHEMA, SCHEMA_V1 as _ACTUAL_SCHEMA_V1,
    TRANSPORT_CONFIG as _ACTUAL_TRANSPORT_CONFIG, TRANSPORT_SCHEMA as _ACTUAL_TRANSPORT_SCHEMA,
)

_POLICY = "constrained_return_development_v1"
_IMPULSE_POLICY = "constrained_return_development_v2"
_SHOOTING_POLICY = "constrained_return_development_v3"
_PREP_POLICY = "constrained_return_development_v4"
_INTERCEPT_POLICY = "constrained_return_development_v5"
_MOMENT_POLICY = "constrained_return_development_v6"
_PRETRIM_POLICY = "constrained_return_development_v7"
_TRACKING_POLICY = "constrained_return_development_v8"
_ACTUAL_POLICY = "constrained_return_development_v9"
_TRANSPORT_POLICY = "constrained_return_development_v10"
_ACTUAL_TRANSPORT_POLICY = "constrained_return_development_v11"
_LANDING_POLICY = "constrained_return_development_v12"
_LANDING_CONFIGURATION = {"maximum_local_calls": 4, "maximum_horizon_s": 40., "maximum_steps_per_call": 400,
    "allowed_delays_s": [0., .5], "runtime_preview_calls": 0,
    "baseline_equivalence_required": True, "predicted_full_handoff_required": True,
    "source_binding_independently_verified": False, "physical_execution": False}
_LANDING_CONFIGURATION["clock_roundoff_tolerance_s"] = 1e-9
# This independent contract is frozen with the isolated driver, never read
# from the driver's module or accepted from the record under examination.
_GUIDANCE = {
    "maximum_duration_s": 1200., "alignment_deg": 15., "velocity_tolerance_mps": 12.,
    "rate_settle_limit_rad_s": .003, "rate_settle_max_s": 30.,
    "preview_interval_s": .5, "landing_height_margin_m": 500.,
    "maximum_landing_engines": 13, "finite_fin_regularization": .05,
    "recording": "every_macrostep", "production_policy_admitted": False,
}
_CONFIGURATIONS = {
    _POLICY: _GUIDANCE,
    _IMPULSE_POLICY: {**_GUIDANCE, "boostback_reference": "fixed_axis_impulse"},
    _SHOOTING_POLICY: {**_GUIDANCE, "boostback_reference": "finite_shooting_fixed_axis_impulse",
                      "entry_bank_reference": "held_site_bearing",
                      "landing_allocation": "opposing_gimballed_pair_when_feasible",
                      "maximum_boostback_forecast_calls": 48},
}
_CONFIGURATIONS[_PREP_POLICY] = {**_CONFIGURATIONS[_SHOOTING_POLICY],
    "coast_attitude_preparation": "powered_upright", "coast_preparation_alignment_deg": 5.,
    "minimum_boostback_macrostep_s": .001, "entry_preparation_altitude_m": 80000.,
    "fixed_entry_bank_angle_deg": 35.}
_CONFIGURATIONS[_INTERCEPT_POLICY] = {**_CONFIGURATIONS[_PREP_POLICY],
    "trajectory_objective": "post_prepare_point_coast_intercept", "terminal_corridor_hold": True}
_CONFIGURATIONS[_MOMENT_POLICY] = {**_CONFIGURATIONS[_INTERCEPT_POLICY],
    "finite_fin_policy": "finite_moment_priority_fins_v1"}
_CONFIGURATIONS[_PRETRIM_POLICY] = {**_CONFIGURATIONS[_MOMENT_POLICY], "entry_pretrim": {
    "policy_id": "finite_entry_pretrim_v1", "maximum_dynamic_pressure_pa": 100.,
    "actual_fin_tolerance_deg": .1, "probe_fractions": [.2, .4, .6, .8, 1.],
    "maximum_transition_probes": 5, "high_q_unprepared_policy": "finite_regularized_fins_v1",
    "latch_rule": "latest_accepted_measured_governed_current_flow_trim_at_low_q"}}
_CONFIGURATIONS[_TRACKING_POLICY] = {**_CONFIGURATIONS[_PRETRIM_POLICY], "reference_tracking": {
    "policy_id": "bounded_reference_tracking_v1", "scope": "entry_and_landing",
    "feedback_scope": "entry_q_above_100_and_all_landing",
    "initial_anchor": "previous_commanded_target_zero_reference_rate",
    "historyless_fixture_anchor": "same_time_actual_pose_zero_reference_rate",
    "rate_limit_source": "guidance.max_angular_acceleration_rad_s2/attitude_frequency_rad_s",
    "acceleration_limit_source": "guidance.max_angular_acceleration_rad_s2",
    "law": "causal_raw_goal_rate_plus_stopping_distance_closing_with_radial_rate_and_acceleration_limits"}}
_CONFIGURATIONS[_ACTUAL_POLICY] = {**_CONFIGURATIONS[_TRACKING_POLICY],
    "trajectory_objective": "actual_finite_sixdof_same_time_arrival", "actual_planning": _ACTUAL_CONFIG}
_ACTUAL_CONFIGURATION_V1 = {**_CONFIGURATIONS[_TRACKING_POLICY],
    "trajectory_objective": "actual_finite_sixdof_same_time_arrival", "actual_planning": _ACTUAL_CONFIG_V1}
_TRANSPORT_REFERENCE = {"policy_id": "parallel_transport_deferred_geographic_roll_v1",
    "scope": "powered_entry_prepare_and_command_off_tail",
    "geographic_roll_reacquisition": "after_four_tau_main_shutdown_tail_in_coast",
    "shutdown_tail_tau_multiplier": 4., "thrust_axis": "local_up", "actual_state_assigned": False}
_CONFIGURATIONS[_TRANSPORT_POLICY] = {**_CONFIGURATIONS[_TRACKING_POLICY],
    "transport_prepare_roll": True, "powered_prepare_reference": _TRANSPORT_REFERENCE}
_CONFIGURATIONS[_ACTUAL_TRANSPORT_POLICY] = {**_CONFIGURATIONS[_TRANSPORT_POLICY],
    "trajectory_objective": "actual_finite_sixdof_same_time_arrival", "actual_planning": _ACTUAL_TRANSPORT_CONFIG}
_CONFIGURATIONS[_LANDING_POLICY] = {**_CONFIGURATIONS[_TRANSPORT_POLICY],
    "landing_onset_decision": _LANDING_CONFIGURATION}
_PREP_PHASES = ("recovery_boostback_slew", "recovery_boostback_burn", "recovery_powered_entry_prepare",
                "recovery_entry_coast", "recovery_landing_13", "recovery_landing_5", "recovery_landing_3")
_TRANSPORT_PHASES = _PREP_PHASES[:3]+("recovery_entry_shutdown_tail",)+_PREP_PHASES[3:]
_SHORT_FORECAST = {
    "maximum_forecast_calls": 48, "maximum_solver_nfev": 12,
    "maximum_forecast_duration_s": 100., "shutdown_tail_s": 1.4,
    "angular_offset_bound_deg": 15., "cut_projection_offset_bound_mps": 300.,
    "angle_difference_step_rad": .002, "cut_difference_step_mps": 5.,
    "alignment_deg": 15., "velocity_tolerance_mps": 12.,
    "rate_settle_limit_rad_s": .003, "rate_settle_max_s": 30.,
    "velocity_residual_scale_mps": 30., "position_residual_scale_m": 10000.,
    "fuel_residual_scale_kg": 10000., "incomplete_forecast_penalty": 100.,
}
_SHORT_FORECAST_V2 = {**_SHORT_FORECAST, "cut_difference_step_mps": 30., "minimum_boostback_step_s": .001,
                      "prepare_tilt_limit_deg": 5., "post_cutoff_control": "powered_upright_prepare"}
_SHORT_FORECAST_V3 = {**_SHORT_FORECAST_V2,
    "objective": "post_prepare_coast_intercept", "terminal_position_residual_scale_m": 1000.,
    "terminal_speed_residual_scale_mps": 100., "vertical_regularization_scale_mps": 200.,
    "maximum_point_continuations_per_forecast": 1, "coast_entry_angle_deg": 35.,
    "coast_terminal_altitude_m": 1500., "coast_model": "existing_full_panel_vector",
    "ideal_arrest_dt_s": .1, "ideal_arrest_horizon_s": 60., "ideal_arrest_terminal_speed_mps": 2.,
    "ideal_arrest_model": "aligned_available_engine_prefix_spool_gravity"}
_MAX_CHECKPOINTS = 12002
_MAX_JSON_NODES = 32_000_000
_FORBIDDEN = (
    "physical_execution", "physical_execution_invoked", "mission_completed",
    "production_policy_admitted", "missionos_dispatch", "model_advantage_established",
    "launch_connected", "launch_connected_catch_supported", "state_reset",
    "attitude_prescribed", "starship_vehicle_validated", "landing_hardware_validated",
    "catch_verified", "simulated_catch_supported",
)


def _phases(policy):
    if policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY, _LANDING_POLICY):
        return _TRANSPORT_PHASES
    return _PREP_PHASES if policy in (_PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) else _PHASES


def _claims(run, record, outcome):
    for section in (run, record, outcome):
        _require(all(section.get(key, False) is False for key in _FORBIDDEN),
                 "claim_boundary", "An isolated development record cannot assert admission or mission success")
    _require(all(record.get(key) is False for key in
                 ("production_policy_admitted", "physical_execution", "missionos_dispatch"))
             and outcome.get("physical_execution") is False and outcome.get("mission_completed") is False,
             "claim_boundary", "Missing explicit development and simulated-execution boundary")
    _require(outcome.get("orbit_gate_reached", False) is False
             and type(outcome.get("payload_released_count", 0)) is int
             and outcome.get("payload_released_count", 0) == 0,
             "claim_boundary", "An isolated booster trial cannot establish orbit or payload release")


def _bind_event_state(event, checkpoints, times, profile):
    state = event.get("state")
    _state(state, profile)
    _require(state["time_s"] == event["time_s"], "event_binding", "Event clock differs from its actual state")
    index = bisect_left(times, event["time_s"])
    _require(index < len(checkpoints) and times[index] == event["time_s"],
             "event_binding", "Each macrostep event requires its actual saved state")
    _same_state(state, checkpoints[index]["state"], "event_binding")


def _planning(record, events, checkpoints, profile, catch_config):
    """Bind structured predictions, without certifying their feasibility."""
    plans = record.get("plans")
    actual_policy = record["policy_id"] in (_ACTUAL_POLICY, _ACTUAL_TRANSPORT_POLICY)
    frozen = record["policy_id"] == _LANDING_POLICY
    policy = _TRANSPORT_POLICY if frozen else record["policy_id"]
    _require(type(plans) is list and 1 <= len(plans) <= (3 if actual_policy else 2),
             "prediction_binding", "Missing bounded local prediction history")
    times = [point["time_s"] for point in checkpoints]
    for item in plans:
        _require(type(item) is dict and set(item) == {"time_s", "state", "plan"}
                 and _number(item["time_s"]), "prediction_binding", "Unbound local planning origin")
        _bind_event_state(item, checkpoints, times, profile)
        plan = item["plan"]
        _require(type(plan) is dict and plan.get("prediction_is_execution") is False,
                 "prediction_binding", "Planning cannot substitute for actual execution")
        _require(actual_policy or frozen or "actual_recovery_prediction" not in plan,
                 "actual_planning", "A full-recovery prediction requires the explicit v9 development policy")
        _require(all(plan.get(key, False) is False for key in _FORBIDDEN),
                 "claim_boundary", "A local prediction cannot establish admission or a terminal outcome")
        _require(_vector(plan.get("target_velocity_enu_mps")),
                 "prediction_binding", "Missing finite local target-velocity request")
        if policy in (_IMPULSE_POLICY, _SHOOTING_POLICY, _PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
            _require(_vector(plan.get("burn_axis_enu")) and _near(_norm(plan["burn_axis_enu"]), 1., 1e-8),
                     "prediction_binding", "Fixed-axis impulse request requires a finite unit burn axis")
        if policy in (_SHOOTING_POLICY, _PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
            _require(_vector(plan.get("bank_heading_enu")) and _near(_norm(plan["bank_heading_enu"]), 1., 1e-8)
                     and abs(plan["bank_heading_enu"][2]) <= 1e-8
                     and type(plan.get("bank_sign")) is int and plan["bank_sign"] in (-1, 1),
                     "prediction_binding", "Held bank reference requires a finite unit horizontal bearing and signed branch")
            if "actual_dynamics_prediction" in plan:
                if "actual_recovery_prediction" not in plan:
                    _short_prediction(plan, item["state"], profile, catch_config,
                        policy=_TRANSPORT_POLICY if record["policy_id"] == _ACTUAL_TRANSPORT_POLICY else
                        _TRACKING_POLICY if actual_policy else record["policy_id"])
            if "actual_recovery_prediction" in plan and not frozen:
                _require(actual_policy and item is plans[-1] and len(plans) == 3,
                         "actual_planning", "Full-recovery forecasts escaped their explicit v9 final plan")
                from .starship_actual_recovery_planning_verifier import verify_actual_recovery_prediction
                index = bisect_left(times, item["time_s"])
                receipt = verify_actual_recovery_prediction(plan, item["state"], profile, catch_config,
                    _CONFIGURATIONS[_TRANSPORT_POLICY if record["policy_id"] == _ACTUAL_TRANSPORT_POLICY else _TRACKING_POLICY],
                    previous_checkpoint=checkpoints[index-1] if index else None)
                context = receipt["origin_context"]["context"]
                _require(_same_value(receipt["origin_context"]["context"]["plan"], plans[-2]["plan"])
                         and _same_value(record.get("actual_planning_receipt"), receipt),
                         "actual_planning", "Selected prediction altered its earlier source plan or detached the run receipt")
                _require(item["time_s"] == plans[-2]["time_s"]
                         and context["start_time_s"] == checkpoints[0]["time_s"]
                         and _near(context["deadline_s"], checkpoints[0]["time_s"]+record["resolved_duration_s"])
                         and context["phase"] in ("recovery_boostback_slew", "recovery_boostback_burn")
                         and all(context[key] is None for key in ("burn_start_s", "settle_start_s", "previous_entry_axis_enu",
                             "entry_pretrim_prepared_at_s", "braking_preview", "reference_tracker"))
                         and context["next_preview_s"] == context["start_time_s"]
                         and (context.get("prepare_tail_start_s") is None if record["policy_id"] == _ACTUAL_TRANSPORT_POLICY else True)
                         and _same_value(context["conditioned_reference"], plans[-2]["plan"]["actual_dynamics_prediction"]["input_reference"]),
                         "actual_planning", "Live refinement rewrote the pre-ignition clocks, cached control memory or carried geographic frame")
    recorded = [{"time_s": event["time_s"], "state": event["state"], "plan": event["plan"]}
                for event in events if "plan" in event]
    _require(_same_value(plans, recorded), "prediction_binding", "Prediction history is not bound to recorded planning events")
    selected_events = [event for event in events if event["event"] == "actual_finite_sixdof_plan_selected"]
    if frozen:
        bundle = record["frozen_boostback_bundle"]
        _require(not selected_events and "actual_planning_receipt" not in record
                 and len(plans) in (1, 2) and plans[0]["plan"] == bundle["initial_plan"]
                 and (len(plans) == 1 or plans[-1]["plan"] == bundle["refreshed_plan"]
                      and plans[-1]["time_s"] == bundle["refresh_time_s"]),
                 "landing_decision", "Frozen execution cannot make another actual forecast or change the source request")
    if actual_policy:
        refreshed = any(event["event"] == "constrained_boostback_plan_refreshed" for event in events)
        _require(len(selected_events) == (1 if refreshed else 0) and len(plans) == (3 if refreshed else 1),
                 "actual_planning", "A reached v9 refinement requires exactly one preserved source plan and selected continuation")
        if refreshed:
            _require(selected_events[0].get("plan") == plans[-1]["plan"]
                     and _same_value(selected_events[0].get("receipt"), record.get("actual_planning_receipt")),
                     "actual_planning", "Selected forecast event did not retain its exact plan and receipt")
    else:
        _require(not selected_events and "actual_planning_receipt" not in record,
                 "actual_planning", "Actual-recovery planning requires the explicit v9 development policy")
    if policy in (_SHOOTING_POLICY, _PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
        for event in events:
            if event["event"] == "constrained_boostback_plan_refreshed":
                _require("actual_dynamics_prediction" in event["plan"],
                         "prediction_binding", "Alignment refresh must retain its bounded finite forecast receipt")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _reference_record(reference, state, profile):
    _require(type(reference) is dict and _vector(reference.get("quaternion"), 4)
             and _near(_norm(reference["quaternion"]), 1., 1e-8)
             and _number(reference.get("time_s")) and reference["time_s"] <= state["time_s"]
             and type(reference.get("bridging")) is bool
             and type(reference.get("diagnostics")) is dict and type(reference.get("frame_diagnostics")) is dict
             and _near(reference.get("maximum_roll_rate_rad_s"),
                       profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]),
             "short_prediction", "Invalid carried geographic reference state")


def _parameter_candidate(seed, parameters):
    axis = seed["burn_axis_enu"]
    hint = [1., 0., 0.] if abs(axis[0]) < .8 else [0., 1., 0.]
    dot = sum(a*b for a, b in zip(hint, axis))
    first = [hint[i]-dot*axis[i] for i in range(3)]
    norm = _norm(first)
    first = [x/norm for x in first]
    second = _cross(axis, first)
    direction = [axis[i]+math.tan(parameters[0])*first[i]+math.tan(parameters[1])*second[i] for i in range(3)]
    norm = _norm(direction)
    direction = [x/norm for x in direction]
    return direction, [seed["target_velocity_enu_mps"][i]+parameters[2]*direction[i] for i in range(3)]


def _forecast_residual(forecast, receipt, profile, catch_config):
    if receipt.get("schema") in ("missionos.starship_short_boostback_shooting.v3", "missionos.starship_short_boostback_shooting.v4"):
        entry = next(item for item in receipt["forecasts"] if item.get("forecast") is forecast)
        continuation = entry.get("point_continuation")
        guard = profile.get("booster_return", {}).get("landing_reserve_kg", 60000.)
        if continuation is None:
            trajectory, missed_altitude = [0., 0., 0., 0.], 0.
        else:
            prediction, budget, arrest = continuation["prediction"], continuation["ideal_fuel_budget"], continuation["ideal_arrest_preview"]
            trajectory = [arrest["position_enu_m"][0]/1000., arrest["position_enu_m"][1]/1000.,
                          max(0., budget["terminal_speed_mps"]-budget["available_ideal_delta_v_mps"])/100.,
                          max(0., -arrest["support_margin_kg"])/10000.]
            missed_altitude = 0. if prediction["reached_terminal_altitude"] and arrest["velocity_arrested_above_capture_floor"] else 100.
        vertical = (forecast["velocity_enu_mps"][2]-receipt["endpoint_target_velocity_enu_mps"][2])/200.
        return [*trajectory, vertical, max(0., guard-forecast["propellant_kg"])/10000.,
                0. if forecast["prediction_complete"] else 100., missed_altitude,
                100. if forecast["prediction_complete"] and continuation is None else 0.]
    velocity = [(value-target)/30. for value, target in zip(forecast["velocity_enu_mps"], receipt["endpoint_target_velocity_enu_mps"])]
    position = [(value-target)/10000. for value, target in zip(forecast["position_enu_m"], receipt["endpoint_target_position_enu_m"])]
    fuel = forecast["propellant_kg"]
    mass = profile["booster"]["dry_mass_kg"]+fuel
    horizon = ((catch_config["initial_pin_clearance_m"]+catch_config["arm_half_width_m"])/(-.5*catch_config["initial_vertical_speed_mps"])
               +4*profile["actuators"]["throttle_tau_s"])
    reserve = mass*9.81/(profile["booster"]["engine_isp_s"]*9.80665)*horizon
    guard = profile.get("booster_return", {}).get("landing_reserve_kg", 60000.)
    return [*velocity, *position, max(0., guard-fuel)/10000., max(0., reserve-fuel)/10000.,
            0. if forecast["prediction_complete"] else 100.]


def _point_continuation(item, forecast, seed, profile, catch_config):
    """Bind optimistic point input and rocket-budget arithmetic, not airloads."""
    _require(type(item) is dict and item.get("schema") == "missionos.starship_post_prepare_point_coast.v1"
             and type(catch_config) is dict
             and all(item.get(key) is False for key in ("prediction_is_execution", "arrival_admitted", "support_admitted"))
             and item.get("input_state_sha256") == _digest(forecast["final_state"])
             and _same_value(item.get("input_position_enu_m"), forecast["position_enu_m"])
             and _same_value(item.get("input_velocity_enu_mps"), forecast["velocity_enu_mps"])
             and _same_value(item.get("bank_heading_enu"), seed["bank_heading_enu"])
             and _same_value(item.get("bank_sign"), seed["bank_sign"]),
             "short_prediction", "Point coast did not start from the exact finite post-prepare endpoint and held bank")
    fuel, dry = forecast["propellant_kg"], profile["booster"]["dry_mass_kg"]
    mass = dry+fuel
    _require(_near(item.get("input_propellant_kg"), fuel) and _near(item.get("input_mass_kg"), mass),
             "short_prediction", "Point coast replaced the finite endpoint propellant or mass")
    prediction, budget = item.get("prediction"), item.get("ideal_fuel_budget")
    _require(type(prediction) is dict and prediction.get("prediction_is_execution") is False
             and prediction.get("attitude_dynamics_integrated") is False
             and prediction.get("lateral_lift_integrated") is True
             and _vector(prediction.get("position_enu_m")) and _vector(prediction.get("velocity_enu_mps"))
             and prediction.get("entry_angle_deg") == 35. and prediction.get("terminal_altitude_m") == 1500.
             and _same_value(prediction.get("bank_heading_enu"), seed["bank_heading_enu"])
             and _same_value(prediction.get("bank_sign"), seed["bank_sign"])
             and type(prediction.get("reached_terminal_altitude")) is bool
             and prediction["reached_terminal_altitude"] is (prediction["position_enu_m"][2] <= 1500.)
             and _number(prediction.get("elapsed_s")) and 0 <= prediction["elapsed_s"] <= 1000.+1e-7
             and type(prediction.get("integration_steps")) is int and 0 <= prediction["integration_steps"] <= 1000
             and prediction.get("point_step_budget") == 1000,
             "short_prediction", "Full-panel ideal coast must retain its fixed assumptions and bounded terminal outcome")
    _require(type(budget) is dict and all(budget.get(key) is False for key in
             ("gravity_loss_in_arrest_integrated", "finite_attitude_in_arrest_integrated", "prediction_is_execution")),
             "claim_boundary", "Ideal arrest budget cannot claim finite physical execution")
    speed = _norm(prediction["velocity_enu_mps"])
    exhaust = profile["booster"]["engine_isp_s"]*9.80665
    after_arrest = mass*math.exp(-speed/exhaust)-dry
    horizon = ((catch_config["initial_pin_clearance_m"]+catch_config["arm_half_width_m"])/(-.5*catch_config["initial_vertical_speed_mps"])
               +4*profile["actuators"]["throttle_tau_s"])
    reserve = (dry+max(0., after_arrest))*9.81/exhaust*horizon
    available_dv = max(0., exhaust*math.log(mass/(dry+reserve)))
    for key, expected in (("terminal_speed_mps", speed), ("available_ideal_delta_v_mps", available_dv),
                          ("after_ideal_arrest_propellant_kg", after_arrest), ("support_reserve_kg", reserve),
                          ("support_margin_kg", after_arrest-reserve)):
        _require(_near(budget.get(key), expected, 1e-6),
                 "short_prediction", "Ideal fuel budget differs from its declared rocket equation and support reserve")
    arrest = item.get("ideal_arrest_preview")
    _require(type(arrest) is dict and arrest.get("schema") == "missionos.starship_ideal_arrest_preview.v1"
             and _same_value(arrest.get("input_position_enu_m"), prediction["position_enu_m"])
             and _same_value(arrest.get("input_velocity_enu_mps"), prediction["velocity_enu_mps"])
             and _near(arrest.get("input_propellant_kg"), fuel)
             and arrest.get("engine_availability") == [engine["available"] for engine in forecast["final_state"]["engine_states"][:13]]
             and _vector(arrest.get("position_enu_m")) and _vector(arrest.get("velocity_enu_mps"))
             and _number(arrest.get("propellant_kg")) and 0 <= arrest["propellant_kg"] <= fuel
             and _near(arrest.get("consumed_propellant_kg"), fuel-arrest["propellant_kg"])
             and _number(arrest.get("elapsed_s")) and 0 <= arrest["elapsed_s"] <= 60.+1e-7
             and type(arrest.get("integration_steps")) is int and 0 <= arrest["integration_steps"] <= 600
             and _near(arrest["elapsed_s"], arrest["integration_steps"]*.1, 1e-6)
             and arrest.get("gravity_integrated") is True and arrest.get("finite_throttle_spool_integrated") is True
             and all(arrest.get(key) is False for key in ("attitude_dynamics_integrated", "aerodynamic_force_integrated",
                 "prediction_is_execution", "arrival_admitted", "support_admitted")),
             "short_prediction", "Ideal aligned arrest must retain its bounded input, availability, fuel and explicit omitted physics")
    remaining = arrest["propellant_kg"]
    com = (dry*profile["booster"]["dry_com_z_m"]+remaining*profile["booster"]["tank_center_z_m"])/(dry+remaining)
    clearance = catch_config["initial_pin_clearance_m"]+.5*catch_config["arm_half_width_m"]
    floor = max(catch_config["support_height_m"]+clearance-point[2]+com for point in catch_config["support_points_body_m"])
    reserve_after_preview = (dry+remaining)*9.81/exhaust*horizon
    _require(_near(arrest.get("required_capture_cg_height_m"), floor)
             and _near(arrest.get("target_pin_clearance_m"), clearance)
             and _near(arrest.get("support_reserve_kg"), reserve_after_preview)
             and _near(arrest.get("support_margin_kg"), remaining-reserve_after_preview)
             and type(arrest.get("velocity_arrested_above_capture_floor")) is bool,
             "short_prediction", "Ideal arrest capture floor or support budget differs from the unchanged geometry and mass")
    reached = arrest["velocity_arrested_above_capture_floor"]
    reason = arrest.get("termination")
    if reached:
        _require(reason == "velocity_arrested_above_capture_floor" and _norm(arrest["velocity_enu_mps"]) <= 2.
                 and arrest["position_enu_m"][2] >= floor,
                 "short_prediction", "Arrest at or below the capture height cannot become a viable capture-entry prediction")
    elif reason == "capture_height_crossed":
        _require(arrest["position_enu_m"][2] < floor, "short_prediction", "Capture-floor failure lacks the actual preview height")
    elif reason == "propellant_exhausted":
        _require(remaining <= 0., "short_prediction", "Fuel exhaustion lacks depleted preview propellant")
    else:
        _require(reason == "time_limit" and _near(arrest["elapsed_s"], 60.),
                 "short_prediction", "Incomplete arrest preview lacks its bounded stop reason")


def _short_prediction(plan, origin, profile, catch_config=None, *, policy=None):
    """Audit bounded forecast records, not the simulator or solver execution."""
    receipt, seed = plan.get("actual_dynamics_prediction"), plan.get("seed_point_plan")
    schema = receipt.get("schema") if type(receipt) is dict else None
    transport = schema == "missionos.starship_short_boostback_shooting.v4"
    intercept = schema in ("missionos.starship_short_boostback_shooting.v3", "missionos.starship_short_boostback_shooting.v4")
    preparation = schema in ("missionos.starship_short_boostback_shooting.v2", "missionos.starship_short_boostback_shooting.v3", "missionos.starship_short_boostback_shooting.v4")
    specification = _SHORT_FORECAST_V3 if intercept else _SHORT_FORECAST_V2 if preparation else _SHORT_FORECAST
    if transport:
        specification = {**_SHORT_FORECAST_V3, "prepare_roll_policy": "parallel_transport_deferred_geographic_roll_v1",
            "post_cutoff_control": "powered_upright_prepare_with_transport_roll",
            "shutdown_tail_model": "four_max_main_throttle_tau_command_off_transport",
            "shutdown_tail_s": 4*profile["actuators"]["throttle_tau_s"]}
    _require(type(receipt) is dict and schema in ("missionos.starship_short_boostback_shooting.v1",
             "missionos.starship_short_boostback_shooting.v2", "missionos.starship_short_boostback_shooting.v3", "missionos.starship_short_boostback_shooting.v4")
             and (policy is None or intercept is (policy in (_INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY))
                  and preparation is (policy in (_PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY))
                  and transport is (policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY)))
             and _same_value(receipt.get("configuration"), specification)
             and type(seed) is dict and seed.get("prediction_is_execution") is False,
             "short_prediction", "Missing frozen short-forecast development contract")
    _require(receipt.get("development_transport_prepare_roll") is True if transport
             else "development_transport_prepare_roll" not in receipt,
             "short_prediction", "Transport preparation needs its explicit versioned forecast opt-in")
    _require(all(receipt.get(key) is False for key in ("prediction_is_execution", "actual_state_assigned",
                 "production_policy_admitted", "support_admitted", "physical_execution", "missionos_dispatch"))
             and plan.get("admissible") is False,
             "claim_boundary", "Finite predictions cannot grant admission or terminal support")
    _same_state(origin, receipt.get("input_state"), "short_prediction")
    _require(receipt.get("input_state_sha256") == _digest(receipt["input_state"])
             and receipt.get("seed_point_plan_sha256") == _digest(seed),
             "short_prediction", "Forecast input or seed lineage hash differs")
    _reference_record(receipt.get("input_reference"), origin, profile)
    for key, seed_key in (("endpoint_target_velocity_enu_mps", "point_post_shutdown_velocity_enu_mps"),
                          ("endpoint_target_position_enu_m", "point_post_shutdown_position_enu_m")):
        _require(_vector(receipt.get(key)) and _same_value(receipt[key], seed.get(seed_key)),
                 "short_prediction", "Forecast target must come from the frozen point seed, not future observations")
    _require(_vector(seed.get("burn_axis_enu")) and _near(_norm(seed["burn_axis_enu"]), 1., 1e-8)
             and _vector(seed.get("target_velocity_enu_mps")), "short_prediction", "Invalid original finite command seed")
    _require(_same_value(plan.get("bank_heading_enu"), seed.get("bank_heading_enu"))
             and _same_value(plan.get("bank_sign"), seed.get("bank_sign")),
             "short_prediction", "Finite shooting changed the held point-plan bank reference")
    entries = receipt.get("forecasts")
    attempted, completed, failed = (receipt.get(key) for key in
                                   ("attempted_forecast_count", "completed_forecast_count", "failed_forecast_count"))
    _require(type(entries) is list and all(type(x) is int for x in (attempted, completed, failed))
             and 1 <= attempted <= 48 and completed >= 1 and failed >= 0
             and attempted == len(entries) == completed+failed,
             "short_prediction", "Forecast failures and derivative calls must remain inside the attempted-call budget")
    actual_completed, valid = 0, []
    point_attempted, point_completed, point_failed = 0, 0, 0
    for index, entry in enumerate(entries, 1):
        _require(type(entry) is dict and entry.get("attempt_index") == index
                 and _vector(entry.get("parameters"))
                 and all(abs(x) <= math.radians(15.)+1e-8 for x in entry["parameters"][:2])
                 and abs(entry["parameters"][2]) <= 300.+1e-8,
                 "short_prediction", "Forecast parameter request exceeds the declared bounded command family")
        direction, target = _parameter_candidate(seed, entry["parameters"])
        candidate = entry.get("candidate")
        _require(type(candidate) is dict, "short_prediction", "Missing bounded forecast command")
        _compare_vector(candidate.get("burn_axis_enu"), direction, "short_prediction")
        _compare_vector(candidate.get("target_velocity_enu_mps"), target, "short_prediction")
        if entry.get("status") == "failed":
            _require(type(entry.get("error_type")) is str and bool(entry["error_type"])
                     and "forecast" not in entry, "short_prediction", "Failed forecast cannot masquerade as a returned prediction")
            continue
        _require(entry.get("status") == "completed", "short_prediction", "Unknown forecast completion status")
        actual_completed += 1
        forecast = entry.get("forecast")
        _require(type(forecast) is dict and type(forecast.get("prediction_complete")) is bool
                 and forecast["prediction_complete"] is (forecast.get("termination") == "shutdown_tail_complete")
                 and forecast.get("termination") in ("shutdown_tail_complete", "time_limit", "rate_settle_failed", "upright_preparation_failed",
                     "angular_rate_envelope_exceeded", "outside_short_forecast_altitude_envelope")
                 and all(forecast.get(key) is False for key in ("prediction_is_execution", "actual_state_assigned",
                     "production_policy_admitted", "arrival_forecast", "support_forecast")),
                 "short_prediction", "A short finite prediction cannot claim arrival or support")
        if transport:
            _require(forecast.get("development_transport_prepare_roll") is True
                     and forecast.get("prepare_roll_policy") == "parallel_transport_deferred_geographic_roll_v1"
                     and forecast.get("shutdown_tail_model") == "four_max_main_throttle_tau_command_off_transport"
                     and _near(forecast.get("shutdown_tail_s"), specification["shutdown_tail_s"]),
                     "short_prediction", "A transport forecast cannot retain an undeclared legacy spool/roll law")
        else:
            _require(all(key not in forecast for key in ("development_transport_prepare_roll", "prepare_roll_policy", "shutdown_tail_model")),
                     "short_prediction", "New prepare-roll fields cannot relabel an old finite prediction")
        _same_state(origin, forecast.get("input_state"), "short_prediction")
        _require(_same_value(forecast.get("input_reference"), receipt["input_reference"]),
                 "short_prediction", "Forecasts did not inherit the same reference history")
        final = forecast.get("final_state")
        _state(final, profile)
        duration, steps = forecast.get("duration_s"), forecast.get("integration_steps")
        _require(_number(duration) and 0 <= duration <= 100.+1e-8
                 and _near(duration, final["time_s"]-origin["time_s"])
                 and type(steps) is int and 0 <= steps <= (100001 if preparation else 1001) and duration <= steps*.25+1e-7
                 and all(before["available"] is after["available"]
                         for before, after in zip(origin["engine_states"], final["engine_states"]))
                 and _near(forecast.get("propellant_kg"), final["propellant_kg"])
                 and final["propellant_kg"] <= origin["propellant_kg"]+1e-7
                 and _vector(forecast.get("position_enu_m")) and _vector(forecast.get("velocity_enu_mps")),
                 "short_prediction", "Forecast clock, step count, inherited fault or fuel summary differs")
        _reference_record(forecast.get("final_reference"), final, profile)
        _require(forecast["final_reference"]["time_s"] >= receipt["input_reference"]["time_s"],
                 "short_prediction", "Forecast reference clock moved backwards")
        _compare_vector(forecast["velocity_enu_mps"], _local_velocity(final), "short_prediction")
        _compare_vector(forecast["position_enu_m"], _prediction_origin(final, profile), "short_prediction", tolerance=1e-4)
        if preparation:
            _short_preparation_events(forecast, candidate, origin, profile, transport=transport)
        if intercept:
            continuation = entry.get("point_continuation")
            if forecast["prediction_complete"]:
                point_attempted += 1
                _require(entry.get("point_status") in ("completed", "failed"),
                         "short_prediction", "Post-prepare endpoint must retain its single point-continuation outcome")
                if entry["point_status"] == "completed":
                    point_completed += 1
                    _point_continuation(continuation, forecast, seed, profile, catch_config)
                else:
                    point_failed += 1
                    _require(continuation is None and type(entry.get("point_error_type")) is str
                             and bool(entry["point_error_type"]),
                             "short_prediction", "Failed point continuation lacks preserved failure evidence")
            else:
                _require(entry.get("point_status") == "not_attempted_incomplete_short_forecast" and continuation is None,
                         "short_prediction", "Incomplete powered preparation cannot be replaced by an ideal coast")
        _require(_vector(entry.get("residual"), 9) and _number(entry.get("objective"))
                 and _near(entry["objective"], sum(x*x for x in entry["residual"]), 1e-6*max(1., entry["objective"])),
                 "short_prediction", "Forecast scalar score differs from its recorded residuals")
        if catch_config is not None:
            _compare_vector(entry["residual"], _forecast_residual(forecast, receipt, profile, catch_config), "short_prediction")
        valid.append(entry)
    _require(actual_completed == completed, "short_prediction", "Returned/failed forecast counters differ from the retained attempts")
    if intercept:
        _require(receipt.get("point_continuation_is_execution") is False
                 and all(type(receipt.get(key)) is int for key in ("point_continuation_attempted_count",
                     "point_continuation_completed_count", "point_continuation_failed_count"))
                 and receipt["point_continuation_attempted_count"] == point_attempted == point_completed+point_failed
                 and receipt["point_continuation_completed_count"] == point_completed
                 and receipt["point_continuation_failed_count"] == point_failed and point_attempted <= completed,
                 "short_prediction", "Point continuation counters exceed one per completed post-prepare short forecast")
    complete = [entry for entry in valid if entry["forecast"]["prediction_complete"]
                and (not intercept or entry["point_status"] == "completed"
                     and entry["point_continuation"]["prediction"]["reached_terminal_altitude"]
                     and entry["point_continuation"]["ideal_arrest_preview"]["velocity_arrested_above_capture_floor"])]
    selected = min(complete or valid, key=lambda entry: entry["objective"])
    _require(receipt.get("selected_attempt_index") == selected["attempt_index"]
             and _same_value(receipt.get("selected_forecast"), selected["forecast"])
             and receipt.get("selected_forecast_complete") is selected["forecast"]["prediction_complete"]
             and _near(receipt.get("selected_objective"), selected["objective"])
             and _same_value(plan.get("burn_axis_enu"), selected["candidate"]["burn_axis_enu"])
             and _same_value(plan.get("target_velocity_enu_mps"), selected["candidate"]["target_velocity_enu_mps"]),
             "short_prediction", "Selected command is not the retained finite forecast with the declared score priority")
    if intercept:
        bound = [math.radians(15.), math.radians(15.), 300.]
        _require(_same_value(receipt.get("selected_point_continuation"), selected["point_continuation"])
                 and receipt.get("selected_point_continuation_complete") is (selected in complete)
                 and receipt.get("parameter_bound_active") == [abs(abs(selected["parameters"][i])-bound[i]) <= 1e-6 for i in range(3)]
                 and receipt.get("global_infeasibility_established") is False,
                 "short_prediction", "Local parameter bounds or point forecast cannot establish global feasibility or infeasibility")
    _require(receipt.get("optimizer_status") in ("solver_stopped", "forecast_budget_exhausted", "solver_error")
             and type(receipt.get("solver_success")) is bool
             and (receipt.get("solver_nfev") is None or type(receipt["solver_nfev"]) is int and 1 <= receipt["solver_nfev"] <= 12),
             "short_prediction", "Invalid bounded local solver termination receipt")
    if receipt["optimizer_status"] == "forecast_budget_exhausted":
        _require(attempted == 48 and receipt["solver_success"] is False,
                 "short_prediction", "Budget exhaustion did not consume the declared attempted-call cap")


def _local_axes(state):
    """Ellipsoid-normal frame at the measured vehicle, not tower-axis velocity."""
    x, y, z = state["r_eci_m"]
    radial = math.hypot(x, y)
    lat = math.atan2(z, radial*(1-_E2))
    for _ in range(12):
        radius = _A/math.sqrt(1-_E2*math.sin(lat)**2)
        lat = math.atan2(z+_E2*radius*math.sin(lat), radial)
    lon = math.atan2(y, x)
    return ([-math.sin(lon), math.cos(lon), 0.],
            [-math.sin(lat)*math.cos(lon), -math.sin(lat)*math.sin(lon), math.cos(lat)],
            [math.cos(lat)*math.cos(lon), math.cos(lat)*math.sin(lon), math.sin(lat)])
def _local_velocity(state):
    axes = _local_axes(state)
    x, y, _ = state["r_eci_m"]
    v = state["v_eci_mps"]
    relative = [v[0]+_ROTATION*y, v[1]-_ROTATION*x, v[2]]
    return [sum(a*b for a, b in zip(relative, axis)) for axis in axes]


def _local_tilt(state):
    body_up = _rotate(state["q_body_to_eci"], [0., 0., 1.])
    up = _local_axes(state)[2]
    return math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(body_up, up))))))


def _short_preparation_events(forecast, candidate, origin, profile, *, transport=False):
    events, end = forecast.get("events"), forecast["final_state"]["time_s"]
    _require(type(events) is list and len(events) <= (4 if transport else 3), "short_prediction", "Invalid bounded preparation event history")
    previous, cutoff, prepared, tail = origin["time_s"], None, None, None
    for event in events:
        _require(type(event) is dict and event.get("event") in (("ignition", "cutoff", "upright_prepared", "transport_shutdown_tail_complete")
                 if transport else ("ignition", "cutoff", "upright_prepared"))
                 and _number(event.get("time_s")) and previous <= event["time_s"] <= end,
                 "short_prediction", "Preparation event is unknown, unordered or out of range")
        previous = event["time_s"]
        state = event.get("state")
        _state(state, profile)
        _require(state["time_s"] == event["time_s"], "short_prediction", "Preparation event does not carry its actual clock")
        if event["event"] == "cutoff":
            _require(cutoff is None, "short_prediction", "Short forecast duplicated its cutoff")
            cutoff = event
        if event["event"] == "upright_prepared":
            _require(prepared is None and cutoff is not None
                     and _local_tilt(state) <= 5. and _norm(state["omega_body_rad_s"]) < .003
                     and _near(event.get("actual_tilt_deg"), _local_tilt(state), 1e-5)
                     and _near(event.get("actual_body_rate_rad_s"), _norm(state["omega_body_rad_s"])),
                     "short_prediction", "Upright preparation did not reach its actual angle and rate bounds")
            prepared = event
        if event["event"] == "transport_shutdown_tail_complete":
            duration = 4*profile["actuators"]["throttle_tau_s"]
            _require(tail is None and prepared is not None and event["geographic_roll_deferred"] is True
                     and event["time_s"] >= prepared["time_s"]+duration-1e-8
                     and _near(event.get("command_off_elapsed_s"), event["time_s"]-prepared["time_s"])
                     and _near(event.get("minimum_tail_s"), duration)
                     and _local_tilt(state) <= 5. and _norm(state["omega_body_rad_s"]) < .003
                     and _near(event.get("actual_tilt_deg"), _local_tilt(state), 1e-5)
                     and _near(event.get("actual_body_rate_rad_s"), _norm(state["omega_body_rad_s"])),
                     "short_prediction", "Short transport tail skipped its actual readiness or minimum finite shutdown duration")
            tail = event
    if forecast["prediction_complete"]:
        _require(prepared is not None and forecast.get("shutdown_start_time_s") == prepared["time_s"]
                 and (tail is not None and tail["time_s"] == end if transport else _near(end-prepared["time_s"], 1.4)),
                 "short_prediction", "Completed preparation forecast lacks its actual gate and finite shutdown duration")
        if transport:
            _same_state(tail["state"], forecast["final_state"], "short_prediction")
    if forecast["termination"] == "upright_preparation_failed":
        maximum = profile.get("booster_return", {}).get("boostback_max_slew_s", 90.)
        final = forecast["final_state"]
        missing_tail = transport and prepared is not None and end-prepared["time_s"] < 4*profile["actuators"]["throttle_tau_s"]-1e-9
        _require(cutoff is not None and (prepared is None or transport and tail is None) and _number(maximum) and maximum > 0
                 and maximum < end-cutoff["time_s"] <= maximum+.25000001
                 and (_local_tilt(final) > 5. or _norm(final["omega_body_rad_s"]) >= .003 or missing_tail),
                 "short_prediction", "Preparation failure lacks its measured angle/rate and configured timeout")


def _cutoff_evidence(record, events, profile, final_phase):
    cutoff = [event for event in events if event["event"] == "constrained_boostback_cutoff"]
    phases = _phases(record.get("policy_id", _POLICY))
    _require(len(cutoff) <= 1 and (len(cutoff) == 1 if phases.index(final_phase) >= 2 else True),
             "cutoff", "Post-boostback trajectory requires exactly one measured cutoff")
    starts = [event for event in events if event["event"] == "boostback_ignition"]
    _require(len(starts) <= 1, "cutoff", "Boostback ignition must not be duplicated")
    supplied = profile.get("booster_return", {})
    _require(type(supplied) is dict, "configuration", "Invalid return guard configuration")
    reserve, maximum = supplied.get("landing_reserve_kg", 60000.), supplied.get("boostback_max_burn_s", 70.)
    _require(_number(reserve) and reserve > 0 and _number(maximum) and maximum > 0,
             "configuration", "Guard thresholds must be finite and positive")
    for event in cutoff:
        basis = event.get("cutoff_basis")
        impulse = record.get("policy_id", _POLICY) in (_IMPULSE_POLICY, _SHOOTING_POLICY, _PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY)
        _require(basis in (("fuel_guard", "time_guard", "velocity_target", "along_axis_impulse")
                          if impulse else ("fuel_guard", "time_guard", "velocity_target")),
                 "cutoff", "Unknown measured cutoff condition")
        state = event["state"]
        plans = [plan for plan in record["plans"] if plan["time_s"] <= event["time_s"]]
        _require(plans, "cutoff", "Cutoff precedes a local command plan")
        target, velocity = plans[-1]["plan"]["target_velocity_enu_mps"], _local_velocity(state)
        error = _norm([target[i]-velocity[i] for i in range(3)])
        _require(_near(event.get("velocity_error_mps"), error, 1e-6),
                 "cutoff", "Cutoff velocity error differs from the actual state and latest command plan")
        projection = None
        if basis == "along_axis_impulse" or "along_axis_velocity_error_mps" in event:
            _require(impulse, "cutoff", "Along-axis impulse measurements are outside the v1 contract")
            axis = plans[-1]["plan"].get("burn_axis_enu")
            _require(_vector(axis) and _near(_norm(axis), 1., 1e-8),
                     "cutoff", "Along-axis cutoff requires the latest planned finite unit burn axis")
            projection = sum((target[i]-velocity[i])*axis[i] for i in range(3))
            _require(_near(event.get("along_axis_velocity_error_mps"), projection, 1e-6),
                     "cutoff", "Recorded signed impulse residual differs from actual velocity and planned axis")
        if basis == "fuel_guard":
            _require(state["propellant_kg"] <= reserve, "cutoff", "Fuel guard did not reach its declared threshold")
        else:
            _require(len(starts) == 1 and starts[0]["time_s"] <= event["time_s"],
                     "cutoff", "Velocity/time cutoff requires a preceding actual ignition")
            if basis == "along_axis_impulse":
                _require(projection <= _GUIDANCE["velocity_tolerance_mps"],
                         "cutoff", "Actual signed impulse residual did not reach the commanded tolerance")
            elif basis == "velocity_target":
                _require(error <= _GUIDANCE["velocity_tolerance_mps"],
                         "cutoff", "Actual velocity did not reach its commanded tolerance")
            else:
                _require(event["time_s"]-starts[0]["time_s"] >= maximum,
                         "cutoff", "Elapsed burn time did not reach the guard threshold")


def _main_pair(checkpoint, profile, policy):
    selected = checkpoint["navigation"].get("selected_main_engine_indices")
    if selected is None:
        return
    command, state = checkpoint.get("command"), checkpoint["state"]
    _require(policy in (_SHOOTING_POLICY, _PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) and checkpoint["phase"].startswith("recovery_landing_")
             and command is not None and type(selected) is list and len(selected) == 2
             and all(type(i) is int and 0 <= i < 13 for i in selected) and selected[0] < selected[1]
             and selected == [i for i, engine in enumerate(command["engines"][:33]) if engine["enabled"]]
             and all(state["engine_states"][i]["available"] for i in selected),
             "main_pair", "Opposing-pair receipt differs from actual bounded available main-engine commands")
    positions = profile["booster"]["engine_positions_body_m"]
    a, b = (positions[i] for i in selected)
    _require(_vector(a) and _vector(b) and math.hypot(a[0]+b[0], a[1]+b[1]) <= 1e-8
             and _near(command["engines"][selected[0]]["throttle"], command["engines"][selected[1]]["throttle"], 1e-9),
             "main_pair", "Selected gimballed pair is not geometrically opposed at equal requested thrust")


def _macrostep_record(checkpoint, record, profile, deadline):
    if not checkpoint["phase"].startswith("recovery_boostback") or checkpoint.get("command") is None:
        return
    receipt = checkpoint["navigation"].get("boostback_macrostep")
    _require(type(receipt) is dict and receipt.get("schema") == "missionos.starship_boostback_macro_step.v1"
             and all(receipt.get(key) is False for key in ("actual_state_assigned", "velocity_clamped", "prediction_is_execution"))
             and receipt.get("minimum_step_s") == .001 and type(receipt.get("geographic_transport_available")) is bool,
             "macrostep_density", "Missing scoped adaptive integration request; physical state must not be clamped")
    state = checkpoint["state"]
    plans = [item["plan"] for item in record["plans"] if item["time_s"] <= state["time_s"]]
    _require(plans, "macrostep_density", "Adaptive integration precedes its measured command plan")
    plan, velocity = plans[-1], _local_velocity(state)
    remaining = sum((plan["target_velocity_enu_mps"][i]-velocity[i])*plan["burn_axis_enu"][i] for i in range(3))
    acceleration = receipt.get("achieved_along_axis_acceleration_mps2")
    maximum = min(.1, deadline-state["time_s"])
    _require(_number(acceleration) and _near(receipt.get("maximum_step_s"), maximum, 1e-8)
             and _near(receipt.get("along_axis_velocity_error_mps"), remaining, 1e-6),
             "macrostep_density", "Adaptive request differs from the actual velocity, current plan or remaining deadline")
    expected = maximum
    if receipt["geographic_transport_available"] and remaining > 12. and acceleration > 0:
        expected = min(maximum, max(.001, (remaining-12.)/acceleration))
    _require(_near(receipt.get("step_s"), expected, 1e-9),
             "macrostep_density", "Adaptive step does not follow the declared finite current-acceleration estimate")
    # The acceleration is an observed producer field, not independently
    # certified force/aerodynamic dynamics. Source binding remains required.


def _terminal_hold(checkpoint, profile, catch_config, policy):
    receipt = checkpoint["navigation"].get("terminal_corridor_hold")
    if policy not in (_INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) or not checkpoint["phase"].startswith("recovery_landing_") or checkpoint.get("command") is None:
        _require(receipt is None, "terminal_corridor", "Terminal hold receipt escaped its declared policy/command phase")
        return
    state = checkpoint["state"]
    actual = _arrival(state, profile, catch_config)
    _observe_handoff(checkpoint["navigation"].get("arrival_observation"), actual, state, catch_config)
    limits = actual["limits"]
    ready = (math.hypot(*actual["midpoint_enu_m"][:2]) <= limits["horizontal_position_m"]
             and all(math.hypot(*pin["velocity_enu_mps"][:2]) <= limits["pin_horizontal_speed_mps"] for pin in actual["pins"])
             and actual["tilt_deg"] <= limits["attitude_angle_deg"]
             and actual["clocking_error_deg"] <= limits["attitude_angle_deg"]
             and actual["body_rate_rad_s"] <= limits["body_rate_rad_s"]
             and state["propellant_kg"] >= limits["propellant_reserve_kg"])
    required = (min(pin["height_above_support_m"] for pin in actual["pins"]) < 100. and not ready
                and state["propellant_kg"] >= limits["propellant_reserve_kg"])
    if not required:
        _require(receipt is None, "terminal_corridor", "Holding is not supported by the current material-point constraints and fuel")
        return
    _require(type(receipt) is dict and set(receipt) == {"active", "target_pin_clearance_m", "observed_nonvertical_ready", "state_assigned"}
             and receipt["active"] is True and receipt["observed_nonvertical_ready"] is False
             and receipt["state_assigned"] is False
             and _near(receipt["target_pin_clearance_m"], catch_config["initial_pin_clearance_m"]+2*catch_config["arm_half_width_m"]),
             "terminal_corridor", "Physical hold request must retain its fresh predicate and unchanged target above the arrival window")


def _pretrim_protocol(checkpoint, sample, profile, policy, latch, remaining_s):
    """Carry only a proven latest low-q governed-current-flow observation."""
    navigation, state = checkpoint["navigation"], checkpoint["state"]
    if policy not in (_PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
        _require("entry_pretrim" not in navigation and "entry_pretrim_prepared_at_s" not in navigation,
                 "entry_pretrim", "Entry-preparation mode requires the explicit v7/v8 contract")
        return latch, "finite_moment_priority_fins_v1" if policy == _MOMENT_POLICY else "finite_regularized_fins_v1"
    if checkpoint.get("command") is None:
        _require(navigation.get("entry_pretrim") is None, "entry_pretrim", "A terminal observation cannot claim another pretrim command")
        return latch, "finite_moment_priority_fins_v1" if latch is not None else "finite_regularized_fins_v1"
    _require(_number(sample.get("dynamic_pressure_pa")) and sample["dynamic_pressure_pa"] >= 0,
             "entry_pretrim", "Preparation policy requires its finite current sampled dynamic pressure")
    low_q_coast = checkpoint["phase"] == "recovery_entry_coast" and sample.get("dynamic_pressure_pa", 0) <= 100.
    if low_q_coast:
        from .starship_entry_pretrim_verifier import verify_pretrim_checkpoint
        trim = navigation.get("predicted_trim_flap_angles_rad")
        axis = navigation.get("governed_entry_axis_enu")
        current_flow = _local_velocity(state)[2] < 0 and axis is not None and trim is not None
        expected_kind = ("governed_current_flow_trim" if current_flow else
                         "future_descending_entry_prediction" if trim is not None else "missing_reference")
        _require(navigation.get("entry_pretrim_reference_kind") == expected_kind,
                 "entry_pretrim", "Preparation reference cannot substitute an ascent/future trim for the actual governed descent goal")
        _require(trim is not None or navigation.get("entry_pretrim", {}).get("reference_validated_for_shape_limits_only") is False,
                 "entry_pretrim", "A missing driver trim reference cannot be replaced by an unattached fin goal")
        if current_flow:
            _require(_vector(axis) and _near(_norm(axis), 1., 1e-8),
                     "entry_pretrim", "Governed current-flow trim requires its finite commanded entry axis")
        prepared = verify_pretrim_checkpoint(checkpoint, sample, profile, expected_trim_angles=trim, remaining_s=remaining_s)
        # Each low-q observation replaces the older latch, including failure.
        latch = state["time_s"] if current_flow and prepared else None
    else:
        _require(navigation.get("entry_pretrim") is None, "entry_pretrim", "Pretrim may execute only in low-q coast")
        if latch is not None:
            _require(latch < state["time_s"], "entry_pretrim", "High-q priority requires a previously observed low-q prepared state")
    _require("entry_pretrim_prepared_at_s" in navigation
             and _same_value(navigation["entry_pretrim_prepared_at_s"], latch),
             "entry_pretrim", "Latched prepared time is not the latest accepted actual governed-flow checkpoint")
    return latch, "finite_moment_priority_fins_v1" if latch is not None else "finite_regularized_fins_v1"


def _reference_protocol(checkpoint, sample, profile, policy, previous, *, checkpoint_index=None):
    navigation = checkpoint["navigation"]
    if policy not in (_TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
        _require("reference_tracking" not in navigation and "reference_tracking_control" not in navigation,
                 "reference_tracking", "Reference tracking requires the explicit v8 development policy")
        return
    from .starship_reference_tracking_verifier import verify_reference_checkpoint
    receipt = verify_reference_checkpoint(checkpoint, sample, profile, previous_checkpoint=previous,
        checkpoint_index=checkpoint_index, initial_previous_phase="recovery_entry_shutdown_tail"
        if policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) else "recovery_powered_entry_prepare")
    axis = navigation.get("governed_entry_axis_enu")
    if receipt is not None and checkpoint["phase"] == "recovery_entry_coast" and axis is not None:
        body_up = _rotate(receipt["requested_q_body_to_eci"], [0., 0., 1.])
        expected = [sum(a*b for a, b in zip(body_up, basis)) for basis in _local_axes(checkpoint["state"])]
        _compare_vector(axis, expected, "reference_tracking", tolerance=1e-8)


def _transport_preparation(checkpoint, profile, policy, previous, events):
    navigation, state, phase = checkpoint["navigation"], checkpoint["state"], checkpoint["phase"]
    if policy not in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
        _require("prepare_attitude_reference" not in navigation and "prepare_shutdown_tail" not in navigation,
                 "prepare_transport", "Deferred prepare roll requires the explicit v10 physical policy")
        return
    if checkpoint.get("command") is None:
        _require("prepare_attitude_reference" not in navigation and "prepare_shutdown_tail" not in navigation,
                 "prepare_transport", "Terminal observation cannot claim another prepare request")
        return
    if phase not in ("recovery_powered_entry_prepare", "recovery_entry_shutdown_tail"):
        _require("prepare_attitude_reference" not in navigation and "prepare_shutdown_tail" not in navigation,
                 "prepare_transport", "Deferred geographic roll escaped the powered-prepare/shutdown scope")
        return
    from .starship_reference_tracking_verifier import _multiply
    item = navigation.get("prepare_attitude_reference")
    _require(type(item) is dict and item.get("policy_id") == "conditioned_geographic_v1"
             and item.get("mode") == "transport_deferred_geographic_roll" and item.get("geographic_roll_deferred") is True
             and item.get("roll_step_rad") == 0. and item.get("roll_error_rad") == 0.
             and item.get("minimum_reference_projection") == .1
             and _near(item.get("maximum_roll_rate_rad_s"), profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]),
             "prepare_transport", "Prepare must transport its request without geographic roll or a new physical gain")
    prior_q = previous["navigation"].get("target_q_body_to_eci") if previous is not None else state["q_body_to_eci"]
    _require(_vector(prior_q, 4) and _near(_norm(prior_q), 1., 1e-8), "prepare_transport", "Missing immediately preceding request frame")
    prior_up, wanted = _rotate(prior_q, [0., 0., 1.]), _local_axes(state)[2]
    axis = _cross(prior_up, wanted)
    sine, cosine = _norm(axis), max(-1., min(1., sum(a*b for a, b in zip(prior_up, wanted))))
    angle = math.atan2(sine, cosine)
    antipodal = sine < 1e-12 and cosine < 0
    if antipodal:
        increment = [0., *_rotate(prior_q, [1., 0., 0.])]
    elif sine < 1e-12:
        increment = [1., 0., 0., 0.]
    else:
        increment = [math.cos(angle/2), *(x/sine*math.sin(angle/2) for x in axis)]
    target = _multiply(increment, prior_q)
    length = _norm(target)
    target = [x/length for x in target]
    if sum(a*b for a, b in zip(target, prior_q)) < 0:
        target = [-x for x in target]
    _compare_vector(item.get("target_q_body_to_eci"), target, "prepare_transport", tolerance=1e-8)
    _compare_vector(navigation.get("target_q_body_to_eci"), target, "prepare_transport", tolerance=1e-8)
    elapsed = state["time_s"]-(previous["time_s"] if previous is not None else state["time_s"])
    _require(_near(item.get("axis_step_rad"), angle, 1e-8) and item.get("antipodal_fallback") is antipodal
             and _near(item.get("reference_projection_norm"), 1., 1e-8) and _near(item.get("elapsed_s"), elapsed, 1e-8),
             "prepare_transport", "Prepare frame geometry or reference clock differs from its adjacent actual request")
    if phase == "recovery_entry_shutdown_tail":
        tail = navigation.get("prepare_shutdown_tail")
        starts = [event for event in events if event.get("event") == "powered_coast_attitude_prepared"
                  and event.get("time_s", math.inf) <= state["time_s"]]
        duration = 4*profile["actuators"]["throttle_tau_s"]
        _require(len(starts) == 1 and type(tail) is dict and set(tail) == {"started_at_s", "duration_s", "elapsed_s", "main_commands_off", "actual_throttle_assigned"}
                 and tail["started_at_s"] == starts[0]["time_s"] and _near(tail["duration_s"], duration)
                 and _near(tail["elapsed_s"], state["time_s"]-starts[0]["time_s"])
                 and tail["main_commands_off"] is True and tail["actual_throttle_assigned"] is False
                 and not any(engine["enabled"] or engine["throttle"] != 0 for engine in checkpoint["command"]["engines"][:33]),
                 "prepare_transport", "Shutdown tail needs a measured start clock and finite command-off actuator evolution")
    else:
        _require("prepare_shutdown_tail" not in navigation, "prepare_transport", "Powered preparation cannot masquerade as command-off tail")


def _events(run, record, outcome, checkpoints, profile, catch_config, start, end):
    events = run.get("events")
    _require(type(events) is list and 2 <= len(events) <= 10000,
             "events", "Missing bounded event sequence")
    frozen = record["policy_id"] == _LANDING_POLICY
    physical_policy = _TRANSPORT_POLICY if frozen else record["policy_id"]
    physical_record = {**record, "policy_id": physical_policy} if frozen else record
    allowed = _TERMINATIONS | {
        "constrained_return_start", "constrained_boostback_plan", "constrained_boostback_plan_refreshed",
        "boostback_ignition", "constrained_boostback_cutoff", "powered_rate_settled_cutoff",
        "landing_stage_requested", "powered_coast_attitude_prepared",
    }
    if record["policy_id"] in (_ACTUAL_POLICY, _ACTUAL_TRANSPORT_POLICY):
        allowed.add("actual_finite_sixdof_plan_selected")
    if record["policy_id"] in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
        allowed.add("powered_coast_shutdown_tail_complete")
    if frozen:
        allowed.update({"powered_coast_shutdown_tail_complete", "frozen_boostback_plan_applied",
            "landing_onset_decision_context_matched", "landing_onset_ignition_requested", "landing_decision_execution_exception"})
    times = [point["time_s"] for point in checkpoints]
    names, stages, previous = [], [], start
    for event in events:
        _require(type(event) is dict and _number(event.get("time_s"))
                 and previous <= event["time_s"] <= end and event.get("event") in allowed,
                 "events", "Unknown, unordered or out-of-range event")
        previous = event["time_s"]
        name = event["event"]
        names.append(name)
        _bind_event_state(event, checkpoints, times, profile)
        if name in ("landing_stage_requested", "landing_onset_ignition_requested"):
            stages.append(event.get("requested_engine_count"))
        if name == "boostback_ignition":
            _require(event.get("requested_engine_count") == 33,
                     "events", "Boostback ignition must retain the declared engine request")
    _require(names[0] == "constrained_return_start" and events[0]["time_s"] == start
             and names[-1] == outcome["termination"] and events[-1]["time_s"] == end
             and not any(name in _TERMINATIONS for name in names[:-1]),
             "events", "Start and terminal events do not bind the actual trajectory")
    _require(stages in ([], [13], [13, 5], [13, 5, 3]),
             "events", "Landing stage requests must follow 13, 5, 3 without duplicates")
    for checkpoint in checkpoints:
        if checkpoint["phase"].startswith("recovery_landing_"):
            stage = int(checkpoint["phase"].rsplit("_", 1)[1])
            _require(stage in stages and any(event["event"] in ("landing_stage_requested", "landing_onset_ignition_requested")
                     and event.get("requested_engine_count") == stage
                     and event["time_s"] <= checkpoint["time_s"] for event in events),
                     "events", "Landing command precedes its actual stage request")
    _planning(record, events, checkpoints, profile, catch_config)
    _require(names.count("constrained_boostback_plan") == 1
             and names.count("constrained_boostback_plan_refreshed") <= 1,
             "prediction_binding", "Initial command plan must exist exactly once with at most one alignment refresh")
    _cutoff_evidence(physical_record, events, profile, checkpoints[-1]["phase"])
    prepare = physical_policy in (_PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY)
    expected_event = "powered_coast_attitude_prepared" if prepare else "powered_rate_settled_cutoff"
    wrong_event = "powered_rate_settled_cutoff" if prepare else "powered_coast_attitude_prepared"
    _require(not any(event["event"] == wrong_event for event in events),
             "events", "Coast preparation event escaped its versioned policy contract")
    settled = [event for event in events if event["event"] == expected_event]
    _require(len(settled) <= 1 and (len(settled) == 1 if _phases(record["policy_id"]).index(checkpoints[-1]["phase"]) >= 3 else True),
             "events", "Coast requires one preceding measured rate-settled cutoff")
    for event in settled:
        _require(_norm(event["state"]["omega_body_rad_s"]) < _GUIDANCE["rate_settle_limit_rad_s"]
                 and any(item["event"] == "constrained_boostback_cutoff"
                         and item["time_s"] <= event["time_s"] for item in events),
                 "events", "Coast began without a measured settled rate after boostback cutoff")
        if prepare:
            _require(_local_tilt(event["state"]) <= 5. and _near(event.get("tilt_deg"), _local_tilt(event["state"]), 1e-5),
                     "events", "Coast began before actual local upright preparation reached its angle bound")
    tails = [event for event in events if event["event"] == "powered_coast_shutdown_tail_complete"]
    if physical_policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
        coast_reached = _phases(_TRANSPORT_POLICY).index(checkpoints[-1]["phase"]) >= 4
        _require(len(tails) <= 1 and (len(tails) == 1 if coast_reached else not tails),
                 "prepare_transport", "Transport-policy coast requires its measured command-off tail completion")
        for event in tails:
            state = event["state"]
            duration = 4*profile["actuators"]["throttle_tau_s"]
            _require(len(settled) == 1 and event["time_s"] >= settled[0]["time_s"]+duration-1e-8
                     and _near(event.get("duration_s"), duration) and event.get("actual_throttle_assigned") is False
                     and _local_tilt(state) <= 5. and _norm(state["omega_body_rad_s"]) < .003
                     and _near(event.get("actual_tilt_deg"), _local_tilt(state), 1e-5)
                     and _near(event.get("actual_body_rate_rad_s"), _norm(state["omega_body_rad_s"])),
                     "prepare_transport", "Tail completion cannot bypass the elapsed physical spool hold or measured final angle/rate gate")
    else:
        _require(not tails, "prepare_transport", "New tail event cannot relabel an old physical law")
    if frozen:
        _landing_events(record, events, checkpoints, profile, catch_config)


def _landing_events(record, events, checkpoints, profile, catch_config):
    from .starship_landing_decision_verifier import digest, operative_context
    from .starship_actual_recovery_planning_verifier import _validate_context
    bundle, decision = record["frozen_boostback_bundle"], record["landing_onset_decision"]
    applied = [event for event in events if event["event"] == "frozen_boostback_plan_applied"]
    refreshed = [event for event in events if event["event"] == "constrained_boostback_plan_refreshed"]
    _require(len(applied) == len(refreshed) <= 1, "landing_decision", "A frozen plan may be applied only at its one alignment refresh")
    for event in applied:
        _require(event["time_s"] == bundle["refresh_time_s"]
                 and event.get("bundle_sha256") == digest(bundle)
                 and event.get("refresh_state_sha256") == digest(event["state"]) == bundle["refresh_state_sha256"]
                 and event.get("refresh_operative_context_sha256") == bundle["refresh_operative_context_sha256"]
                 and type(event.get("runtime_short_forecast_calls")) is int and event["runtime_short_forecast_calls"] == 0
                 and type(event.get("runtime_actual_forecast_calls")) is int and event["runtime_actual_forecast_calls"] == 0,
                 "landing_decision", "Frozen refresh state/context or zero-call receipt is detached")
    matched = [event for event in events if event["event"] == "landing_onset_decision_context_matched"]
    ignition = [event for event in events if event["event"] == "landing_onset_ignition_requested"]
    _require(len(matched) <= 1 and len(ignition) <= 1 and (not ignition or matched),
             "landing_decision", "A scheduled request requires one preceding live context match")
    times = [point["time_s"] for point in checkpoints]
    for event in matched:
        snapshot = event.get("actual_controller_context")
        _validate_context(snapshot, event["state"], profile, catch_config, _CONFIGURATIONS[_TRANSPORT_POLICY])
        _require(event["time_s"] == decision["origin_time_s"] and snapshot["context"]["phase"] == "recovery_entry_coast"
                 and event.get("decision_sha256") == digest(decision)
                 and event.get("origin_state_sha256") == digest(event["state"]) == decision["origin_state_sha256"]
                 and event.get("origin_operative_context_sha256") == digest(operative_context(snapshot)) == decision["origin_operative_context_sha256"]
                 and type(event.get("runtime_preview_calls")) is int and event["runtime_preview_calls"] == 0
                 and event.get("prediction_is_execution") is False,
                 "landing_decision", "Live decision match requires its actual capsule and same-time finite state")
        index = bisect_left(times, event["time_s"])
        _require(index > 0 and snapshot["context"]["prior_command_reference"] == {
            "quaternion": checkpoints[index-1]["navigation"]["target_q_body_to_eci"],
            "time_s": checkpoints[index-1]["time_s"]},
            "landing_decision", "Live match invented a prior executed request")
    for event in ignition:
        index = bisect_left(times, event["time_s"])
        scheduled = decision["scheduled_onset_s"]
        _require(event.get("scheduled_onset_s") == scheduled and event["time_s"] >= scheduled-1e-9
                 and (index == 0 or checkpoints[index-1]["time_s"] < scheduled-1e-9)
                 and event.get("forecast_action_delay_s") is None and event.get("requested_engine_count") == 13
                 and event.get("actual_state_assigned") is False,
                 "landing_decision", "Actual ignition skipped its frozen schedule or used a new preview action")
    if matched and record["checkpoints"][-1]["time_s"] >= decision["scheduled_onset_s"]-1e-9:
        _require(ignition or events[-1]["event"] == "landing_decision_execution_exception",
                 "landing_decision", "A reached live scheduled onset lacks its actual request or retained failure")


def verify_constrained_recovery(run, separation_state, profile, catch_config, *, catch_run=None, landing_evidence=None):
    """Check a development record and its optional exact-state catch continuation.

    ``passed`` concerns record consistency. An actual arrival with its mandatory
    catch continuation still missing returns ``passed=False`` and the explicit
    ``requires_catch_continuation=True`` only after all return checks pass.
    """
    result = {
        "schema": "missionos.starship_constrained_recovery_verification.v1", "passed": False,
        "handoff_reached": False, "support": False, "catch_supported_after_handoff": False,
        "requires_catch_continuation": False, "checkpoint_count": 0, "issues": [],
        "production_policy_admitted": False, "physical_execution": False, "mission_completed": False,
        "launch_connected": False, "missionos_dispatch": False, "model_advantage_established": False,
        "limitations": [
            "Stored-state consistency and material-point reconstruction, not independent dynamics replay.",
            "Source, preceding launch and approval binding belong to the enclosing caller.",
            "Local predictions and surrogate contact geometry do not validate SpaceX hardware or guidance.",
        ],
    }
    try:
        # A finite v6 receipt adds primary and secondary optimality vectors to
        # each sample/checkpoint. Keep the already-declared 12,002-step horizon
        # representable; this changes storage capacity, never arrival limits.
        candidate_record = run.get("recovery_record") if type(run) is dict else None
        candidate_points = candidate_record.get("checkpoints") if type(candidate_record) is dict else None
        _require(type(candidate_points) is list and len(candidate_points) <= _MAX_CHECKPOINTS,
                 "input_limit", "Missing or oversized development checkpoint history")
        _json(run, maximum_nodes=_MAX_JSON_NODES)
        _json(separation_state)
        _profile_and_config(profile, catch_config)
        _state(separation_state, profile)
        _require(type(run) is dict and run.get("scenario") == "booster_return"
                 and run.get("body_id") == "booster" and run.get("guidance_policy") in _CONFIGURATIONS,
                 "record", "Expected isolated constrained-return evidence")
        policy = run["guidance_policy"]
        phases = _phases(policy)
        record, outcome = run.get("recovery_record"), run.get("outcome")
        _require(type(record) is dict and type(outcome) is dict
                 and record.get("schema") == "missionos.starship_constrained_recovery.v1"
                 and record.get("policy_id") == policy,
                 "record", "Missing constrained development contract")
        _claims(run, record, outcome)
        expected_configuration = _CONFIGURATIONS[policy]
        physical_policy = _TRANSPORT_POLICY if policy == _LANDING_POLICY else policy
        physical_record = {**record, "policy_id": physical_policy} if policy == _LANDING_POLICY else record
        if policy == _LANDING_POLICY:
            from .starship_landing_decision_verifier import verify_frozen_boostback_bundle, verify_landing_onset_decision
            _require(type(landing_evidence) is dict and set(landing_evidence) == {"source_run", "source_map", "baseline", "prediction"},
                     "landing_decision", "Policy12 requires external saved source, baseline and qualified prediction")
            _json(landing_evidence, maximum_nodes=_MAX_JSON_NODES)
            bundle, decision = record.get("frozen_boostback_bundle"), record.get("landing_onset_decision")
            _require(type(bundle) is dict and type(decision) is dict
                     and verify_frozen_boostback_bundle(bundle, profile, catch_config, separation_state,
                         source_run=landing_evidence["source_run"], source_map=landing_evidence["source_map"])["passed"]
                     and verify_landing_onset_decision(decision, profile, catch_config,
                         source_run=landing_evidence["source_run"], baseline=landing_evidence["baseline"],
                         prediction=landing_evidence["prediction"], source_map=landing_evidence["source_map"])["passed"],
                     "landing_decision", "Frozen runtime choice is detached from its raw source, exact baseline or full-gate prediction")
            source_run = landing_evidence["source_run"]
            _require(verify_constrained_recovery(source_run, source_run["recovery_record"]["input_separation_state"],
                                               profile, catch_config)["passed"],
                     "landing_decision", "Frozen source itself failed ordinary stored-return consistency")
            expected_configuration = {**expected_configuration, "frozen_boostback": {
                "bundle_sha256": _digest(bundle), "runtime_short_forecast_calls": 0, "runtime_actual_forecast_calls": 0}}
        else:
            _require("frozen_boostback_bundle" not in record and "landing_onset_decision" not in record
                     and "execution_failure" not in record,
                     "landing_decision", "A frozen landing choice requires the explicit v12 development policy")
        if policy == _ACTUAL_TRANSPORT_POLICY:
            receipt = record.get("actual_planning_receipt")
            _require(receipt is None or type(receipt) is dict and receipt.get("schema") == _ACTUAL_TRANSPORT_SCHEMA,
                     "configuration", "Composite transport planning requires its matching-v10 receipt schema")
        if policy == _ACTUAL_POLICY:
            actual_receipt = record.get("actual_planning_receipt")
            if actual_receipt is not None:
                _require(type(actual_receipt) is dict and actual_receipt.get("schema") in (_ACTUAL_SCHEMA_V1, _ACTUAL_SCHEMA),
                         "configuration", "Actual planning requires a known versioned receipt schema")
                expected_configuration = _ACTUAL_CONFIGURATION_V1 if actual_receipt["schema"] == _ACTUAL_SCHEMA_V1 else expected_configuration
            elif _same_value(record.get("guidance_configuration"), _ACTUAL_CONFIGURATION_V1):
                # An old v1 short prefix may not yet have reached refinement.
                expected_configuration = _ACTUAL_CONFIGURATION_V1
        _require(_same_value(record.get("guidance_configuration"), expected_configuration),
                 "configuration", "Guidance differs from the independent frozen development specification")
        for state in (run.get("initial_state"), run.get("booster_separation_state"), record.get("input_separation_state")):
            _same_state(separation_state, state, "separation_binding")
        requested, resolved = record.get("requested_duration_s"), record.get("resolved_duration_s")
        _require(_number(resolved) and 0 < resolved <= 1200
                 and (resolved == 1200 if requested is None else _number(requested) and requested == resolved),
                 "time_order", "Invalid requested and resolved duration binding")
        checkpoints, samples = record.get("checkpoints"), run.get("samples")
        _require(type(checkpoints) is list and 2 <= len(checkpoints) <= _MAX_CHECKPOINTS
                 and type(samples) is list and len(samples) == len(checkpoints),
                 "record", "Missing dense macrostep observations")
        previous, previous_phase, pretrim_latch = None, 0, None
        for index, (checkpoint, sample) in enumerate(zip(checkpoints, samples)):
            _require(type(checkpoint) is dict and type(sample) is dict and sample.get("body_id") == "booster",
                     "record", "Invalid booster checkpoint")
            state, phase = checkpoint.get("state"), checkpoint.get("phase")
            _state(state, profile)
            _require(phase in phases and phases.index(phase) >= previous_phase
                     and checkpoint.get("time_s") == state["time_s"] and sample.get("phase") == phase,
                     "phase", "Recovery phase moved backwards or clocks differ")
            previous_phase = phases.index(phase)
            _same_state(state, sample, "sample_binding")
            from .starship_wind_verifier import verify_wind_observation
            _require(verify_wind_observation(sample, profile)["passed"],
                     "wind_observation", "Recorded air/ground velocity differs from declared wind")
            mass, centroid, centroid_rate, _ = _flow_and_centroid(state, profile)
            _require(_near(sample.get("mass_kg"), mass) and _near(sample.get("com_z_m"), centroid[2]),
                     "mass_properties", "Mass and centroid must follow recorded fuel")
            _compare_vector(checkpoint.get("com_rate_body_mps"), centroid_rate, "mass_properties")
            _compare_vector(sample.get("com_rate_body_mps"), centroid_rate, "mass_properties")
            _require(type(checkpoint.get("navigation")) is dict
                     and _same_value(checkpoint["navigation"], sample.get("controller")),
                     "sample_binding", "Controller observations differ between sample and checkpoint")
            command = checkpoint.get("command")
            command_phase = ("recovery_powered_rate_settle" if phase == "recovery_powered_entry_prepare" else
                             "recovery_entry_coast" if phase == "recovery_entry_shutdown_tail" else phase)
            _command(command, command_phase, state, profile)
            _main_pair(checkpoint, profile, physical_policy)
            _terminal_hold(checkpoint, profile, catch_config, physical_policy)
            _transport_preparation(checkpoint, profile, physical_policy, checkpoints[index-1] if index else None, run.get("events", []))
            if physical_policy in (_PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
                _macrostep_record(checkpoint, physical_record, profile, separation_state["time_s"]+resolved)
            _require(_same_value(command, sample.get("command")), "command_binding", "Sample command is unbound")
            if index < len(checkpoints)-1:
                _require(command is not None, "command_binding", "A macrostep requires its executed actuator request")
            else:
                _require(command is None, "command_binding", "Terminal observation cannot assert another integrated command")
            pretrim_latch, fin_policy = _pretrim_protocol(checkpoint, sample, profile, physical_policy, pretrim_latch,
                                                        separation_state["time_s"]+resolved-state["time_s"])
            _reference_protocol(checkpoint, sample, profile, physical_policy, checkpoints[index-1] if index else None,
                                checkpoint_index=index)
            _fin_allocation(checkpoint, sample, profile,
                            phase == "recovery_entry_coast" or phase.startswith("recovery_landing_"),
                            separation_state["time_s"]+resolved-state["time_s"],
                            expected_policy=fin_policy)
            if previous is None:
                _same_state(separation_state, state, "separation_binding")
            else:
                _continuity(previous, state, profile)
                prior = checkpoints[index-1]
                powered = any(engine["enabled"] for engine in prior["command"]["engines"][:33])
                step = .1 if _prediction_origin(previous, profile)[2] < 100000. or powered else .25
                if physical_policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) and prior["phase"] == "recovery_entry_shutdown_tail":
                    step = .1
                    tail = prior["navigation"]["prepare_shutdown_tail"]
                    remaining = tail["started_at_s"]+4*profile["actuators"]["throttle_tau_s"]-previous["time_s"]
                    if remaining > 1e-9:
                        step = min(step, remaining)
                _require(state["time_s"]-previous["time_s"] <= step+1e-8,
                         "macrostep_density", "A finite integration macrostep is missing")
                if physical_policy in (_PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) and prior["phase"].startswith("recovery_boostback"):
                    declared_step = prior["navigation"]["boostback_macrostep"]["step_s"]
                    _require(state["time_s"]-previous["time_s"] <= declared_step+1e-8,
                             "macrostep_density", "Recorded integration skipped its adaptive cutoff-resolution request")
            previous = state
        final = checkpoints[-1]["state"]
        _same_state(final, run.get("final_state"), "final_binding")
        start, end = separation_state["time_s"], final["time_s"]
        duration = end-start
        _require(0 < duration <= resolved+1e-7 and _near(outcome.get("start_time_s"), start)
                 and _near(outcome.get("end_time_s"), end) and _near(outcome.get("duration_s"), duration),
                 "time_order", "Outcome clock is not the bounded actual integration clock")
        _require(type(outcome.get("integration_steps")) is int
                 and outcome["integration_steps"] == len(checkpoints)-1,
                 "macrostep_density", "Integration step count differs from dense state history")
        termination = outcome.get("termination")
        _require((termination in _TERMINATIONS or policy == _LANDING_POLICY and termination == "landing_decision_execution_exception")
                 and outcome.get("phase") == checkpoints[-1]["phase"],
                 "outcome", "Invalid terminal phase or reason")
        if termination == "landing_decision_execution_exception":
            failure = record.get("execution_failure")
            _require(type(failure) is str and 1 <= len(failure) <= 128
                     and failure[0].isalpha() and all(c.isascii() and (c.isalnum() or c == "_") for c in failure),
                     "landing_decision", "A retained actual exception requires its sanitized failure class")
        elif policy == _LANDING_POLICY:
            _require("execution_failure" not in record, "landing_decision", "Successful execution cannot carry an unbound failure")
        _events(run, record, outcome, checkpoints, profile, catch_config, start, end)
        if termination == "time_limit":
            _require(_near(duration, resolved), "outcome", "Time limit ended before the requested deadline")
        if termination == "angular_rate_envelope_exceeded":
            _require(_norm(final["omega_body_rad_s"]) > 5., "outcome", "Angular-rate bound was not exceeded")
        if termination == "rate_settle_failed":
            cutoffs = [event for event in run["events"] if event["event"] == "constrained_boostback_cutoff"]
            if physical_policy in (_PREP_POLICY, _INTERCEPT_POLICY, _MOMENT_POLICY, _PRETRIM_POLICY, _TRACKING_POLICY, _ACTUAL_POLICY, _TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY):
                maximum = profile.get("booster_return", {}).get("boostback_max_slew_s", 90.)
                missing_tail_time = False
                if physical_policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) and outcome["phase"] == "recovery_entry_shutdown_tail":
                    prepared = [event for event in run["events"] if event["event"] == "powered_coast_attitude_prepared"]
                    missing_tail_time = len(prepared) == 1 and end-prepared[0]["time_s"] < 4*profile["actuators"]["throttle_tau_s"]-1e-9
                _require(len(cutoffs) == 1 and outcome["phase"] in (("recovery_powered_entry_prepare", "recovery_entry_shutdown_tail")
                         if physical_policy in (_TRANSPORT_POLICY, _ACTUAL_TRANSPORT_POLICY) else ("recovery_powered_entry_prepare",))
                         and (_local_tilt(final) > 5. or _norm(final["omega_body_rad_s"]) >= .003 or missing_tail_time)
                         and _number(maximum) and maximum > 0 and maximum < end-cutoffs[0]["time_s"]
                         <= maximum+.25000001,
                         "outcome", "Powered upright preparation failure lacks the measured gate and configured timeout")
            else:
                _require(len(cutoffs) == 1 and outcome["phase"] == "recovery_powered_rate_settle"
                         and _norm(final["omega_body_rad_s"]) >= _GUIDANCE["rate_settle_limit_rad_s"]
                         and _GUIDANCE["rate_settle_max_s"] < end-cutoffs[0]["time_s"]
                         <= _GUIDANCE["rate_settle_max_s"]+.25000001,
                         "outcome", "Rate-settle failure lacks the measured rate and elapsed timeout")
        if termination == "surface_contact":
            try:
                _contact(run.get("contact"), samples[-1], "contact",
                         profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])
            except _ContactInvalid as exc:
                raise _Invalid("surface_contact", exc.issue["detail"]) from exc
        else:
            _require(run.get("contact") is None, "surface_contact", "A noncontact trajectory cannot carry a contact receipt")
        handoff = record.get("handoff")
        _require(type(handoff) is dict and type(handoff.get("eligible")) is bool and handoff.get("time_s") == end,
                 "handoff", "Missing measured terminal gate")
        actual = _arrival(final, profile, catch_config)
        _observe_handoff(handoff.get("observation"), actual, final, catch_config)
        _require(_same_value(handoff.get("limits"), handoff["observation"]["limits"]),
                 "handoff_limits", "Terminal limits differ across observations")
        reached = handoff["eligible"]
        _require(reached is (termination == "catch_handoff"), "handoff", "Handoff and termination disagree")
        _require(outcome.get("handoff_reached") is reached,
                 "handoff", "Outcome arrival summary differs from actual terminal observation")
        if reached:
            _require(actual["eligible"] and outcome["phase"].startswith("recovery_landing_"),
                     "handoff_geometry", "Actual material points did not reach all terminal constraints")
            _same_state(final, handoff.get("state"), "handoff_binding")
            if catch_run is None:
                result.update(handoff_reached=True, checkpoint_count=len(checkpoints), requires_catch_continuation=True)
                raise _Invalid("catch_binding", "Actual arrived state requires its terminal contact continuation")
            _require(type(catch_run) is dict, "catch_binding", "Malformed catch continuation")
            _json(catch_run)
            initialization = catch_run.get("catch_record", {}).get("initialization", {})
            _require(initialization.get("kind") == "supplied_state" and initialization.get("launch_connected") is False,
                     "catch_binding", "An initialized catch fixture cannot replace the arrived physical state")
            _require(catch_run["catch_record"].get("control_policy") in ("fixed_v1", "net_thrust_trim_v1"),
                     "catch_binding", "Unknown terminal control policy")
            _same_state(final, catch_run.get("initial_state"), "catch_binding")
            catch_samples = catch_run.get("samples")
            _require(type(catch_samples) is list and catch_samples, "catch_binding", "Missing catch observations")
            _same_state(final, catch_samples[0], "catch_binding")
            verification = verify_catch(catch_run, profile, catch_config)
            _require(verification["passed"], "catch_verification", "Independent contact continuation failed verification")
            result["catch_verification"] = verification
            result["support"] = bool(verification["simulated_catch_supported"])
            result["catch_supported_after_handoff"] = result["support"]
        else:
            _require(handoff.get("state") is None and catch_run is None,
                     "catch_binding", "A contact continuation is forbidden before actual arrival")
        result.update(passed=True, handoff_reached=reached, checkpoint_count=len(checkpoints))
    except _Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError, AttributeError):
        result["issues"].append({"code": "record", "detail": "Malformed constrained-return evidence or configuration"})
    return result
