"""Local feedback math, measured geometry and bounded finite-plant probes."""
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_booster_catch import _initialized
from src.runtime.starship_booster_recovery import _tower_observation
from src.runtime.starship_terminal_guidance import hover_feedback_gains, terminal_horizontal_request

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fixture():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    body, state = _initialized(profile, catch, "booster_catch")
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    state = replace(state, v_eci_mps=env.cross(earth, state.r_eci_m),
                    omega_body_rad_s=dyn.inverse_rotate(state.q_body_to_eci, earth))
    return profile, catch, body, state


def observe(fixture, state=None):
    profile, catch, body, original = fixture
    state = state or original
    return _tower_observation(state, body, profile, catch)


def axes(fixture):
    profile, _, _, state = fixture
    site = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], time_s=state.time_s)
    up, east, north = env.local_frame(site)
    return east, north, up


@pytest.mark.parametrize("frequency,damping,gravity", [(.28, 1., 9.81), (.5, .7, 9.), (.2, 1.4, 10.)])
def test_polynomial_state_feedback_places_all_four_local_poles(frequency, damping, gravity):
    gains = hover_feedback_gains(frequency, damping, gravity)
    w, g = frequency, gravity
    matrix = np.array([[0., 1., 0., 0.], [0., 0., g, 0.], [0., 0., 0., 1.],
                       [-w*w*gains["position_rad_per_m"], -w*w*gains["velocity_rad_s_per_m"],
                        -w*w*(1+gains["tilt_dimensionless"]),
                        -2*damping*w-w*w*gains["tilt_rate_s"]]])
    assert np.poly(matrix) == pytest.approx([1., 4*w, 6*w*w, 4*w**3, w**4])
    poles = np.linalg.eigvals(matrix)
    assert all(pole.real < -.99*w for pole in poles)
    assert max(abs(pole+w) for pole in poles) < .001*w


def test_old_pin_pd_counterexample_has_positive_real_poles(fixture):
    profile, catch, _, _ = fixture
    observation = observe(fixture)
    lever = 60-observation["com_body_m"][2]
    w, tau, g = profile["guidance"]["attitude_frequency_rad_s"], catch["terminal_position_tau_s"], 9.81
    # x''=g*theta, old theta_target=-(x+L*theta)/(g*tau²)-2*v/(g*tau).
    old = [1., 2*w, w*w*(1+lever/(g*tau*tau)), 2*w*w/tau, w*w/(tau*tau)]
    assert max(root.real for root in np.roots(old)) > .03
    assert max(root.real for root in np.roots([1., 4*w, 6*w*w, 4*w**3, w**4])) < -.99*w


def test_earth_fixed_upright_hover_requests_no_horizontal_force(fixture):
    profile, catch, _, state = fixture
    before = deepcopy(fixture)
    acceleration, info = terminal_horizontal_request(state, observe(fixture), profile, catch, 9.81)
    assert acceleration == pytest.approx([0., 0.], abs=1e-8)
    assert info["observed_tilt_rate_enu_rad_s"] == pytest.approx([0., 0.], abs=1e-12)
    assert info["earth_rotation_removed"] is True
    assert info["inner_controller_retuned"] is False
    assert info["physical_state_assigned"] is False
    assert info["nonlinear_stability_established"] is False
    assert info["arrival_admitted"] is info["support_admitted"] is False
    assert fixture == before
    json.dumps(info, allow_nan=False)


@pytest.mark.parametrize("component", [0, 1])
def test_measured_position_and_velocity_request_braking(fixture, component):
    profile, catch, _, state = fixture
    axis = axes(fixture)[component]
    position = env.add(state.r_eci_m, env.scale(axis, 1.))
    velocity = env.add(env.cross((0., 0., env.EARTH_ROTATION_RAD_S), position), env.scale(axis, .1))
    moved = replace(state, r_eci_m=position, v_eci_mps=velocity)
    acceleration, info = terminal_horizontal_request(moved, observe(fixture, moved), profile, catch, 9.81)
    assert acceleration[component] < 0
    assert info["observed_cg_position_enu_m"][component] == pytest.approx(1., abs=1e-8)
    assert info["observed_cg_velocity_enu_mps"][component] == pytest.approx(.1, abs=1e-8)


