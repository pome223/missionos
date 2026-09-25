"""Information boundaries and stopping arithmetic for the CPU development gate."""

import copy
import math

import numpy as np
import pytest

from scripts.urban_headroom_contract import (
    CASES,
    PROTOCOL,
    case_scene,
    planner_geometry,
)
from scripts.urban_navigation_contract import route_check, digest
from scripts.select_urban_depth_route import rank_routes, INPUT_KEYS
from scripts.px4_urban_headroom_trial import validate_selection


@pytest.mark.parametrize("case", CASES)
def test_frozen_cohort_keeps_executable_control_and_omits_truth(case):
    scene = case_scene(case)
    assert route_check(scene, scene["routes"][scene["feasibility_route"]])["admissible"]
    p = planner_geometry(scene)
    p["observation_enu_m"] = [0, 0, 3]
    assert set(p) == INPUT_KEYS
    assert "scene_sha256" not in p and "family" not in p
    assert len(p["map_boxes"]) == len(scene["buildings"]) - len(
        scene["omitted_map_entities"]
    )


def test_depth_blocks_direct_route_before_truth_is_consulted():
    p = planner_geometry(case_scene("headroom_detour_0"))
    p["observation_enu_m"] = [0, 0, 3]
    assert rank_routes(p, [])["route_id"] == "forward"
    result = rank_routes(p, np.array([[3, 0, 3], [3, 0.5, 3], [3, -0.5, 3]]))
    assert result["scores"]["forward"]["rejected_by_observed_surface"]
    assert result["route_id"] in ("left_detour", "right_detour")


def test_truth_mask_or_hidden_geometry_cannot_enter_planner_input():
    p = planner_geometry(case_scene(CASES[0]))
    p["observation_enu_m"] = [0, 0, 3]
    for key in ("scene_sha256", "admissible_routes", "truth_boxes"):
        with pytest.raises(ValueError, match="whitelist"):
            rank_routes({**p, key: []}, [])


def test_unknown_or_invalid_depth_is_not_a_safe_route_certificate():
    p = planner_geometry(case_scene(CASES[0]))
    p["observation_enu_m"] = [0, 0, 3]
    assert PROTOCOL["unknown_space_is_not_certified_free"] is True
    with pytest.raises(ValueError, match="nonfinite"):
        rank_routes(p, [[math.nan, 0, 0]])


def selection_fixture():
    config = {"session_id": "fixture", "scene_sha256": "scene"}
    command = {"route_id": "climb", "route_sha256": "route"}
    selection = {
        **config,
        **command,
        "schema_version": "urban_depth_route_selection.v1",
        "selector": "history_depth",
        "model_invoked": False,
        "model_forecast_used_for_dispatch": False,
        "uses_scene_truth_for_selection": False,
        "protocol_sha256": digest(PROTOCOL),
        "observation_unix_s": 100,
        "observation_enu_m": [0, 0, 3],
        "observation_yaw_ned_rad": math.pi / 2,
    }
    current = {
        "gazebo_pose_enu_m": [0, 0, 3],
        "yaw_ned_rad": math.pi / 2,
        "local_ned_velocity_mps": [0, 0, 0],
        "telemetry_age_seconds": 0.1,
    }
    return selection, command, config, current


def test_depth_dispatch_rejects_stale_inputs_and_model_claims():
    args = selection_fixture()
    assert validate_selection(*args, 101) == 1
    with pytest.raises(ValueError, match="expired"):
        validate_selection(*args, 161)
    for key in (
        "model_invoked",
        "model_forecast_used_for_dispatch",
        "uses_scene_truth_for_selection",
    ):
        mutated = copy.deepcopy(args)
        mutated[0][key] = True
        with pytest.raises(ValueError, match="contract"):
            validate_selection(*mutated, 101)


def test_source_drift_does_not_become_a_valid_same_start_comparison():
    args = selection_fixture()
    args[3]["gazebo_pose_enu_m"] = [0.2, 0, 3]
    with pytest.raises(ValueError, match="initial-state"):
        validate_selection(*args, 101)


def test_futility_bound_cannot_open_gpu_gate_or_claim_candidate_matrix():
    from scripts.verify_urban_headroom import gate_result

    ten = gate_result(10, 10)
    assert ten["oracle_additional_arrivals_upper_bound"] == 2
    assert ten["numerical_headroom_gate_impossible"] is True
    assert ten["cohort_completed"] is False
    assert ten["gpu_gate_passed"] is False
    assert ten["complete_candidate_outcome_matrix_collected"] is False
    nine = gate_result(9, 12)
    assert nine["numerical_headroom_gate_impossible"] is False
    assert nine["gpu_gate_passed"] is False
    assert nine["decision"] == "requires_candidate_matrix_and_scorer_no_gpu_yet"


def test_incomplete_measurement_is_distinct_from_model_failure():
    from scripts.verify_urban_headroom import gate_result

    result = gate_result(1, 2, infrastructure_failures=1)
    assert result["decision"] == "incomplete_stop"
    assert result["new_model_calls"] == 0
    assert result["learned_navigation_benefit_established"] is False


@pytest.mark.parametrize("successes,attempted", [(13, 12), (4, 3), (-1, 1), (True, 1)])
def test_denominator_cannot_be_inflated(successes, attempted):
    from scripts.verify_urban_headroom import gate_result

    with pytest.raises(ValueError, match="denominator"):
        gate_result(successes, attempted)
