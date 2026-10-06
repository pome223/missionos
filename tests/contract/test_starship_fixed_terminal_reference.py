"""Public reference mathematics; no simulator integration or future trace."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_fixed_terminal_reference as reference
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_booster_catch import configuration
from src.runtime.starship_constrained_recovery import physical_guidance_configuration
from src.runtime.starship_sixdof_mission import _attitude, vehicle


def fixture():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 2000., time_s=100.)
    up, east, _ = env.local_frame(point)
    q = tuple(float(x) for x in _attitude(up, east))
    state = dyn.State6DOF(100., point.r, env.add(point.v, env.scale(up, -100.)), q, (0., 0., 0.), 60000.,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    frame = ConditionedGeographicFrame(q, 99.9, maximum_roll_rate_rad_s=.03/.28)
    snapshot = shooting.capture_context(state, profile, catch, physical_guidance_configuration(True),
        phase="recovery_entry_coast", start_time_s=90., deadline_s=1290.,
        plan={"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., 0.],
              "bank_heading_enu": [1., 0., 0.], "bank_sign": -1}, refreshed=True,
        burn_start_s=92., settle_start_s=95., previous_entry_axis_enu=[0., 0., 1.],
        entry_pretrim_prepared_at_s=None, next_preview_s=90., braking_preview=None,
        prior_command_reference={"quaternion": list(q), "time_s": 99.9},
        conditioned_reference=frame, reference_tracker=None, prepare_tail_start_s=99.)
    prior = deepcopy(snapshot["state"])
    prior["time_s"] = 99.9
    command = {"engines": [{"enabled": False, "throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0.}
        for _ in body.engines], "flap_angles_rad": [0. for _ in body.aero_panels]}
    return profile, catch, body, state, snapshot, prior, command


@pytest.mark.parametrize("conditions", [(0., 0., 0., 1., 0., 0., 10.),
    (100., -50., 3., 0., 0., 0., 40.), (1000., -100., -9., 3.4, -1.5, 0., 41.75838279559599)])
def test_unique_quintic_satisfies_all_six_endpoint_conditions(conditions):
    p0, v0, a0, p1, v1, a1, duration = conditions
    coefficients = reference.quintic_coefficients(*conditions)
    for time, expected in [(0., [p0, v0, a0]), (duration, [p1, v1, a1])]:
        assert [reference._polynomial(coefficients, time, order) for order in range(3)] == pytest.approx(expected, abs=2e-10)


def test_reference_jerk_is_analytic_not_a_future_state_difference():
    coefficients = reference.quintic_coefficients(100., -10., 2., 0., 0., 0., 20.)
    time = 3.
    expected = 6*coefficients[3]+24*coefficients[4]*time+60*coefficients[5]*time*time
    assert reference._polynomial(coefficients, time, 3) == pytest.approx(expected)


def test_plan_uses_actual_material_pin_geometry_and_keeps_physical_inputs_immutable(monkeypatch):
    profile, catch, _, _, snapshot, prior, command = fixture()
    original = deepcopy((snapshot, prior, command))
    monkeypatch.setattr(dyn, "step", lambda *a, **k: pytest.fail("no plant integration allowed"))
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    assert (snapshot, prior, command) == original
    assert plan["initial_pin_kinematics"]["physical_plant_integrations"] == 0
    assert plan["duration_source"] == "initial_existing_zem_zev_tgo"
    assert plan["target_position_enu_m"] == [0., 0., 103.4]
    assert plan["target_velocity_enu_mps"] == [0., 0., -1.5]
    assert plan["reference_tracker_history_reset"] is False
    assert plan["physical_state_assigned"] is False
    json.dumps(plan, allow_nan=False)


def test_endpoint_pose_and_pin_conditions_are_goals_not_admission():
    profile, catch, _, _, snapshot, prior, command = fixture()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    first = reference.evaluate_terminal_reference(plan, plan["reference_start_time_s"])
    final = reference.evaluate_terminal_reference(plan, plan["terminal_time_s"])
    assert first["position_enu_m"] == pytest.approx(plan["initial_pin_kinematics"]["position_enu_m"])
    assert first["velocity_enu_mps"] == pytest.approx(plan["initial_pin_kinematics"]["velocity_enu_mps"])
    assert first["acceleration_enu_mps2"] == pytest.approx(plan["initial_pin_kinematics"]["acceleration_enu_mps2"])
    assert final["position_enu_m"] == pytest.approx(plan["target_position_enu_m"], abs=1e-9)
    assert final["velocity_enu_mps"] == pytest.approx(plan["target_velocity_enu_mps"], abs=1e-10)
    assert final["acceleration_enu_mps2"] == pytest.approx([0., 0., 0.], abs=1e-10)
    assert abs(sum(a*b for a, b in zip(final["pose_q_body_to_eci"], plan["pose_target_q_body_to_eci"]))) == pytest.approx(1.)
    assert final["reference_is_execution"] is False
    assert plan["joint_reference_feasibility_established"] is False


def test_pose_quintic_peak_bounds_are_not_inner_stability_claims():
    duration, theta = 41.75838279559599, math.radians(90.)
    coefficients = reference.quintic_coefficients(0., 0., 0., 1., 0., 0., duration)
    assert theta*reference._polynomial(coefficients, duration/2, 1) == pytest.approx(1.875*theta/duration)
    first_extremum = duration*(3-math.sqrt(3))/6
    assert theta*reference._polynomial(coefficients, first_extremum, 2) == pytest.approx(10*theta/(math.sqrt(3)*duration**2))
    assert 1.875*theta/duration < .03/.28
    assert 10*theta/(math.sqrt(3)*duration**2) < .03


def test_changing_fuel_geometry_enters_initial_pin_acceleration_and_never_fixed_dry_cg():
    profile, catch, body, state, _, _, _ = fixture()
    command = dyn.Command6DOF(tuple(dyn.EngineCommand(True, .4) if i in (3, 8) else dyn.EngineCommand()
        for i in range(len(body.engines))), tuple(0. for _ in body.aero_panels))
    hot = dyn.State6DOF(state.time_s, state.r_eci_m, state.v_eci_mps, state.q_body_to_eci,
        (0., .01, 0.), state.propellant_kg, tuple(dyn.EngineState(.4 if i in (3, 8) else 0.)
        for i in range(len(body.engines))), state.flap_angles_rad)
    value = reference.initial_pin_kinematics(hot, body, profile, catch, command)
    assert value["com_rate_body_mps"][2] != 0.
    assert value["com_acceleration_body_mps2"][2] != 0.
    assert value["physical_state_assigned"] is False


@pytest.mark.parametrize("badtime", [100., 100.1, 99.5])
def test_causal_prior_observation_clock_required(badtime):
    profile, catch, _, _, snapshot, prior, command = fixture()
    prior["time_s"] = badtime
    with pytest.raises(ValueError, match="exact_causal_coast_anchor"):
        reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)


@pytest.mark.parametrize("duration", [True, 0., -1., float("nan"), float("inf")])
def test_invalid_quintic_duration_rejected(duration):
    with pytest.raises(ValueError):
        reference.quintic_coefficients(0., 0., 0., 1., 0., 0., duration)
