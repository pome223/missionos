"""Stored-output admission is independent of flight success and producer flags."""
from __future__ import annotations

from copy import deepcopy
import ast
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_sixdof_verifier as verifier


ROOT = Path(__file__).resolve().parents[2]
A = 6_378_137.
OMEGA = 7.292115e-5


def sample(time=0., *, position=None, velocity=None):
    position = position or [A+15_000., 0., 0.]
    velocity = velocity or [0., OMEGA*position[0], -10.]
    relative = [velocity[0]+OMEGA*position[1], velocity[1]-OMEGA*position[0], velocity[2]]
    return {"time_s": time, "body_id": "ship", "phase": "entry_perturbation",
            "r_eci_m": position, "v_eci_mps": velocity, "q_body_to_eci": [1., 0., 0., 0.],
            "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 100., "mass_kg": 1000.,
            "altitude_m": position[0]-A, "ground_speed_mps": math.hypot(*relative),
            "com_z_m": 0., "com_rate_body_mps": [0., 0., 0.], "dynamic_pressure_pa": 1., "contact": False}


def study_with(first, last, *, scenario="entry_perturbation", termination="time_limit", contact=None):
    outcome = {"termination": termination, "phase": last["phase"], "duration_s": last["time_s"],
               "integration_steps": 1, "max_altitude_m": max(first["altitude_m"], last["altitude_m"]),
               "final_ground_speed_mps": last["ground_speed_mps"], "final_altitude_m": last["altitude_m"],
               "orbit_gate_reached": False, "payload_released_count": 0, "booster_return_invoked": False,
               "six_dof_integrated": True, "attitude_prescribed": False, "starship_vehicle_validated": False,
               "contact_receipt": contact}
    events = [{"time_s": 0., "event": "initial_state"}, {"time_s": last["time_s"], "event": termination}]
    if contact is not None:
        events[-1]["contact"] = deepcopy(contact)
    return {"schema": "missionos.starship_sixdof_study.v1",
            "profile": {"payload": {"count": 26, "mass_each_kg": 1700.},
                        "geometry": {"radius_m": 4.5, "ship_length_m": 52., "booster_length_m": 71.},
                        "guidance": {"target_perigee_m": 228_000., "release_max_rate_rad_s": .01}},
            "runs": [{"scenario": scenario, "samples": [first, last], "initial_state": deepcopy(first),
                      "final_state": deepcopy(last), "events": events, "outcome": outcome,
                      "satellites": [], "booster_run": None, "booster_separation_state": None}],
            "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False}}


@pytest.fixture
def study():
    return study_with(sample(), sample(1.))


@pytest.fixture
def impact_study():
    first = sample(position=[A+101., 0., 0.], velocity=[-100., OMEGA*(A+101.), 0.])
    last = sample(1., position=[A+1., 0., 0.], velocity=[-100., OMEGA*(A+1.), 0.])
    first["phase"] = last["phase"] = "landing_burn"
    last["contact"] = True
    receipt = {"contact": True, "event_time_s": 1., "initial_overlap": False, "time_bracket_s": [1.-1e-9, 1.],
               "point_eci_m": [A, 0., 0.], "point_body_m": [-1., 0., 0.], "signed_clearance_m": 0.,
               "point_velocity_eci_mps": [-100., OMEGA*(A+1.), 0.], "surface_normal_eci": [1., 0., 0.],
               "surface_relative_velocity_mps": [-100., OMEGA, 0.], "surface_relative_speed_mps": math.hypot(100., OMEGA),
               "surface_normal_speed_mps": -100., "landing_verified": False, "starship_vehicle_validated": False,
               "contact_response_modeled": False, "q_body_to_eci": last["q_body_to_eci"],
               "omega_body_rad_s": last["omega_body_rad_s"], "propellant_kg": last["propellant_kg"]}
    return study_with(first, last, scenario="launch", termination="surface_impact", contact=receipt)


def assert_rejected(study, expected=None):
    result = verifier.verify_study(study, expected)
    assert result["passed"] is False
    assert result["issues"]
    assert len(result["issues"]) <= 5
    assert result["mission_completed"] is False
    assert result["physical_execution"] is False
    json.dumps(result, allow_nan=False)
    return result


def test_time_limit_is_valid_evidence_not_mission_success(study):
    result = verifier.verify_study(study, "entry_perturbation")
    assert result["passed"] is True, result
    observed = result["observed_outcomes"][0]
    assert observed["termination"] == "time_limit"
    assert observed["mission_completed"] is False
    assert observed["physical_execution"] is False
    assert observed["simulation_success"] is None
    assert observed["final_contact_point_speed_mps"] is None
    assert observed["booster_catch_verified"] is None


def test_observed_impact_passes_integrity_without_becoming_success(impact_study):
    result = verifier.verify_study(impact_study, "launch")
    assert result["passed"] is True, result
    observed = result["observed_outcomes"][0]
    assert observed["termination"] == "surface_impact"
    assert observed["final_contact_point_speed_mps"] == pytest.approx(100.)
    assert observed["simulation_success"] is None
    assert observed["mission_completed"] is False


def test_numerical_failure_with_finite_last_state_is_preserved(study):
    run = study["runs"][0]
    run["events"][-1]["event"] = run["outcome"]["termination"] = "numerical_failure"
    result = verifier.verify_study(study)
    assert result["passed"] is True
    assert result["observed_outcomes"][0]["termination"] == "numerical_failure"


@pytest.mark.parametrize("value", [None, [], "study", 1, True, {"schema": "wrong"}])
def test_malformed_outer_objects_fail_closed(value):
    assert_rejected(value)


@pytest.mark.parametrize("expected", ["unknown", [], {}, True, 1])
def test_bad_expected_scenario_is_bounded(study, expected):
    assert_rejected(study, expected)


def test_approved_scenario_must_match_exact_run_set(study):
    assert_rejected(study, "launch")
    assert_rejected(study, "all")
    study["runs"].append(deepcopy(study["runs"][0]))
    assert_rejected(study)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10**500])
