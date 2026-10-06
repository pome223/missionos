"""Opt-in configuration/reference tests without physical integration.

Synthetic forecast bookkeeping below cannot establish any vehicle trajectory.
Actual plant-prefix checks are separately admitted by the study runner.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime import starship_boostback_shooting as shooting
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_sixdof_mission import _attitude, vehicle


def fixture():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 80000., time_s=100.)
    up, east, _ = env.local_frame(point)
    q = _attitude(up, east)
    state = dyn.State6DOF(100., point.r, point.v, q, (0., 0., 0.), 60000.,
        tuple(dyn.EngineState(throttle=.4 if i < 3 else 0.) for i in range(len(body.engines))),
        tuple(0. for _ in body.aero_panels))
    reference = ConditionedGeographicFrame(q, 100., maximum_roll_rate_rad_s=.03/.28)
    seed = {"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., 0.],
        "point_post_shutdown_position_enu_m": [0., 0., 80000.], "point_post_shutdown_velocity_enu_mps": [0., 0., 0.],
        "bank_heading_enu": [1., 0., 0.], "bank_sign": -1,
        "prediction_is_execution": False, "production_policy_admitted": False}
    return state, profile, catch, seed, reference, body, up, east


def test_default_configuration_and_public_optional_signatures_stay_legacy():
    _, profile, _, _, _, _, _, _ = fixture()
    original = deepcopy(shooting.CONFIG)
    assert shooting.FORECAST_SCHEMA == "missionos.starship_short_boostback_shooting.v3"
    assert shooting._forecast_configuration(profile, False) == original
    assert shooting._forecast_configuration(profile, False) is not shooting.CONFIG
    for function in (shooting._forecast_boostback, shooting.refine_boostback_plan):
        parameter = inspect.signature(function).parameters["development_transport_prepare_roll"]
        assert parameter.default is False and parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert "prepare_roll_policy" not in original and "shutdown_tail_model" not in original
    assert shooting.CONFIG == original


def test_opt_in_configuration_uses_actual_main_engine_tau_without_mutating_profile():
    _, profile, _, _, _, body, _, _ = fixture()
    original, config = deepcopy(profile), deepcopy(shooting.CONFIG)
    # Distinguish maximum main-engine lag from a different RCS lag without
    # changing production coefficients or constructing a synthetic trajectory.
    engines = list(body.engines)
    engines[0] = replace(engines[0], throttle_time_constant_s=.6)
    engines[-1] = replace(engines[-1], throttle_time_constant_s=10.)
    declaration = shooting._forecast_configuration(profile, True, body=replace(body, engines=tuple(engines)))
    assert declaration["shutdown_tail_s"] == 2.4
    assert declaration["prepare_roll_policy"] == "parallel_transport_deferred_geographic_roll_v1"
    assert declaration["post_cutoff_control"] == "powered_upright_prepare_with_transport_roll"
    assert declaration["shutdown_tail_model"] == "four_max_main_throttle_tau_command_off_transport"
    assert profile == original and shooting.CONFIG == config


@pytest.mark.parametrize("flag", [None, 0, 1, "true", [], {}])
def test_non_boolean_opt_in_is_refused_before_any_forecast(flag, monkeypatch):
    state, profile, catch, seed, reference, _, _, _ = fixture()
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid opt-in reached an integration")
    monkeypatch.setattr(dyn, "step", forbidden)
    for function in (shooting._forecast_boostback, shooting.refine_boostback_plan):
        with pytest.raises(ValueError, match="invalid_development_transport_prepare_roll"):
            function(state, profile, catch, seed, reference=reference, development_transport_prepare_roll=flag)


def test_deferred_goal_roll_avoids_an_upright_half_turn_without_assigning_body_pose():
    state, _, _, _, _, _, up, east = fixture()
    before = asdict(state)
    rolled = dyn.normalize_quaternion(dyn.quaternion_multiply(dyn.axis_angle(up, math.pi), state.q_body_to_eci))
    inherited = ConditionedGeographicFrame(rolled, 100., maximum_roll_rate_rad_s=.03/.28)
    legacy, transport = deepcopy(inherited), deepcopy(inherited)
    preferred = _attitude(up, east)
    geographic = legacy.target(up, east, preferred, time_s=100.1)
    carried = transport.target(up, east, preferred, time_s=100.1,
        force_bridge=True, defer_geographic_reacquisition=True)
    x_before = dyn.rotate(rolled, (1., 0., 0.))
    assert env.dot(dyn.rotate(geographic, (1., 0., 0.)), x_before) < -.999999
    assert env.dot(dyn.rotate(carried, (1., 0., 0.)), x_before) > .999999
    assert env.dot(dyn.rotate(carried, (0., 0., 1.)), up) > .999999
    assert transport.diagnostics["geographic_roll_deferred"] is True
    assert transport.diagnostics["mode"] == "transport_deferred_geographic_roll"
    assert "geographic_roll_deferred" not in legacy.diagnostics
    assert asdict(state) == before


def _stub_loop(monkeypatch, *, prepare=False):
    """Record requested reference/control phases; no nonlinear plant is called."""
    state, profile, catch, seed, reference, body, up, east = fixture()
    if prepare:
        tilted = dyn.quaternion_multiply(dyn.axis_angle(east, math.radians(20.)), state.q_body_to_eci)
        state = replace(state, q_body_to_eci=tilted)
    calls, commanded_counts = [], []
    original = ConditionedGeographicFrame.target
    def target(self, *args, **kwargs):
        calls.append(deepcopy(kwargs))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ConditionedGeographicFrame, "target", target)
    def coast(*args, **kwargs):
        commanded_counts.append(0)
        return None, {}
    def control(state, vehicle, target, throttle, count, profile, **kwargs):
        commanded_counts.append(count)
        return None, {}
    monkeypatch.setattr(shooting, "control_coast_stopping_distance", coast)
    monkeypatch.setattr(shooting, "control_with_measured_tvc", control)
    # Clock-only substitutions are bookkeeping fixtures, never physical states
    # or evidence that the model can execute the requested preparation.
    monkeypatch.setattr(dyn, "step", lambda state, body, command, step: replace(state, time_s=state.time_s+step))
    return state, profile, catch, seed, reference, calls, commanded_counts


@pytest.mark.parametrize("prepare", [False, True])
def test_true_forecast_passes_transport_options_in_powered_prepare_and_command_off_tail(monkeypatch, prepare):
    state, profile, catch, seed, reference, calls, counts = _stub_loop(monkeypatch, prepare=prepare)
    result = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=.3,
        development_transport_prepare_roll=True)
    assert calls and all(call["force_bridge"] is True and call["defer_geographic_reacquisition"] is True for call in calls)
    assert all(count == (3 if prepare else 0) for count in counts)
    assert result["development_transport_prepare_roll"] is True
    assert result["shutdown_tail_s"] == 4*profile["actuators"]["throttle_tau_s"]
    assert result["final_state"]["q_body_to_eci"] == list(state.q_body_to_eci)
    assert not result["actual_state_assigned"] and not result["arrival_forecast"]


def test_omitted_and_false_forecast_paths_pass_no_new_reference_keywords_and_match_exactly(monkeypatch):
    state, profile, catch, seed, reference, calls, _ = _stub_loop(monkeypatch)
    default = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=.3)
    explicit = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=.3,
        development_transport_prepare_roll=False)
    assert default == explicit
    assert calls and all(set(call) == {"time_s"} for call in calls)
    assert "development_transport_prepare_roll" not in default
    assert "shutdown_tail_model" not in default and "prepare_roll_policy" not in default


def test_opt_in_tail_bookkeeping_uses_four_main_tau_instead_of_legacy_constant(monkeypatch):
    state, profile, catch, seed, reference, _, counts = _stub_loop(monkeypatch)
    profile["actuators"]["throttle_tau_s"] = .2
    result = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=1.,
        development_transport_prepare_roll=True)
    assert result["prediction_complete"]
    assert result["duration_s"] == pytest.approx(.8)
    assert result["shutdown_tail_s"] == .8 and all(count == 0 for count in counts)
    tail = result["events"][-1]
    assert tail["event"] == "transport_shutdown_tail_complete"
    assert tail["command_off_elapsed_s"] >= tail["minimum_tail_s"]-1e-9
    assert tail["actual_tilt_deg"] <= 5. and tail["actual_body_rate_rad_s"] < .003


@pytest.mark.parametrize("failed_gate", ["tilt", "rate"])
def test_opt_in_minimum_tail_does_not_replace_final_measured_orientation_gate(monkeypatch, failed_gate):
    state, profile, catch, seed, reference, _, counts = _stub_loop(monkeypatch)
    profile["actuators"]["throttle_tau_s"] = .2
    _, east, _, _, _, _ = shooting._navigation(state, profile)
    tilted = dyn.quaternion_multiply(dyn.axis_angle(east, math.radians(10.)), state.q_body_to_eci)
    def fixture_clock(state_now, body, command, step):
        now = state_now.time_s+step
        unsettled = now < 101.2-1e-8
        return replace(state_now, time_s=now,
            q_body_to_eci=tilted if unsettled and failed_gate == "tilt" else state.q_body_to_eci,
            omega_body_rad_s=(.003, 0., 0.) if unsettled and failed_gate == "rate" else (0., 0., 0.))
    monkeypatch.setattr(dyn, "step", fixture_clock)
    result = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=1.5,
        development_transport_prepare_roll=True)
    assert result["duration_s"] == pytest.approx(1.2)
    assert result["duration_s"] > result["shutdown_tail_s"]
    assert all(count == 0 for count in counts)
    assert result["prediction_complete"]
    assert result["events"][-1]["actual_tilt_deg"] <= 5.
    assert result["events"][-1]["actual_body_rate_rad_s"] < .003


def test_opt_in_unsettled_tail_keeps_original_cutoff_timeout_and_failed_evidence(monkeypatch):
    state, profile, catch, seed, reference, _, counts = _stub_loop(monkeypatch)
    profile["booster_return"]["boostback_max_slew_s"] = .5
    monkeypatch.setattr(dyn, "step", lambda state, body, command, step:
        replace(state, time_s=state.time_s+step, omega_body_rad_s=(.004, 0., 0.)))
    result = shooting._forecast_boostback(state, profile, catch, seed, reference=reference, duration_s=2.,
        development_transport_prepare_roll=True)
    assert result["termination"] == "upright_preparation_failed"
    assert result["duration_s"] > .5 and result["duration_s"] <= .7
    assert all(count == 0 for count in counts)
    assert not result["prediction_complete"]
    assert "transport_shutdown_tail_complete" not in [event["event"] for event in result["events"]]


@pytest.mark.parametrize("flag", [False, True])
def test_refine_configuration_schema_and_forecast_flag_are_bound_without_integration(monkeypatch, flag):
    import scipy.optimize
    state, profile, catch, seed, reference, _, _, _ = fixture()
    configuration, original = deepcopy(shooting.CONFIG), deepcopy(profile)
    calls = []
    def forecast(initial, profile, catch, candidate, **kwargs):
        calls.append(deepcopy(kwargs))
        return {"prediction_complete": False, "duration_s": 0., "position_enu_m": [0., 0., 80000.],
            "velocity_enu_mps": [0., 0., 0.], "propellant_kg": initial.propellant_kg}
    def solver(objective, parameters, **kwargs):
        objective(parameters)
        return SimpleNamespace(success=True, nfev=1)
    monkeypatch.setattr(shooting, "_forecast_boostback", forecast)
    monkeypatch.setattr(scipy.optimize, "least_squares", solver)
    result = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference,
                                          development_transport_prepare_roll=flag)
    receipt = result["actual_dynamics_prediction"]
    assert len(calls) == receipt["attempted_forecast_count"] == 1
    assert receipt["schema"] == (shooting.TRANSPORT_FORECAST_SCHEMA if flag else shooting.FORECAST_SCHEMA)
    if flag:
        assert calls[0]["development_transport_prepare_roll"] is True
        assert receipt["development_transport_prepare_roll"] is True
        assert receipt["configuration"]["prepare_roll_policy"] == shooting.TRANSPORT_PREPARE_POLICY
    else:
        assert "development_transport_prepare_roll" not in calls[0] and "development_transport_prepare_roll" not in receipt
        assert receipt["configuration"] == configuration
    assert shooting.CONFIG == configuration and profile == original
    assert result["seed_point_plan"] == seed
    assert result["bank_heading_enu"] == seed["bank_heading_enu"] and result["bank_sign"] == seed["bank_sign"]
    assert not result["admissible"] and not receipt["support_admitted"]
