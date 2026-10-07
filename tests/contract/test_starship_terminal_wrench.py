"""Pure finite-wrench projection tests with public geometry; no integration."""
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime import starship_physics as env
from src.runtime.starship_sixdof_mission import vehicle
from src.runtime.starship_terminal_wrench import allocate_terminal_wrench
from src.runtime.starship_fin_allocation import actuator_endpoint


def fixture(throttle=.5):
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, "booster")
    engines = tuple(dyn.EngineState(throttle=throttle if i in (3, 8) else 0.) for i in range(len(body.engines)))
    state = dyn.State6DOF(0., (env.EARTH_EQUATORIAL_RADIUS_M+1000., 0., 0.), (0., 0., 0.),
        (1., 0., 0., 0.), (0., 0., 0.), 20000., engines, tuple(0. for _ in body.aero_panels))
    command = dyn.Command6DOF(tuple(dyn.EngineCommand(i in (3, 8), throttle if i in (3, 8) else 0.)
        for i in range(len(body.engines))), tuple(0. for _ in body.aero_panels))
    observed = {"com_body_m": list(dyn.mass_properties(body, state.propellant_kg).com_body_m)}
    force = 2*body.engines[3].max_thrust_n*throttle
    return body, state, command, observed, force


def allocate(body, state, command, observed, desired, floor=0., demand=(0., 0., 0.)):
    return allocate_terminal_wrench(state, body, command, observed, desired, demand,
        interval_s=.1, up_eci=(0., 0., 1.), minimum_vertical_thrust_n=floor)


def test_aligned_balanced_hover_projection_preserves_body_and_moment_headroom():
    body, state, command, observed, force = fixture()
    before = deepcopy((state, command, observed))
    selected, receipt = allocate(body, state, command, observed, (0., 0., force), force)
    assert receipt["status"] == "accepted"
    entry = receipt["mask_candidates"][receipt["selected_mask_index"]]
    assert entry["objective"] < 1e-12
    assert entry["mask"] == [3, 8]
    assert selected.flap_angles_rad == command.flap_angles_rad
    assert all(a.gimbal_x_rad == b.gimbal_x_rad and a.gimbal_y_rad == b.gimbal_y_rad
        for a, b in zip(selected.engines, command.engines))
    assert (state, command, observed) == before
    assert receipt["physical_state_assigned"] is False
    assert receipt["joint_trajectory_feasibility_established"] is False


def test_misaligned_projection_does_not_divide_by_axis_alignment_and_preserves_shutdown_lag():
    body, state, command, observed, force = fixture()
    desired = (force*math.sqrt(.75), 0., force*.5)
    selected, receipt = allocate(body, state, command, observed, desired, force*.5)
    entry = receipt["mask_candidates"][receipt["selected_mask_index"]]
    assert receipt["status"] == "accepted"
    assert entry["mask"] == []
    expected = force*math.exp(-.1/body.engines[3].throttle_time_constant_s)
    assert entry["predicted_force_eci_n"][2] == pytest.approx(expected)
    assert expected < force and expected > force*.5
    assert all(not e.enabled for e in selected.engines[:13])
    assert state.engine_states[3].throttle == .5


def test_actual_profile_point7_rate_radius_is_retained_and_off_decay_is_exact_clipped_lag():
    body, state, command, observed, force = fixture(1.)
    assert body.engines[3].throttle_time_constant_s*body.engines[3].throttle_rate_per_s == .7
    _, receipt = allocate(body, state, command, observed, (0., 0., 0.))
    off = receipt["mask_candidates"][0]
    expected = 2*body.engines[3].max_thrust_n*actuator_endpoint(1., 0., .35, 2., .1)
    assert off["force_a_eci_n"][2] == pytest.approx(expected)
    assert expected != pytest.approx(force*math.exp(-.1/.35), rel=1e-4)
    for entry in receipt["mask_candidates"]:
        for interval in entry["selected_engine_affine_intervals"]:
            index = interval["engine_index"]
            actual = state.engine_states[index].throttle
            assert interval["command_interval"] == pytest.approx([max(0., actual-.7), min(1., actual+.7)])


def test_finite_throttle_minimum_and_kkt_certificate_are_not_clipped_after_solving():
    body, state, command, observed, force = fixture()
    _, receipt = allocate(body, state, command, observed, (0., 0., force*1.05), force)
    assert len(receipt["mask_candidates"]) <= 20
    for entry in receipt["mask_candidates"]:
        if not entry["feasible"]:
            continue
        lo, hi = entry["feasible_interval"]
        value = entry["throttle"]
        assert lo <= value <= hi
        if entry["mask"]:
            assert value >= max(body.engines[i].min_throttle for i in entry["mask"])
        b = entry["force_b_eci_n"]
        scale = max(1., sum(v*v for v in b))
        gradient = entry["half_objective_gradient"]
        tolerance = 1e-12*scale
        assert gradient >= -tolerance if value == lo else gradient <= tolerance if value == hi else abs(gradient) <= tolerance
        for constraint in entry["constraints"]:
            assert constraint["slope"]*value+constraint["intercept"] >= -1e-6


def test_impossible_vertical_floor_returns_base_command_explicitly_deferred():
    body, state, command, observed, force = fixture()
    selected, receipt = allocate(body, state, command, observed, (0., 0., force), 1e9)
    assert selected is command
    assert receipt["status"] == "deferred"
    assert receipt["reason"] == "no_finite_projection_preserves_vertical_and_moment_headroom"
    assert not any(entry["feasible"] for entry in receipt["mask_candidates"])


def test_unavailable_engine_is_excluded_without_assigning_availability_or_force():
    body, state, command, observed, force = fixture()
    engines = list(state.engine_states)
    engines[3] = replace(engines[3], available=False)
    failed = replace(state, engine_states=tuple(engines))
    _, receipt = allocate(body, failed, command, observed, (0., 0., force))
    assert all(3 not in entry["mask"] for entry in receipt["mask_candidates"])
    assert receipt["finite_endpoint_geometry"][3]["force_per_throttle_body_n"] == (0., 0., 0.)
    assert failed.engine_states[3].available is False


def test_factory_booster_main13_is_rejected_outside_landing_mask():
    body, state, command, observed, force = fixture()
    assert body.engines[13].name == "booster_main_13"
    engines = list(command.engines)
    engines[13] = dyn.EngineCommand(True, .4)
    expanded = replace(command, engines=tuple(engines))
    selected, receipt = allocate(body, state, expanded, observed, (0., 0., force))
    assert selected is expanded
    assert receipt["status"] == "deferred" and receipt["reason"] == "outside_landing_main_mask_scope"


@pytest.mark.parametrize("value", [True, 0., -.1, float("inf"), float("nan")])
def test_invalid_interval_rejected_before_prediction(value):
    body, state, command, observed, force = fixture()
    with pytest.raises(ValueError):
        allocate_terminal_wrench(state, body, command, observed, (0., 0., force), (0., 0., 0.),
            interval_s=value, up_eci=(0., 0., 1.), minimum_vertical_thrust_n=0.)
