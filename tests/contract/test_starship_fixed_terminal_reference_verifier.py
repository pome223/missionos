"""Independent public reference contracts; no integration or future trace."""

from copy import deepcopy
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_fixed_terminal_reference as producer
from src.runtime import starship_fixed_terminal_reference_verifier as check

PUBLIC = runpy.run_path(str(Path(__file__).with_name("test_starship_fixed_terminal_reference.py")))


def fixture():
    profile, catch, _, _, snapshot, prior, command = PUBLIC["fixture"]()
    plan = producer.build_terminal_reference(
        snapshot, profile, catch, prior_state=prior, previous_command=command
    )
    return profile, catch, snapshot, plan


def verdict(data):
    profile, catch, snapshot, plan = data
    return check.verify_fixed_terminal_reference(plan, snapshot, profile, catch)


def test_quintic_pose_pin_and_model_rhs_algebra_is_consistent_without_feasibility_claim():
    data = fixture()
    before = deepcopy(data)
    result = verdict(data)
    assert result["passed"], result
    assert result["arithmetic_passed"] is True
    assert result["source_bound_arithmetic_passed"] is False
    assert result["physical_invocation_admitted"] is False
    assert result["pose_request_caps_satisfied"]
    assert all(
        result[key] is False
        for key in (
            "past_source_anchors_bound",
            "aerodynamic_rhs_independently_replayed",
            "joint_reference_feasibility_established",
            "dynamics_replayed",
            "source_authenticated",
            "arrival_admitted",
            "support_admitted",
            "physical_execution",
        )
    )
    assert data == before


def test_source_bound_check_rejects_self_consistent_unbound_origin_fixture():
    profile, catch, snapshot, plan = fixture()
    result = check.verify_source_bound_terminal_reference(plan, snapshot, profile, catch)
    assert result["passed"] is False
    assert result["arithmetic_passed"] is True
    assert result["past_source_anchors_bound"] is False
    assert result["source_bound_arithmetic_passed"] is False
    assert result["physical_invocation_admitted"] is False
    assert "prior checkpoint" in result["issues"][0]


@pytest.mark.parametrize(
    "change",
    [
        "past_clock",
        "command_hash",
        "profile_hash",
        "coefficient",
        "endpoint",
        "duration",
        "engine_force",
        "engine_torque",
        "throttle_derivative",
        "fuel_rate",
        "com_acceleration",
        "gyro_acceleration",
        "material_acceleration",
        "gravity",
        "inertia",
        "history_reset",
        "pose_anchor",
        "rotation_vector",
        "pose_peak",
        "raw_continuity",
        "source_rhs_claim",
        "admission",
        "extra",
    ],
)
def test_changed_source_kinematics_boundary_coefficients_or_pose_claim_is_rejected(change):
    data = list(fixture())
    profile, catch, snapshot, plan = data
    kin = plan["initial_pin_kinematics"]
    loads = kin["source_model_load_inputs"]
    if change == "past_clock":
        plan["prior_state"]["time_s"] = snapshot["state"]["time_s"]
    elif change == "command_hash":
        plan["previous_command_sha256"] = "f" * 64
    elif change == "profile_hash":
        plan["profile_sha256"] = "f" * 64
    elif change == "coefficient":
        plan["position_coefficients"][0][3] += 0.001
    elif change == "endpoint":
        plan["target_position_enu_m"][2] += 0.1
    elif change == "duration":
        plan["duration_s"] += 0.5
    elif change == "engine_force":
        loads["engine_loads"][0]["force_body_n"][2] += 10.0
    elif change == "engine_torque":
        loads["engine_loads"][0]["torque_body_nm"][1] += 10.0
    elif change == "throttle_derivative":
        loads["engine_throttle_derivatives_per_s"][0] = 0.1
    elif change == "fuel_rate":
        loads["fuel_rate_kg_s"] = -1.0
    elif change == "com_acceleration":
        kin["com_acceleration_body_mps2"][2] += 0.01
    elif change == "gyro_acceleration":
        kin["angular_acceleration_body_rad_s2"][1] += 0.01
    elif change == "material_acceleration":
        kin["acceleration_enu_mps2"][2] += 0.01
    elif change == "gravity":
        loads["gravity_acceleration_eci_mps2"][0] += 0.01
    elif change == "inertia":
        loads["inertia_kg_m2"][0][0] += 1000.0
    elif change == "history_reset":
        plan["reference_tracker_history_reset"] = True
    elif change == "pose_anchor":
        plan["pose_start_q_body_to_eci"] = snapshot["state"]["q_body_to_eci"][:]
        plan["pose_start_q_body_to_eci"][0] += 0.001
    elif change == "rotation_vector":
        plan["pose_relative_rotation_vector_eci_rad"][0] += 0.001
    elif change == "pose_peak":
        plan["pose_peak_rate_rad_s"] *= 2
    elif change == "raw_continuity":
        plan["raw_pose_global_rate_continuity_established"] = True
    elif change == "source_rhs_claim":
        loads["aero_and_plant_rhs_independently_replayed"] = True
    elif change == "admission":
        plan["joint_reference_feasibility_established"] = True
    else:
        plan["new_future_observation"] = {}
    assert not verdict(data)["passed"]


@pytest.mark.parametrize("where", ["start", "middle", "end", "after"])
def test_same_clock_polynomial_jerk_and_pose_samples_are_checked(where):
    _, _, _, plan = fixture()
    t = (
        plan["reference_start_time_s"]
        + {
            "start": 0.0,
            "middle": plan["duration_s"] * 0.5,
            "end": plan["duration_s"],
            "after": plan["duration_s"] + 5.0,
        }[where]
    )
    sample = producer.evaluate_terminal_reference(plan, t)
    assert check.verify_reference_sample(sample, plan, t)["passed"]
    changed = deepcopy(sample)
    changed["jerk_enu_mps3"][0] += 0.001
    assert not check.verify_reference_sample(changed, plan, t)["passed"]
    changed = deepcopy(sample)
    changed["time_s"] += 0.1
    assert not check.verify_reference_sample(changed, plan, t)["passed"]


def test_reference_verifier_has_no_factory_model_producer_or_integrator_import():
    import ast

    imports = [
        node.module
        for node in ast.walk(ast.parse(Path(check.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        name
        not in (
            "starship_fixed_terminal_reference",
            "starship_sixdof",
            "starship_sixdof_mission",
            "starship_physics",
            "starship_actual_recovery_shooting",
        )
        for name in imports
    )
