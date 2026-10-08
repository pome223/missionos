"""Fresh quantitative margins and an independent verifier, not flight fixtures."""
from copy import deepcopy
from dataclasses import replace
import inspect
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_return_feasibility as tool, starship_return_feasibility_verifier as independent
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle


def example(fuel=100000.):
    profile = json.loads((Path(__file__).parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, payload_count=0)
    radius = 6378137.+300000.
    state = dyn.State6DOF(1000., (radius, 0., 0.), (0., math.sqrt(3.986004418e14/radius), 0.),
        (math.sqrt(.5), -math.sqrt(.5), 0., 0.), (.001, 0., 0.), fuel,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    observed = tool.evaluate(profile, state, 26, fuel)
    certificate = {"qualification_complete": True, "normalized_profile_sha256": observed["inputs"]["normalized_profile_sha256"],
        "backend": observed["inputs"]["backend"], "maximum_return_consumption_kg": 50000.,
        "maximum_reference_deorbit_fuel_kg": 10000., "matching_tolerances": dict(tool.MATCH_LIMITS),
        "coast_corridors": [{"case_id": "explicit_math_fixture", "retained_count": 0,
                            "samples": [[t, *state.r_eci_m, *state.v_eci_mps, *state.q_body_to_eci,
                                         *state.omega_body_rad_s, 100000.] for t in (1000., 1002.)]}]}
    proof = {**tool.calculate(observed["inputs"], certificate), "certificate_sha256": "explicit_math_fixture"}
    return profile, state, certificate, proof


def checked(certificate, proof):
    return independent.verify(proof, certificate, "explicit_math_fixture", time_s=proof["time_s"],
        fuel_kg=proof["inputs"]["fuel_observed_kg"], released_count=26)


def test_return_fuel_margin_is_recomputed_independently():
    _, _, certificate, proof = example()
    assert proof["return_fuel_margin_kg"] == 20900.
    assert proof["return_admitted"] is True
    assert checked(certificate, proof)["passed"] is True


def test_insufficient_return_fuel_can_only_admit_bounded_coast():
    _, _, certificate, proof = example(24000.)
    assert proof["return_admitted"] is False
    assert proof["bounded_coast_admitted"] is True
    assert proof["maximum_coast_s"] == 30.
    assert checked(certificate, proof)["passed"] is True
    _, _, certificate, proof = example(1000.)
    assert proof["bounded_coast_admitted"] is False


@pytest.mark.parametrize("mutation", ("clock", "fuel_margin", "orbit", "pose", "inventory", "certificate", "claim"))
def test_margins_cannot_be_relabelled_as_fresh_or_certified(mutation):
    _, _, certificate, proof = example()
    changed = deepcopy(proof)
    if mutation == "clock":
        changed["time_s"] += 1
    if mutation == "fuel_margin":
        changed["return_fuel_margin_kg"] += 1
    if mutation == "orbit":
        changed["inputs"]["perigee_altitude_m"] += 1000
    if mutation == "pose":
        changed["inputs"]["coast_axis_error_deg"] += 1
    if mutation == "inventory":
        changed["inputs"]["retained_count"] = 1
    if mutation == "certificate":
        changed["certificate_sha256"] = "other"
    if mutation == "claim":
        changed["physical_recovery_certified"] = True
    assert checked(certificate, changed)["passed"] is False


def test_independent_guard_imports_no_optimizer_or_simulator():
    source = inspect.getsource(independent)
    assert "from ." not in source
    assert "import numpy" not in source
    assert "simulate(" not in source


def test_negative_propulsion_margin_is_not_admitted():
    profile, state, certificate, _ = example()
    engines = list(state.engine_states)
    engines[0] = replace(engines[0], available=False)
    value = tool.evaluate(profile, replace(state, engine_states=tuple(engines)), 26, 100000.)
    proof = {**tool.calculate(value["inputs"], certificate), "certificate_sha256": "explicit_math_fixture"}
    assert proof["return_admitted"] is False
    assert checked(certificate, proof)["passed"] is True


@pytest.mark.parametrize("changes", [
    {"time_s": 1100.}, {"propellant_kg": 105000.},
    {"r_eci_m": (6678237., 0., 0.)}, {"v_eci_mps": (0., 7750., 0.)},
    {"omega_body_rad_s": (.002, 0., 0.)},
])
def test_fresh_but_unseen_state_cannot_borrow_an_orbit_box(changes):
    profile, state, certificate, _ = example()
    state = replace(state, **changes)
    inputs = tool.evaluate(profile, state, 26, state.propellant_kg)["inputs"]
    proof = {**tool.calculate(inputs, certificate), "certificate_sha256": "explicit_math_fixture"}
    assert proof["return_admitted"] is False
    assert proof["within_tested_state_domain"] is False
    assert checked(certificate, proof)["passed"] is True


def test_missing_corridors_and_forged_matching_tolerances_cannot_admit_return():
    _, _, certificate, proof = example()
    for key in ("coast_corridors", "matching_tolerances"):
        altered = deepcopy(certificate)
        altered.pop(key)
        assert tool.calculate(proof["inputs"], altered)["return_admitted"] is False
        assert checked(altered, proof)["passed"] is False


def test_terminal_violation_is_explicit_and_keeps_best_effort_guidance():
    observed = {"time_s": 5000., "propellant_kg": 30000.}
    budget = {"available_landing_engines": 2, "aligned_net_acceleration_mps2": 5.}
    receipt = tool.terminal_receipt(observed, budget, {"maximum_terminal_consumption_kg": 20000.})
    assert receipt["passed"] is False
    assert receipt["continuation"] == "unqualified_best_effort_terminal_guidance"
    assert receipt["time_s"] == 5000.
    assert receipt["fuel_lower_bound_kg"] == 29900.
    assert receipt["physical_recovery_certified"] is False


def test_backend_readiness_has_specific_reason_before_execution(monkeypatch):
    profile, _, _, _ = example()
    value = {'schema': 'missionos.starship_state_return_qualification.v2',
        'qualification_complete': True, 'policy_id': tool.POLICY, 'retained_counts': list(range(27)),
        'source_sha256': tool.sources(), 'normalized_profile_sha256': tool.digest(tool.normalized_profile(profile)),
        'backend': {'numpy': 'explicit-other-test-version', 'scipy': 'explicit-other-test-version'},
        'matching_tolerances': tool.MATCH_LIMITS, 'coast_corridors': [{} for _ in range(39)]}
    monkeypatch.setattr(tool, 'load_certificate', lambda: (value, 'explicit_fixture'))
    assert tool.readiness(profile)[2] == 'return_qualification_backend_mismatch'
    assert tool.readiness(profile, check_backend=False)[2] is None
    value['source_sha256'] = {}
    assert tool.readiness(profile)[2] == 'return_qualification_source_mismatch'


def test_late_engine_failure_is_explicit_launch_scenario_and_inhibition_is_recordable():
    from src.runtime.starship_sixdof_mission import simulate
    from src.runtime.starship_sixdof_verifier import verify_study, _TERMINATIONS
    profile, _, _, _ = example()
    run = json.loads(json.dumps(simulate(profile, scenario='terminal_engine_out', duration_s=.2)))
    assert run['initial_state']['phase'] == 'stack_ascent'
    assert run['outcome']['terminal_qualification_violated'] is False
    assert 'return_inhibited_unresolved' in _TERMINATIONS
    assert verify_study({'schema': 'missionos.starship_sixdof_study.v1', 'profile': profile, 'runs': [run],
                         'provenance': {'physical_execution_invoked': False, 'starship_vehicle_validated': False}},
                        expected_scenario='terminal_engine_out')['passed'] is True
