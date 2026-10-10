"""Tampered completed flags cannot establish observed-goal fixture completion."""

from copy import deepcopy
import json

import pytest

from src.runtime.ship_urban_goal_contract import context_digest
from src.runtime.ship_urban_loop import digest
from src.runtime.ship_urban_loop_fixture import run_fixture
from src.runtime.ship_urban_loop_verifier import _clear_observed_segment, verify_urban_loop


@pytest.fixture(scope="module")
def completed_goal(tmp_path_factory):
    result = run_fixture(
        tmp_path_factory.mktemp("goal-verifier") / "run",
        approved=True, goal_responsive=True, obstacle_side="left",
    )
    assert result["status"] == "completed", result["failure"]
    assert verify_urban_loop(result)["goal_completion_verified"], verify_urban_loop(result)
    return result


def clone(result):
    # Reopen the persisted JSON shape, not shared in-memory dict references.
    return json.loads(json.dumps(result))


def events(result, name):
    return [e for e in result["events"] if e["event"] == name]


def edit_observations(result, change):
    """Edit every saved occurrence of a row and return old/new content bindings."""
    hashes = {}

    def visit(value):
        if isinstance(value, dict):
            if {"sequence", "position_ned_m", "goal_context"} <= set(value):
                old = digest(value)
                change(value)
                hashes[old] = digest(value)
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(result)
    return hashes


def reseal(result, observation_hashes=None):
    """Repair provenance hashes so tests reach semantic checks, not stale hashes.

    These are unkeyed content bindings, not signatures or a tamper-proof log.
    Do not recompute the claimed Rules/forecast clearance flags.
    """
    observation_hashes = observation_hashes or {}
    plan = result["plan"]
    plan_hash = result["plan_sha256"] = digest(plan)
    observed = {digest(e["observation"]): e["observation"] for e in events(result, "observation")}
    for e in result["events"]:
        e["plan_sha256"] = plan_hash
    cycles = {e["cycle"] for e in events(result, "model_requested")}
    for cycle in cycles:
        requested = [e for e in events(result, "model_requested") if e["cycle"] == cycle]
        received = [e for e in events(result, "model_received") if e["cycle"] == cycle]
        if len(requested) != 2 or len(received) != 2:
            continue
        for index, (sent, got) in enumerate(zip(requested, received)):
            request, response = sent["request"], got["response"]
            request["plan_sha256"] = plan_hash
            request["approved_goal"] = {
                "position_ned_m": list(plan["goal_ned_m"]),
                "tolerance_m": plan["target_error_m"],
                "max_leg_m": plan["max_leg_m"], "clearance_m": plan["clearance_m"],
            }
            if index:
                vla = received[0]["response"]
                request["proposal"] = deepcopy(vla)
                response["proposal_sha256"] = digest(vla)
                response["observation_context_sha256"] = context_digest(request["observation"]["goal_context"])
                candidates = {c["id"]: c for c in vla["candidates"]}
                for forecast in response.get("forecasts", []):
                    if forecast["candidate_id"] in candidates:
                        forecast["candidate_sha256"] = digest(candidates[forecast["candidate_id"]])
            sent["request_sha256"] = response["request_sha256"] = digest(request)
            got["response_sha256"] = digest(response)
        for authorized in events(result, "segment_authorized"):
            if authorized["cycle"] != cycle:
                continue
            permit = authorized["permit"]
            permit["plan_sha256"] = plan_hash
            permit["vla_response_sha256"] = digest(received[0]["response"])
            permit["wam_response_sha256"] = digest(received[1]["response"])
            permit["observation_sha256"] = observation_hashes.get(
                permit["observation_sha256"], permit["observation_sha256"])
            permit["rules"]["observation_sha256"] = permit["observation_sha256"]
            row = observed[permit["observation_sha256"]]
            permit["rules"]["observation_context_sha256"] = context_digest(row["goal_context"])
            for e in result["events"]:
                if e["cycle"] == cycle and e["event"] in {"segment_dispatched", "segment_arrived"}:
                    e["permit_sha256"] = digest(permit)
                    if e["event"] == "segment_dispatched":
                        e["receipt"]["permit_sha256"] = digest(permit)


def rejected(result, expected=None):
    verification = verify_urban_loop(result)
    assert not verification["control_sequence_verified"], verification
    assert verification["goal_completion_verified"] is False
    if expected:
        assert any(expected in reason for reason in verification["reasons"]), verification


