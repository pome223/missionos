"""Short finite-plant records and independent constrained-return tamper checks.

The near-terminal and hull-contact starts are initialized fixtures. They do
not establish launch-to-catch feasibility, controller robustness or model value.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_constrained_recovery_verifier as checker

ROOT = Path(__file__).resolve().parents[2]


def test_offline_json_capacity_keeps_a_finite_explicit_bound():
    checker._json([1., 2.], maximum_nodes=3)
    with pytest.raises(checker._Invalid) as problem:
        checker._json([1., 2.], maximum_nodes=2)
    assert problem.value.issue["code"] == "input_limit"
    for budget in (True, 0, -1, 32_000_001, math.inf):
        with pytest.raises(checker._Invalid):
            checker._json([], maximum_nodes=budget)


def test_oversized_checkpoint_history_fails_before_expanded_receipt_traversal(profile, configuration):
    run = {"recovery_record": {"checkpoints": [None]*12003}}
    result = checker.verify_constrained_recovery(run, {}, profile, configuration)
    assert result["passed"] is False
    assert result["issues"][0]["code"] == "input_limit"
    assert result["requires_catch_continuation"] is False


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(scope="module")
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


@pytest.fixture(scope="module")
def configuration():
    return json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())


@pytest.fixture(scope="module")
def negative(profile, configuration):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    from src.runtime.starship_constrained_recovery import simulate_constrained_recovery
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              80000., time_s=100.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(100., point.r, env.add(point.v, env.add(env.scale(up, 600.), env.scale(east, 500.))),
        _attitude(up, east), (.001, .002, .003), 260000.,
        tuple(dyn.EngineState(available=i != 5) for i in range(len(body.engines))), tuple(0. for _ in body.aero_panels))
    initial = saved(asdict(state))
    return initial, saved(simulate_constrained_recovery(profile, initial, configuration, duration_s=.3))


def check(run, initial, profile, configuration, catch=None):
    value = checker.verify_constrained_recovery(run, initial, profile, configuration, catch_run=catch)
    assert all(value[key] is False for key in ("production_policy_admitted", "physical_execution",
               "mission_completed", "launch_connected", "missionos_dispatch", "model_advantage_established"))
    json.dumps(value, allow_nan=False)
    return value


def test_finite_negative_prefix_is_verifiable_without_awarding_success(negative, profile, configuration):
    initial, run = negative
    result = check(run, initial, profile, configuration)
    assert result["passed"] and not result["handoff_reached"] and not result["support"], result
    assert not result["requires_catch_continuation"]
    assert result["checkpoint_count"] == run["outcome"]["integration_steps"]+1
    assert all(not point["state"]["engine_states"][5]["available"]
               for point in run["recovery_record"]["checkpoints"])


def test_ordinary_production_checker_refuses_isolated_policy(negative, profile, configuration):
    from src.runtime.starship_booster_recovery_verifier import verify_recovery
    initial, run = negative
    assert not verify_recovery(run, initial, profile, configuration)["passed"]


def test_frozen_guidance_matches_driver_without_importing_it_at_runtime():
    from src.runtime.starship_constrained_recovery import CONFIG, POLICY_ID
    assert checker._CONFIGURATIONS[POLICY_ID] == CONFIG
    source = inspect.getsource(checker)
    for name in ("starship_constrained_recovery import", "starship_constrained_guidance",
                 "starship_sixdof import", "starship_booster_catch import", "import numpy"):
        assert name not in source


@pytest.mark.parametrize("field", checker._STATE if hasattr(checker, "_STATE") else (
    "time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
    "propellant_kg", "engine_states", "flap_angles_rad"))
def test_all_inherited_fields_bound_exactly(negative, profile, configuration, field):
    initial, original = negative
    run = deepcopy(original)
    state = run["recovery_record"]["input_separation_state"]
    if field == "engine_states":
        state[field][5]["available"] = True
    elif field == "flap_angles_rad":
        state[field][-1] += .01
    elif isinstance(state[field], list):
        state[field][0] += .01
    else:
        state[field] += .01
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "separation_binding", result


@pytest.mark.parametrize("field", ("physical_execution", "mission_completed", "production_policy_admitted",
                                  "missionos_dispatch", "model_advantage_established", "launch_connected"))
def test_unsupported_claims_fail_closed(negative, profile, configuration, field):
    initial, original = negative
    run = deepcopy(original)
    run["outcome"][field] = True
    assert not check(run, initial, profile, configuration)["passed"]


@pytest.mark.parametrize("mutation", ("configuration", "prediction_truth", "plan_event", "event_state",
                                    "sample_state", "command", "command_receipt", "late_command", "clock"))
def test_record_bindings_fail_closed(negative, profile, configuration, mutation):
    initial, original = negative
    run = deepcopy(original)
    points = run["recovery_record"]["checkpoints"]
    if mutation == "configuration":
        run["recovery_record"]["guidance_configuration"]["alignment_deg"] += 1
    elif mutation == "prediction_truth":
        run["recovery_record"]["plans"][0]["plan"]["prediction_is_execution"] = True
    elif mutation == "plan_event":
        run["recovery_record"]["plans"][0]["plan"]["target_velocity_enu_mps"][0] += 1
    elif mutation == "event_state":
        run["events"][0]["state"]["v_eci_mps"][0] += 1
    elif mutation == "sample_state":
        run["samples"][1]["v_eci_mps"][0] += 1
    elif mutation == "command":
        points[0]["command"]["engines"][0]["gimbal_x_rad"] = 100.
    elif mutation == "command_receipt":
        points[0]["command"]["engines"][0]["throttle"] = .9
    elif mutation == "late_command":
        points[-1]["command"] = deepcopy(points[0]["command"])
        run["samples"][-1]["command"] = deepcopy(points[0]["command"])
    else:
        run["outcome"]["end_time_s"] += .01
    assert not check(run, initial, profile, configuration)["passed"]


def test_removed_macrostep_cannot_be_hidden_by_count(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    del run["recovery_record"]["checkpoints"][1]
    del run["samples"][1]
    run["outcome"]["integration_steps"] -= 1
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "macrostep_density", result


@pytest.mark.parametrize("mutation", ("refuel", "teleport", "availability", "flap_rate", "gimbal_rate"))
def test_actual_state_discontinuities_are_refused(negative, profile, configuration, mutation):
    initial, original = negative
    run = deepcopy(original)
    point, sample = run["recovery_record"]["checkpoints"][1], run["samples"][1]
    state = point["state"]
    if mutation == "refuel":
        state["propellant_kg"] += 1000.
        mass, center, rate, _ = checker._flow_and_centroid(state, profile)
        sample.update(mass_kg=mass, com_z_m=center[2], com_rate_body_mps=rate)
        point["com_rate_body_mps"] = rate
    elif mutation == "teleport":
        state["r_eci_m"][0] += 1000.
    elif mutation == "availability":
        state["engine_states"][5]["available"] = True
    elif mutation == "flap_rate":
        state["flap_angles_rad"][-1] = .7
    else:
        state["engine_states"][0]["gimbal_x_rad"] = .1
    sample.update(deepcopy(state))
    assert not check(run, initial, profile, configuration)["passed"]


@pytest.mark.parametrize("basis,fuel,elapsed,accepted", (
    ("fuel_guard", 60000., .1, True), ("fuel_guard", 60000.01, .1, False),
    ("time_guard", 260000., 70., True), ("time_guard", 260000., 69.999, False),
    ("velocity_target", 260000., .1, True),
))
def test_cutoff_measured_predicate_boundaries(negative, profile, basis, fuel, elapsed, accepted):
    # Arithmetic predicate fixtures, not integrated cutoff trajectories.
    initial, original = negative
    state = deepcopy(initial)
    state.update(time_s=initial["time_s"]+elapsed, propellant_kg=fuel)
    target = checker._local_velocity(state)
    record = {"plans": [{"time_s": initial["time_s"], "plan": {"target_velocity_enu_mps": target}}]}
    events = [{"event": "boostback_ignition", "time_s": initial["time_s"]},
              {"event": "constrained_boostback_cutoff", "time_s": state["time_s"], "state": state,
               "cutoff_basis": basis, "velocity_error_mps": 0.}]
    if accepted:
        checker._cutoff_evidence(record, events, profile, "recovery_entry_coast")
    else:
        with pytest.raises(checker._Invalid):
            checker._cutoff_evidence(record, events, profile, "recovery_entry_coast")


def test_empty_or_forged_catch_dependency_is_not_enabled(negative, profile, configuration):
    initial, original = negative
    for run in ({}, deepcopy(original)):
        if run:
            run["recovery_record"]["handoff"]["eligible"] = True
            run["outcome"]["termination"] = "catch_handoff"
        result = check(run, initial, profile, configuration)
        assert not result["passed"] and not result["requires_catch_continuation"]


@pytest.fixture(scope="module")
def terminal(profile, configuration):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_booster_catch import _initialized, simulate_catch
    from src.runtime.starship_constrained_recovery import simulate_constrained_recovery
    _, state = _initialized(profile, configuration, "booster_catch")
    up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    state = replace(state, r_eci_m=env.add(state.r_eci_m, env.scale(up, 1.5)))
    initial = saved(asdict(state))
    run = saved(simulate_constrained_recovery(profile, initial, configuration, duration_s=2.))
    assert run["outcome"]["termination"] == "catch_handoff", run["outcome"]
    catch = saved(simulate_catch(profile, configuration, initial_state=deepcopy(run["final_state"]),
                                 duration_s=30., control_policy="net_thrust_trim_v1"))
    return initial, run, catch


def test_actual_arrival_dependency_is_explicit_and_not_a_pass(terminal, profile, configuration):
    initial, run, _ = terminal
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["handoff_reached"] and not result["support"], result
    assert result["requires_catch_continuation"]
    assert len(result["issues"]) == 1 and result["issues"][0]["code"] == "catch_binding"


def test_exact_arrival_requires_matching_state_and_independent_support_check(terminal, profile, configuration):
    initial, run, catch = terminal
    result = check(run, initial, profile, configuration, catch)
    assert result["passed"] and result["handoff_reached"] and not result["requires_catch_continuation"], result
    assert result["support"] is result["catch_verification"]["simulated_catch_supported"]
    assert result["support"] is result["catch_supported_after_handoff"]
    for mutation in ("fuel", "fixture", "missing"):
        changed = deepcopy(catch)
        if mutation == "fuel":
            changed["initial_state"]["propellant_kg"] += 1.
        elif mutation == "fixture":
            changed["catch_record"]["initialization"]["kind"] = "initialized_fixture"
        else:
            changed["samples"] = []
        result = check(run, initial, profile, configuration, changed)
        assert not result["passed"] and not result["requires_catch_continuation"] and not result["support"]


@pytest.mark.parametrize("field", ("horizontal_position_m", "propellant_reserve_kg", "attitude_angle_deg"))
def test_handoff_gate_cannot_be_widened(terminal, profile, configuration, field):
    initial, original, catch = terminal
    run = deepcopy(original)
    run["recovery_record"]["handoff"]["observation"]["limits"][field] *= 2.
    run["recovery_record"]["handoff"]["limits"][field] *= 2.
    assert not check(run, initial, profile, configuration, catch)["passed"]


@pytest.fixture(scope="module")
def impact(profile, configuration):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    from src.runtime.starship_constrained_recovery import simulate_constrained_recovery
    body = vehicle(profile, "booster")
    fuel = 100000.
    center = dyn.mass_properties(body, fuel).com_body_m[2]
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              center+.05, time_s=100.)
    up, east, _ = env.local_frame(point)
    q = _attitude(up, east)
    state = dyn.State6DOF(100., point.r, env.add(point.v, env.scale(up, -100.)), q,
        dyn.inverse_rotate(q, (0., 0., env.EARTH_ROTATION_RAD_S)), fuel,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    initial = saved(asdict(state))
    run = saved(simulate_constrained_recovery(profile, initial, configuration, duration_s=.3))
    assert run["outcome"]["termination"] == "surface_contact", run["outcome"]
    return initial, run


def test_actual_hull_contact_receipt_preserves_failed_result(impact, profile, configuration):
    initial, run = impact
    result = check(run, initial, profile, configuration)
    assert result["passed"] and not result["handoff_reached"] and not result["support"], result
    assert run["contact"]["surface_relative_speed_mps"] > 90.
    assert run["contact"]["moving_com_correction_applied"] is True


@pytest.mark.parametrize("field", ("point_eci_m", "point_velocity_eci_mps", "surface_relative_speed_mps",
                                  "signed_clearance_m", "event_time_s", "propellant_kg", "landing_verified"))
def test_contact_receipt_tampering_cannot_hide_impact(impact, profile, configuration, field):
    initial, original = impact
    run = deepcopy(original)
    receipt = run["contact"]
    if field == "landing_verified":
        receipt[field] = True
    elif isinstance(receipt[field], list):
        receipt[field][0] += 1000.
    else:
        receipt[field] += 1000.
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "surface_contact", result


@pytest.mark.parametrize("policy,residual,recorded,axis,accepted", (
    (checker._IMPULSE_POLICY, 12., 12., [1., 0., 0.], True),
    (checker._IMPULSE_POLICY, -100., -100., [1., 0., 0.], True),
    (checker._IMPULSE_POLICY, 12.0001, 12.0001, [1., 0., 0.], False),
    (checker._IMPULSE_POLICY, 12., 11., [1., 0., 0.], False),
    (checker._IMPULSE_POLICY, 12., 12., None, False),
    (checker._IMPULSE_POLICY, 12., 12., [2., 0., 0.], False),
    (checker._IMPULSE_POLICY, 12., 12., [True, 0., 0.], False),
    (checker._POLICY, 12., 12., [1., 0., 0.], False),
))
def test_impulse_projection_is_signed_measured_and_version_scoped(
        negative, profile, policy, residual, recorded, axis, accepted):
    # These are independently constructed cutoff arithmetic fixtures, not a
    # full finite trajectory or certification of the proposed burn direction.
    initial, _ = negative
    state = deepcopy(initial)
    state["time_s"] += .1
    v = checker._local_velocity(state)
    target = [v[0]+residual, v[1]+100., v[2]]
    plan = {"target_velocity_enu_mps": target}
    if axis is not None:
        plan["burn_axis_enu"] = axis
    record = {"policy_id": policy, "plans": [{"time_s": initial["time_s"], "plan": plan}]}
    event = {"event": "constrained_boostback_cutoff", "time_s": state["time_s"], "state": state,
             "cutoff_basis": "along_axis_impulse", "velocity_error_mps": (residual**2+10000.)**.5,
             "along_axis_velocity_error_mps": recorded}
    events = [{"event": "boostback_ignition", "time_s": initial["time_s"]}, event]
    if accepted:
        checker._cutoff_evidence(record, events, profile, "recovery_entry_coast")
    else:
        with pytest.raises(checker._Invalid):
            checker._cutoff_evidence(record, events, profile, "recovery_entry_coast")


def test_impulse_cutoff_still_requires_real_ignition_and_full_velocity_observation(negative, profile):
    initial, _ = negative
    state = deepcopy(initial)
    state["time_s"] += .1
    target = checker._local_velocity(state)
    record = {"policy_id": checker._IMPULSE_POLICY, "plans": [{"time_s": initial["time_s"],
              "plan": {"target_velocity_enu_mps": target, "burn_axis_enu": [1., 0., 0.]}}]}
    cutoff = {"event": "constrained_boostback_cutoff", "time_s": state["time_s"], "state": state,
              "cutoff_basis": "along_axis_impulse", "velocity_error_mps": 0., "along_axis_velocity_error_mps": 0.}
    with pytest.raises(checker._Invalid):
        checker._cutoff_evidence(record, [cutoff], profile, "recovery_entry_coast")
    cutoff["velocity_error_mps"] = 100.
    with pytest.raises(checker._Invalid):
        checker._cutoff_evidence(record, [{"event": "boostback_ignition", "time_s": initial["time_s"]}, cutoff],
                                 profile, "recovery_entry_coast")


def test_mismatched_policy_configuration_cannot_relabel_execution(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["guidance_policy"] = checker._IMPULSE_POLICY
    run["recovery_record"]["policy_id"] = checker._IMPULSE_POLICY
    run["recovery_record"]["guidance_configuration"] = deepcopy(checker._GUIDANCE)
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "configuration", result


@pytest.mark.parametrize("axis", (None, [2., 0., 0.], [True, 0., 0.]))
def test_v2_record_contract_cannot_omit_or_forge_planned_unit_axis(negative, profile, configuration, axis):
    # Deliberately malformed versioned-record fixtures, not evidence that a
    # v1 physical run was produced by a v2 controller.
    initial, original = negative
    run = deepcopy(original)
    run["guidance_policy"] = checker._IMPULSE_POLICY
    run["recovery_record"]["policy_id"] = checker._IMPULSE_POLICY
    run["recovery_record"]["guidance_configuration"] = deepcopy(checker._CONFIGURATIONS[checker._IMPULSE_POLICY])
    for checkpoint, sample in zip(run["recovery_record"]["checkpoints"], run["samples"]):
        for key in ("entry_pretrim", "entry_pretrim_prepared_at_s", "entry_pretrim_reference_kind",
                    "reference_tracking", "reference_tracking_control"):
            checkpoint["navigation"].pop(key, None)
        sample["controller"] = deepcopy(checkpoint["navigation"])
    for item in run["recovery_record"]["plans"]:
        item["plan"].pop("burn_axis_enu", None)
        if axis is not None:
            item["plan"]["burn_axis_enu"] = deepcopy(axis)
    for event in run["events"]:
        if "plan" in event:
            event["plan"].pop("burn_axis_enu", None)
            if axis is not None:
                event["plan"]["burn_axis_enu"] = deepcopy(axis)
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "prediction_binding", result


@pytest.fixture(scope="module")
def finite_plan(negative, profile, configuration):
    from src.runtime.starship_boostback_shooting import refine_boostback_plan
    initial, _ = negative
    velocity = checker._local_velocity(initial)
    position = checker._prediction_origin(initial, profile)
    seed = {"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [*velocity[:2], velocity[2]+20.],
            "point_post_shutdown_velocity_enu_mps": [*velocity[:2], velocity[2]+20.],
            "point_post_shutdown_position_enu_m": [*position[:2], position[2]+1000.],
            "bank_heading_enu": [0., 1., 0.], "bank_sign": 1,
            "prediction_is_execution": False, "production_policy_admitted": False}
    # Actual short finite boostback/settle/shutdown forecasts only. No return,
    # entry, landing or terminal support is executed by this fixture.
    return initial, refine_boostback_plan(initial, profile, configuration, seed)


def test_finite_shooting_receipt_has_independent_origin_and_counter_arithmetic(finite_plan, profile, configuration):
    initial, plan = finite_plan
    checker._short_prediction(plan, initial, profile, configuration)
    assert plan["actual_dynamics_prediction"]["attempted_forecast_count"] <= 48
    assert plan["actual_dynamics_prediction"]["selected_forecast"]["prediction_is_execution"] is False
    assert plan["admissible"] is False


@pytest.mark.parametrize("mutation", ("origin", "seed_hash", "target", "counter", "budget", "parameter",
                                    "selection", "authority", "score", "held_bearing"))
def test_finite_forecast_receipt_tampering_refused(finite_plan, profile, configuration, mutation):
    initial, original = finite_plan
    plan = deepcopy(original)
    receipt = plan["actual_dynamics_prediction"]
    if mutation == "origin":
        receipt["input_state"]["propellant_kg"] += 1.
        receipt["input_state_sha256"] = checker._digest(receipt["input_state"])
    elif mutation == "seed_hash":
        receipt["seed_point_plan_sha256"] = "rewritten"
    elif mutation == "target":
        receipt["endpoint_target_velocity_enu_mps"][0] += 1.
    elif mutation == "counter":
        receipt["attempted_forecast_count"] += 1
    elif mutation == "budget":
        receipt["configuration"]["maximum_forecast_calls"] += 1
    elif mutation == "parameter":
        receipt["forecasts"][0]["parameters"][2] = 301.
    elif mutation == "selection":
        plan["target_velocity_enu_mps"][0] += 1.
    elif mutation == "authority":
        receipt["support_admitted"] = True
    elif mutation == "score":
        receipt["forecasts"][receipt["selected_attempt_index"]-1]["objective"] += 1.
    else:
        plan["bank_sign"] = -plan["bank_sign"]
    with pytest.raises(checker._Invalid):
        checker._short_prediction(plan, initial, profile, configuration)


@pytest.mark.parametrize("bearing,sign", (([0., 2., 0.], 1), ([0., 0., 1.], 1), ([0., 1., 0.], True)))
def test_v3_held_bank_reference_is_unit_horizontal_and_strict_signed_integer(negative, profile, configuration, bearing, sign):
    initial, original = negative
    run = deepcopy(original)
    run["guidance_policy"] = checker._SHOOTING_POLICY
    run["recovery_record"]["policy_id"] = checker._SHOOTING_POLICY
    run["recovery_record"]["guidance_configuration"] = deepcopy(checker._CONFIGURATIONS[checker._SHOOTING_POLICY])
    for checkpoint, sample in zip(run["recovery_record"]["checkpoints"], run["samples"]):
        for key in ("entry_pretrim", "entry_pretrim_prepared_at_s", "entry_pretrim_reference_kind",
                    "reference_tracking", "reference_tracking_control"):
            checkpoint["navigation"].pop(key, None)
        sample["controller"] = deepcopy(checkpoint["navigation"])
    for item in run["recovery_record"]["plans"]:
        item["plan"]["bank_heading_enu"] = deepcopy(bearing)
        item["plan"]["bank_sign"] = sign
    for event in run["events"]:
        if "plan" in event:
            event["plan"]["bank_heading_enu"] = deepcopy(bearing)
            event["plan"]["bank_sign"] = sign
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "prediction_binding", result


def test_opposing_pair_receipt_binds_available_gimballed_geometry_and_actual_commands(profile):
    # Request-allocation arithmetic fixture, not a trajectory or support test.
    state = {"engine_states": [{"available": True} for _ in range(45)]}
    engines = [{"enabled": i in (3, 8), "throttle": .5 if i in (3, 8) else 0.} for i in range(45)]
    point = {"phase": "recovery_landing_13", "state": state, "command": {"engines": engines},
             "navigation": {"selected_main_engine_indices": [3, 8]}}
    checker._main_pair(point, profile, checker._SHOOTING_POLICY)
    for mutation in ("enabled", "available", "geometry", "throttle", "version"):
        changed = deepcopy(point)
        policy = checker._SHOOTING_POLICY
        if mutation == "enabled":
            changed["command"]["engines"][8]["enabled"] = False
        elif mutation == "available":
            changed["state"]["engine_states"][8]["available"] = False
        elif mutation == "geometry":
            changed["navigation"]["selected_main_engine_indices"] = [3, 7]
            changed["command"]["engines"][8]["enabled"] = False
            changed["command"]["engines"][7].update(enabled=True, throttle=.5)
        elif mutation == "throttle":
            changed["command"]["engines"][8]["throttle"] = .6
        else:
            policy = checker._IMPULSE_POLICY
        with pytest.raises(checker._Invalid):
            checker._main_pair(changed, profile, policy)


@pytest.mark.parametrize("mutation", ("step", "minimum", "velocity", "clamp", "deadline"))
def test_v4_adaptive_macrostep_request_cannot_be_rewritten(negative, profile, configuration, mutation):
    initial, original = negative
    if original["guidance_policy"] not in (checker._PREP_POLICY, checker._INTERCEPT_POLICY,
                                          checker._MOMENT_POLICY, checker._PRETRIM_POLICY, checker._TRACKING_POLICY,
                                          checker._ACTUAL_POLICY, checker._TRANSPORT_POLICY,
                                          checker._ACTUAL_TRANSPORT_POLICY):
        pytest.skip("Only the versioned adaptive policy creates this receipt")
    run = deepcopy(original)
    point = run["recovery_record"]["checkpoints"][0]
    receipt = point["navigation"]["boostback_macrostep"]
    if mutation == "step":
        receipt["step_s"] /= 2.
    elif mutation == "minimum":
        receipt["minimum_step_s"] = 0.
    elif mutation == "velocity":
        receipt["along_axis_velocity_error_mps"] += 1.
    elif mutation == "clamp":
        receipt["velocity_clamped"] = True
    else:
        receipt["maximum_step_s"] = .2
    run["samples"][0]["controller"] = deepcopy(point["navigation"])
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "macrostep_density", result


@pytest.fixture(scope="module")
def preparation_timeout(negative, profile, configuration):
    from src.runtime import starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import point_state
    from src.runtime import starship_physics as env
    from src.runtime.starship_constrained_recovery import simulate_constrained_recovery
    initial, _ = negative
    state = dyn.state_from_dict(initial)
    _, east, _ = env.local_frame(point_state(state))
    q = dyn.normalize_quaternion(dyn.quaternion_multiply(dyn.axis_angle(east, .35), state.q_body_to_eci))
    initial = deepcopy(initial)
    initial.update(propellant_kg=60000., q_body_to_eci=list(q), omega_body_rad_s=[0., 0., .0001])
    supplied = deepcopy(profile)
    supplied["booster_return"]["boostback_max_slew_s"] = .2
    run = saved(simulate_constrained_recovery(supplied, initial, configuration, duration_s=.6))
    assert run["outcome"]["termination"] == "rate_settle_failed"
    assert run["outcome"]["phase"] == "recovery_powered_entry_prepare"
    return initial, run, supplied


def test_v4_actual_powered_preparation_timeout_is_verifiable_without_success(preparation_timeout, configuration):
    initial, run, profile = preparation_timeout
    result = check(run, initial, profile, configuration)
    assert result["passed"] and not result["support"] and not result["handoff_reached"], result
    assert checker._local_tilt(run["final_state"]) > 5.
    assert run["final_state"]["propellant_kg"] < initial["propellant_kg"]


def test_false_upright_prepared_event_cannot_hide_actual_failed_angle(preparation_timeout, configuration):
    initial, original, profile = preparation_timeout
    run = deepcopy(original)
    run["events"].insert(-1, {"event": "powered_coast_attitude_prepared", "time_s": run["final_state"]["time_s"],
                             "state": deepcopy(run["final_state"]), "tilt_deg": 0.})
    result = check(run, initial, profile, configuration)
    assert not result["passed"] and result["issues"][0]["code"] == "events", result


@pytest.mark.parametrize("mutation", ("point_count", "point_input", "point_mass", "ideal_fuel",
                                    "arrest_floor", "arrest_admission", "global_infeasible"))
def test_v5_coast_arrest_budget_and_negative_claim_are_bound(finite_plan, profile, configuration, mutation):
    initial, original = finite_plan
    plan = deepcopy(original)
    receipt = plan["actual_dynamics_prediction"]
    entry = next(item for item in receipt["forecasts"] if item.get("point_status") == "completed")
    continuation = entry["point_continuation"]
    if mutation == "point_count":
        receipt["point_continuation_attempted_count"] += 1
    elif mutation == "point_input":
        continuation["input_position_enu_m"][0] += 1.
    elif mutation == "point_mass":
        continuation["input_mass_kg"] += 1.
    elif mutation == "ideal_fuel":
        continuation["ideal_fuel_budget"]["support_margin_kg"] += 1.
    elif mutation == "arrest_floor":
        continuation["ideal_arrest_preview"]["required_capture_cg_height_m"] -= 1.
    elif mutation == "arrest_admission":
        continuation["ideal_arrest_preview"]["arrival_admitted"] = True
    else:
        receipt["global_infeasibility_established"] = True
    with pytest.raises(checker._Invalid):
        checker._short_prediction(plan, initial, profile, configuration)


@pytest.fixture(scope="module")
def corridor_checkpoint(profile, configuration):
    from src.runtime import starship_physics as env
    from src.runtime.starship_booster_catch import _initialized
    from src.runtime.starship_booster_recovery import _tower_observation
    body, state = _initialized(profile, configuration, "booster_catch")
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    _, east, _ = env.local_frame(point)
    state = replace(state, r_eci_m=env.add(state.r_eci_m, env.scale(east, 1.)))
    arrival = saved(_tower_observation(state, body, profile, configuration))
    return {"phase": "recovery_landing_13", "state": saved(asdict(state)), "command": {},
            "navigation": {"arrival_observation": arrival, "terminal_corridor_hold": {
                "active": True, "target_pin_clearance_m": configuration["initial_pin_clearance_m"]+2*configuration["arm_half_width_m"],
                "observed_nonvertical_ready": False, "state_assigned": False}}}


def test_v5_corridor_hold_uses_fresh_actual_material_points_and_keeps_geometry(corridor_checkpoint, profile, configuration):
    checker._terminal_hold(corridor_checkpoint, profile, configuration, checker._INTERCEPT_POLICY)
    assert corridor_checkpoint["navigation"]["terminal_corridor_hold"]["target_pin_clearance_m"] == pytest.approx(4.6)
    assert corridor_checkpoint["navigation"]["arrival_observation"]["eligible"] is False


@pytest.mark.parametrize("mutation", ("omitted", "clamp", "target", "observation", "version"))
def test_false_corridor_hold_receipt_cannot_replace_observation(corridor_checkpoint, profile, configuration, mutation):
    point = deepcopy(corridor_checkpoint)
    policy = checker._INTERCEPT_POLICY
    if mutation == "omitted":
        del point["navigation"]["terminal_corridor_hold"]
    elif mutation == "clamp":
        point["navigation"]["terminal_corridor_hold"]["state_assigned"] = True
    elif mutation == "target":
        point["navigation"]["terminal_corridor_hold"]["target_pin_clearance_m"] = 3.4
    elif mutation == "observation":
        point["navigation"]["arrival_observation"]["time_s"] += .1
    else:
        policy = checker._PREP_POLICY
    with pytest.raises(checker._Invalid):
        checker._terminal_hold(point, profile, configuration, policy)
