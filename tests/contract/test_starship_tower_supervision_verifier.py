"""Static governance fixtures; no model/network, trajectory or diversion proof."""

from copy import deepcopy
import inspect
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_tower_supervision_verifier as checker

F = runpy.run_path(str(Path(__file__).with_name("test_starship_tower_supervision.py")))
KEY = F["KEY"]


@pytest.fixture(scope="module")
def state():
    return F["initial_state"].__wrapped__()


def verified(record, *, external=None, key=KEY):
    result = checker.verify_tower_supervision(
        record,
        expected_scope=deepcopy(record["approved_scope"]),
        expected_observations=external,
        resolution_signing_key=key,
    )
    assert all(
        result[name] is False
        for name in (
            "approval_independently_verified",
            "physical_effect_verified",
            "flight_outcome_improvement",
            "mission_completed",
            "physical_execution",
            "model_value_established",
            "ledger_completeness_verified",
            "authenticated_human_independently_verified",
            "ditch_trajectory_implemented",
        )
    )
    return result


def make(state, flow="bounded", *, apply=False):
    supervise, ledger = F["supervisor"]()
    obs = F["observation"]
    tick = F["tick"]
    proposal = F["proposal"]
    if flow == "unavailable":
        tick(supervise, obs(state, ready=False))
        tick(supervise, obs(state, 601.0, ready=False))
    else:
        request = tick(
            supervise, obs(state, ready=None if flow == "collection" else True)
        ).routing_request
        route = (
            "need_observation"
            if flow == "collection"
            else "human_review"
            if flow in ("human", "timeout", "pending")
            else "bounded"
        )
        event = tick(
            supervise,
            obs(state, 601.0, ready=None if flow == "collection" else True),
            routing_proposal=proposal(
                request, route=route, action="replan" if apply else "continue"
            ),
        )
        if flow == "collection":
            tick(supervise, obs(state, 602.0, ready=True))
            follow = tick(supervise, obs(state, 603.0, ready=True)).routing_request
            tick(supervise, obs(state, 604.0, ready=True), routing_proposal=proposal(follow))
            tick(supervise, obs(state, 605.0, ready=True))
        elif flow == "human":
            current = obs(state, 602.0)
            tick(supervise, current)
            decision = ledger.resolve(
                context=F["CONTEXT"],
                request_sha256=event.human_request["sha256"],
                observed_evidence_sha256=current["evidence_sha256"],
                latest_observation=F["tower"]._ledger_observation(current),
                action="divert",
                simulation_time_s=602.0,
                wall_time_s=1002.0,
                run_active=True,
            )
            tick(supervise, obs(state, 602.1), human_decision=decision)
            tick(supervise, obs(state, 602.2))
        elif flow == "timeout":
            tick(supervise, obs(state, 675.0))
            tick(supervise, obs(state, 676.0))
        elif flow != "pending":
            tick(supervise, obs(state, 602.0))
    if apply:
        directive = supervise.receipt()["directive"]
        at = directive["simulation_time_s"] + 1.0
        after = obs(state, at, mode="divert")
        tick(supervise, after)
        supervise.record_application(
            directive_sha256=directive["sha256"], observed_evidence_sha256=after["evidence_sha256"]
        )
    return supervise.receipt()


@pytest.mark.parametrize(
    "flow", ("bounded", "unavailable", "collection", "human", "timeout", "pending")
)
def test_independent_replay_of_each_bounded_route(state, flow):
    record = make(state, flow)
    result = verified(record)
    assert result["passed"], result
    assert result["recorded_routing_request_count"] <= 2
    assert result["recorded_routing_proposal_count"] <= 2
    assert not result["actual_guidance_mode_observed"]


def test_accepted_command_does_not_prove_application_or_physical_value(state):
    record = make(state, apply=True)
    result = verified(record)
    assert result["passed"] and result["later_application_report_consistent"]
    assert not result["actual_guidance_mode_observed"]
    result = verified(record, external=deepcopy(record["observations"]))
    assert result["passed"] and result["actual_guidance_mode_observed"]
    assert result["actual_guidance_mode_change_observed"]