@pytest.mark.parametrize("component", [0, 1])
def test_pin_lever_removed_before_position_feedback_and_rate_sign_is_physical(fixture, component):
    profile, catch, _, state = fixture
    east, north, _ = axes(fixture)
    rotation_axis = north if component == 0 else env.scale(east, -1.)
    angle, rate = math.radians(1.), .003
    quaternion = dyn.quaternion_multiply(dyn.axis_angle(rotation_axis, angle), state.q_body_to_eci)
    inertial_rate = env.add((0., 0., env.EARTH_ROTATION_RAD_S), env.scale(rotation_axis, rate))
    tilted = replace(state, q_body_to_eci=quaternion,
                     omega_body_rad_s=dyn.inverse_rotate(quaternion, inertial_rate))
    acceleration, info = terminal_horizontal_request(tilted, observe(fixture, tilted), profile, catch, 9.81)
    assert info["observed_pin_midpoint_enu_m"][component] > .4
    assert info["observed_cg_position_enu_m"][component] == pytest.approx(0., abs=1e-8)
    assert info["observed_tilt_enu_rad"][component] == pytest.approx(angle, abs=1e-10)
    assert info["observed_tilt_rate_enu_rad_s"][component] == pytest.approx(rate, abs=1e-10)
    expected = -5*angle-info["feedback_gains"]["tilt_rate_s"]*rate
    assert info["unbounded_target_tilt_enu_rad"][component] == pytest.approx(expected, abs=1e-8)
    assert acceleration[component] < 0


def test_tilt_cone_is_finite_and_uses_existing_configuration(fixture):
    profile, catch, _, state = fixture
    east, north, _ = axes(fixture)
    position = env.add(state.r_eci_m, env.add(env.scale(east, 10000.), env.scale(north, -10000.)))
    moved = replace(state, r_eci_m=position)
    acceleration, info = terminal_horizontal_request(moved, observe(fixture, moved), profile, catch, 9.81)
    assert math.hypot(*acceleration) == pytest.approx(9.81*math.tan(math.radians(catch["terminal_max_tilt_deg"])))
    assert info["tilt_request_saturated"] is True
    assert acceleration[0] < 0 < acceleration[1]
    assert all(math.isfinite(value) for value in acceleration)


@pytest.mark.parametrize("value", [True, None, 0., -1., math.nan, math.inf])
def test_invalid_gravity_fails_before_generating_a_request(fixture, value):
    profile, catch, _, state = fixture
    with pytest.raises(ValueError, match="gravity"):
        terminal_horizontal_request(state, observe(fixture), profile, catch, value)


def test_old_observation_is_not_silently_reused(fixture):
    profile, catch, _, state = fixture
    arrival = observe(fixture)
    arrival["time_s"] -= .1
    with pytest.raises(ValueError, match="time_mismatch"):
        terminal_horizontal_request(state, arrival, profile, catch, 9.81)


def test_missing_or_nonfinite_material_geometry_is_rejected(fixture):
    profile, catch, _, state = fixture
    arrival = observe(fixture)
    arrival["com_body_m"] = [0., 0., math.nan]
    with pytest.raises(ValueError, match="centroid"):
        terminal_horizontal_request(state, arrival, profile, catch, 9.81)


