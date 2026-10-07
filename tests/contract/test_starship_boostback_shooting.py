"""Short finite forecasts and hard budget/lineage checks, never full recovery."""
from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_boostback_shooting as shooting

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def inputs():
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              80000., time_s=100.)
    up, east, north = env.local_frame(point)
    q = _attitude(up, east)
    velocity = env.add(point.v, env.add(env.scale(up, 400.), env.add(env.scale(east, 300.), env.scale(north, 75.))))
    state = dyn.State6DOF(100., point.r, velocity, q, (.001, -.001, .0005), 260000.,
        tuple(dyn.EngineState(throttle=.4 if i < 3 else 0., available=i != 5) for i in range(len(body.engines))),
        tuple(0. for _ in body.aero_panels))
    reference = ConditionedGeographicFrame(q, 99.9,
        maximum_roll_rate_rad_s=profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"])
    reference.bridging = True
    reference.diagnostics = {"fixture_roll_history": "carried"}
    seed = {"target_velocity_enu_mps": [300., 75., 420.], "burn_axis_enu": [0., 0., 1.],
            "point_post_shutdown_velocity_enu_mps": [300., 75., 420.],
            "point_post_shutdown_position_enu_m": [0., 0., 82000.],
            "bank_heading_enu": [0., 1., 0.], "bank_sign": 1,
            "prediction_is_execution": False, "production_policy_admitted": False}
    return state, profile, catch, seed, reference


def short_forecasts(monkeypatch, duration=.3):
    original = shooting._forecast_boostback
    calls = []

    def forward(*args, **kwargs):
        calls.append(deepcopy(args[3]))
        return original(*args, **kwargs, duration_s=duration)

    monkeypatch.setattr(shooting, "_forecast_boostback", forward)
    return calls


def test_short_finite_prediction_inherits_fault_and_full_reference_without_mutation(inputs):
    state, profile, catch, seed, reference = deepcopy(inputs)
    snapshots = asdict(state), deepcopy(profile), deepcopy(catch), deepcopy(seed), deepcopy(reference.__dict__)
    before_frame = shooting._reference_state(reference)
    forecast = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=.3)
    assert forecast["input_state"] == json.loads(json.dumps(asdict(state)))
    assert forecast["input_reference"] == before_frame
    assert forecast["final_state"] != forecast["input_state"]
    assert forecast["integration_steps"] >= 3
    assert forecast["requested_full_engine_steps"] == 3
    assert forecast["final_state"]["engine_states"][5]["available"] is False
    assert forecast["duration_s"] == pytest.approx(.3)
    assert forecast["termination"] == "time_limit" and not forecast["prediction_complete"]
    assert asdict(state) == snapshots[0] and profile == snapshots[1] and catch == snapshots[2] and seed == snapshots[3]
    assert shooting._reference_state(reference) == before_frame
    assert reference.frame.quaternion == snapshots[4]["frame"].quaternion
    assert not any(forecast[key] for key in ("prediction_is_execution", "actual_state_assigned",
                   "production_policy_admitted", "arrival_forecast", "support_forecast"))


def test_unaligned_candidate_spends_actual_center_engine_steps(inputs):
    state, profile, catch, seed, reference = inputs
    plan = {**seed, "burn_axis_enu": [1., 0., 0.]}
    forecast = shooting._forecast_boostback(state, profile, catch, plan, reference=reference, duration_s=.3)
    assert forecast["requested_center_slew_steps"] == 3 and forecast["requested_full_engine_steps"] == 0
    assert forecast["final_state"]["propellant_kg"] < state.propellant_kg
    assert forecast["final_state"]["q_body_to_eci"] != list(state.q_body_to_eci)


