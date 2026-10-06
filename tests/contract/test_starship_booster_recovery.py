"""Opt-in recovery guidance; forecasts never replace executed six-DOF state."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_booster_recovery as recovery
from src.runtime.starship_sixdof_mission import vehicle
from src.runtime.starship_booster_catch import _initialized

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


@pytest.fixture(scope="module")
def catch_configuration():
    return json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())


@pytest.mark.parametrize("angle", [0., .01, .2, 1., math.pi])
def test_command_axis_slew_is_finite_bounded_unit_length(angle):
    requested = (math.sin(angle), 0., math.cos(angle))
    result = recovery._slew_axis((0., 0., 1.), requested, .1)
    assert sum(x*x for x in result) == pytest.approx(1.)
    assert math.acos(max(-1., min(1., result[2]))) == pytest.approx(min(angle, .1))


def test_entry_command_uses_bounded_attitude_candidates_and_public_configured_isp(profile):
    booster = vehicle(profile, "booster")
    axis, info = recovery._entry_axis([1000., -500., 20000.], [-100., 25., -1200.], 330000., 80000.,
                                       booster.aero_panels, isp_s=profile["booster"]["engine_isp_s"])
    tail = recovery._norm((100., -25., 1200.))
    angle = math.degrees(math.acos(max(-1., min(1., env.dot(axis, tail)))))
    assert angle <= recovery.CONFIG["entry_max_angle_deg"]+1e-9
    assert info["candidate_count"] <= 15
    _, low = recovery._entry_axis([1000., -500., 20000.], [-100., 25., -1200.], 330000., 80000.,
                                  booster.aero_panels, isp_s=profile["booster"]["engine_isp_s"]*.5)
    assert low["desired_aerodynamic_acceleration_enu_mps2"][2] > info["desired_aerodynamic_acceleration_enu_mps2"][2]


@pytest.mark.parametrize("duration", [True, 0, -1, float("nan"), float("inf"), 1200.1])
def test_invalid_duration_rejected_before_flight(profile, catch_configuration, duration):
    _, state = _initialized(profile, catch_configuration, "booster_catch")
    with pytest.raises(ValueError, match="invalid_recovery_duration"):
        recovery.simulate_recovery(profile, asdict(state), catch_configuration, duration_s=duration)


def test_forecast_phase_cannot_override_executed_run(profile, catch_configuration):
    _, state = _initialized(profile, catch_configuration, "booster_catch")
    with pytest.raises(ValueError, match="initial_phase_override_requires_forecast"):
        recovery.simulate_recovery(profile, asdict(state), catch_configuration,
                                   _initial_phase="recovery_entry_coast", duration_s=.1)


def test_clock_must_advance_before_bounded_rollout(profile, catch_configuration):
    _, state = _initialized(profile, catch_configuration, "booster_catch")
    state = replace(state, time_s=1e20)
    with pytest.raises(ValueError, match="recovery_clock_must_advance"):
        recovery.simulate_recovery(profile, asdict(state), catch_configuration, duration_s=1.)


def test_full_forecast_is_same_policy_continuation_without_mutating_state_or_reference(profile, catch_configuration):
    from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
    booster, state = _initialized(profile, catch_configuration, "booster_catch")
    up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    state = replace(state, r_eci_m=env.add(state.r_eci_m, env.scale(up, 200.)), omega_body_rad_s=(.02, -.03, 0.))
    before = asdict(state)
    reference = ConditionedGeographicFrame(state.q_body_to_eci, state.time_s,
                                            maximum_roll_rate_rad_s=profile["guidance"]["attitude_frequency_rad_s"])
    reference_before = deepcopy(reference.__dict__)
    predicted = recovery._predict_cutoff(state, booster, profile, catch_configuration, full=True, reference=reference, remaining_duration_s=3.)
    actual = recovery.simulate_recovery(profile, before, catch_configuration,
                                        _initial_phase="recovery_powered_rate_settle", _forecast=True,
                                        _reference=deepcopy(reference), duration_s=3.)
    assert predicted["termination"] == actual["outcome"]["termination"]
    assert predicted["elapsed_s"] == actual["outcome"]["duration_s"]
    assert predicted["terminal_propellant_kg"] == actual["final_state"]["propellant_kg"]
    assert predicted["prediction_is_execution"] is False
    assert actual["recovery_record"]["forecast_only"] is True
    assert actual["recovery_record"]["full_coast_prediction_count"] == 0
    assert asdict(state) == before
    assert reference.time_s == reference_before["time_s"]
    assert reference.frame.quaternion == reference_before["frame"].quaternion
    assert reference.bridging == reference_before["bridging"]


def test_static_trim_rejects_unavailable_fin_moment_without_changing_vehicle(profile):
    booster = vehicle(profile, "booster")
    com = dyn.mass_properties(booster, 70000.).com_body_m
    velocity = (-250., 0., -1540.)
    tail = recovery._norm(tuple(-v for v in velocity))
    side = recovery._norm(env.add((1., 0., 0.), env.scale(tail, -tail[0])))
    results = []
    for angle in (30., 45.):
        axis = env.add(env.scale(tail, math.cos(math.radians(angle))), env.scale(side, math.sin(math.radians(angle))))
        force, trim = recovery._trim_entry_candidate(velocity, axis, .04, booster.aero_panels, com, 80000.)
        assert all(math.isfinite(x) for x in force)
        assert all(abs(a) <= p.max_deflection_rad+1e-12 for a, p in zip(trim["predicted_trim_flap_angles_rad"], booster.aero_panels))
        assert trim["trim_is_executed_deflection"] is False
        results.append(trim)
    assert results[0]["steady_trim_feasible"] is True
    assert results[1]["steady_trim_feasible"] is False
    assert max(abs(x) for x in results[1]["predicted_residual_moment_body_nm"]) > 80000.


def test_trimmed_prediction_reproduces_integrator_panel_forces(profile):
    from src.runtime.starship_sixdof_mission import _attitude
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 25000., 70000., time_s=300.)
    up, east, north = env.local_frame(point)
    local_velocity = (-250., 0., -1540.)
    axis = recovery._norm((.65, .1, .75))
    axis_eci = tuple(sum(axis[j]*a[j] for j in range(3)) for a in zip(east, north, up))
    velocity_eci = tuple(sum(local_velocity[j]*a[j] for j in range(3)) for a in zip(east, north, up))
    q = _attitude(axis_eci, east)
    state = dyn.State6DOF(300., point.r, env.add(point.v, velocity_eci), q, (0., 0., 0.), 70000.,
                          tuple(dyn.EngineState() for _ in booster.engines), tuple(0. for _ in booster.aero_panels))
    observation = dyn.observe(state, booster)
    density = env.standard_atmosphere(observation["altitude_m"])["density_kg_m3"]
    force, trim = recovery._trim_entry_candidate(local_velocity, axis, density, booster.aero_panels,
                                                observation["com_body_m"], 80000.)
    state = replace(state, flap_angles_rad=tuple(trim["predicted_trim_flap_angles_rad"]))
    actual = dyn.observe(state, booster)
    force_eci = dyn.rotate(q, actual["aero_force_body_n"])
    assert force == pytest.approx([env.dot(force_eci, a) for a in (east, north, up)], rel=1e-8, abs=.01)
    assert trim["predicted_residual_moment_body_nm"] == pytest.approx(actual["aero_torque_body_nm"], rel=1e-7, abs=.01)


def test_upcoming_pressure_rejects_low_density_trap_without_inflating_current_force(profile):
    booster = vehicle(profile, "booster")
    com = dyn.mass_properties(booster, 70000.).com_body_m
    velocity = (-200., 0., -1300.)
    tail = recovery._norm(tuple(-v for v in velocity))
    side = recovery._norm(env.add((1., 0., 0.), env.scale(tail, -tail[0])))
    axis = env.add(env.scale(tail, .5), env.scale(side, math.sqrt(3)/2))
    force, permissive = recovery._trim_entry_candidate(velocity, axis, .00001, booster.aero_panels, com, 80000.)
    checked_force, conservative = recovery._trim_entry_candidate(velocity, axis, .00001, booster.aero_panels, com, 80000.,
                                                                profile["guidance"]["max_q_pa"])
    assert permissive["steady_trim_feasible"] is True
    assert conservative["steady_trim_feasible"] is False
    assert conservative["trim_validation_dynamic_pressure_pa"] == profile["guidance"]["max_q_pa"]
    assert conservative["prediction_dynamic_pressure_pa"] == pytest.approx(.5*.00001*sum(v*v for v in velocity))
    ratio = conservative["trim_validation_dynamic_pressure_pa"]/conservative["prediction_dynamic_pressure_pa"]
    assert conservative["trim_validation_residual_moment_body_nm"] == pytest.approx([v*ratio for v in conservative["predicted_residual_moment_body_nm"]])
    # Current force stays at the low current pressure; only constraint checking
    # uses the future envelope. Different trim angles may change it modestly.
    assert recovery._length(checked_force) < 2*recovery._length(force)


def _preview_state(profile, local_velocity):
    from src.runtime.starship_sixdof_mission import _attitude
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 50000., 30000., time_s=300.)
    up, east, north = env.local_frame(point)
    velocity = tuple(sum(local_velocity[j]*a[j] for j in range(3)) for a in zip(east, north, up))
    state = dyn.State6DOF(300., point.r, env.add(point.v, velocity), _attitude(up, east), (0., 0., 0.), 30000.,
                          tuple(dyn.EngineState() for _ in booster.engines), tuple(0. for _ in booster.aero_panels))
    return booster, state, dyn.observe(state, booster)


def test_vector_preview_recovers_vertical_1d_case_and_marks_fuel_limited_estimate(profile):
    from src.runtime.starship_sixdof_booster import _drag_aware_stop_estimate, _navigation
    booster, state, observed = _preview_state(profile, (0., 0., -1000.))
    old = _drag_aware_stop_estimate(state, booster, observed, _navigation(state, profile)[0], -1000., profile, 13)
    before = asdict(state)
    actual = recovery._vector_burn_preview(state, booster, observed, profile, 13)
    assert actual["estimated_stopping_height_m"] == pytest.approx(old["estimated_stopping_height_m"], abs=1e-6)
    assert actual["estimated_burn_time_s"] == pytest.approx(old["estimated_burn_time_s"], abs=1e-8)
    assert actual["burn_fuel_feasible"] is False
    assert actual["estimate_exhausted_available_fuel"] is True
    assert actual["estimated_propellant_is_required_amount"] is False
    assert actual["estimated_final_speed_mps"] > 2.
    assert actual["prediction_is_execution"] is False
    assert actual["steps"] <= 10000
    assert asdict(state) == before


def test_vector_preview_never_counts_horizontal_distance_as_height_loss(profile):
    from src.runtime.starship_sixdof_booster import _drag_aware_stop_estimate, _navigation
    booster, state, observed = _preview_state(profile, (500., 0., 0.))
    velocity = _navigation(state, profile)[3]
    direction = env.scale(velocity, -1/env.norm(velocity))
    mistaken = _drag_aware_stop_estimate(state, booster, observed, direction, -500., profile, 13)
    actual = recovery._vector_burn_preview(state, booster, observed, profile, 13)
    assert abs(actual["initial_velocity_enu_mps"][2]) < 1e-9
    assert actual["estimated_stopping_height_m"] < 100.
    assert actual["estimated_horizontal_displacement_m"][0] > 500.
    assert mistaken["estimated_stopping_height_m"] > actual["estimated_stopping_height_m"]+500.


@pytest.mark.parametrize("velocity", [(0., 0., -1000.), (500., 0., 0.)])
def test_vector_preview_serializes_native_claims_from_in_memory_numpy_state(profile, velocity):
    booster, state, _ = _preview_state(profile, velocity)
    # A live launch contains NumPy scalars; replaying JSON silently turns them
    # into Python floats and previously hid the boolean receipt failure.
    state = replace(state, propellant_kg=np.float64(state.propellant_kg),
                    r_eci_m=tuple(np.float64(x) for x in state.r_eci_m),
                    v_eci_mps=tuple(np.float64(x) for x in state.v_eci_mps),
                    q_body_to_eci=tuple(np.float64(x) for x in state.q_body_to_eci))
    before = asdict(state)
    preview = recovery._vector_burn_preview(state, booster, dyn.observe(state, booster), profile)
    json.dumps(preview, allow_nan=False)
    assert type(preview["estimate_exhausted_available_fuel"]) is bool
    assert asdict(state) == before
    assert type(state.propellant_kg) is np.float64


def test_finite_forecast_serializes_native_reserve_claim_from_numpy_fuel(profile, catch_configuration):
    booster, state = _initialized(profile, catch_configuration, "booster_catch")
    up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    state = replace(state, propellant_kg=np.float64(state.propellant_kg),
                    r_eci_m=env.add(state.r_eci_m, env.scale(up, 200.)),
                    omega_body_rad_s=(.02, -.03, 0.))
    before = asdict(state)
    preview = recovery._predict_cutoff(state, booster, profile, catch_configuration,
                                       full=True, remaining_duration_s=.2)
    json.dumps(preview, allow_nan=False)
    assert type(preview["terminal_fuel_reserve_met"]) is bool
    assert preview["prediction_is_execution"] is False
    assert asdict(state) == before
    assert type(state.propellant_kg) is np.float64
