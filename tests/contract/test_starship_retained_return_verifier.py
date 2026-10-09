"""Tamper and failure-mode admission for retained-payload return evidence."""
from __future__ import annotations

from copy import deepcopy
import inspect
import json

import pytest

from src.runtime import starship_retained_return as policy
from src.runtime import starship_retained_return_verifier as verifier

A = 6378137.
RATE = 7.292115e-5


def profile():
    # Co-located dry hull/fuel/payload have an elementary, independently
    # calculable tensor: diag(645,645,553 1/3), mass1120, centroid0.
    return {"geometry": {"radius_m": 1., "ship_length_m": 2.},
            "ship": {"dry_mass_kg": 1000., "propellant_kg": 100., "dry_com_z_m": 0.,
                     "tank_center_z_m": 0., "tank_length_m": 2., "engine_count": 3,
                     "gimbal_engine_count": 3, "engine_thrust_n": 10000., "engine_isp_s": 300.,
                     "dry_inertia_kg_m2": None, "engine_positions_body_m": [[0., 0., -2.]]*3, "flap_panels": []},
            "payload": {"count": 2, "mass_each_kg": 10., "center_z_m": 0., "dimensions_m": [1., 1., 1.]},
            "actuators": {"max_gimbal_deg": 8., "gimbal_rate_deg_s": 12., "throttle_tau_s": .35, "gimbal_tau_s": .2},
            "guidance": {"flip_min_throttle": .4, "max_angular_acceleration_rad_s2": .03, "flip_altitude_m": 2000., "coast_before_return_s": 5.},
            "integration": {"powered_dt_s": .1, "coast_dt_s": .25}}