def test_call_budget_includes_derivative_forecasts_and_uses_cached_selected_forecast(inputs, monkeypatch):
    import scipy.optimize
    state, profile, catch, seed, reference = inputs
    calls = short_forecasts(monkeypatch)
    monkeypatch.setitem(shooting.CONFIG, "maximum_forecast_calls", 6)

    def many_evaluations(fun, parameters, **kwargs):
        for index in range(100):
            fun(np.asarray([.001*index, 0., 0.]))
        pytest.fail("The hard forecast-call budget did not stop the solver")

    monkeypatch.setattr(scipy.optimize, "least_squares", many_evaluations)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    receipt = result["actual_dynamics_prediction"]
    assert receipt["attempted_forecast_count"] == receipt["completed_forecast_count"] == len(calls) == 6
    assert receipt["failed_forecast_count"] == 0
    assert receipt["optimizer_status"] == "forecast_budget_exhausted"
    assert receipt["selected_attempt_index"] in range(1, 7)
    assert receipt["selected_forecast"] == receipt["forecasts"][receipt["selected_attempt_index"]-1]["forecast"]
    assert result["seed_point_plan"] == seed
    assert receipt["seed_point_plan_sha256"] == shooting._digest(seed)
    assert receipt["endpoint_target_velocity_enu_mps"] == seed["point_post_shutdown_velocity_enu_mps"]
    assert receipt["input_reference"] == shooting._reference_state(reference)
    assert not result["admissible"] and not receipt["selected_forecast_complete"]


def test_real_bounded_solver_counts_every_actual_short_forecast(inputs, monkeypatch):
    state, profile, catch, seed, reference = inputs
    calls = short_forecasts(monkeypatch)
    monkeypatch.setitem(shooting.CONFIG, "maximum_forecast_calls", 8)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    receipt = result["actual_dynamics_prediction"]
    assert 1 <= receipt["attempted_forecast_count"] == len(calls) <= 8
    assert receipt["attempted_forecast_count"] == receipt["completed_forecast_count"]+receipt["failed_forecast_count"]
    assert receipt["input_state_sha256"] == shooting._digest(json.loads(json.dumps(asdict(state))))
    assert np.linalg.norm(result["burn_axis_enu"]) == pytest.approx(1.)
    bound = math.radians(shooting.CONFIG["angular_offset_bound_deg"])
    for item in receipt["forecasts"]:
        assert all(abs(x) <= bound+1e-10 for x in item["parameters"][:2])
        assert abs(item["parameters"][2]) <= shooting.CONFIG["cut_projection_offset_bound_mps"]
    assert not result["prediction_is_execution"] and not result["production_policy_admitted"]
    assert not receipt["support_admitted"] and not receipt["physical_execution"] and not receipt["missionos_dispatch"]
    assert not result["admissible"]


def test_failed_forecasts_count_as_attempts_and_are_retained(inputs, monkeypatch):
    import scipy.optimize
    state, profile, catch, seed, reference = inputs
    original = shooting._forecast_boostback
    calls = []

    def sometimes_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("injected_integrator_failure")
        return original(*args, **kwargs, duration_s=.3)

    def many_evaluations(fun, parameters, **kwargs):
        for index in range(100):
            fun(np.asarray([.001*index, 0., 0.]))

    monkeypatch.setattr(shooting, "_forecast_boostback", sometimes_fails)
    monkeypatch.setattr(scipy.optimize, "least_squares", many_evaluations)
    monkeypatch.setitem(shooting.CONFIG, "maximum_forecast_calls", 5)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    receipt = result["actual_dynamics_prediction"]
    assert receipt["attempted_forecast_count"] == len(calls) == 5
    assert receipt["failed_forecast_count"] == 2 and receipt["completed_forecast_count"] == 3
    assert [x["status"] for x in receipt["forecasts"]] == ["failed", "failed", "completed", "completed", "completed"]
    assert all(x["error_type"] == "RuntimeError" for x in receipt["forecasts"][:2])
    assert receipt["selected_attempt_index"] >= 3


def test_all_failed_forecasts_expose_receipt_without_admitted_fallback(inputs, monkeypatch):
    state, profile, catch, seed, reference = inputs
    count = 0

    def fails(*args, **kwargs):
        nonlocal count
        count += 1
        raise ValueError("injected_finite_state_failure")

    monkeypatch.setattr(shooting, "_forecast_boostback", fails)
    with pytest.raises(shooting.ForecastUnavailable) as error:
        shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    receipt = error.value.forecast_receipt
    assert receipt["attempted_forecast_count"] == receipt["failed_forecast_count"] == count
    assert receipt["completed_forecast_count"] == 0 and count <= shooting.CONFIG["maximum_forecast_calls"]
    assert not receipt["support_admitted"] and not receipt["production_policy_admitted"]


@pytest.mark.parametrize("field,value", (("burn_axis_enu", [2., 0., 0.]), ("burn_axis_enu", [True, 0., 0.]),
    ("target_velocity_enu_mps", [0., float("nan"), 0.]), ("point_post_shutdown_velocity_enu_mps", None),
    ("prediction_is_execution", True), ("physical_execution", True), ("production_policy_admitted", True)))
