"""Moment-first allocation preserves finite authority instead of trading it away."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_fin_allocation as fin
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_booster_control import control_with_measured_tvc
from src.runtime.starship_sixdof_mission import _attitude, control, vehicle


@pytest.fixture(scope="module")
def configured():
    path = Path(__file__).resolve().parents[2]/"examples"/"spaceflight"/"starship-sixdof-profile.json"
    profile = json.loads(path.read_text())
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              5000., 60000., time_s=450.)
    up, east, _ = env.local_frame(point)
    axis = env.add(env.scale(east, .9), env.scale(up, math.sqrt(1.-.9*.9)))
    state = dyn.State6DOF(point.time_s, point.r,
        env.add(point.v, env.add(env.scale(east, -600.), env.scale(up, -580.))),
        _attitude(axis, east), (0., .04, 0.), 60000.,
        tuple(dyn.EngineState() for _ in booster.engines), (0., 0., 0., 0., -.1, .1))
    return profile, booster, state


def test_full_rank_primary_cannot_be_traded_for_a_better_trim():
    matrix, demand = np.eye(3), np.array([.01, -.02, .03])
    primary = fin.bounded_least_squares(matrix, demand, [-.04]*3, [.04]*3)
    selected = fin.closest_trim_with_primary_image(matrix, matrix@primary, [.3, .3, -.3], [.4]*3,
                                                  [-.04]*3, [.04]*3)
    assert selected == pytest.approx(demand)


def test_rank_deficient_secondary_projection_has_independent_analytic_minimum():
    matrix = np.asarray([[1., 1., 1.]])
    selected = fin.closest_trim_with_primary_image(matrix, [.03], [.2, -.15, .1], [.4]*3,
                                                  [-.04]*3, [.04]*3)
    assert selected == pytest.approx([.04, -.04, .03])
    assert matrix@selected == pytest.approx([.03])


def test_rank_two_projection_preserves_both_moments_on_the_active_face():
    matrix = np.asarray([[1., 1., 0.], [0., 1., 1.]])
    selected = fin.closest_trim_with_primary_image(matrix, [.02, .03], [.1, -.1, .1], [.4]*3,
                                                  [-.04]*3, [.04]*3)
    assert selected == pytest.approx([.03, -.01, .04])
    assert matrix@selected == pytest.approx([.02, .03])


def test_zero_effectiveness_only_selects_finite_reachable_trim():
    selected = fin.closest_trim_with_primary_image(np.zeros((3, 3)), [0., 0., 0.],
                                                  [.3, -.3, .01], [.4]*3, [-.04]*3, [.04]*3)
    assert selected == pytest.approx([.04, -.04, .01])


def test_infeasible_primary_demand_retains_the_minimum_error_and_kkt():
    matrix, demand = np.eye(3), np.asarray([.4, -.4, 0.])
    primary = fin.bounded_least_squares(matrix, demand, [-.04]*3, [.04]*3)
    selected = fin.closest_trim_with_primary_image(matrix, matrix@primary, [-.3, .3, 0.], [.4]*3,
                                                  [-.04]*3, [.04]*3)
    gradient = matrix.T@(matrix@selected-demand)
    assert selected == pytest.approx([.04, -.04, 0.])
    assert gradient[0] < 0 < gradient[1]
    assert np.linalg.norm(matrix@selected-demand) > 0.


def test_moment_priority_resolves_underused_finite_authority_in_public_high_q_fixture(configured):
    profile, booster, state = configured
    before = asdict(state)
    observed = dyn.observe(state, booster)
    trim = (0., 0., 0., 0., -.38, .38)
    demand = (0., 150000., 0.)
    _, old_remaining, old = fin.allocate_fins(state, booster, demand, observed, profile,
                                              interval_s=.1, trim_angles_rad=trim)
    targets, remaining, receipt = fin.allocate_fins(state, booster, demand, observed, profile,
        interval_s=.1, trim_angles_rad=trim, policy_id=fin.MOMENT_PRIORITY_POLICY_ID)
    assert abs(old_remaining[1]) > 80000.
    assert abs(remaining[1]) < 1000.
    assert receipt["schema"] == "missionos.starship_fin_allocation.v2"
    assert receipt["policy_id"] == fin.MOMENT_PRIORITY_POLICY_ID
    assert receipt["primary_objective"] <= receipt["primary_optimum_objective"]+1e-12
    assert max(abs(x) for x in receipt["primary_half_objective_gradient"]) < 1e-9
    assert max(abs(x) for x in receipt["secondary_image_residual"]) < 1e-10
    assert receipt["actual_state_assigned"] is False and receipt["prediction_is_execution"] is False
    assert receipt["production_policy_admitted"] is False
    for index, predicted, target in zip(receipt["fin_indices"], receipt["predicted_endpoint_angles_rad"],
                                         receipt["command_angles_rad"]):
        panel = booster.aero_panels[index]
        assert abs(target) <= panel.max_deflection_rad
        assert abs(predicted-state.flap_angles_rad[index]) <= panel.deflection_rate_rad_s*.1+1e-10
        assert fin.actuator_endpoint(state.flap_angles_rad[index], target, panel.deflection_time_constant_s,
                                     panel.deflection_rate_rad_s, .1) == pytest.approx(predicted, abs=1e-10)
    # Same-time loads verify the proposed finite endpoint, not a successful flight.
    endpoint = list(state.flap_angles_rad)
    for i, value in zip(receipt["fin_indices"], receipt["predicted_endpoint_angles_rad"]):
        endpoint[i] = value
    actual = dyn.observe(replace(state, flap_angles_rad=tuple(endpoint)), booster)
    increment = np.asarray(actual["aero_torque_body_nm"])-observed["aero_torque_body_nm"]
    assert np.asarray(demand)-increment == pytest.approx(remaining, abs=1e-6)
    assert len(targets) == len(old["actual_angles_rad"])+3
    assert asdict(state) == before


def test_default_regularized_path_remains_exactly_identical(configured):
    profile, booster, state = configured
    observed = dyn.observe(state, booster)
    ordinary = fin.allocate_fins(state, booster, [0., 150000., 0.], observed, profile, interval_s=.1)
    explicit = fin.allocate_fins(state, booster, [0., 150000., 0.], observed, profile, interval_s=.1,
                                 policy_id=fin.POLICY_ID)
    assert ordinary[0] == explicit[0]
    assert np.array_equal(ordinary[1], explicit[1])
    assert ordinary[2] == explicit[2]
    assert ordinary[2]["schema"] == "missionos.starship_fin_allocation.v1"
    assert "primary_optimum_delta_rad" not in ordinary[2]


def test_control_plumbing_requires_explicit_bool_and_preserves_engine_enable_throttle(configured):
    profile, booster, state = configured
    target = dyn.quaternion_multiply(state.q_body_to_eci, dyn.axis_angle((0., 1., 0.), .1))
    with pytest.raises(ValueError, match="development_fin_policy"):
        control(state, booster, target, .4, 3, profile,
                development_fin_policy=fin.MOMENT_PRIORITY_POLICY_ID)
    legacy, _ = control_with_measured_tvc(state, booster, target, .4, 3, profile,
        use_flaps=True, development_fin_allocation=True)
    candidate, diagnostic = control_with_measured_tvc(state, booster, target, .4, 3, profile,
        use_flaps=True, development_fin_allocation=True, development_fin_policy=fin.MOMENT_PRIORITY_POLICY_ID)
    assert diagnostic["development_fin_allocation"]["policy_id"] == fin.MOMENT_PRIORITY_POLICY_ID
    assert [(e.enabled, e.throttle) for e in candidate.engines[:33]] == [(e.enabled, e.throttle) for e in legacy.engines[:33]]


@pytest.mark.parametrize("policy", [True, 0, "unknown", None])
def test_bad_policy_is_refused(configured, policy):
    profile, booster, state = configured
    with pytest.raises(ValueError, match="fin_allocation_policy"):
        fin.allocate_fins(state, booster, [0., 0., 0.], dyn.observe(state, booster), profile,
                          interval_s=.1, policy_id=policy)
    with pytest.raises(ValueError, match="development_fin_policy"):
        control(state, booster, state.q_body_to_eci, 0., 0, profile,
                development_fin_allocation=True, development_fin_policy=policy)


def test_secondary_projector_refuses_an_image_outside_the_box():
    with pytest.raises(ValueError, match="no_box_projection"):
        fin.closest_trim_with_primary_image(np.eye(3), [.5, 0., 0.], [.1]*3, [.4]*3, [-.04]*3, [.04]*3)


def test_inputs_are_not_mutated_by_projection():
    arguments = [np.eye(3), np.asarray([.01, .02, .03]), np.asarray([.2, -.2, .1]),
                 np.asarray([.4]*3), np.asarray([-.04]*3), np.asarray([.04]*3)]
    before = deepcopy(arguments)
    fin.closest_trim_with_primary_image(*arguments)
    assert all(np.array_equal(a, b) for a, b in zip(arguments, before))