def test_nonfinite_even_in_uninterpreted_fields_fails_closed(study, value):
    study["extra"] = {"ignored_by_producer": value}
    assert_rejected(study)


def test_cycles_and_depth_are_bounded(study):
    study["cycle"] = study
    assert_rejected(study)
    current = nested = {}
    for _ in range(60):
        current["nested"] = {}
        current = current["nested"]
    assert_rejected(nested)


@pytest.mark.parametrize("field,value", [("physical_execution_invoked", True), ("starship_vehicle_validated", True),
                                         ("physical_execution_invoked", 0)])
def test_provenance_requires_explicit_false(study, field, value):
    study["provenance"][field] = value
    assert_rejected(study)


@pytest.mark.parametrize("field,value", [("time_s", -1), ("time_s", True), ("r_eci_m", [0., 0., 0.]),
                                         ("q_body_to_eci", [2., 0., 0., 0.]), ("omega_body_rad_s", [0., 1.]),
                                         ("propellant_kg", -1), ("ground_speed_mps", 999), ("mass_kg", 0)])
def test_trajectory_state_constraints(study, field, value):
    study["runs"][0]["samples"][1][field] = value
    assert_rejected(study)


def test_time_can_repeat_at_an_instantaneous_event_but_cannot_regress(study):
    samples = study["runs"][0]["samples"]
    samples.insert(1, deepcopy(samples[0]))
    assert verifier.verify_study(study)["passed"] is True
    samples.insert(1, sample(.5))
    assert_rejected(study)


@pytest.mark.parametrize("field,value", [("duration_s", 3), ("final_ground_speed_mps", 99), ("max_altitude_m", 0),
                                         ("payload_released_count", 26), ("booster_return_invoked", True),
                                         ("orbit_gate_reached", True), ("attitude_prescribed", True),
                                         ("termination", "landed_successfully")])
def test_outcome_claims_do_not_override_observations(study, field, value):
    study["runs"][0]["outcome"][field] = value
    assert_rejected(study)


def test_recorded_checks_are_not_trusted_as_verification(study):
    study["checks"] = [{"passed": True, "name": "all physics perfect"}]
    study["runs"][0]["final_state"]["propellant_kg"] = 999.
    assert_rejected(study)


def test_missing_or_out_of_order_terminal_event_fails(study):
    study["runs"][0]["events"][-1]["time_s"] = .5
    assert_rejected(study)
    study["runs"][0]["events"][-1]["time_s"] = 2.
    assert_rejected(study)


def test_unbound_escape_cannot_claim_a_deployment_orbit():
    first = sample(position=[A+300_000., 0., 0.], velocity=[0., 12_000., 0.])
    last = deepcopy(first)
    last["time_s"] = 1.
    study = study_with(first, last, scenario="launch")
    run = study["runs"][0]
    run["outcome"]["orbit_gate_reached"] = True
    run["events"].insert(1, {"time_s": 0., "event": "orbit_cutoff_command",
                            "orbit": {"status": "bound", "perigee_altitude_m": 300_000.}})
    result = assert_rejected(study)
    assert "bound observed trajectory" in result["issues"][0]["detail"]


def test_contact_flag_cannot_substitute_for_contact_evidence(study):
    run = study["runs"][0]
    run["outcome"]["termination"] = run["events"][-1]["event"] = "low_speed_surface_contact"
    run["samples"][-1]["contact"] = True
    assert_rejected(study)