def test_public_goal_receipt_reopens_without_native_claims(completed_goal):
    verdict = verify_urban_loop(clone(completed_goal))
    assert verdict["control_sequence_verified"] and verdict["goal_completion_verified"]
    assert verdict["observed_geometry_verified"]
    assert not any(verdict[k] for k in (
        "native_model_use_verified", "whole_mission_completion_verified",
        "physical_execution_verified", "energy_savings_verified",
    ))


def test_independent_slabs_cover_tangent_parallel_and_zero_length():
    context = {"coverage": {"lower_ned_m": [-1, -2, -1], "upper_ned_m": [5, 2, 1]},
               "obstacles": [{"center_ned_m": [2, 0, 0], "half_size_m": [0.5, 0.5, 0.5]}]}
    assert not _clear_observed_segment([0, 0.75, 0], [4, 0.75, 0], context, 0.25)
    assert _clear_observed_segment([0, 0.76, 0], [4, 0.76, 0], context, 0.25)
    assert not _clear_observed_segment([2, 0, 0], [2, 0, 0], context, 0.25)
    assert _clear_observed_segment([0, 0, 0], [0, 0, 0], context, 0.25)
    assert not _clear_observed_segment([-0.8, 0, 0], [-0.5, 0, 0], context, 0.25)


def test_valid_rebinding_alone_preserves_the_receipt(completed_goal):
    result = clone(completed_goal)
    reseal(result)
    assert verify_urban_loop(result)["goal_completion_verified"]


def test_geometry_is_recomputed_even_when_rules_and_forecast_claim_clear(completed_goal):
    result = clone(completed_goal)
    box = {"id": "concealed-obstruction", "center_ned_m": [1017, 0, -30],
           "half_size_m": [0.2, 0.2, 0.5]}
    hashes = edit_observations(result, lambda row: row["goal_context"]["obstacles"].append(deepcopy(box)))
    reseal(result, hashes)
    assert events(result, "segment_authorized")[0]["permit"]["rules"]["allowed"] is True
    rejected(result, "goal_selected_leg_intersects_observed_obstacle")


@pytest.mark.parametrize("change", ["missing", "candidate", "stale", "unclear"])
def test_forecast_binding_and_presence_are_required(completed_goal, change):
    result = clone(completed_goal)
    wam = events(result, "model_received")[1]["response"]
    if change == "missing":
        wam.pop("forecasts")
    elif change == "candidate":
        wam["forecasts"][0]["candidate_id"] = "unproposed"
    elif change == "unclear":
        for f in wam["forecasts"]:
            if f["candidate_id"] == wam["selected_candidate_id"]:
                f["predicted_clear"] = False
    reseal(result)
    if change == "stale":
        wam["observation_context_sha256"] = "0" * 64
        events(result, "model_received")[1]["response_sha256"] = digest(wam)
    rejected(result)


@pytest.mark.parametrize("where", ["request", "observation", "context"])
def test_future_or_scenario_information_cannot_enter_the_request(completed_goal, where):
    result = clone(completed_goal)
    if where == "request":
        events(result, "model_requested")[0]["request"]["future_obstacle_position"] = [1021, 0, -30]
        hashes = {}
    else:
        def insert(row):
            destination = row if where == "observation" else row["goal_context"]
            destination["future_obstacle_position"] = [1021, 0, -30]
        hashes = edit_observations(result, insert)
    # The helper may itself reject the changed context schema during resealing;
    # hashes are still repaired for the observation/request-only cases.
    if where != "context":
        reseal(result, hashes)
    rejected(result)


def test_expired_image_fails_after_rebinding(completed_goal):
    result = clone(completed_goal)
    sequence = events(result, "model_requested")[0]["request"]["observation"]["sequence"]

    def stale(row):
        if row["sequence"] == sequence:
            row["image_observed_at_s"] -= result["plan"]["observation_age_s"] + 1

    hashes = edit_observations(result, stale)
    reseal(result, hashes)
    rejected(result)


def test_new_geometry_after_forecast_invalidates_dispatch(completed_goal):
    result = clone(completed_goal)
    dispatch = events(result, "segment_dispatched")[0]
    row = [e["observation"] for e in events(result, "observation") if e["at_s"] <= dispatch["at_s"]][-1]
    sequence = row["sequence"]

    def changed(observation):
        if observation["sequence"] == sequence:
            observation["goal_context"]["obstacles"].append({
                "id": "new-context", "center_ned_m": [1100, 10, -30], "half_size_m": [1, 1, 0.5],
            })

    hashes = edit_observations(result, changed)
    reseal(result, hashes)
    rejected(result, "goal_context_changed_before_dispatch")


