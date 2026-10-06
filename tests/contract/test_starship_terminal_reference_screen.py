"""Necessary reference bounds; synthetic public inputs, zero integration."""
from copy import deepcopy

import pytest

from test_starship_fixed_terminal_reference import fixture
from src.runtime import starship_fixed_terminal_reference as reference
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import _attitude
from src.runtime.starship_terminal_reference_screen import screen_terminal_reference, _fixed_and_fin_bounds, repair_terminal_reference_screen


def test_zero_integration_screen_does_not_turn_pose_bounds_into_capture_or_tracking_proof(monkeypatch):
    profile, catch, _, _, snapshot, prior, command = fixture()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    before = deepcopy((plan, snapshot, profile, catch))
    monkeypatch.setattr(dyn, "step", lambda *a, **k: pytest.fail("no integration in screen"))
    result = screen_terminal_reference(plan, snapshot, profile, catch)
    assert result["physical_plant_integrations"] == 0
    assert result["reference_model_evaluations"] == 303
    assert len(result["nodes"]) == 101
    assert (plan, snapshot, profile, catch) == before
    assert result["joint_tracking_feasibility_established"] is False
    assert result["vehicle_global_infeasibility_established"] is False
    assert result["tower_interference_validated"] is False
    assert result["sampled_lower_fuel_integral_continuously_certified"] is False
    assert result["candidate_plant_call_allowed"] is False
    assert result["support_reserve_includes_existing_four_tau_shutdown"] is True
    assert result["nominal_fuel_budget_is_reference_feasibility_proof"] is False


def test_safe_pose_rate_alone_does_not_mask_a_joint_force_cone_conflict():
    profile, catch, _, _, snapshot, prior, command = fixture()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    site, axes = reference._tower_frame(profile, plan["reference_start_time_s"])
    sideways = tuple(float(x) for x in _attitude(axes[0], axes[2]))
    # A deliberately inadmissible independent-pose family for this screen's
    # necessary-bound test. The full reference checker would also reject its
    # request-history/endpoint binding; it is not a flight or qualifying packet.
    plan.update(pose_start_q_body_to_eci=list(sideways), pose_target_q_body_to_eci=list(sideways),
        pose_relative_rotation_vector_eci_rad=[0., 0., 0.], pose_peak_rate_rad_s=0., pose_peak_acceleration_rad_s2=0.)
    result = screen_terminal_reference(plan, snapshot, profile, catch)
    assert result["pose_peak_rate_bound_passed"] is True
    assert result["pose_peak_acceleration_bound_passed"] is True
    assert result["candidate_plant_call_allowed"] is False
    assert any("gimbal_force_cone" in code for issue in result["known_reference_violations"] for code in issue["codes"])


def test_fully_movable_fin_norm_bound_covers_every_hinge_angle_not_only_trim():
    _, _, body, _, _, _, _ = fixture()
    panel = next(p for p in body.aero_panels if p.max_deflection_rad > 0)
    speed, rho = (3., 4., 1.), 1.2
    observed = {"panel_loads": [{"force_body_n": (0., 0., 0.), "local_air_velocity_body_mps": speed}],
        "atmosphere": {"density_kg_m3": rho}}
    proxy = deepcopy(body)
    object.__setattr__(proxy, "aero_panels", (panel,))
    _, _, bound = _fixed_and_fin_bounds(observed, proxy, rho, 0.)
    for index in range(41):
        angle = -panel.max_deflection_rad+2*panel.max_deflection_rad*index/40
        normal = dyn.rotate(dyn.axis_angle(panel.hinge_axis_body, angle), panel.normal_body)
        ns = env.dot(speed, normal)
        tangent = env.add(speed, env.scale(normal, -ns))
        force = env.add(env.scale(normal, -.5*rho*panel.area_m2*panel.normal_coefficient*ns*abs(ns)),
            env.scale(tangent, -.5*rho*panel.area_m2*panel.tangential_coefficient*env.norm(tangent)))
        assert env.norm(force) <= bound+1e-12


def test_startup_capacity_uses_actual_rate_tau_and_excludes_unavailable_main():
    profile, catch, _, _, snapshot, prior, command = fixture()
    snapshot["state"]["engine_states"][0]["available"] = False
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    result = screen_terminal_reference(plan, snapshot, profile, catch)
    assert result["nodes"][0]["main_startup_capacity_n"] == 0.
    final = result["nodes"][-1]
    expected = 12*profile["booster"]["engine_thrust_n"]
    assert final["main_startup_capacity_n"] == pytest.approx(expected, rel=1e-12)
    assert result["nodes"][0]["minimum_enabled_main_throttle"] == .4
    assert result["discrete_minimum_throttle_history_feasibility_established"] is False


def test_pose_peak_violation_rejects_only_the_reference_not_vehicle():
    profile, catch, _, _, snapshot, prior, command = fixture()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    plan["pose_peak_rate_rad_s"] = .2
    result = screen_terminal_reference(plan, snapshot, profile, catch)
    assert result["candidate_plant_call_allowed"] is False
    assert result["vehicle_global_infeasibility_established"] is False


def test_profile_and_context_hashes_cannot_be_swapped_before_screening():
    profile, catch, _, _, snapshot, prior, command = fixture()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    plan["origin_context_sha256"] = "a"*64
    with pytest.raises(ValueError, match="origin_profile_binding"):
        screen_terminal_reference(plan, snapshot, profile, catch)


def test_bound_repair_reuses_stored_model_inputs_and_preserves_original(monkeypatch):
    profile, catch, _, _, snapshot, prior, command = fixture()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    original = screen_terminal_reference(plan, snapshot, profile, catch)
    before = deepcopy(original)
    monkeypatch.setattr(dyn, "observe", lambda *a, **k: pytest.fail("stored-bound repair cannot reevaluate model"))
    monkeypatch.setattr(dyn, "step", lambda *a, **k: pytest.fail("stored-bound repair cannot integrate"))
    repaired = repair_terminal_reference_screen(original, plan, snapshot, profile, catch)
    assert original == before
    assert repaired["schema"] == "missionos.starship_fixed_terminal_reference_screen.v2"
    assert repaired["reference_model_evaluations"] == repaired["new_reference_model_evaluations"] == 0
    assert repaired["original_reference_model_evaluations"] == 303
    for a, b in zip(original["nodes"], repaired["nodes"]):
        assert b["density_lower_bound_kg_m3"] <= min(i["density_kg_m3"] for i in a["aerodynamic_bound_source_inputs"])
        assert b["density_upper_bound_kg_m3"] >= max(i["density_kg_m3"] for i in a["aerodynamic_bound_source_inputs"])
        assert b["radius_lower_bound_m"] <= min(env.norm(i["r_eci_m"]) for i in a["aerodynamic_bound_source_inputs"])
        assert all(x <= y for x, y in zip(b["required_main_force_box_lower_body_n"], a["required_main_force_box_lower_body_n"]))
        assert all(x >= y for x, y in zip(b["required_main_force_box_upper_body_n"], a["required_main_force_box_upper_body_n"]))