@pytest.mark.parametrize("field,value", [("surface_relative_speed_mps", 1.), ("surface_normal_speed_mps", 100.),
                                         ("point_body_m", [1000., 0., 0.]), ("landing_verified", True),
                                         ("event_time_s", 2.), ("signed_clearance_m", 1.)])
def test_contact_receipt_tampering_is_detected_even_if_copied_to_event(impact_study, field, value):
    run = impact_study["runs"][0]
    run["outcome"]["contact_receipt"][field] = value
    run["events"][-1]["contact"] = deepcopy(run["outcome"]["contact_receipt"])
    assert_rejected(impact_study)


def test_impact_cannot_be_relabelled_as_low_speed(impact_study):
    run = impact_study["runs"][0]
    run["outcome"]["termination"] = run["events"][-1]["event"] = "low_speed_surface_contact"
    assert_rejected(impact_study)


def test_internally_consistent_slow_contact_receipt_cannot_override_fast_body(impact_study):
    run = impact_study["runs"][0]
    receipt = run["outcome"]["contact_receipt"]
    receipt["point_velocity_eci_mps"] = [-1., OMEGA*A, 0.]
    receipt["surface_relative_velocity_mps"] = [-1., 0., 0.]
    receipt["surface_relative_speed_mps"] = 1.
    receipt["surface_normal_speed_mps"] = -1.
    run["events"][-1]["contact"] = deepcopy(receipt)
    run["outcome"]["termination"] = run["events"][-1]["event"] = "low_speed_surface_contact"
    result = assert_rejected(impact_study)
    assert "saved rigid-body state" in result["issues"][0]["detail"]


def test_old_contact_without_moving_centroid_observation_fails_closed(impact_study):
    del impact_study["runs"][0]["samples"][-1]["com_rate_body_mps"]
    result = assert_rejected(impact_study)
    assert "moving-centroid rate observation" in result["issues"][0]["detail"]


def test_inward_normal_cannot_falsify_contact_descent_direction(impact_study):
    run = impact_study["runs"][0]
    receipt = run["outcome"]["contact_receipt"]
    receipt["surface_normal_eci"] = [-1., 0., 0.]
    receipt["surface_normal_speed_mps"] = 100.
    run["events"][-1]["contact"] = deepcopy(receipt)
    result = assert_rejected(impact_study)
    assert "ellipsoid gradient" in result["issues"][0]["detail"]


def test_recorded_contact_point_must_belong_to_configured_hull(impact_study):
    run = impact_study["runs"][0]
    receipt = run["outcome"]["contact_receipt"]
    receipt["point_body_m"] = [-10., 0., 0.]
    run["events"][-1]["contact"] = deepcopy(receipt)
    result = assert_rejected(impact_study)
    assert "outside configured cylinder" in result["issues"][0]["detail"]


def test_samples_budget_is_aggregate(monkeypatch, study):
    run = study["runs"][0]
    # Three independent two-point cases exceed the aggregate bound of five,
    # without exceeding a container limit during the initial JSON scan.
    study["runs"] = [deepcopy(run) for _ in range(3)]
    for item, name in zip(study["runs"], verifier.SCENARIOS[2:]):
        item["scenario"] = name
    # Check the trajectory limit separately from the JSON container limit.
    monkeypatch.setattr(verifier, "_json_tree", lambda _: None)
    monkeypatch.setattr(verifier, "MAX_SAMPLES", 5)
    assert_rejected(study, None)


def test_actual_short_producer_run_is_accepted():
    from src.runtime.starship_sixdof_mission import simulate
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    run = simulate(profile, scenario="gimbal_step", duration_s=.2)
    study = {"schema": "missionos.starship_sixdof_study.v1", "profile": profile, "runs": [run],
             "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False}}
    # JSON roundtrip matches the actual worker/file boundary, converting tuples.
    result = verifier.verify_study(json.loads(json.dumps(study)), "gimbal_step")
    assert result["passed"] is True, result
    assert result["observed_outcomes"][0]["duration_s"] == pytest.approx(.2)


def test_verifier_does_not_import_or_invoke_simulator():
    source = (ROOT/"src/runtime/starship_sixdof_verifier.py").read_text()
    relative_imports = [node.module for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.ImportFrom) and node.level]
    assert sorted(relative_imports) == ["starship_booster_recovery_verifier", "starship_return_feasibility_verifier", "starship_wind_verifier"]
    wind_source = (ROOT/"src/runtime/starship_wind_verifier.py").read_text()
    assert not [node for node in ast.walk(ast.parse(wind_source))
                if isinstance(node, ast.ImportFrom) and node.level]
    assert "import numpy" not in wind_source
    assert "import numpy" not in source
    assert "simulate(" not in source
