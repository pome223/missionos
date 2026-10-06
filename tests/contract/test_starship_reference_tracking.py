"""Bounded causal requests and optional existing-gain attitude tracking."""
from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_booster_control import control_with_measured_tvc
from src.runtime.starship_reference_tracking import (
    POLICY_ID, ReferenceTracker, tracking_acceleration,
)
from src.runtime.starship_sixdof_mission import control, vehicle


def profile():
    return json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())


def quaternion(axis, angle):
    magnitude = math.hypot(*axis)
    return (math.cos(angle/2), *(math.sin(angle/2)*value/magnitude for value in axis))


def fixture():
    config = profile()
    booster = vehicle(config, "booster")
    state = dyn.State6DOF(0., (8_378_137., 0., 0.), (0., 0., 0.),
        (1., 0., 0., 0.), (.02, -.01, .03), 78_000.,
        tuple(dyn.EngineState(throttle=.4 if i < 3 else 0.) for i in range(len(booster.engines))),
        tuple(0. for _ in booster.aero_panels))
    return config, booster, state


def test_first_sample_preserves_declared_anchor_without_assigning_raw_goal():
    config = profile()
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 4.)
    goal = quaternion((0., 1., 0.), 2.)
    requested, detail = tracker.update(goal, (1., 0., 0., 0.), (0., 0., 0.), 4.)
    receipt = detail["receipt"]
    assert requested == (1., 0., 0., 0.)
    assert receipt["raw_goal_q_body_to_eci"] == list(goal)
    assert receipt["first_update"] is receipt["initialized_at_current_time"] is True
    assert receipt["reference_rate_eci_rad_s"] == [0., 0., 0.]
    assert receipt["reference_acceleration_eci_rad_s2"] == [0., 0., 0.]
    assert receipt["actual_state_assigned"] is receipt["request_is_execution"] is False


def test_first_positive_interval_binds_prior_request_and_clock():
    config = profile()
    previous = quaternion((1., 0., 0.), .2)
    tracker = ReferenceTracker(config, previous, 12.)
    requested, detail = tracker.update(quaternion((0., 1., 0.), 1.), previous, (0., 0., 0.), 12.1)
    receipt = detail["receipt"]
    assert receipt["previous_requested_q_body_to_eci"] == list(previous)
    assert receipt["previous_time_s"] == 12.
    assert receipt["first_update"] is True
    assert receipt["initialized_at_current_time"] is False
    assert requested != previous
    assert math.hypot(*receipt["reference_acceleration_eci_rad_s2"]) == pytest.approx(.03)


def test_sudden_and_reversed_goals_preserve_rate_acceleration_and_history_bounds():
    config = profile()
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    maximum_rate = .03/.28
    previous_request, previous_rate, previous_time = tracker.quaternion, (0., 0., 0.), 0.
    for index in range(1, 121):
        goal = quaternion((0., 1., 0.), 2. if index < 60 else -2.)
        requested, detail = tracker.update(goal, (1., 0., 0., 0.), (0., 0., 0.), index*.1)
        receipt = detail["receipt"]
        rate = receipt["reference_rate_eci_rad_s"]
        acceleration = receipt["reference_acceleration_eci_rad_s2"]
        assert receipt["previous_requested_q_body_to_eci"] == list(previous_request)
        assert receipt["previous_reference_rate_eci_rad_s"] == list(previous_rate)
        assert receipt["previous_time_s"] == previous_time
        assert math.hypot(*rate) <= maximum_rate+1e-12
        assert math.hypot(*acceleration) <= .03+1e-12
        assert math.hypot(*requested) == pytest.approx(1.)
        assert receipt["rate_clipped"] is True
        delta = dyn.quaternion_multiply(requested, (previous_request[0], *[-x for x in previous_request[1:]]))
        angle = 2*math.atan2(math.hypot(*delta[1:]), abs(delta[0]))
        assert angle == pytest.approx(math.hypot(*rate)*receipt["interval_s"], abs=1e-12)
        previous_request, previous_rate, previous_time = requested, rate, index*.1