def sample(time, height, marker=None, *, phase="ballistic_return"):
    result = {"time_s": time, "body_id": "ship", "phase": phase,
              "r_eci_m": [A+height, 0., 0.], "v_eci_mps": [-100., RATE*(A+height), 0.],
              "altitude_m": height, "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.],
              "propellant_kg": 100., "mass_kg": 1120., "com_z_m": 0.,
              "inertia_kg_m2": [[645., 0., 0.], [0., 645., 0.], [0., 0., 553.+1/3]],
              "engine_states": [{"available": True, "throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0.} for _ in range(15)],
              "flap_angles_rad": [0., 0., 0.]}
    if marker:
        result["retained_return"] = marker
    return result


@pytest.fixture
def evidence():
    p = profile()
    # Cross-check independently implemented verifier against producer budget
    # on a hand-derived rigid-body fixture, with no trajectory or simulator run.
    low, high = 2000., 20000.
    for _ in range(40):
        middle = (low+high)/2
        if policy.terminal_budget(sample(100., middle), p)["trigger"]:
            low = middle
        else:
            high = middle
    activation = sample(10., 300000., "activation", phase="orbital_coast")
    previous = sample(99.9, high+5., "previous")
    trigger = sample(100., low-5., "trigger")
    budget = policy.terminal_budget(trigger, p)
    assert budget["trigger"] is True
    previous_budget = policy.terminal_budget(previous, p)
    assert previous_budget["trigger"] is False
    record = policy.new_record(policy.POLICY_ID)
    record.update(status="triggered", evaluation_count=100,
                  activation={"time_s": 10., "payload_retained_count": 2, "payload_retained_mass_kg": 20., "state": deepcopy(activation)},
                  trigger={"time_s": 100., "state": deepcopy(trigger), "budget": budget,
                           "previous": {"state": deepcopy(previous), "budget": previous_budget}})
    run = {"scenario": "deployment_no_effect", "samples": [activation, previous, trigger, sample(101., low-100., phase="landing_burn")],
           "events": [{"time_s": 10., "event": "retained_return_activated", "policy_id": policy.POLICY_ID},
                      {"time_s": 10., "event": "return_requested"},
                      {"time_s": 100., "event": "retained_return_terminal_trigger", "policy_id": policy.POLICY_ID,
                       "required_altitude_m": budget["required_altitude_m"]},
                      {"time_s": 100., "event": "flip_and_landing_command"}],
           "outcome": {"termination": "time_limit", "payload_released_count": 0, "contact_receipt": None},
           "retained_return": record}
    run["events"].insert(0, {"time_s": 5., "event": "orbit_cutoff_command"})
    return run, p


def check(evidence, expected=policy.POLICY_ID):
    result = verifier.verify_retained_return(*evidence, expected_policy=expected)
    assert result["landing_verified"] is result["mission_completed"] is result["physical_execution"] is False
    json.dumps(result, allow_nan=False)
    return result


def reject(evidence, code=None, expected=policy.POLICY_ID):
    result = check(evidence, expected)
    assert result["passed"] is False, result
    assert result["issues"]
    if code:
        assert result["issues"][0]["code"] == code, result
    return result


def test_independent_valid_trigger_does_not_claim_landing(evidence):
    result = check(evidence)
    assert result["passed"], result
    assert result["policy_active"] and result["trigger_verified"]
    assert result["observed_low_speed_contact"] is False
    assert result["contact_point_speed_mps"] is None


@pytest.mark.parametrize("expected", [policy.CONTINUOUS_POLICY_ID, policy.CONDITIONED_POLICY_ID])
def test_continuous_frame_policy_keeps_budget_but_requires_explicit_approval(evidence, expected):
    run, p = evidence
    run["retained_return"]["policy_id"] = expected
    for event in run["events"]:
        if "policy_id" in event:
            event["policy_id"] = expected
    assert check((run, p), expected)["passed"]
    reject((run, p), "approval_policy", policy.POLICY_ID)


def test_verifier_has_no_producer_or_simulator_import():
    source = inspect.getsource(verifier)
    assert "import numpy" not in source
    import ast
    imports = [n.module for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom) and n.level]
    assert imports == ["starship_return_feasibility_verifier"]
    from pathlib import Path
    independent = (Path(__file__).resolve().parents[2]/"src/runtime/starship_return_feasibility_verifier.py").read_text()
    assert "from ." not in independent and "import numpy" not in independent
    assert "simulate(" not in independent
    assert "import starship" not in source


def test_fixed_policy_valid_without_activation(evidence):
    run, p = evidence
    run["retained_return"] = policy.new_record("fixed_v1")
    run["events"] = [e for e in run["events"] if not e["event"].startswith("retained_return")]
    for state in run["samples"]:
        state.pop("retained_return", None)
    result = check((run, p), "fixed_v1")
    assert result["passed"] and not result["policy_active"]


def test_fixed_policy_cannot_hide_an_adaptive_command(evidence):
    run, p = evidence
    run["retained_return"] = policy.new_record("fixed_v1")
    reject((run, p), "fixed_policy", "fixed_v1")


def test_unreached_return_is_valid_nonactivation(evidence):
    run, p = evidence
    run["retained_return"] = policy.new_record(policy.POLICY_ID)
    run["events"] = []
    run["samples"] = [sample(1., 10000., phase="ship_ascent")]
    result = check((run, p))
    assert result["passed"] and not result["policy_active"]


def test_activation_without_terminal_trigger_is_valid_time_limit(evidence):
    run, p = evidence
    run["retained_return"].update(status="active", trigger=None, evaluation_count=0)
    run["events"] = run["events"][:3]
    run["samples"] = run["samples"][:1]
    result = check((run, p))
    assert result["passed"] and result["policy_active"] and not result["trigger_verified"]


@pytest.mark.parametrize("speed,termination,low", [(238.74, "surface_impact", False), (4.5, "low_speed_surface_contact", True)])
def test_contact_observation_is_separate_from_valid_record(evidence, speed, termination, low):
    run, _ = evidence
    run["outcome"].update(termination=termination, contact_receipt={"contact": True, "landing_verified": False,
        "starship_vehicle_validated": False, "surface_relative_velocity_mps": [speed, 0., 0.], "surface_relative_speed_mps": speed})
    result = check(evidence)
    assert result["passed"] and result["observed_low_speed_contact"] is low
    assert result["contact_point_speed_mps"] == speed


@pytest.mark.parametrize("field", ["mass_kg", "propellant_kg", "time_s", "vertical_speed_mps", "mach", "gravity_mps2",
                                   "thrust_axis_angle_rad", "transverse_rate_rad_s", "available_thrust_n", "tvc_moment_estimate_nm",
                                   "inertia_upper_kg_m2", "slew_accel_estimate_rad_s2", "aligned_net_acceleration_mps2",
                                   "actuator_lag_s", "preparation_time_s", "required_altitude_m", "fuel_budget_estimate_kg",
                                   "fuel_margin_estimate_kg"])
def test_each_budget_identity_is_recomputed(evidence, field):
    evidence[0]["retained_return"]["trigger"]["budget"][field] += 1.
    reject(evidence, "budget_identity")


@pytest.mark.parametrize("field,value", [("trigger", False), ("applicable", False), ("reason", "custom"),
                                         ("available_landing_engines", True), ("available_landing_engines", 2)])
def test_discrete_budget_fields_are_not_trusted(evidence, field, value):
    evidence[0]["retained_return"]["trigger"]["budget"][field] = value
    reject(evidence, "budget_identity")


@pytest.mark.parametrize("field,value", [("mass_kg", 1164.2), ("com_z_m", 1.), ("propellant_kg", -1.),
                                         ("inertia_kg_m2", [[645., 0., 0.], [0., 644., 0.], [0., 0., 553.+1/3]])])
def test_matching_forged_snapshots_still_fail_composition(evidence, field, value):
    run, _ = evidence
    run["retained_return"]["activation"]["state"][field] = deepcopy(value)
    run["samples"][0][field] = deepcopy(value)
    reject(evidence)


def test_approval_policy_mismatch_rejected(evidence):
    reject(evidence, "approval_policy", "fixed_v1")


@pytest.mark.parametrize("field,value", [("payload_retained_count", 1), ("payload_retained_count", True), ("payload_retained_mass_kg", 10.)])
def test_retained_payload_is_bound_to_releases(evidence, field, value):
    evidence[0]["retained_return"]["activation"][field] = value
    reject(evidence, "payload")


def test_declared_released_count_without_release_event_rejected(evidence):
    evidence[0]["outcome"]["payload_released_count"] = 1
    reject(evidence, "payload")


def test_missing_activation_does_not_pass_after_return(evidence):
    evidence[0]["retained_return"] = policy.new_record(policy.POLICY_ID)
    reject(evidence, "activation")


def test_activation_must_use_observed_return_time(evidence):
    next(e for e in evidence[0]["events"] if e["event"] == "return_requested")["time_s"] += 10.
    reject(evidence, "activation")


def test_full_actuator_state_binds_record_to_trajectory(evidence):
    evidence[0]["retained_return"]["trigger"]["state"]["engine_states"][0]["available"] = False
    reject(evidence, "state_binding")


def test_previous_step_must_be_bound_to_saved_sample(evidence):
    evidence[0]["samples"] = [s for s in evidence[0]["samples"] if s.get("retained_return") != "previous"]
    reject(evidence, "state_binding")


def test_previous_step_cannot_already_have_crossed(evidence):
    run, p = evidence
    prior = deepcopy(run["samples"][2])
    prior.update(time_s=99.9, retained_return="previous")
    run["samples"][1] = deepcopy(prior)
    run["retained_return"]["trigger"]["previous"] = {"state": prior, "budget": policy.terminal_budget(prior, p)}
    reject(evidence, "previous")


def test_previous_step_cannot_be_stale(evidence):
    run, _ = evidence
    run["samples"][1]["time_s"] = run["retained_return"]["trigger"]["previous"]["state"]["time_s"] = 95.
    run["retained_return"]["trigger"]["previous"]["budget"]["time_s"] = 95.
    reject(evidence, "previous")


def test_missing_executed_flip_command_is_not_a_trigger(evidence):
    evidence[0]["events"] = [e for e in evidence[0]["events"] if e["event"] != "flip_and_landing_command"]
    reject(evidence, "trigger_event")


@pytest.mark.parametrize("value", [None, [], True, "fixed_v1", float("nan")])
def test_malformed_records_fail_closed(evidence, value):
    evidence[0]["retained_return"] = value
    reject(evidence)


@pytest.mark.parametrize("value", [True, -1, 1.5, 1000001])
def test_evaluation_count_requires_bounded_integer(evidence, value):
    evidence[0]["retained_return"]["evaluation_count"] = value
    reject(evidence, "evaluation_count")


def test_nonfinite_cycle_and_extra_budget_fail_closed(evidence):
    run, p = evidence
    bad = deepcopy(run)
    bad["retained_return"]["extra"] = float("nan")
    reject((bad, p), "invalid_number")
    bad = deepcopy(run)
    bad["retained_return"]["extra"] = bad["retained_return"]
    reject((bad, p), "invalid_json")
    bad = deepcopy(run)
    bad["retained_return"]["trigger"]["budget"]["magic_margin"] = 1000.
    reject((bad, p), "budget_schema")


def test_no_available_propulsion_produces_valid_null_diagnostic(evidence):
    run, p = evidence
    state = deepcopy(run["samples"][2])
    for e in state["engine_states"][:3]:
        e["available"] = False
    expected = policy.terminal_budget(state, p)
    assert expected["preparation_time_s"] is expected["required_altitude_m"] is None
    actual = verifier._check_budget(expected, state, p, 2)
    assert actual["reason"] == "insufficient_modeled_propulsion"
    assert actual["required_altitude_m"] is None
    assert expected["trigger"] is False


def test_configured_return_time_cannot_be_retimed(evidence):
    run, _ = evidence
    next(e for e in run["events"] if e["event"] == "orbit_cutoff_command")["time_s"] = 1.
    reject(evidence, "return_timing")


def test_active_record_cannot_conceal_an_earlier_crossing(evidence):
    run, _ = evidence
    run["retained_return"].update(status="active", trigger=None)
    run["events"] = run["events"][:3]
    for state in run["samples"][1:]:
        state.pop("retained_return", None)
    run["samples"][-1]["phase"] = "ballistic_return"
    reject(evidence, "earlier_crossing")


def _remove_policy_history(run):
    run["retained_return"] = policy.new_record(policy.POLICY_ID)
    run["events"] = [e for e in run["events"] if e["event"] == "orbit_cutoff_command"]
    for state in run["samples"]:
        state.pop("retained_return", None)


def test_deleting_policy_events_does_not_make_completed_return_unactivated(evidence):
    run, _ = evidence
    _remove_policy_history(run)
    reject(evidence, "activation")


@pytest.mark.parametrize("phase", ["deorbit_slew", "deorbit_burn"])
def test_physical_deorbit_phase_requires_activation_even_without_events(evidence, phase):
    run, _ = evidence
    _remove_policy_history(run)
    run["events"] = []
    run["samples"] = [sample(12., 300000., phase=phase)]
    reject(evidence, "activation")


def test_elapsed_coast_independently_requires_activation(evidence):
    run, _ = evidence
    _remove_policy_history(run)
    run["samples"] = [sample(5., 300000., phase="orbital_coast"), sample(20., 300000., phase="orbital_coast")]
    reject(evidence, "activation")


def test_exact_horizon_before_processing_return_remains_valid(evidence):
    run, _ = evidence
    _remove_policy_history(run)
    run["samples"] = [sample(5., 300000., phase="orbital_coast"), sample(10., 300000., phase="orbital_coast")]
    assert check(evidence)["passed"]


def test_zero_evaluations_cannot_cover_observed_ballistic_flight(evidence):
    run, _ = evidence
    run["retained_return"].update(status="active", trigger=None, evaluation_count=0)
    run["events"] = run["events"][:3]
    for state in run["samples"][1:]:
        state.pop("retained_return", None)
    reject(evidence, "evaluation_count")


def test_one_evaluation_cannot_delete_adjacent_crossing_proof(evidence):
    run, _ = evidence
    run["retained_return"]["evaluation_count"] = 1
    run["retained_return"]["trigger"]["previous"] = None
    run["samples"][1].pop("retained_return")
    reject(evidence, "evaluation_count")


def test_landing_phase_requires_trigger_even_when_events_and_prior_samples_deleted(evidence):
    run, _ = evidence
    run["retained_return"].update(status="active", trigger=None, evaluation_count=0)
    run["events"] = run["events"][:3]
    run["samples"] = [run["samples"][0], run["samples"][-1]]
    reject(evidence, "trigger")