@pytest.mark.parametrize(
    "mutation",
    (
        "scope",
        "plan",
        "source",
        "body",
        "observation_hash",
        "obs_context",
        "obs_clock",
        "tick_clock",
        "tick_source",
        "tick_liveness",
        "request_hash",
        "request_scope",
        "request_deadline",
        "request_state",
        "proposal_hash",
        "proposal_authority",
        "proposal_numeric",
        "classifier_fixture",
        "classifier_confidence",
        "classifier_llm",
        "accepted_clock",
        "accepted_source",
        "accepted_numeric",
        "directive_hash",
        "directive_clock",
        "directive_action",
        "rules",
        "status",
        "budget",
        "mission_claim",
    ),
)
def test_forged_state_scope_clocks_or_authority_are_rejected(state, mutation):
    record = make(state)
    if mutation == "scope":
        record["approved_scope"]["maximum_observation_age_s"] = 76
    elif mutation == "plan":
        record["approved_scope"]["context"]["plan_sha256"] = "b" * 64
    elif mutation == "source":
        record["approved_scope"]["source_sha256"] = "b" * 64
    elif mutation == "body":
        record["observations"][0]["state"]["propellant_kg"] += 1
    elif mutation == "observation_hash":
        record["observations"][0]["evidence_sha256"] = "b" * 64
    elif mutation == "obs_context":
        record["observations"][0]["context"]["run_id"] = "other"
    elif mutation == "obs_clock":
        record["observations"][0]["simulation_time_s"] += 0.1
    elif mutation == "tick_clock":
        record["tick_inputs"][1]["simulation_time_s"] = 599
    elif mutation == "tick_source":
        record["tick_inputs"][1]["source_sha256"] = "b" * 64
    elif mutation == "tick_liveness":
        record["tick_inputs"][1]["run_active"] = False
    elif mutation == "request_hash":
        record["routing_requests"][0]["sha256"] = "b" * 64
    elif mutation == "request_scope":
        record["routing_requests"][0]["allowed_actions"].append("set_thrust")
    elif mutation == "request_deadline":
        record["routing_requests"][0]["original_simulation_deadline_s"] += 1
    elif mutation == "request_state":
        record["routing_requests"][0]["observation"]["state"]["propellant_kg"] += 1
    elif mutation == "proposal_hash":
        record["routing_proposals"][0]["request_sha256"] = "b" * 64
    elif mutation == "proposal_authority":
        record["routing_proposals"][0]["approval_granted"] = True
    elif mutation == "proposal_numeric":
        record["routing_proposals"][0]["numeric_flight_authority"] = True
    elif mutation == "classifier_fixture":
        record["routing_proposals"][0]["classifier_evidence"]["model_inference_invoked"] = True
    elif mutation == "classifier_confidence":
        record["routing_proposals"][0]["classifier_evidence"]["confidence_used_for_rules"] = True
    elif mutation == "classifier_llm":
        record["routing_proposals"][0]["classifier_evidence"]["llm_calls"] = 1
    elif mutation == "accepted_clock":
        record["accepted_decision"]["simulation_time_s"] += 1
    elif mutation == "accepted_source":
        record["accepted_decision"]["source"] = "llm_approved"
    elif mutation == "accepted_numeric":
        record["accepted_decision"]["numeric_flight_authority"] = True
    elif mutation == "directive_hash":
        record["directive"]["sha256"] = "b" * 64
    elif mutation == "directive_clock":
        record["directive"]["simulation_time_s"] = 601
    elif mutation == "directive_action":
        record["directive"]["action"] = "divert"
    elif mutation == "rules":
        record["rules"][0]["allowed"] = False
    elif mutation == "status":
        record["status"] = "application_reported"
    elif mutation == "budget":
        record["routing_requests"].append(deepcopy(record["routing_requests"][0]))
    else:
        record["mission_completed"] = True
    result = checker.verify_tower_supervision(
        record, expected_scope=F["scope"](), resolution_signing_key=KEY
    )
    assert not result["passed"], result


@pytest.mark.parametrize("mutation", ("earliest", "completion", "state", "deadline", "operation"))
def test_collection_needs_one_real_later_sample_without_deadline_extension(state, mutation):
    record = make(state, "collection")
    item = record["observation_collection"]
    if mutation == "earliest":
        item["earliest_simulation_time_s"] -= 1
    elif mutation == "completion":
        item["completed_evidence_sha256"] = item["issued_evidence_sha256"]
    elif mutation == "state":
        item["completed_state_sha256"] = "a" * 64
    elif mutation == "deadline":
        item["original_wall_deadline_s"] += 1
    else:
        item["actuator_operation"] = True
    assert not verified(record)["passed"]


@pytest.mark.parametrize(
    "mutation", ("wrong_key", "request", "decision", "action", "cached_state", "identity_claim")
)
def test_human_integrity_and_cached_freshness_cannot_create_approval(state, mutation):
    record = make(state, "human")
    key = KEY
    if mutation == "wrong_key":
        key = b"x" * 32
    elif mutation == "request":
        record["human_resolution"]["request"]["signature"] = "a" * 64
    elif mutation == "decision":
        record["human_resolution"]["decision"]["signature"] = "a" * 64
    elif mutation == "action":
        record["human_resolution"]["decision"]["effective_action"] = "set_thrust"
    elif mutation == "cached_state":
        record["human_resolution"]["decision"]["observed_evidence_sha256"] = "a" * 64
    else:
        record["human_resolution"]["human_identity_independently_verified"] = True
    assert not verified(record, key=key)["passed"]


@pytest.mark.parametrize(
    "mutation", ("same_time", "wrong_mode", "wrong_state", "physical", "external")
)
def test_application_needs_later_actual_mode_data_and_never_awards_improvement(state, mutation):
    record = make(state, apply=True)
    external = deepcopy(record["observations"])
    if mutation == "same_time":
        record["application_report"]["simulation_time_s"] = record["directive"]["simulation_time_s"]
    elif mutation == "wrong_mode":
        record["application_report"]["observed_return_mode"] = "capture"
    elif mutation == "wrong_state":
        record["application_report"]["state_sha256"] = "a" * 64
    elif mutation == "physical":
        record["application_report"]["physical_effect_verified"] = True
    else:
        external[-1]["return_mode"] = "capture"
    assert not verified(record, external=external)["passed"]