def test_invalid_or_execution_claiming_seed_refused_before_any_forecast(inputs, monkeypatch, field, value):
    state, profile, catch, original, reference = inputs
    seed = deepcopy(original)
    seed[field] = value

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid input reached the finite forecaster")

    monkeypatch.setattr(shooting, "_forecast_boostback", forbidden)
    with pytest.raises(ValueError):
        shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)


def test_forecast_can_accept_persisted_exact_state_and_refuses_future_reference(inputs):
    state, profile, catch, seed, reference = inputs
    initial = json.loads(json.dumps(asdict(state)))
    result = shooting._forecast_boostback(initial, profile, catch, seed, reference=reference, duration_s=.1)
    assert result["input_state"] == initial
    future = deepcopy(reference)
    future.time_s = state.time_s+.1
    with pytest.raises(ValueError, match="invalid_inherited"):
        shooting.refine_boostback_plan(state, profile, catch, seed, reference=future)


def test_short_forecast_exercises_measured_cutoff_settle_and_nonzero_shutdown_tail(inputs):
    from dataclasses import replace
    state, profile, catch, seed, reference = deepcopy(inputs)
    state = replace(state, propellant_kg=60000., omega_body_rad_s=(0., 0., .0001))
    result = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=2.)
    assert result["prediction_complete"] and result["termination"] == "shutdown_tail_complete"
    assert result["cutoff_basis"] == "fuel_guard"
    assert [e["event"] for e in result["events"]] == ["cutoff", "upright_prepared"]
    assert result["duration_s"] == pytest.approx(1.4)
    assert result["final_state"]["engine_states"][0]["throttle"] > 0.
    assert result["final_state"]["engine_states"][5]["available"] is False


def test_refinement_preserves_frozen_point_plan_bank_reference(inputs, monkeypatch):
    state, profile, catch, original, reference = inputs
    seed = {**original, "bank_heading_enu": [0., -1., 0.], "bank_sign": -1}
    short_forecasts(monkeypatch)
    monkeypatch.setitem(shooting.CONFIG, "maximum_forecast_calls", 4)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    assert result["bank_heading_enu"] == seed["bank_heading_enu"] and result["bank_sign"] == -1
    assert result["seed_point_plan"] == seed


def test_solver_exception_preserves_attempted_forecasts_without_retry_or_admission(inputs, monkeypatch):
    import scipy.optimize
    state, profile, catch, seed, reference = inputs
    calls = short_forecasts(monkeypatch)

    def fails(*args, **kwargs):
        raise RuntimeError("injected_solver_failure")

    monkeypatch.setattr(scipy.optimize, "least_squares", fails)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    receipt = result["actual_dynamics_prediction"]
    assert receipt["optimizer_status"] == "solver_error" and receipt["solver_error_type"] == "RuntimeError"
    assert receipt["attempted_forecast_count"] == receipt["completed_forecast_count"] == len(calls) == 1
    assert not receipt["solver_success"] and not receipt["support_admitted"] and not result["admissible"]


def test_adaptive_cutoff_step_uses_achieved_acceleration_and_actual_integration(inputs):
    from dataclasses import replace
    from src.runtime import starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import vehicle
    state, profile, _, _, _ = inputs
    engines = tuple(replace(engine, throttle=1. if index < 33 and engine.available else 0.)
                    for index, engine in enumerate(state.engine_states))
    state = replace(state, engine_states=engines)
    body = vehicle(profile, "booster")
    observed = dyn.observe(state, body)
    _, velocity = shooting._local_state(state, profile)
    target = [*velocity[:2], velocity[2]+13.]
    before = asdict(state)
    step, receipt = shooting.boostback_macro_step(state, body, observed, profile, target, [0., 0., 1.])
    assert .001 <= step < .1 and type(step) is float
    assert receipt["achieved_along_axis_acceleration_mps2"] > 100.
    assert asdict(state) == before and not receipt["actual_state_assigned"] and not receipt["velocity_clamped"]
    command = dyn.Command6DOF(tuple(dyn.EngineCommand(index < 33, 1. if index < 33 else 0.)
                                   for index in range(len(body.engines))), state.flap_angles_rad)
    adaptive = dyn.step(state, body, command, step)
    coarse = dyn.step(state, body, command, .1)
    _, adaptive_velocity = shooting._local_state(adaptive, profile)
    _, coarse_velocity = shooting._local_state(coarse, profile)
    adaptive_residual, coarse_residual = target[2]-adaptive_velocity[2], target[2]-coarse_velocity[2]
    assert abs(adaptive_residual-12.) < .1
    assert abs(adaptive_residual-12.) < abs(coarse_residual-12.)
    assert adaptive.propellant_kg < state.propellant_kg
    assert adaptive.r_eci_m != state.r_eci_m and adaptive_velocity != target
    assert adaptive.time_s-state.time_s == pytest.approx(step)


