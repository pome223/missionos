"""Observed obstacle changes must alter bounded fixture goals without granting authority."""

import math

import pytest

from src.runtime.ship_urban_goal_contract import context_digest
from src.runtime.ship_urban_loop import digest
from src.runtime.ship_urban_loop_fixture import run_fixture
from src.runtime.ship_urban_loop_verifier import verify_urban_loop


ENTRY = [1015.0, 0.0, -30.0]
GOAL = [1027.0, 0.0, -30.0]
SIDES = ("left", "right", "none")


@pytest.fixture(scope="module")
def goal_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("urban-observed-goal")
    return {
        side: run_fixture(
            root / side,
            approved=True,
            goal_responsive=True,
            obstacle_side=side,
        )
        for side in SIDES
    }


def events(result, name):
    return [event for event in result["events"] if event["event"] == name]


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


@pytest.mark.parametrize("side", SIDES)
def test_goal_fixture_reaches_approved_goal_in_three_bounded_legs(goal_runs, side):
    result = goal_runs[side]
    assert result["status"] == "completed", result["failure"]
    assert result["completed_updates"] == 3
    assert result["goal_reached"] is True
    assert verify_urban_loop(result)["control_sequence_verified"]
    permits = [event["permit"] for event in events(result, "segment_authorized")]
    assert len(permits) == 3
    start = ENTRY
    for permit in permits:
        target = permit["target_ned_m"]
        assert permit["start_ned_m"] == start
        assert 0.5 <= math.dist(start, target) <= 5.0
        assert math.dist(target, GOAL) < math.dist(start, GOAL)
        start = target
    assert start == GOAL
    arrivals = events(result, "segment_arrived")
    assert len(arrivals) == 3
    assert math.dist(arrivals[-1]["observation"]["position_ned_m"], GOAL) <= 0.25
    goal_arrivals = events(result, "goal_arrival_observed")
    assert len(goal_arrivals) == 1
    assert goal_arrivals[0]["observation"] == arrivals[-1]["observation"]
    assert goal_arrivals[0]["observation"]["position_ned_m"] == GOAL
    final_hold = events(result, "ap_exit_hold_observed")
    assert len(final_hold) == 1
    assert final_hold[0]["observation"]["ap_mode"] == "hold"
    assert math.dist(final_hold[0]["observation"]["position_ned_m"], GOAL) <= 0.25


def test_mirrored_obstacle_changes_second_target_after_common_first_leg(goal_runs):
    targets = {
        side: [event["permit"]["target_ned_m"] for event in events(result, "segment_authorized")]
        for side, result in goal_runs.items()
    }
    assert all(rows[0] == [1019.0, 0.0, -30.0] for rows in targets.values())
    left, right = targets["left"][1], targets["right"][1]
    assert left[0] == right[0] == 1023.0
    assert left[2] == right[2] == -30.0
    assert abs(left[1]) == abs(right[1]) == 1.0
    assert left[1] == -right[1]
    assert all(rows[-1] == GOAL for rows in targets.values())