def test_even_nonblocking_geometry_change_during_motion_terminates_the_receipt(completed_goal):
    result = clone(completed_goal)
    dispatch = events(result, "segment_dispatched")[0]
    arrival = events(result, "segment_arrived")[0]
    sequence = next(e["observation"]["sequence"] for e in events(result, "observation")
                    if dispatch["at_s"] < e["at_s"] < arrival["at_s"])

    def changed(row):
        if row["sequence"] == sequence:
            row["goal_context"]["obstacles"].append({
                "id": "distant-change", "center_ned_m": [1100, 10, -30],
                "half_size_m": [1, 1, 0.5],
            })

    reseal(result, edit_observations(result, changed))
    rejected(result, "goal_context_changed_after_dispatch")


@pytest.mark.parametrize("model_index", [0, 1])
def test_transient_geometry_change_during_inference_cannot_be_hidden(completed_goal, model_index):
    result = clone(completed_goal)
    sent = events(result, "model_requested")[model_index]
    got = events(result, "model_received")[model_index]
    sequence = next(e["observation"]["sequence"] for e in events(result, "observation")
                    if sent["at_s"] < e["at_s"] < got["at_s"])

    def changed(row):
        if row["sequence"] == sequence:
            row["goal_context"]["obstacles"].append({
                "id": "transient-change", "center_ned_m": [1100, 10, -30],
                "half_size_m": [1, 1, 0.5],
            })

    reseal(result, edit_observations(result, changed))
    rejected(result, "goal_context_changed_during_inference")


@pytest.mark.parametrize("change", ["missing", "moving", "away"])
def test_exit_hold_is_required_at_the_goal(completed_goal, change):
    result = clone(completed_goal)
    exit_hold = events(result, "ap_exit_hold_observed")[0]
    if change == "missing":
        result["events"].remove(exit_hold)
    else:
        sequence = exit_hold["observation"]["sequence"]

        def changed(row):
            if row["sequence"] == sequence:
                if change == "moving":
                    row["velocity_ned_mps"][0] = result["plan"]["hold_speed_mps"] + 0.1
                else:
                    row["position_ned_m"][0] -= 1

        reseal(result, edit_observations(result, changed))
    rejected(result)


@pytest.mark.parametrize("field,value", [("constraint_source", "model-asserted"),
                                          ("blocking_obstacle_ids", ["ignored-obstacle"])])
def test_rules_geometry_evidence_contract_is_required(completed_goal, field, value):
    result = clone(completed_goal)
    events(result, "segment_authorized")[0]["permit"]["rules"][field] = value
    reseal(result)
    rejected(result, "goal_candidate_authority_execution_binding")


def test_no_further_model_calls_are_accepted_after_a_stable_goal_arrival(completed_goal):
    result = clone(completed_goal)
    result["plan"]["goal_ned_m"] = deepcopy(events(result, "segment_arrived")[0]["observation"]["position_ned_m"])
    reseal(result)
    rejected(result, "goal_action_after_goal_already_reached")


def test_completed_flag_cannot_replace_the_approved_goal(completed_goal):
    result = clone(completed_goal)
    result["plan"]["goal_ned_m"][0] += 1
    reseal(result)
    assert result["goal_reached"] and result["status"] == "completed"
    rejected(result, "goal_completion_not_observed")


def test_completed_flag_cannot_expand_the_action_budget(completed_goal):
    result = clone(completed_goal)
    result["plan"]["updates"] = 2
    reseal(result)
    rejected(result, "goal_action_budget_or_cycle")


def test_replayed_trace_cannot_supply_another_arrival(completed_goal):
    result = clone(completed_goal)
    arrival = events(result, "segment_arrived")[0]
    index = result["events"].index(arrival)
    result["events"].insert(index + 1, deepcopy(arrival))
    rejected(result, "goal_finite_counts")


def test_tampered_candidate_cannot_be_hidden_by_unchanged_permit(completed_goal):
    result = clone(completed_goal)
    selected = events(result, "model_received")[1]["response"]["selected_candidate_id"]
    for candidate in events(result, "model_received")[0]["response"]["candidates"]:
        if candidate["id"] == selected:
            candidate["target_ned_m"][1] += 0.5
    reseal(result)
    rejected(result, "goal_candidate_authority_execution_binding")
