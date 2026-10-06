"""Measured finite thrust allocation, separate from return-planning success."""
from dataclasses import replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import control, vehicle
from src.runtime.starship_booster_control import (
    control_coast_stopping_distance, control_with_measured_tvc, reallocate_measured_tvc,
)


def fixture():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    booster = vehicle(profile, "booster")
    state = dyn.State6DOF(0., (8_378_137., 0., 0.), (0., 0., 0.), (1., 0., 0., 0.), (.04, 0., 0.),
        78_000., tuple(dyn.EngineState(throttle=.4 if i < 3 else 0.) for i in range(len(booster.engines))),
        tuple(0. for _ in booster.aero_panels))
    return profile, booster, state


def test_spooling_shutdown_keeps_gimbal_authority_without_restarting_engines():
    profile, booster, state = fixture()
    legacy, _ = control(state, booster, state.q_body_to_eci, 0., 0, profile)
    corrected, diagnostic = control_with_measured_tvc(state, booster, state.q_body_to_eci, 0., 0, profile)
    assert all(not command.enabled and command.throttle == 0. for command in corrected.engines[:33])
    assert all(command.gimbal_x_rad == command.gimbal_y_rad == 0. for command in legacy.engines[:33])
    assert any(abs(command.gimbal_x_rad) > .01 for command in corrected.engines[:3])
    assert diagnostic["measured_tvc_allocation"]["gimballed_engine_count"] == 3
    assert diagnostic["measured_tvc_allocation"]["main_thrust_n"] == pytest.approx(3*.4*profile["booster"]["engine_thrust_n"])


def test_integrated_finite_shutdown_brakes_rotation_more_than_command_only_allocator():
    profile, booster, initial = fixture()
    results = []
    for controller in (control, control_with_measured_tvc):
        state = initial
        peak_gimbal = 0.
        for _ in range(150):
            command, _ = controller(state, booster, initial.q_body_to_eci, 0., 0, profile)
            assert all(not c.enabled and c.throttle == 0 for c in command.engines[:33])
            before = state
            state = dyn.step(state, booster, command, .02, gravity=False, j2=False, atmosphere=False, gravity_gradient=False)
            assert state.time_s > before.time_s
            assert state.propellant_kg < before.propellant_kg
            peak_gimbal = max(peak_gimbal, *(abs(e.gimbal_x_rad) for e in state.engine_states[:3]))
        results.append((state, peak_gimbal))
    legacy, corrected = results[0][0], results[1][0]
    assert math.hypot(*corrected.omega_body_rad_s) < math.hypot(*legacy.omega_body_rad_s)-.005
    assert results[0][1] == 0.
    assert 0.01 < results[1][1] <= math.radians(profile["actuators"]["max_gimbal_deg"])
    assert corrected.q_body_to_eci != initial.q_body_to_eci
    assert corrected.engine_states[0].throttle < .001  # Actual spool decays, never snapped off.


def test_nonuniform_engine_states_and_failure_use_measured_loads():
    profile, booster, state = fixture()
    engines = list(state.engine_states)
    engines[0] = replace(engines[0], available=False)
    engines[1] = replace(engines[1], throttle=.1)
    engines[2] = replace(engines[2], throttle=.3)
    state = replace(state, engine_states=tuple(engines))
    command, diagnostic = control_with_measured_tvc(state, booster, state.q_body_to_eci, .4, 3, profile)
    allocation = diagnostic["measured_tvc_allocation"]
    assert allocation["gimballed_engine_count"] == 2
    assert allocation["main_thrust_n"] == pytest.approx(.4*profile["booster"]["engine_thrust_n"])
    assert all(c.enabled and c.throttle == .4 for c in command.engines[:3])


def test_exact_local_gimbal_jacobian_uses_current_deflected_engine_axis():
    profile, booster, state = fixture()
    engines = tuple(replace(e, throttle=.3 if i == 0 else 0., gimbal_x_rad=.08 if i == 0 else 0., gimbal_y_rad=-.05 if i == 0 else 0.) for i, e in enumerate(state.engine_states))
    state = replace(state, omega_body_rad_s=(0., 0., 0.), engine_states=engines)
    observed = dyn.observe(state, booster)
    delta = 1e-5
    perturbed = replace(state, engine_states=(replace(engines[0], gimbal_x_rad=.08+delta), *engines[1:]))
    target_torque = dyn.observe(perturbed, booster)["thrust_torque_body_nm"]
    base = dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in booster.engines), state.flap_angles_rad)
    command, _ = reallocate_measured_tvc(state, booster, base, {"requested_torque_body_nm": target_torque}, profile, observed=observed)
    held = replace(state, engine_states=tuple(replace(e, gimbal_x_rad=c.gimbal_x_rad, gimbal_y_rad=c.gimbal_y_rad) for e, c in zip(engines, command.engines)))
    achieved = np.asarray(dyn.observe(held, booster)["thrust_torque_body_nm"])
    assert np.linalg.norm(achieved-target_torque) < .1