@pytest.mark.parametrize("side", SIDES)
def test_goal_fixture_has_six_bound_model_requests_without_future_case_labels(goal_runs, side):
    result = goal_runs[side]
    requests = [event["request"] for event in events(result, "model_requested")]
    responses = [event["response"] for event in events(result, "model_received")]
    assert [(request["cycle"], request["model"]) for request in requests] == [
        (cycle, model) for cycle in (1, 2, 3) for model in ("vla", "wam")
    ]
    assert len(responses) == 6
    assert len({request["request_id"] for request in requests}) == 6
    forbidden = {
        "obstacle_side",
        "scenario",
        "scenario_id",
        "scenario_name",
        "case",
        "case_id",
        "case_name",
        "future_schedule",
        "future_observations",
        "expected_choice",
        "expected_candidate_id",
    }
    for request in requests:
        assert not forbidden.intersection(nested_keys(request))
        assert request["approved_goal"] == {
            "position_ned_m": GOAL,
            "tolerance_m": result["plan"]["target_error_m"],
            "max_leg_m": 5.0,
            "clearance_m": 0.25,
        }
        observation = request["observation"]
        context = observation["goal_context"]
        assert context["schema_version"] == "ship_urban_observed_geometry.v1"
        assert context["observed_at_s"] == observation["observed_at_s"]
        for boundary in ("lower_ned_m", "upper_ned_m"):
            assert list(context["coverage"][boundary]) == list(result["plan"][boundary])
        obstacles = context["obstacles"]
        if request["cycle"] == 1 or side == "none":
            assert obstacles == []
        else:
            assert len(obstacles) == 1
            obstacle = obstacles[0]
            assert obstacle["id"] == "observed-obstacle"
            assert obstacle["center_ned_m"][0] == 1021.0
            assert abs(obstacle["center_ned_m"][1]) == 0.25
            assert obstacle["center_ned_m"][2] == -30.0
            assert obstacle["half_size_m"] == [0.15, 0.30, 0.5]
            first_arrival = events(result, "segment_arrived")[0]
            assert context["observed_at_s"] > first_arrival["observation"]["observed_at_s"]
    for response in responses:
        assert response["dispatch_allowed"] is False
        assert response["fixture"] is True


@pytest.mark.parametrize("side", SIDES)
def test_goal_forecasts_and_rules_bind_current_geometry_and_original_candidates(goal_runs, side):
    result = goal_runs[side]
    observations = {
        digest(event["observation"]): event["observation"]
        for event in events(result, "observation")
    }
    for cycle in (1, 2, 3):
        requests = {
            event["request"]["model"]: event["request"]
            for event in events(result, "model_requested")
            if event["cycle"] == cycle
        }
        responses = {
            event["response"]["model"]: event["response"]
            for event in events(result, "model_received")
            if event["cycle"] == cycle
        }
        vla, wam = responses["vla"], responses["wam"]
        assert requests["wam"]["proposal"] == vla
        assert wam["proposal_sha256"] == digest(vla)
        assert wam["forecast_schema"] == "ship_urban_goal_forecast.fixture.v1"
        assert wam["observation_context_sha256"] == context_digest(
            requests["wam"]["observation"]["goal_context"]
        )
        candidates = {candidate["id"]: candidate for candidate in vla["candidates"]}
        forecasts = wam["forecasts"]
        assert len(forecasts) == len(candidates)
        assert {forecast["candidate_id"] for forecast in forecasts} == set(candidates)
        for forecast in forecasts:
            assert forecast["candidate_sha256"] == digest(candidates[forecast["candidate_id"]])
            assert type(forecast["predicted_clear"]) is bool
        selected = [
            forecast
            for forecast in forecasts
            if forecast["candidate_id"] == wam["selected_candidate_id"]
        ]
        assert len(selected) == 1
        assert selected[0]["predicted_clear"] is True
        permit = next(
            event["permit"]
            for event in events(result, "segment_authorized")
            if event["cycle"] == cycle
        )
        context = observations[permit["observation_sha256"]]["goal_context"]
        assert permit["rules"]["observation_context_sha256"] == context_digest(context)
        assert permit["target_ned_m"] == candidates[wam["selected_candidate_id"]]["target_ned_m"]
        if cycle == 2 and side != "none":
            assert permit["target_ned_m"][1] * context["obstacles"][0]["center_ned_m"][1] < 0


@pytest.mark.parametrize("side", SIDES)
def test_goal_fixture_preserves_native_execution_and_completion_claim_boundaries(goal_runs, side):
    result = goal_runs[side]
    assert result["owned_processes_reaped"] is True
    assert result["model_shutdown_verified"] is True
    assert result["late_response_rejected"] is True
    assert result["fixture_ap_return_handoff"] is True
    assert result["execution_backend"] == "fixture_http_processes"
    for field in (
        "vla_invoked",
        "wam_invoked",
        "px4_runtime_invoked",
        "gazebo_runtime_invoked",
        "native_model_adapter_connected",
        "physical_execution_invoked",
        "whole_mission_completion_verified",
        "onboard_energy_savings_verified",
    ):
        assert result[field] is False