def test_adaptive_step_minimum_and_truncated_deadline_do_not_create_an_integration(inputs):
    from dataclasses import replace
    from src.runtime import starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import vehicle
    state, profile, _, _, _ = inputs
    state = replace(state, engine_states=tuple(replace(engine, throttle=1. if index < 33 and engine.available else 0.)
                                               for index, engine in enumerate(state.engine_states)))
    body = vehicle(profile, "booster")
    observed = dyn.observe(state, body)
    _, v = shooting._local_state(state, profile)
    target = [*v[:2], v[2]+12.00001]
    minimum, _ = shooting.boostback_macro_step(state, body, observed, profile, target, [0., 0., 1.])
    deadline, _ = shooting.boostback_macro_step(state, body, observed, profile, target, [0., 0., 1.], maximum_step_s=.0005)
    assert minimum == .001 and deadline == .0005


def test_powered_preparation_turns_the_actual_body_and_spends_fuel_without_assigning_attitude(inputs):
    from dataclasses import replace
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    state, profile, catch, seed, reference = deepcopy(inputs)
    _, east, _, _, _, _ = shooting._navigation(state, profile)
    tilted = dyn.normalize_quaternion(dyn.quaternion_multiply(dyn.axis_angle(east, math.radians(20.)), state.q_body_to_eci))
    state = replace(state, propellant_kg=60000., q_body_to_eci=tilted, omega_body_rad_s=(0., 0., .0001))
    reference = None
    result = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=.5)
    assert result["requested_powered_prepare_steps"] >= 5
    assert [event["event"] for event in result["events"]] == ["cutoff"]
    assert result["final_state"]["q_body_to_eci"] != list(state.q_body_to_eci)
    assert result["final_state"]["propellant_kg"] < state.propellant_kg
    assert not result["prediction_complete"] and not result["actual_state_assigned"]
    before_up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    final_up = dyn.rotate(result["final_state"]["q_body_to_eci"], (0., 0., 1.))
    up, *_ = shooting._navigation(state, profile)
    assert env.dot(final_up, up) > env.dot(before_up, up)
    assert env.dot(final_up, up) < 1.-1e-6


def test_powered_preparation_timeout_is_an_observed_failure(inputs):
    from dataclasses import replace
    from src.runtime import starship_sixdof as dyn
    state, profile, catch, seed, _ = deepcopy(inputs)
    _, east, _, _, _, _ = shooting._navigation(state, profile)
    tilted = dyn.normalize_quaternion(dyn.quaternion_multiply(dyn.axis_angle(east, math.radians(20.)), state.q_body_to_eci))
    state = replace(state, propellant_kg=60000., q_body_to_eci=tilted, omega_body_rad_s=(0., 0., .0001))
    profile["booster_return"]["boostback_max_slew_s"] = .2
    result = shooting._forecast_boostback(state, profile, catch, seed, duration_s=1.)
    assert result["termination"] == "upright_preparation_failed"
    assert result["duration_s"] > .2 and result["duration_s"] <= .3+1e-8
    assert not result["prediction_complete"]


def test_post_prepare_point_coast_uses_actual_endpoint_and_one_full_vector_call(inputs, monkeypatch):
    from dataclasses import replace
    from src.runtime import starship_constrained_guidance as guidance
    state, profile, catch, seed, reference = deepcopy(inputs)
    state = replace(state, propellant_kg=60000., omega_body_rad_s=(0., 0., .0001))
    forecast = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=2.)
    original, calls = guidance.point_coast, []

    def point(*args, **kwargs):
        calls.append((deepcopy(args[:3]), deepcopy(kwargs)))
        return original(*args, **kwargs)

    monkeypatch.setattr(guidance, "point_coast", point)
    result = shooting._post_prepare_coast(forecast, profile, catch, seed)
    assert len(calls) == 1
    assert calls[0][0][0] == forecast["position_enu_m"] and calls[0][0][1] == forecast["velocity_enu_mps"]
    assert calls[0][0][2] == profile["booster"]["dry_mass_kg"]+forecast["propellant_kg"]
    assert calls[0][1]["full_panel_vector"] is True and calls[0][1]["entry_angle_deg"] == 35.
    assert result["input_state_sha256"] == shooting._digest(forecast["final_state"])
    assert result["ideal_arrest_preview"]["input_position_enu_m"] == result["prediction"]["position_enu_m"]
    assert not result["arrival_admitted"] and not result["support_admitted"]


