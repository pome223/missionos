"""Finite command preparation and conservative signed-pair authority guards."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from src.runtime import starship_entry_pretrim as pretrim
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_booster_control import control_coast_stopping_distance
from src.runtime.starship_sixdof_mission import _attitude, vehicle


@pytest.fixture
def configured():
    path = Path(__file__).resolve().parents[2]/"examples"/"spaceflight"/"starship-sixdof-profile.json"
    profile = json.loads(path.read_text())
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              80000., 100000., time_s=250.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(point.time_s, point.r, env.add(point.v, env.scale(up, -500.)),
        _attitude(up, east), (0., 0., 0.), 100000.,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    return profile, body, state


def base(profile, body, state):
    return control_coast_stopping_distance(state, body, state.q_body_to_eci, profile, control_interval_s=.1)


def trim():
    return [0., 0., 0., 0., -.38, .38]


def test_accepted_command_preserves_engines_and_only_predicts_finite_fin_motion(configured):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    before = deepcopy((asdict(state), diagnostic, profile))
    targets = trim()
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, targets, interval_s=.1)
    assert receipt["status"] == "accepted"
    assert result.engines == command.engines
    assert result.flap_angles_rad == tuple(targets)
    assert receipt["actual_near_trim"] is False and receipt["prepared"] is False
    assert receipt["probe_attempted_count"] == receipt["probe_completed_count"] == 5
    assert receipt["probe_failed_count"] == 0
    assert [p["fraction"] for p in receipt["probes"]] == [.2, .4, .6, .8, 1.]
    assert all(p["sampled_headroom_met"] for p in receipt["probes"])
    assert receipt["rcs_signed_capacity_nm"] == [[80000., 80000.]]*3
    assert all(group["supported_group"] for group in receipt["rcs_groups"])
    assert receipt["actual_state_assigned"] is False and receipt["arrival_admitted"] is False
    assert receipt["support_admitted"] is False and receipt["production_policy_admitted"] is False
    json.dumps(receipt, allow_nan=False)
    assert (asdict(state), diagnostic, profile) == before
    assert targets == trim()
    # A tiny real finite-actuator step checks prediction; this is not a flight.
    endpoint = dyn.step(state, body, result, .1, gravity=False, atmosphere=False)
    assert endpoint.flap_angles_rad == pytest.approx(receipt["probes"][-1]["predicted_flap_angles_rad"], abs=1e-7)
    assert abs(endpoint.flap_angles_rad[4]) < abs(targets[4])


def test_prepared_gate_is_actual_input_angles_not_predicted_endpoint(configured):
    profile, body, state = configured
    state = replace(state, flap_angles_rad=tuple(trim()))
    command, diagnostic = base(profile, body, state)
    _, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert receipt["status"] == "accepted"
    assert receipt["actual_near_trim"] is True and receipt["prepared"] is True
    assert receipt["measured_trim_error_deg"] == 0.


@pytest.mark.parametrize("invalid", [None, [0.]*5, [0., 0., .1, 0., -.38, .38],
                                      [0., 0., 0., 0., -.5, .38], [0., 0., 0., True, -.38, .38],
                                      [0., 0., 0., float("nan"), -.38, .38]])
def test_bad_trim_reference_defers_identical_base_command(configured, invalid):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, invalid, interval_s=.1)
    assert result is command and receipt["reason"] == "invalid_trim_reference"
    assert receipt["prepared"] is False and receipt["probe_attempted_count"] == 0


@pytest.mark.parametrize("interval", [True, 0., -.1, .26, float("nan")])
def test_bad_interval_defers_without_probe(configured, interval):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=interval)
    assert result is command and receipt["reason"] == "invalid_interval"
    assert receipt["probe_attempted_count"] == 0


@pytest.mark.parametrize("key,value", [("control_allocation", "other"), ("main_engine_commands_off", False),
                                       ("target_q_body_to_eci", [2., 0., 0., 0.]),
                                       ("requested_torque_body_nm", [0., float("nan"), 0.])])
def test_invalid_coast_reference_never_waives_guards(configured, key, value):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    diagnostic = {**diagnostic, key: value}
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "invalid_base_reference"
    assert receipt["prepared"] is False


def test_high_dynamic_pressure_defers_before_any_probe(configured):
    profile, body, state = configured
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              1000., state.propellant_kg, time_s=state.time_s)
    up, east, _ = env.local_frame(point)
    state = replace(state, r_eci_m=point.r, v_eci_mps=env.add(point.v, env.scale(up, -1000.)),
                    q_body_to_eci=_attitude(up, east))
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "outside_low_dynamic_pressure"
    assert receipt["dynamic_pressure_pa"] > 100.
    assert receipt["probe_attempted_count"] == 0


def test_one_missing_rcs_mate_rejects_coupled_partial_group(configured):
    profile, body, state = configured
    engines = list(state.engine_states)
    engines[33] = replace(engines[33], available=False)
    state = replace(state, engine_states=tuple(engines))
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "unsupported_or_unbalanced_rcs_group"
    group = receipt["rcs_groups"][0]
    assert group["available_indices"] == [34]
    assert group["supported_group"] is False
    assert any(abs(x) > 0. for x in group["force_sum_body_n"])


def test_both_missing_jets_remove_bidirectional_authority(configured):
    profile, body, state = configured
    engines = list(state.engine_states)
    for index in (33, 34):
        engines[index] = replace(engines[index], available=False)
    state = replace(state, engine_states=tuple(engines))
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "unavailable_bidirectional_rcs"
    assert receipt["rcs_signed_capacity_nm"][0][0] == 0.


def test_rcs_cross_axis_geometry_cannot_be_treated_as_a_diagonal_box(configured):
    profile, body, state = configured
    engines = list(body.engines)
    index = next(i for i, engine in enumerate(engines) if engine.name == "rcs_2_1_1")
    engine = engines[index]
    engines[index] = replace(engine, position_body_m=(engine.position_body_m[0], engine.position_body_m[1], engine.position_body_m[2]+1.))
    body = replace(body, engines=tuple(engines))
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "unsupported_or_unbalanced_rcs_group"
    assert receipt["rcs_groups"][-1]["force_and_cross_axis_balanced"] is False


def test_mismatched_actual_pair_throttles_are_not_supported_authority(configured):
    profile, body, state = configured
    engines = list(state.engine_states)
    engines[33] = replace(engines[33], throttle=.1)
    state = replace(state, engine_states=tuple(engines))
    command, diagnostic = base(profile, body, state)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "unsupported_or_unbalanced_rcs_group"
    assert receipt["rcs_groups"][0]["matched_actual_throttle"] is False


def test_command_and_declared_demand_must_match(configured):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    diagnostic = deepcopy(diagnostic)
    diagnostic["requested_torque_body_nm"][1] += 10.
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "inconsistent_base_rcs_command"
    assert receipt["probe_attempted_count"] == 0


def test_probe_headroom_excess_retains_all_five_negative_samples(configured, monkeypatch):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    observed = dyn.observe(state, body)
    calls = 0
    def predicted(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = deepcopy(observed)
        if calls > 1:
            result["aero_torque_body_nm"] = list(result["aero_torque_body_nm"])
            result["aero_torque_body_nm"][1] += 1e6
        return result
    monkeypatch.setattr(pretrim.dyn, "observe", predicted)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "sampled_rcs_headroom_exceeded"
    assert receipt["probe_attempted_count"] == receipt["probe_completed_count"] == 5
    assert all(not probe["sampled_headroom_met"] for probe in receipt["probes"])
    assert receipt["prepared"] is False


def test_probe_exception_is_deferred_with_fixed_class_and_no_secret_text(configured, monkeypatch):
    profile, body, state = configured
    command, diagnostic = base(profile, body, state)
    observed = dyn.observe(state, body)
    calls = 0
    def fails(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("secret-token and private local path")
        return observed
    monkeypatch.setattr(pretrim.dyn, "observe", fails)
    result, receipt = pretrim.prepare_entry_fins(state, body, command, diagnostic, profile, trim(), interval_s=.1)
    assert result is command and receipt["reason"] == "prediction_failure"
    assert receipt["error_class"] == "RuntimeError"
    assert receipt["probe_attempted_count"] == receipt["probe_failed_count"] == 1
    assert receipt["probe_completed_count"] == 0
    assert "secret-token" not in json.dumps(receipt)