def test_antipodal_quaternion_sign_selects_identical_shortest_request():
    config = profile()
    a = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    b = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    target = (0., -1., 0., 0.)
    qa, da = a.update(target, (1., 0., 0., 0.), (0., 0., 0.), .1)
    qb, db = b.update(tuple(-x for x in target), (1., 0., 0., 0.), (0., 0., 0.), .1)
    assert qa == qb
    assert da["receipt"]["goal_error_rotation_eci_rad"] == db["receipt"]["goal_error_rotation_eci_rad"]
    assert da["receipt"]["goal_error_rotation_eci_rad"] == pytest.approx([math.pi, 0., 0.])


@pytest.mark.parametrize("scalar", [1e-16, -1e-16])
def test_pi_tie_ignores_tiny_scalar_sign_for_equivalent_quaternions(scalar):
    config = profile()
    goal = (scalar, 0., -1., 0.)
    a = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    b = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    qa, da = a.update(goal, (1., 0., 0., 0.), (0., 0., 0.), .1)
    qb, db = b.update(tuple(-x for x in goal), (1., 0., 0., 0.), (0., 0., 0.), .1)
    assert qa == qb
    assert da["receipt"]["raw_goal_rotation_eci_rad"] == db["receipt"]["raw_goal_rotation_eci_rad"]


def test_constant_analytic_moving_goal_converges_after_bounded_acceleration_ramp():
    config = profile()
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    rate = math.radians(2.5)
    errors = []
    for index in range(1, 61):
        goal = quaternion((0., 1., 0.), rate*index*.1)
        requested, detail = tracker.update(goal, (1., 0., 0., 0.), (0., 0., 0.), index*.1)
        delta = dyn.quaternion_multiply(goal, (requested[0], *[-x for x in requested[1:]]))
        errors.append(2*math.atan2(math.hypot(*delta[1:]), abs(delta[0])))
    # This is reference geometry only, with no physical flight propagation.
    assert max(errors) < math.radians(2.)
    assert errors[-1] < 1e-10
    assert detail["receipt"]["reference_rate_eci_rad_s"][1] == pytest.approx(rate, abs=1e-12)


@pytest.mark.parametrize("initial_rate", [0., .05, -.05])
def test_stationary_goal_brakes_and_settles_over_twenty_four_seconds(initial_rate):
    config = profile()
    axis = (1/math.sqrt(14), 2/math.sqrt(14), 3/math.sqrt(14))
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 0.,
        initial_reference_rate_eci_rad_s=tuple(initial_rate*x for x in axis))
    goal = quaternion(axis, 1.)
    for index in range(1, 241):
        requested, detail = tracker.update(goal, (1., 0., 0., 0.), (0., 0., 0.), index*.1)
        receipt = detail["receipt"]
        assert math.hypot(*receipt["reference_rate_eci_rad_s"]) <= .03/.28+1e-12
        assert math.hypot(*receipt["reference_acceleration_eci_rad_s2"]) <= .03+1e-12
        angle = math.hypot(*receipt["closing_error_rotation_eci_rad"])
        closing = math.hypot(*receipt["closing_reference_rate_eci_rad_s"])
        assert closing*receipt["interval_s"]+closing**2/(2*.03) <= angle+1e-12
    error = dyn.quaternion_multiply(goal, (requested[0], *[-x for x in requested[1:]]))
    assert 2*math.atan2(math.hypot(*error[1:]), abs(error[0])) < 1e-10
    assert math.hypot(*receipt["reference_rate_eci_rad_s"]) < 1e-10


def test_causal_goal_history_removes_motion_before_measuring_closing_error():
    config = profile()
    rate = .04
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 0.,
        initial_reference_rate_eci_rad_s=(0., rate, 0.))
    goal = quaternion((0., 1., 0.), rate*.1)
    requested, detail = tracker.update(goal, (1., 0., 0., 0.), (0., 0., 0.), .1)
    receipt = detail["receipt"]
    assert receipt["previous_raw_goal_q_body_to_eci"] == [1., 0., 0., 0.]
    assert receipt["previous_raw_goal_time_s"] == 0.
    assert receipt["goal_rate_eci_rad_s"] == pytest.approx([0., rate, 0.])
    assert receipt["closing_error_rotation_eci_rad"] == pytest.approx([0., 0., 0.])
    assert requested == pytest.approx(goal)