def test_coast_failure_keeps_completed_short_forecast_and_separate_call_counter(inputs, monkeypatch):
    from dataclasses import replace
    state, profile, catch, seed, reference = deepcopy(inputs)
    state = replace(state, propellant_kg=60000., omega_body_rad_s=(0., 0., .0001))
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("injected_point_coast_failure")

    monkeypatch.setattr(shooting, "_post_prepare_coast", fail)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    receipt = result["actual_dynamics_prediction"]
    assert receipt["completed_forecast_count"] == receipt["attempted_forecast_count"]
    assert receipt["failed_forecast_count"] == 0
    assert receipt["point_continuation_attempted_count"] == receipt["point_continuation_failed_count"] == len(calls)
    assert receipt["point_continuation_completed_count"] == 0
    assert all(entry["status"] == "completed" and entry["point_status"] == "failed" for entry in receipt["forecasts"])
    assert receipt["selected_point_continuation"] is None and not receipt["selected_point_continuation_complete"]
    assert not result["admissible"] and not receipt["global_infeasibility_established"]


def test_partial_short_forecast_never_starts_an_ideal_coast(inputs, monkeypatch):
    state, profile, catch, seed, reference = inputs
    short_forecasts(monkeypatch)
    monkeypatch.setitem(shooting.CONFIG, "maximum_forecast_calls", 4)

    def forbidden(*args, **kwargs):
        pytest.fail("A point coast replaced an incomplete finite preparation")

    monkeypatch.setattr(shooting, "_post_prepare_coast", forbidden)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    assert result["actual_dynamics_prediction"]["point_continuation_attempted_count"] == 0


def test_ideal_arrest_after_crossing_capture_floor_is_not_a_viable_prediction(inputs):
    state, profile, catch, _, _ = inputs
    forecast = {"propellant_kg": state.propellant_kg, "final_state": json.loads(json.dumps(asdict(state)))}
    dry, fuel = profile["booster"]["dry_mass_kg"], state.propellant_kg
    com = (dry*profile["booster"]["dry_com_z_m"]+fuel*profile["booster"]["tank_center_z_m"])/(dry+fuel)
    floor = catch["support_height_m"]+catch["initial_pin_clearance_m"]+.5*catch["arm_half_width_m"]-60.+com
    coast = {"position_enu_m": [0., 0., floor-.1], "velocity_enu_mps": [.1, 0., -.1], "elapsed_s": 100.}
    result = shooting._ideal_arrest_preview(coast, forecast, profile, catch)
    assert result["termination"] == "capture_height_crossed" and not result["velocity_arrested_above_capture_floor"]
    assert result["integration_steps"] == 0 and result["required_capture_cg_height_m"] == pytest.approx(floor)


def test_ideal_arrest_retains_available_engines_spool_gravity_and_horizontal_displacement(inputs):
    state, profile, catch, _, _ = inputs
    forecast = {"propellant_kg": state.propellant_kg, "final_state": json.loads(json.dumps(asdict(state)))}
    coast = {"position_enu_m": [1000., 0., 2000.], "velocity_enu_mps": [-100., 0., -100.], "elapsed_s": 100.}
    result = shooting._ideal_arrest_preview(coast, forecast, profile, catch)
    assert result["velocity_arrested_above_capture_floor"]
    assert result["position_enu_m"][0] < 1000. and result["position_enu_m"][2] >= result["required_capture_cg_height_m"]
    assert result["propellant_kg"] < state.propellant_kg and result["elapsed_s"] > 0
    assert result["engine_availability"][5] is False
    assert result["finite_throttle_spool_integrated"] and result["gravity_integrated"]
    assert not result["attitude_dynamics_integrated"] and not result["aerodynamic_force_integrated"] and not result["support_admitted"]
