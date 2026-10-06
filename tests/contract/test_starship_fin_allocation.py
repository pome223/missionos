"""Independent quadratic optimality and finite plant response, not flight success."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_fin_allocation as allocation
from src.runtime import starship_booster_recovery as recovery
from src.runtime import starship_booster_recovery_verifier as verifier
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import _attitude, control, vehicle
from scripts import run_starship_fin_comparison as experiment
from scripts import run_starship_coast_fin_comparison as coast_experiment

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def configured():
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((ROOT / "examples/spaceflight/starship-catch-profile.json").read_text())
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 50000., 200000., time_s=180.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(180., point.r, env.add(point.v, env.scale(up, -1000.)),
        _attitude(up, east), (0., 0., 0.), 200000.,
        tuple(dyn.EngineState() for _ in booster.engines), tuple(0. for _ in booster.aero_panels))
    return profile, catch, booster, state


def test_box_minimum_redistributes_after_saturation_instead_of_clipping():
    matrix, rhs = np.array([[1., 1.], [0., 1.]]), np.array([2., 0.])
    result = allocation.bounded_least_squares(matrix, rhs, [0., 0.], [1., 1.])
    clipped = np.clip(np.linalg.lstsq(matrix, rhs, rcond=None)[0], 0, 1)
    assert result == pytest.approx([1., .5])
    assert np.linalg.norm(matrix@result-rhs)**2 < np.linalg.norm(matrix@clipped-rhs)**2


def test_random_regularized_boxes_satisfy_independent_convex_kkt_conditions():
    rng = np.random.default_rng(921)
    for _ in range(30):
        matrix = np.vstack((rng.normal(size=(3, 3)), .05*np.eye(3)))
        rhs = rng.normal(size=6)*3
        lower, upper = -rng.uniform(.01, 1, 3), rng.uniform(.01, 1, 3)
        solution = allocation.bounded_least_squares(matrix, rhs, lower, upper)
        gradient = matrix.T@(matrix@solution-rhs)
        assert np.all(solution >= lower) and np.all(solution <= upper)
        for index, value in enumerate(gradient):
            if abs(solution[index]-lower[index]) < 1e-9:
                assert value >= -1e-9
            elif abs(solution[index]-upper[index]) < 1e-9:
                assert value <= 1e-9
            else:
                assert abs(value) < 1e-9


@pytest.mark.parametrize("target", [.01, .2, -.4])
def test_response_prediction_matches_actual_finite_actuator_integration(target):
    panel = dyn.AeroPanel("grid_fin_0", (0., 0., 2.), (1., 0., 0.), 1.,
        max_deflection_rad=.5, deflection_time_constant_s=.25, deflection_rate_rad_s=.3)
    v = dyn.Vehicle6DOF(100., (0., 0., 0.), ((100., 0., 0.), (0., 100., 0.), (0., 0., 100.)),
        0., (0., 0., 0.), 1., 1., (), (panel,))
    s = dyn.State6DOF(0., (7e6, 0., 0.), (0., 0., 0.), (1., 0., 0., 0.), (0., 0., 0.), 0., (), (0.,))
    command = dyn.Command6DOF((), (target,))
    final = s
    for _ in range(100):
        final = dyn.step(final, v, command, .001, gravity=False, atmosphere=False)
    prediction = allocation.actuator_endpoint(0., target, .25, .3, .1)
    assert final.flap_angles_rad[0] == pytest.approx(prediction, abs=1e-8)


def test_candidate_requests_real_bounded_commands_and_preserves_state(configured):
    profile, _, booster, state = configured
    before = asdict(state)
    target = dyn.quaternion_multiply(state.q_body_to_eci, dyn.axis_angle((1., 0., 0.), .15))
    ordinary, _ = control(state, booster, target, .4, 3, profile, use_flaps=True)
    candidate, info = control(state, booster, target, .4, 3, profile, use_flaps=True,
        development_fin_allocation=True, control_interval_s=.1, trim_angles_rad=(0.,)*6)
    assert asdict(state) == before
    assert [(e.enabled, e.throttle) for e in candidate.engines[:33]] == [(e.enabled, e.throttle) for e in ordinary.engines[:33]]
    receipt = info["development_fin_allocation"]
    assert receipt["prediction_is_execution"] is False
    assert receipt["actual_state_assigned"] is False
    for i, predicted, target_angle in zip(receipt["fin_indices"], receipt["predicted_endpoint_angles_rad"], receipt["command_angles_rad"]):
        panel = booster.aero_panels[i]
        assert abs(target_angle) <= panel.max_deflection_rad
        assert abs(predicted-state.flap_angles_rad[i]) <= .1*panel.deflection_rate_rad_s+1e-9
    actual = dyn.step(state, booster, candidate, .1)
    assert actual.flap_angles_rad[3:] == pytest.approx(receipt["predicted_endpoint_angles_rad"], abs=1e-5)
    assert actual.q_body_to_eci != state.q_body_to_eci


@pytest.mark.parametrize("interval", [True, 0., -.1, .3, float("nan")])
def test_bad_response_interval_is_rejected(configured, interval):
    profile, _, booster, state = configured
    with pytest.raises(ValueError, match="invalid_fin_control_interval"):
        allocation.allocate_fins(state, booster, [0., 0., 0.], dyn.observe(state, booster), profile, interval_s=interval)


def test_fins_require_explicit_local_scheduled_experiment(configured):
    profile, catch, _, state = configured
    with pytest.raises(ValueError, match="requires_scheduled_experiment"):
        recovery.simulate_recovery(profile, asdict(state), catch, duration_s=.6, _development_fin_allocation=True)


@pytest.fixture(scope="module")
def candidate_record(configured):
    profile, catch, booster, state = configured
    # Ascending short case genuinely executes ignition/cutoff/coast. It remains
    # an initialized boundary fixture, not evidence of a launch-to-catch flight.
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    up, _, _ = env.local_frame(point)
    state = replace(state, v_eci_mps=env.add(env.cross((0., 0., env.EARTH_ROTATION_RAD_S), state.r_eci_m), env.scale(up, 600.)))
    run = recovery.simulate_recovery(profile, asdict(state), catch, duration_s=.6,
        _development_cutoff_time_s=180.2, _development_fin_allocation=True)
    return json.loads(json.dumps(asdict(state))), json.loads(json.dumps(run, allow_nan=False))


def test_candidate_is_checked_only_in_explicit_experiment_scope(configured, candidate_record):
    profile, catch, _, _ = configured
    initial, run = candidate_record
    verdict = verifier.verify_recovery(run, initial, profile, catch, development_cutoff_time_s=180.2, development_fin_allocation=True)
    assert verdict["passed"], verdict
    ordinary = verifier.verify_recovery(run, initial, profile, catch, development_cutoff_time_s=180.2)
    assert ordinary["passed"] is False and ordinary["issues"][0]["code"] == "development_scope"


@pytest.mark.parametrize("field", ["predicted_endpoint_angles_rad", "half_objective_gradient", "objective", "production_policy_admitted"])
def test_independent_checker_rejects_altered_finite_allocation(configured, candidate_record, field):
    profile, catch, _, _ = configured
    initial, original = candidate_record
    run = deepcopy(original)
    index = next(i for i, c in enumerate(run["recovery_record"]["checkpoints"]) if "development_fin_allocation" in c["navigation"])
    receipt = run["recovery_record"]["checkpoints"][index]["navigation"]["development_fin_allocation"]
    if field == "production_policy_admitted":
        receipt[field] = True
    elif field == "objective":
        receipt[field] += 1
    else:
        receipt[field][0] += .1
    run["samples"][index]["controller"]["development_fin_allocation"] = deepcopy(receipt)
    result = verifier.verify_recovery(run, initial, profile, catch, development_cutoff_time_s=180.2, development_fin_allocation=True)
    assert result["passed"] is False and result["issues"][0]["code"] == "fin_allocation", result


@pytest.mark.parametrize("mutation", ["lost_scope", "lost_receipt", "sample_only", "lost_both_scopes"])
def test_candidate_cannot_erase_experiment_identity(configured, candidate_record, mutation):
    profile, catch, _, _ = configured
    initial, original = candidate_record
    run = deepcopy(original)
    checkpoints = run["recovery_record"]["checkpoints"]
    index = next(i for i, c in enumerate(checkpoints) if "development_fin_allocation" in c["navigation"])
    kwargs = {"development_cutoff_time_s": 180.2, "development_fin_allocation": True}
    if mutation in ("lost_scope", "lost_both_scopes"):
        run["recovery_record"].pop("development_fin_allocation")
    if mutation == "lost_both_scopes":
        kwargs["development_fin_allocation"] = False
    if mutation == "lost_receipt":
        checkpoints[index]["navigation"].pop("development_fin_allocation")
        run["samples"][index]["controller"].pop("development_fin_allocation")
    if mutation == "sample_only":
        run["samples"][index]["controller"].pop("development_fin_allocation")
    verdict = verifier.verify_recovery(run, initial, profile, catch, **kwargs)
    assert verdict["passed"] is False and verdict["issues"], verdict


def test_paired_cli_requires_opt_in_and_preserves_failed_artifacts(tmp_path):
    arguments = ["--separation-study", str(tmp_path / "missing.json"), "--baseline-dir", str(tmp_path), "--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        experiment.main(arguments)
    failure = tmp_path / "failure.json"
    failure.write_text("retained negative result")
    with pytest.raises(SystemExit):
        experiment.main(["--approve-simulation", *arguments])
    assert failure.read_text() == "retained negative result"


@pytest.mark.parametrize("scope,enabled", [("unknown", True), ("coast_only", False)])
def test_application_scope_cannot_be_enabled_without_explicit_experiment(configured, scope, enabled):
    profile, catch, _, state = configured
    with pytest.raises(ValueError, match="invalid_development_fin_scope"):
        recovery.simulate_recovery(profile, asdict(state), catch, duration_s=.6,
            _development_cutoff_time_s=180.2, _development_fin_allocation=enabled, _development_fin_scope=scope)


def test_coast_only_marker_requires_matching_verifier_scope(configured, candidate_record):
    profile, catch, _, _ = configured
    initial, old = candidate_record
    coast = recovery.simulate_recovery(profile, initial, catch, duration_s=.6,
        _development_cutoff_time_s=180.2, _development_fin_allocation=True, _development_fin_scope="coast_only")
    coast = json.loads(json.dumps(coast, allow_nan=False))
    verdict = verifier.verify_recovery(coast, initial, profile, catch, development_cutoff_time_s=180.2,
        development_fin_allocation=True, development_fin_scope="coast_only")
    assert verdict["passed"], verdict
    assert [c["state"] for c in coast["recovery_record"]["checkpoints"]] == [c["state"] for c in old["recovery_record"]["checkpoints"]]
    assert not verifier.verify_recovery(coast, initial, profile, catch, development_cutoff_time_s=180.2,
        development_fin_allocation=True)["passed"]
    index = next(i for i, c in enumerate(coast["recovery_record"]["checkpoints"]) if "development_fin_allocation" in c["navigation"])
    checkpoint, sample = coast["recovery_record"]["checkpoints"][index], coast["samples"][index]
    with pytest.raises(verifier._Invalid):
        verifier._fin_allocation(checkpoint, sample, profile, False, .2)


def test_coast_comparison_cli_rejects_unapproved_or_overwritten_run(tmp_path):
    arguments = ["--separation-study", str(tmp_path / "missing.json"), "--previous-fin-dir", str(tmp_path), "--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        coast_experiment.main(arguments)
    failure = tmp_path / "failure.json"
    failure.write_text("retained")
    with pytest.raises(SystemExit):
        coast_experiment.main(["--approve-simulation", *arguments])
    assert failure.read_text() == "retained"