def test_inertial_rate_and_acceleration_are_rotated_into_actual_body_axes():
    config = profile()
    actual = quaternion((0., 0., 1.), math.pi/2)
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    _, detail = tracker.update(quaternion((1., 0., 0.), .5), actual, (0., 0., .1), .1)
    assert detail["reference_rate_body_rad_s"] == pytest.approx((0., -.003, 0.), abs=1e-12)
    assert detail["reference_acceleration_body_rad_s"] == pytest.approx((0., -.03, 0.), abs=1e-12)


def test_failed_update_does_not_mutate_requested_history():
    config = profile()
    tracker = ReferenceTracker(config, (1., 0., 0., 0.), 0.)
    tracker.update((1., 0., 0., 0.), (1., 0., 0., 0.), (0., 0., 0.), .1)
    before = deepcopy(vars(tracker))
    with pytest.raises(ValueError, match="invalid_reference_interval"):
        tracker.update((1., 0., 0., 0.), (1., 0., 0., 0.), (0., 0., 0.), .1)
    assert vars(tracker) == before


@pytest.mark.parametrize("clock", [math.nan, math.inf, True, "1", -1., 1.01])
def test_invalid_update_clock_is_rejected(clock):
    tracker = ReferenceTracker(profile(), (1., 0., 0., 0.), 0.)
    with pytest.raises(ValueError):
        tracker.update((1., 0., 0., 0.), (1., 0., 0., 0.), (0., 0., 0.), clock)


@pytest.mark.parametrize("target", [(0., 0., 0., 0.), (2., 0., 0., 0.),
                                     (1., 0., 0.), (True, 0., 0., 0.), (math.nan, 0., 0., 0.)])
def test_invalid_quaternions_are_rejected(target):
    tracker = ReferenceTracker(profile(), (1., 0., 0., 0.), 0.)
    with pytest.raises(ValueError):
        tracker.update(target, (1., 0., 0., 0.), (0., 0., 0.), .1)


@pytest.mark.parametrize("field,value", [("max_angular_acceleration_rad_s2", 0.),
    ("attitude_frequency_rad_s", -1.), ("attitude_damping_ratio", True)])
def test_invalid_profiles_are_rejected(field, value):
    config = profile()
    config["guidance"][field] = value
    with pytest.raises(ValueError, match="invalid_reference_profile"):
        ReferenceTracker(config, (1., 0., 0., 0.), 0.)


def test_input_sequences_and_profile_are_not_mutated_or_retained_as_aliases():
    config = profile()
    initial, actual, rate = [1., 0., 0., 0.], [1., 0., 0., 0.], [0., 0., 0.]
    before = deepcopy((config, initial, actual, rate))
    tracker = ReferenceTracker(config, initial, 0.)
    tracker.update(initial, actual, rate, .1)
    assert (config, initial, actual, rate) == before
    initial[0] = 0.
    config["guidance"]["max_angular_acceleration_rad_s2"] = .8
    assert tracker.quaternion == (1., 0., 0., 0.)
    assert tracker.maximum_acceleration_rad_s2 == .03


def test_internal_numpy_real_scalars_are_cast_without_accepting_boolean_vectors():
    config = profile()
    tracker = ReferenceTracker(config, np.array((1., 0., 0., 0.), dtype=np.float64), np.float64(0.))
    requested, detail = tracker.update(np.array((1., 0., 0., 0.), dtype=np.float32),
        np.array((1., 0., 0., 0.), dtype=np.float64), np.zeros(3), np.float64(.1))
    assert all(type(value) is float for value in requested)
    assert all(type(value) is float for value in detail["receipt"]["reference_rate_eci_rad_s"])
    with pytest.raises(ValueError):
        tracker.update((True, 0., 0., 0.), requested, (0., 0., 0.), .2)