def test_finite_balanced_pair_hover_channel_local_jacobian(fixture):
    """Twelve finite plant steps, not a flight or nonlinear stability certificate.

    Project the actual east/pitch channel with its two finite gimbals. Other
    axes, throttle scheduling, large errors and changing operating points are
    outside this local derivative test. Net-thrust trim matches the caller.
    """
    from src.runtime.starship_booster_control import control_with_measured_tvc, reallocate_measured_tvc
    from src.runtime.starship_sixdof_mission import _attitude

    profile, catch, body, original = fixture
    before = deepcopy(fixture)
    east, north, up = axes(fixture)
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    gravity = env.EARTH_MU_M3_S2/env.norm(original.r_eci_m)**2
    mass = dyn.mass_properties(body, original.propellant_kg).mass_kg
    selected = (3, 8)
    throttle = mass*gravity/sum(body.engines[i].max_thrust_n for i in selected)
    assert all(body.engines[i].min_throttle < throttle < 1 for i in selected)
    initial = replace(original, engine_states=tuple(
        dyn.EngineState(throttle=throttle if index in selected else 0.)
        for index in range(len(body.engines))))
    dt = .02

    def finite_map(values):
        x, speed, angle, rate, first_gimbal, second_gimbal = map(float, values)
        position = env.add(initial.r_eci_m, env.scale(east, x))
        quaternion = dyn.quaternion_multiply(dyn.axis_angle(north, angle), initial.q_body_to_eci)
        engines = list(initial.engine_states)
        for index, gimbal in zip(selected, (first_gimbal, second_gimbal)):
            engines[index] = replace(engines[index], gimbal_y_rad=gimbal)
        state = replace(initial, r_eci_m=position, q_body_to_eci=quaternion,
                        v_eci_mps=env.add(env.cross(earth, position), env.scale(east, speed)),
                        omega_body_rad_s=dyn.inverse_rotate(quaternion, env.add(earth, env.scale(north, rate))),
                        engine_states=tuple(engines))
        observed = dyn.observe(state, body)
        arrival = _tower_observation(state, body, profile, catch)
        acceleration, _ = terminal_horizontal_request(state, arrival, profile, catch, gravity)
        requested = env.add(env.add(env.scale(east, acceleration[0]), env.scale(north, acceleration[1])), env.scale(up, gravity))
        target = _attitude(env.unit(requested), east)
        trim = dyn.quaternion_from_two_vectors(env.unit(observed["thrust_force_body_n"]), (0., 0., 1.))
        target = dyn.normalize_quaternion(dyn.quaternion_multiply(target, trim))
        command, diagnostic = control_with_measured_tvc(state, body, target, throttle, 2, profile)
        commands = list(command.engines)
        for index in range(33):
            commands[index] = replace(commands[index], enabled=index in selected,
                                      throttle=throttle if index in selected else 0.)
        command = dyn.Command6DOF(tuple(commands), command.flap_angles_rad)
        command, _ = reallocate_measured_tvc(state, body, command, diagnostic, profile, observed=observed)
        actual = dyn.step(state, body, command, dt)
        assert actual.time_s > state.time_s
        assert actual.propellant_kg < state.propellant_kg
        _, after = terminal_horizontal_request(actual, _tower_observation(actual, body, profile, catch), profile, catch, gravity)
        return np.array([after["observed_cg_position_enu_m"][0], after["observed_cg_velocity_enu_mps"][0],
                         after["observed_tilt_enu_rad"][0], after["observed_tilt_rate_enu_rad_s"][0],
                         actual.engine_states[selected[0]].gimbal_y_rad, actual.engine_states[selected[1]].gimbal_y_rad])

    jacobian = np.empty((6, 6))
    for column, epsilon in enumerate((.001, .001, .00001, .00001, .00001, .00001)):
        probe = np.zeros(6)
        probe[column] = epsilon
        jacobian[:, column] = (finite_map(probe)-finite_map(-probe))/(2*epsilon)
    poles = np.linalg.eigvals(jacobian)
    assert np.all(np.isfinite(poles))
    # Opposite gimbal-y offsets have zero force/pitch effect for this balanced
    # pair. The measured allocator deliberately retains that actuator null
    # space; it is a neutral mode, not translational/attitude convergence.
    null_mode = np.array([0., 0., 0., 0., 1., -1.])
    assert jacobian@null_mode == pytest.approx(null_mode, abs=1e-5)
    neutral = np.abs(poles-1.) < 1e-7
    assert sum(neutral) == 1, poles
    assert all(abs(pole) < .999 for pole in poles[~neutral]), poles
    assert fixture == before