def test_dead_and_source_blocked_ticks_remain_visible_without_directive(state):
    for change in ({"run_active": False}, {"source_sha256": "b" * 64}):
        supervision, _ = F["supervisor"]()
        F["tick"](supervision, F["observation"](state), **change)
        record = supervision.receipt()
        assert len(record["observations"]) == len(record["tick_inputs"]) == 1
        assert verified(record)["passed"]
        assert record["directive"] is None


def test_same_clock_repeat_never_counts_as_new_sampling_or_later_command(state):
    supervision, _ = F["supervisor"]()
    obs = F["observation"](state)
    request = F["tick"](supervision, obs).routing_request
    F["tick"](supervision, obs, routing_proposal=F["proposal"](request))
    F["tick"](supervision, obs)
    result = verified(supervision.receipt())
    assert result["passed"] and not result["directive_emitted"]


def test_checker_never_imports_controller_provider_or_dynamics():
    source = inspect.getsource(checker)
    for name in (
        "starship_tower_supervision import",
        "starship_sixdof import",
        "starship_constrained_recovery import",
        "starship_flight_supervisor import",
        "JevAssuranceJudge(",
        "OperatorResolutionLedger(",
    ):
        assert name not in source


def stale_human_record(state, *, followup=None, wall_only=False):
    supervise, ledger = F["supervisor"]()
    obs, tick = F["observation"], F["tick"]
    request = tick(supervise, obs(state)).routing_request
    handover = tick(
        supervise, obs(state, 601.0), routing_proposal=F["proposal"](request, route="human_review")
    ).human_request
    cited = obs(state, 602.0)
    tick(supervise, cited)
    decision = ledger.resolve(
        context=F["CONTEXT"],
        request_sha256=handover["sha256"],
        observed_evidence_sha256=cited["evidence_sha256"],
        latest_observation=F["tower"]._ledger_observation(cited),
        action="continue_capture",
        simulation_time_s=602.0,
        wall_time_s=1002.0,
        run_active=True,
    )
    received = obs(state, 602.5, wall=1006.0) if wall_only else obs(state, 605.0)
    output = tick(supervise, received, human_decision=decision)
    assert output.directive is None and supervise.receipt()["accepted_decision"] is None
    if followup == "unavailable":
        tick(supervise, obs(state, 606.0, ready=False, wall=1007.0 if wall_only else None))
        tick(supervise, obs(state, 607.0, ready=False, wall=1008.0 if wall_only else None))
    elif followup == "deadline":
        tick(supervise, obs(state, 675.0))
        tick(supervise, obs(state, 676.0))
    return supervise.receipt()


@pytest.mark.parametrize("followup", [None, "unavailable", "deadline"])
@pytest.mark.parametrize("wall_only", [False, True])
def test_stale_signed_human_choice_is_logged_without_killing_or_renewing_request(
    state, followup, wall_only
):
    record = stale_human_record(state, followup=followup, wall_only=wall_only)
    result = verified(record)
    assert result["passed"], result
    assert result["human_resolution_signature_valid"]
    assert len(record["events"]) == 1
    event = record["events"][0]
    assert event["request_slot_consumed"] is True
    assert event["reapproval_created"] is event["executor_command_issued"] is False
    if followup is None:
        assert record["status"] == "awaiting_current_rules_after_stale_human"
        assert record["accepted_decision"] is record["directive"] is None
    else:
        assert record["accepted_decision"]["action"] == record["directive"]["action"] == "divert"
        assert record["accepted_decision"]["source"] != "operator_resolution"
    assert not result["actual_guidance_mode_observed"] and not result["physical_effect_verified"]


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "empty",
        "clock",
        "age",
        "decision",
        "observation",
        "renewal",
        "command",
        "slot",
        "extra",
    ],
)
def test_stale_rejection_event_is_bound_to_actual_signed_decision_and_consumption_tick(
    state, change
):
    record = stale_human_record(state)
    event = record["events"][0]
    if change == "absent":
        record.pop("events")
    elif change == "empty":
        record["events"] = []
    elif change == "clock":
        event["simulation_time_s"] += 0.1
    elif change == "age":
        event["decision_observation_simulation_age_s"] = 0.0
    elif change == "decision":
        event["decision_sha256"] = "f" * 64
    elif change == "observation":
        event["latest_observed_evidence_sha256"] = event["decision_observed_evidence_sha256"]
    elif change == "renewal":
        event["reapproval_created"] = True
    elif change == "command":
        event["executor_command_issued"] = True
    elif change == "slot":
        event["request_slot_consumed"] = False
    else:
        event["new_authority"] = True
    assert not verified(record)["passed"]


def test_normal_archived_shape_cannot_add_unearned_stale_event(state):
    record = make(state, "human")
    record["events"] = []
    assert not verified(record)["passed"]
