"""Public sampled-bound evidence; model inputs are fixtures, no integration."""

from copy import deepcopy
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_fixed_terminal_reference as reference
from src.runtime import starship_terminal_reference_screen as producer
from src.runtime import starship_terminal_reference_screen_verifier as check

PUBLIC = runpy.run_path(str(Path(__file__).with_name("test_starship_fixed_terminal_reference.py")))


@pytest.fixture(scope="module")
def evidence():
    profile, catch, _, _, snapshot, prior, command = PUBLIC["fixture"]()
    plan = reference.build_terminal_reference(
        snapshot, profile, catch, prior_state=prior, previous_command=command
    )
    original = producer.screen_terminal_reference(plan, snapshot, profile, catch)
    corrected = producer.repair_terminal_reference_screen(original, plan, snapshot, profile, catch)
    return corrected, original, plan, snapshot, profile, catch


def verdict(data):
    return check.verify_terminal_reference_screen(*data)


def test_independent_corrected_sampled_bounds_preserve_original_and_no_global_claim(evidence):
    before = deepcopy(evidence)
    result = verdict(evidence)
    assert result["passed"], result
    assert result["arithmetic_passed"] is True
    assert result["source_bound_arithmetic_passed"] is False
    assert result["candidate_plant_call_allowed"] is False
    assert result["physical_invocation_admitted"] is False
    assert result["nominal_force_and_fuel_independently_replayed"] is False
    assert result["nominal_rate_derivatives_independently_replayed"] is False
    assert result["physical_plant_integrations_performed"] == 0
    assert all(
        result[key] is False
        for key in (
            "continuous_fuel_bound_certified",
            "joint_reference_feasibility_established",
            "vehicle_global_infeasibility_established",
            "tower_interference_validated",
            "source_authenticated",
            "dynamics_replayed",
            "arrival_admitted",
            "support_admitted",
            "physical_execution",
        )
    )
    assert evidence == before


@pytest.mark.parametrize(
    "change",
    [
        "original_hash",
        "profile",
        "sample",
        "model_counter",
        "integration",
        "density_width",
        "global_speed",
        "radius_lower",
        "gravity_bound",
        "fin_bound",
        "rcs_bound",
        "com_bound",
        "endpoint_force",
        "plate_force",
        "startup",
        "gimbal",
        "roundoff",
        "known_codes",
        "allowed",
        "continuous_fuel",
        "throttle_history",
        "tower",
        "extra",
    ],
)
def test_repaired_force_box_cannot_hide_changed_inputs_bounds_or_claims(evidence, change):
    data = list(deepcopy(evidence))
    screen, original, plan, snapshot, profile, catch = data
    node = screen["nodes"][40]
    if change == "original_hash":
        screen["original_screen_sha256"] = "f" * 64
    elif change == "profile":
        profile["actuators"]["max_gimbal_deg"] += 2.0
    elif change == "sample":
        node["reference_sample"]["position_enu_m"][0] += 0.01
    elif change == "model_counter":
        screen["new_reference_model_evaluations"] = 1
    elif change == "integration":
        screen["physical_plant_integrations"] = 1
    elif change == "density_width":
        node["density_lower_bound_kg_m3"] = node["density_upper_bound_kg_m3"]
    elif change == "global_speed":
        node["panel_speed_upper_bounds_mps"][0] += 0.01
    elif change == "radius_lower":
        node["radius_lower_bound_m"] += 1.0
    elif change == "gravity_bound":
        node["gravity_acceleration_norm_upper_bound_mps2"] += 0.01
    elif change == "fin_bound":
        node["movable_fin_force_norm_bound_n"] += 10.0
    elif change == "rcs_bound":
        node["rcs_force_component_bound_body_n"][0] += 10.0
    elif change == "com_bound":
        screen["com_velocity_bound_mps"] += 0.01
    elif change == "endpoint_force":
        node["required_force_body_endpoints_n"][0][0] += 10.0
    elif change == "plate_force":
        node["aerodynamic_bound_source_inputs"][0]["panel_loads"][0]["force_body_n"][0] += 10.0
    elif change == "startup":
        node["main_startup_capacity_n"] += 10.0
    elif change == "gimbal":
        node["gimbal_cone_x_over_z"] += 0.01
    elif change == "roundoff":
        node["force_roundoff_n"] = 1e6
    elif change == "known_codes":
        node["violations"] = ["vehicle_impossible"]
    elif change == "allowed":
        screen["candidate_plant_call_allowed"] = not screen["candidate_plant_call_allowed"]
    elif change == "continuous_fuel":
        screen["sampled_lower_fuel_integral_continuously_certified"] = True
    elif change == "throttle_history":
        screen["discrete_minimum_throttle_history_feasibility_established"] = True
    elif change == "tower":
        screen["tower_interference_validated"] = True
    else:
        screen["all_future_families_failed"] = True
    assert not verdict(data)["passed"]


def test_v1_producer_rejection_is_not_accepted_as_repaired_independent_proof(evidence):
    corrected, original, plan, snapshot, profile, catch = evidence
    assert not check.verify_terminal_reference_screen(
        original, original, plan, snapshot, profile, catch
    )["passed"]


def test_no_integrator_model_observer_or_screen_producer_import():
    import ast

    imports = [
        node.module
        for node in ast.walk(ast.parse(Path(check.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        name
        not in (
            "starship_terminal_reference_screen",
            "starship_fixed_terminal_reference",
            "starship_sixdof",
            "starship_physics",
            "starship_sixdof_mission",
        )
        for name in imports
    )