def test_absent_physical_main_thrust_preserves_legacy_command():
    profile, booster, state = fixture()
    state = replace(state, engine_states=tuple(replace(e, throttle=0.) for e in state.engine_states))
    legacy, _ = control(state, booster, state.q_body_to_eci, 0., 0, profile)
    command, diagnostic = control_with_measured_tvc(state, booster, state.q_body_to_eci, 0., 0, profile)
    assert command == legacy
    assert diagnostic["measured_tvc_allocation"]["applied"] is False


def coast_fixture():
    profile, booster, state = fixture()
    state = replace(state, q_body_to_eci=(math.cos(math.radians(70)), 0., math.sin(math.radians(70)), 0.),
                    omega_body_rad_s=(0., 0., 0.), engine_states=tuple(dyn.EngineState() for _ in booster.engines))
    return profile, booster, state


def test_coast_rate_reference_is_derived_from_physical_stop_distance_and_delay():
    profile, booster, state = coast_fixture()
    command, info = control_coast_stopping_distance(state, booster, (1., 0., 0., 0.), profile)
    speed, alpha, delay = (info[key] for key in
                          ("coast_rate_limit_rad_s", "coast_stopping_alpha_rad_s2", "coast_actuator_delay_s"))
    assert speed*delay+speed*speed/(2*alpha) == pytest.approx(math.radians(140.))
    assert all(not c.enabled and c.throttle == 0 for c in command.engines[:33])
    assert any(c.enabled for c in command.engines[33:])
    equivalent, _ = control_coast_stopping_distance(state, booster, (-1., 0., 0., 0.), profile)
    assert command == equivalent


def test_integrated_140_degree_coast_slew_finishes_with_actual_jets_and_fuel():
    from src.runtime.starship_sixdof_booster import _coast_control_profile
    profile, booster, initial = coast_fixture()
    results = []
    # Fixed control comparison: same 140-degree initial error, 180 s horizon,
    # zero initial rates, unchanged RCS/vehicle/finite actuator parameters.
    # Gravity/aerodynamics are off only to isolate attainable rotational control.
    for candidate in (False, True):
        state, settled_at = initial, None
        for _ in range(720):
            if candidate:
                command, _ = control_coast_stopping_distance(state, booster, (1., 0., 0., 0.), profile)
            else:
                coast_profile, _, _ = _coast_control_profile(profile, dyn.observe(state, booster))
                command, _ = control(state, booster, (1., 0., 0., 0.), 0., 0, coast_profile)
            assert all(not c.enabled and c.throttle == 0 for c in command.engines[:33])
            state = dyn.step(state, booster, command, .25, gravity=False, j2=False,
                             atmosphere=False, gravity_gradient=False)
            error = math.degrees(2*math.acos(min(1., abs(state.q_body_to_eci[0]))))
            rate = math.hypot(*state.omega_body_rad_s)
            if settled_at is None and error < 1 and rate < .003:
                settled_at = state.time_s
        results.append((state, error, rate, settled_at))
    legacy, candidate = results
    assert legacy[1] > 20. and legacy[3] is None
    assert candidate[1] < 1. and candidate[2] < .003 and candidate[3] < 150.
    assert candidate[0].propellant_kg < legacy[0].propellant_kg < initial.propellant_kg
    assert candidate[0].r_eci_m == legacy[0].r_eci_m == initial.r_eci_m  # Opposed jets impart torque without translation.


def test_coast_slew_uses_residual_tvc_without_enabling_main_engines():
    profile, booster, state = fixture()
    command, info = control_coast_stopping_distance(state, booster, state.q_body_to_eci, profile)
    assert all(not c.enabled and c.throttle == 0 for c in command.engines[:33])
    assert any(abs(c.gimbal_x_rad) > 1e-5 for c in command.engines[:3])
    assert info["measured_tvc_allocation"]["applied"] is True


@pytest.mark.parametrize("interval", [True, 0, -.1, 2, math.nan, math.inf])
def test_coast_slew_requires_a_bounded_control_interval(interval):
    profile, booster, state = coast_fixture()
    with pytest.raises(ValueError, match="coast control interval"):
        control_coast_stopping_distance(state, booster, (1., 0., 0., 0.), profile, control_interval_s=interval)
