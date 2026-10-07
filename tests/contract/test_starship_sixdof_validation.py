"""Independent-reference and evidence coverage checks for the six-DOF kernel."""
import copy
from dataclasses import replace
from hashlib import sha256
import json
import math
import shutil

import pytest

from src.runtime import starship_sixdof as kernel
from src.runtime import starship_sixdof_validation as validation


@pytest.fixture(scope="module")
def result():
    return validation.run_validation(approve_simulation=True)


def test_analytic_public_reference_suite_passes_without_native_claim(result):
    assert result["passed"], {c["case_id"]: c["passed"] for c in result["cases"]}
    assert result["runtime_invocation"]["native_basilisk_invoked"] is False
    assert result["runtime_invocation"]["basilisk_version"] is None
    for key in ("nasa_full_checkcase_passed", "nasa_certification", "starship_fidelity_validated", "physical_execution"):
        assert result[key] is False
    assert len(result["cases"]) == 9


@pytest.mark.parametrize("axis,name", list(enumerate(("roll", "pitch", "yaw"))))
def test_each_rotation_axis_has_analytic_rate_and_orientation_reference(result, axis, name):
    case = next(c for c in result["cases"] if c["case_id"] == "analytic_constant_torque_"+name)
    assert case["max_rate_error_rad_s"] < 1e-10
    assert case["max_attitude_chord"] < 1e-7
    last = case["trace"][-1]
    assert last["omega_body_rad_s"][axis] > .05
    assert all(abs(last["omega_body_rad_s"][i]) < 1e-12 for i in range(3) if i != axis)
    assert abs(last["q_body_to_eci"][axis+1]) > .1


def test_nonlinear_nonprincipal_rotation_conserves_inertial_momentum(result):
    case = next(c for c in result["cases"] if c["case_id"] == "asymmetric_torque_free_rigid_body")
    assert min(case["axis_rate_excursions_rad_s"]) > .1
    assert case["energy_relative_error"] < 1e-7
    assert case["inertial_momentum_relative_error"] < 1e-7
    assert all(abs(sum(x*x for x in p["q_body_to_eci"])-1) < 1e-12 for p in case["trace"])


def test_published_nasa_reference_uses_all_times_and_all_three_inertial_body_rates(result):
    case = next(c for c in result["cases"] if c["case_id"] == "nesc_case02_rotational_rates_only")
    assert case["sample_count"] == 301
    assert case["trace"][0]["time_s"] == 0.
    assert math.isclose(case["trace"][-1]["time_s"], 30., abs_tol=1e-10)
    assert max(case["max_axis_rate_error_rad_s"]) < 1e-6
    assert case["reference"]["subset_sha256"] == validation.NASA_CSV_SHA256
    assert case["reference"]["source_sha256"] == validation.NASA_UPSTREAM_SHA256
    assert case["nasa_full_checkcase_passed"] is False


def test_coupled_case_exercises_all_position_velocity_and_rate_components(result):
    case = next(c for c in result["cases"] if c["case_id"] == "coupled_body_force_and_torque")
    assert all(min(values) > .01 for values in case["axis_excursions"].values())
    assert case["off_diagonal_inertia_kg_m2"][0][1] != 0
    convergence = case["step_convergence"]
    assert convergence["passed"]
    assert 8 < convergence["ratio"] < 32
    assert convergence["independent_reference"] is False
    assert case["native_basilisk"] is None


@pytest.mark.parametrize("case_id", ["analytic_mass_flow_constant_thrust", "analytic_mass_flow_depletion_then_coast"])
def test_mass_and_velocity_close_against_rocket_equation(result, case_id):
    case = next(c for c in result["cases"] if c["case_id"] == case_id)
    assert case["mass_error_kg"] < 1e-8
    assert case["velocity_error_mps"] < 1e-6
    assert case["variable_mass_flux_dynamics_validated"] is False
    if case_id.endswith("coast"):
        assert case["trace"][-1]["propellant_kg"] == 0
        assert case["trace"][-1]["v_eci_mps"] == pytest.approx(case["trace"][-5]["v_eci_mps"], abs=1e-12)


def test_com_and_full_inertia_use_independent_parallel_axis_reference(result):
    case = next(c for c in result["cases"] if c["case_id"] == "full_composite_inertia_and_center_of_mass")
    assert [x["propellant_kg"] for x in case["samples"]] == [0., 25., 50., 100.]
    assert max(x["max_inertia_error_kg_m2"] for x in case["samples"]) < 1e-9