def test_reference_rate_tracking_removes_zero_error_constant_rate_damping():
    config = profile()
    rate = (0., math.radians(2.5), 0.)
    acceleration, receipt = tracking_acceleration((0., 0., 0.), rate, rate, (0., 0., 0.), config)
    assert acceleration == (0., 0., 0.)
    assert receipt["policy_id"] == POLICY_ID
    assert receipt["transport_term_body_rad_s2"] == [0., 0., 0.]
    assert receipt["reference_tracking_is_execution"] is False


def test_rotating_body_transport_term_has_correct_sign_and_acceleration_clip():
    config = profile()
    acceleration, receipt = tracking_acceleration((0., 0., 0.), (0., 0., .2), (.04, 0., 0.), (.01, 0., 0.), config)
    assert receipt["transport_term_body_rad_s2"] == pytest.approx([0., .008, 0.])
    assert receipt["raw_requested_acceleration_body_rad_s2"] == pytest.approx([.0324, -.008, -.112])
    assert acceleration == pytest.approx((.03, -.008, -.03))
    assert receipt["acceleration_clipped"] is True


@pytest.mark.parametrize("rate,acceleration", [((True, 0., 0.), (0., 0., 0.)),
    ((.2, 0., 0.), (0., 0., 0.)), ((0., 0., 0.), (.04, 0., 0.)),
    ((0., 0., 0.), (math.inf, 0., 0.))])
def test_feedforward_requires_finite_bounded_vectors(rate, acceleration):
    with pytest.raises(ValueError):
        tracking_acceleration((0., 0., 0.), (0., 0., 0.), rate, acceleration, profile())


def test_default_controller_and_explicit_none_preserve_identical_commands_and_diagnostics():
    config, booster, state = fixture()
    a = control(state, booster, state.q_body_to_eci, .4, 3, config)
    b = control(state, booster, state.q_body_to_eci, .4, 3, config,
        reference_rate_body_rad_s=None, reference_acceleration_body_rad_s=None)
    assert asdict(a[0]) == asdict(b[0])
    assert a[1] == b[1]
    assert "reference_tracking_control" not in a[1]


def test_optional_control_receipt_binds_actual_inertia_gyro_aero_and_prefin_demand():
    config, booster, state = fixture()
    original = asdict(state)
    rate, acceleration = (.01, .02, 0.), (0., .01, 0.)
    _, diagnostic = control(state, booster, state.q_body_to_eci, .4, 3, config,
        reference_rate_body_rad_s=rate, reference_acceleration_body_rad_s=acceleration)
    receipt = diagnostic["reference_tracking_control"]
    observed = dyn.observe(state, booster)
    inertia = np.asarray(observed["inertia_kg_m2"])
    omega = np.asarray(state.omega_body_rad_s)
    expected = inertia@np.asarray(receipt["limited_requested_acceleration_body_rad_s2"])+np.cross(omega, inertia@omega)
    assert receipt["requested_rigid_body_torque_body_nm"] == pytest.approx(expected)
    assert receipt["actual_aero_torque_body_nm"] == list(observed["aero_torque_body_nm"])
    assert diagnostic["requested_torque_body_nm"] == receipt["pre_fin_torque_body_nm"]
    assert asdict(state) == original


def test_measured_tvc_adapter_passes_reference_tracking_without_changing_main_commands():
    config, booster, state = fixture()
    command, diagnostic = control_with_measured_tvc(state, booster, state.q_body_to_eci, .4, 3, config,
        reference_rate_body_rad_s=(0., .03, 0.), reference_acceleration_body_rad_s=(0., 0., 0.))
    assert "reference_tracking_control" in diagnostic
    assert "measured_tvc_allocation" in diagnostic
    assert all(c.enabled and c.throttle == .4 for c in command.engines[:3])
    assert all(not c.enabled and c.throttle == 0. for c in command.engines[3:33])


def test_acceleration_without_reference_rate_is_rejected():
    config, booster, state = fixture()
    with pytest.raises(ValueError, match="reference_acceleration_requires_rate"):
        control(state, booster, state.q_body_to_eci, .4, 3, config,
            reference_acceleration_body_rad_s=(0., .01, 0.))
