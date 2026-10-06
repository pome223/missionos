"""Public frozen-load certificates; no integration, native or model calls."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_terminal_wrench_verifier as check

PUBLIC = runpy.run_path(str(Path(__file__).with_name("test_starship_terminal_wrench.py")))


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


def fixture(*, hot=0.5, floor=None, failed=False):
    body, state, base, observed, force = PUBLIC["fixture"](hot)
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    if failed:
        engines = list(state.engine_states)
        engines[3] = replace(engines[3], available=False)
        state = replace(state, engine_states=tuple(engines))
    selected, receipt = PUBLIC["allocate"](
        body, state, base, observed, (0.0, 0.0, force), force if floor is None else floor
    )
    return (
        profile,
        saved(asdict(state)),
        saved(asdict(base)),
        saved(asdict(selected)),
        saved(receipt),
    )


def verdict(data):
    profile, state, base, selected, receipt = data
    return check.verify_terminal_wrench(receipt, state, base, selected, profile)


@pytest.mark.parametrize("case", ["accepted", "hot_off", "deferred", "unavailable"])
def test_independent_finite_geometry_affine_domains_and_exact_off_tail(case):
    data = fixture(
        hot=1.0 if case == "hot_off" else 0.5,
        floor=1e9 if case == "deferred" else 0.0 if case == "unavailable" else None,
        failed=case == "unavailable",
    )
    before = deepcopy(data)
    result = verdict(data)
    assert result["passed"], result
    assert result["finite_affine_projection_selected"] is (data[-1]["status"] == "accepted")
    assert all(
        result[key] is False
        for key in (
            "global_allocation_optimality_established",
            "joint_trajectory_feasibility_established",
            "actual_force_independently_observed",
            "dynamics_replayed",
            "arrival_admitted",
            "support_admitted",
            "physical_execution",
        )
    )
    assert data == before
    if case == "hot_off":
        assert data[-1]["mask_candidates"][0]["force_a_eci_n"][2] != pytest.approx(
            2 * data[0]["booster"]["engine_thrust_n"] * __import__("math").exp(-0.1 / 0.35),
            rel=1e-4,
        )


@pytest.mark.parametrize(
    "change",
    [
        "com",
        "force",
        "torque",
        "endpoint",
        "available",
        "affine_domain",
        "vertical_halfspace",
        "moment_headroom",
        "roundoff",
        "interval",
        "false_feasible",
        "selection",
        "cost",
        "gradient",
        "gimbal",
        "jet",
        "flap",
        "claim",
        "extra",
    ],
)
def test_mutated_frozen_pose_response_constraints_and_selected_command_are_rejected(change):
    data = list(fixture())
    profile, state, base, selected, receipt = data
    entry = receipt["mask_candidates"][receipt["selected_mask_index"]]
    if change == "com":
        receipt["frozen_com_body_m"][2] += 0.1
    elif change == "force":
        receipt["finite_endpoint_geometry"][3]["force_per_throttle_body_n"][0] += 1.0
    elif change == "torque":
        receipt["finite_endpoint_geometry"][3]["torque_per_throttle_body_nm"][1] += 1.0
    elif change == "endpoint":
        receipt["finite_endpoint_geometry"][3]["base_endpoint_throttle"] += 0.01
    elif change == "available":
        state["engine_states"][3]["available"] = False
    elif change == "affine_domain":
        entry["selected_engine_affine_intervals"][0]["command_interval"][1] = 0.2
    elif change == "vertical_halfspace":
        entry["constraints"][0]["intercept"] += 100.0
    elif change == "moment_headroom":
        receipt["base_moment_error_abs_nm"][1] += 10.0
    elif change == "roundoff":
        receipt["moment_roundoff_bound_nm"][1] = 1e6
    elif change == "interval":
        receipt["interval_s"] = 0.3
    elif change == "false_feasible":
        entry["feasible"] = False
    elif change == "selection":
        receipt["selected_mask_index"] = 0
    elif change == "cost":
        entry["objective"] += 100.0
    elif change == "gradient":
        entry["half_objective_gradient"] += 100.0
    elif change == "gimbal":
        selected["engines"][3]["gimbal_x_rad"] += 0.001
    elif change == "jet":
        selected["engines"][33]["enabled"] = True
        selected["engines"][33]["throttle"] = 0.1
    elif change == "flap":
        selected["flap_angles_rad"][3] += 0.001
    elif change == "claim":
        receipt["joint_trajectory_feasibility_established"] = True
    else:
        receipt["real_hardware_safe"] = True
    assert not verdict(data)["passed"]


def test_new_off_mask_is_valid_without_loosening_old_landing_phase_contract():
    body, state, base, observed, force = PUBLIC["fixture"]()
    selected, receipt = PUBLIC["allocate"](
        body, state, base, observed, (force * 0.866, 0.0, force * 0.5), force * 0.5
    )
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    result = check.verify_terminal_wrench(
        saved(receipt), saved(asdict(state)), saved(asdict(base)), saved(asdict(selected)), profile
    )
    assert (
        result["passed"]
        and receipt["mask_candidates"][receipt["selected_mask_index"]]["mask"] == []
    )
    from src.runtime.starship_booster_recovery_verifier import _command, _Invalid

    with pytest.raises(_Invalid):
        _command(saved(asdict(selected)), "recovery_landing_13", saved(asdict(state)), profile)


def test_expected_request_binding_prevents_a_self_consistent_different_force():
    profile, state, base, selected, receipt = fixture()
    result = check.verify_terminal_wrench(
        receipt, state, base, selected, profile, desired_force_eci_n=[0.0, 0.0, 1.0]
    )
    assert not result["passed"]


def test_verifier_has_no_producer_factory_or_integrator_import():
    import ast

    imports = [
        node.module
        for node in ast.walk(ast.parse(Path(check.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        name
        not in (
            "starship_terminal_wrench",
            "starship_sixdof",
            "starship_sixdof_mission",
            "starship_fin_allocation",
        )
        for name in imports
    )