def test_quaternion_sign_equivalence_does_not_mask_wrong_rotation():
    q = [math.cos(.7), math.sin(.7), 0., 0.]
    assert validation.quaternion_distance(q, [-x for x in q]) == 0.
    assert validation.quaternion_distance(q, [q[0], -q[1], 0., 0.]) > .1
    with pytest.raises(ValueError, match="unnormalized"):
        validation.quaternion_distance(q, [2., 0., 0., 0.])


def comparison_fixture():
    # Explicit arithmetic fixture; not evidence of a native Basilisk invocation.
    return [{"time_s": float(t), "r_eci_m": [float(t), 2., 3.], "v_eci_mps": [1., 0., 0.],
             "omega_body_rad_s": [.1, .2, .3], "q_body_to_eci": [1., 0., 0., 0.]}
            for t in range(3)]


@pytest.mark.parametrize("key", ["r_eci_m", "v_eci_mps", "omega_body_rad_s", "q_body_to_eci"])
def test_native_comparison_checks_every_state_and_interior_sample(key):
    left = comparison_fixture()
    right = copy.deepcopy(left)
    if key == "q_body_to_eci":
        right[1][key] = [math.cos(.01), math.sin(.01), 0., 0.]
    else:
        right[1][key][2] += .1
    assert validation.compare_native(left, right)["passed"] is False


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop(),
    lambda r: r[1].update(time_s=.5),
    lambda r: r[1].update(time_s=float("nan")),
    lambda r: r[1]["omega_body_rad_s"].pop(),
    lambda r: r[1]["r_eci_m"].append(3.),
    lambda r: r[1]["v_eci_mps"].__setitem__(0, float("nan")),
    lambda r: r[1]["omega_body_rad_s"].__setitem__(0, True),
])
def test_native_reference_coverage_clock_and_numerics_fail_closed(mutation):
    left = comparison_fixture()
    right = copy.deepcopy(left)
    mutation(right)
    with pytest.raises(ValueError):
        validation.compare_native(left, right)


def test_duplicate_grid_does_not_count_as_additional_evidence():
    trace = comparison_fixture()
    trace[1]["time_s"] = 0.
    with pytest.raises(ValueError, match="clock_alignment"):
        validation.compare_native(trace, copy.deepcopy(trace))


def test_source_rewrite_rejected_even_with_rewritten_metadata(tmp_path, monkeypatch):
    destination = tmp_path / "source"
    shutil.copytree(validation.SOURCE, destination)
    path = destination / "nesc-case02-sim01-angular-rates.csv"
    path.write_bytes(path.read_bytes().replace(b"9.999999999960824", b"8.999999999960824", 1))
    metadata = json.loads((destination / "nesc-source.json").read_text())
    metadata["subset_sha256"] = sha256(path.read_bytes()).hexdigest()
    (destination / "nesc-source.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(validation, "SOURCE", destination)
    with pytest.raises(ValueError, match="nasa_source_hash_mismatch"):
        validation.nasa_rotation_case()


def test_analytic_reference_rejects_kernel_that_prescribes_frozen_attitude(monkeypatch):
    actual = kernel.step
    def frozen(state, *args, **kwargs):
        output = actual(state, *args, **kwargs)
        return replace(output, q_body_to_eci=state.q_body_to_eci)
    monkeypatch.setattr(kernel, "step", frozen)
    cases = validation.analytic_axis_cases()
    assert all(case["passed"] is False for case in cases)
    assert all(case["max_rate_error_rad_s"] < 1e-10 for case in cases)


def test_validation_does_not_use_production_observed_invariants(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("reference must not reuse production observe diagnostics")
    monkeypatch.setattr(kernel, "observe", forbidden)
    assert validation.free_rotation_case()["passed"]


def test_native_path_requires_opt_in_before_import_or_execution():
    with pytest.raises(PermissionError):
        validation.native_basilisk(None, None, None, None)
    with pytest.raises(PermissionError):
        validation.run_validation()


def test_native_path_rejects_unpinned_version_before_import(monkeypatch):
    monkeypatch.setattr(validation, "version", lambda _: "unreviewed-version")
    with pytest.raises(ValueError, match="version_mismatch"):
        validation.native_basilisk(None, None, None, None, approve_simulation=True)


def test_actual_native_basilisk_coupled_body_matches_all_states():
    pytest.importorskip("Basilisk")
    case = validation.coupled_case(native=True, approve_simulation=True)
    assert case["passed"], case["native_basilisk"]
    assert case["native_basilisk"]["sample_count"] == len(case["trace"]) == 1601
    assert all(x["native_time_ns"] == round(x["time_s"]*1e9) for x in case["native_basilisk"]["trace"])
