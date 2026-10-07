"""Static observation fixtures test governance, not a launch or diversion."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime import starship_tower_supervision as tower
from src.runtime.starship_booster_catch import _initialized, configuration
from src.runtime.starship_operator_resolution import OperatorResolutionError, OperatorResolutionLedger, SCOPE

KEY = b"explicit-fixture-local-signing-key-32bytes"
CONTEXT = {"session_id": "session-1", "plan_id": "plan-1", "plan_sha256": "a"*64,
           "run_id": "run-1", "request_id": "request-1"}
SOURCE = "d"*64


def scope(**changes):
    value = {"schema": tower.SCOPE_SCHEMA, "scope": SCOPE, "context": deepcopy(CONTEXT),
        "approval_record_sha256": "e"*64, "source_sha256": SOURCE, "issued_simulation_time_s": 590.,
        "issued_wall_time_s": 990., "original_simulation_deadline_s": 675., "original_wall_deadline_s": 1075.,
        "maximum_observation_age_s": 2., "observation_collection_allowed": True, "human_resolution_allowed": True}
    value.update(changes)
    return value


@pytest.fixture(scope="module")
def initial_state():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    return _initialized(profile, catch, "booster_catch")[1]


def observation(state, simulation=600., *, ready=True, mode="capture", wall=None):
    # Clock changes are declarative fixtures, explicitly not numerical steps.
    return tower.tower_observation(context=deepcopy(CONTEXT), observation_id=f"obs-{simulation}".replace(".", "-"),
        state=asdict(replace(state, time_s=simulation)), wall_time_s=1000.+simulation-600. if wall is None else wall,
        tower_ready=ready, return_mode=mode)


def supervisor(**changes):
    ledger = OperatorResolutionLedger(KEY)
    return tower.TowerSupervision(scope(**changes), resolution_ledger=ledger, resolution_signing_key=KEY), ledger


def tick(supervision, obs, **changes):
    values = {"simulation_time_s": obs["simulation_time_s"], "wall_time_s": obs["wall_time_s"],
              "run_active": True, "source_sha256": SOURCE, "plan_sha256": CONTEXT["plan_sha256"]}
    values.update(changes)
    return supervision.tick(obs, **values)


class JudgeFixture:
    fixture_only = True

    def __init__(self, route="bounded", action="continue"):
        self.route, self.action, self.prompts = route, action, []

    def judge(self, prompt):
        self.prompts.append(deepcopy(prompt))
        return SimpleNamespace(output={"proposed_response_kind": self.action, "parameters": {}},
            invocation_evidence={"assessment_route": self.route}, model_inference_invoked=True)


def proposal(request, *, route="bounded", action="continue"):
    return tower.TowerRoutingAdapter(JudgeFixture(route, action)).assess(request, run_active=True, source_current=True)


def accepted(supervision, state, *, action="continue"):
    request = tick(supervision, observation(state)).routing_request
    tick(supervision, observation(state, 601.), routing_proposal=proposal(request, action=action))
    return request


def test_proposal_acceptance_and_later_one_use_directive_are_distinct(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    assert request["sha256"] == tower._digest({k: v for k, v in request.items() if k != "sha256"})
    routed = proposal(request)
    result = tick(supervision, observation(initial_state, 601.), routing_proposal=routed)
    assert result.directive is None
    receipt = supervision.receipt()
    assert receipt["accepted_decision"]["action"] == "continue_capture"
    assert receipt["accepted_decision"]["executor_command_issued"] is False
    later = tick(supervision, observation(initial_state, 602.))
    assert later.directive["action"] == "continue_capture"
    assert later.directive["requires_executor_rules_check"] is True
    assert later.directive["executor_application_reported"] is False
    assert tick(supervision, observation(initial_state, 603.)).directive is None
    assert len(supervision.receipt()["rules"]) == 1
    assert supervision.receipt()["physical_effect_verified"] is False


def test_known_tower_unavailable_defaults_to_divert_without_classifier(initial_state):
    supervision, _ = supervisor()
    first = tick(supervision, observation(initial_state, ready=False))
    assert first.routing_request is first.directive is None
    assert supervision.receipt()["accepted_decision"]["source"] == "deterministic_tower_unavailable"
    later = tick(supervision, observation(initial_state, 601., ready=False))
    assert later.directive["action"] == "divert"
    assert supervision.receipt()["routing_proposals"] == []


@pytest.mark.parametrize("current_ready", [False, None])
def test_current_readiness_overrides_previously_accepted_capture(initial_state, current_ready):
    supervision, _ = supervisor()
    accepted(supervision, initial_state)
    result = tick(supervision, observation(initial_state, 602., ready=current_ready))
    if current_ready is False:
        assert result.directive["action"] == "divert"
    else:
        assert result.directive is None
        assert supervision.receipt()["status"] == "accepted_waiting_fresh_readiness"
        assert tick(supervision, observation(initial_state, 603., ready=True)).directive["action"] == "continue_capture"


def test_need_observation_requests_real_later_sampling_without_advancing_time(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state, ready=None)).routing_request
    result = tick(supervision, observation(initial_state, 601., ready=None),
                  routing_proposal=proposal(request, route="need_observation", action="operator_escalation"))
    operation = result.observation_request
    assert operation["earliest_simulation_time_s"] == 603.
    assert operation["actuator_operation"] is False
    assert operation["completed_evidence_sha256"] is None
    assert tick(supervision, observation(initial_state, 602., ready=True)).routing_request is None
    fresh = observation(initial_state, 603., ready=True)
    followup = tick(supervision, fresh).routing_request
    assert followup["round"] == 2
    assert followup["observation"] == fresh
    assert followup["original_simulation_deadline_s"] == request["original_simulation_deadline_s"]
    tick(supervision, observation(initial_state, 604.), routing_proposal=proposal(followup))
    assert tick(supervision, observation(initial_state, 605.)).directive["action"] == "continue_capture"
    collection = supervision.receipt()["observation_collection"]
    assert collection["completed_state_sha256"] == fresh["state_sha256"]
    assert collection["completed_evidence_sha256"] == fresh["evidence_sha256"]


def test_second_observation_request_hands_over_to_human_instead_of_repeating(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state, ready=None)).routing_request
    tick(supervision, observation(initial_state, 601., ready=None), routing_proposal=proposal(request, route="need_observation"))
    followup = tick(supervision, observation(initial_state, 603., ready=None)).routing_request
    result = tick(supervision, observation(initial_state, 604., ready=None), routing_proposal=proposal(followup, route="need_observation"))
    assert result.observation_request is None
    assert result.human_request is not None
    assert len(supervision.receipt()["routing_requests"]) == 2
    assert supervision.receipt()["human_resolution"]["request"] == result.human_request


def test_signed_human_choice_waits_for_later_rules_and_requires_bound_fresh_evidence(initial_state):
    supervision, ledger = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    handover = tick(supervision, observation(initial_state, 601.), routing_proposal=proposal(request, route="human_review"))
    fresh = observation(initial_state, 602.)
    tick(supervision, fresh)
    decision = ledger.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
        observed_evidence_sha256=fresh["evidence_sha256"], latest_observation=tower._ledger_observation(fresh),
        action="continue_capture", simulation_time_s=602., wall_time_s=1002., run_active=True)
    received = tick(supervision, observation(initial_state, 602.1), human_decision=decision)
    assert received.directive is None
    assert supervision.receipt()["human_resolution"]["decision"] == decision
    assert supervision.receipt()["accepted_decision"]["source"] == "operator_resolution"
    later = tick(supervision, observation(initial_state, 602.2))
    assert later.directive["action"] == "continue_capture"
    assert decision["human_identity_authenticated"] is False
    assert supervision.receipt()["human_resolution"]["human_identity_independently_verified"] is False


def test_unknown_readiness_cannot_be_signed_as_capture_but_can_divert(initial_state):
    supervision, ledger = supervisor()
    request = tick(supervision, observation(initial_state, ready=None)).routing_request
    obs = observation(initial_state, 601., ready=None)
    handover = tick(supervision, obs, routing_proposal=proposal(request, route="human_review"))
    values = {"context": CONTEXT, "request_sha256": handover.human_request["sha256"],
        "observed_evidence_sha256": obs["evidence_sha256"], "latest_observation": tower._ledger_observation(obs),
        "simulation_time_s": 601., "wall_time_s": 1001., "run_active": True}
    with pytest.raises(OperatorResolutionError, match="tower_readiness_unknown"):
        ledger.resolve(action="continue_capture", **values)
    decision = ledger.resolve(action="divert", **values)
    tick(supervision, observation(initial_state, 602., ready=None), human_decision=decision)
    assert tick(supervision, observation(initial_state, 603., ready=None)).directive["action"] == "divert"


@pytest.mark.parametrize("field,value", [("context", {**CONTEXT, "session_id": "other"}),
    ("request_sha256", "f"*64), ("effective_action", "set_thrust"), ("observed_evidence_sha256", "f"*64)])
def test_forged_or_cross_context_human_record_is_never_accepted(initial_state, field, value):
    supervision, ledger = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    obs = observation(initial_state, 601.)
    handover = tick(supervision, obs, routing_proposal=proposal(request, route="human_review"))
    decision = ledger.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
        observed_evidence_sha256=obs["evidence_sha256"], latest_observation=tower._ledger_observation(obs),
        action="continue_capture", simulation_time_s=601., wall_time_s=1001., run_active=True)
    decision[field] = value
    with pytest.raises(tower.TowerSupervisionError, match="unbound_tower_human_decision"):
        tick(supervision, observation(initial_state, 602.), human_decision=decision)
    assert supervision.receipt()["accepted_decision"] is None


def test_original_deadline_expires_pending_human_request_without_approval(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    tick(supervision, observation(initial_state, 601.), routing_proposal=proposal(request, route="human_review"))
    at_deadline = tick(supervision, observation(initial_state, 675.))
    assert at_deadline.directive is None
    receipt = supervision.receipt()
    assert receipt["human_resolution"]["decision"]["source"] == "timeout_fallback"
    assert receipt["human_resolution"]["decision"]["operator_response_received"] is False
    assert receipt["accepted_decision"]["action"] == "divert"
    assert tick(supervision, observation(initial_state, 675.1)).directive["action"] == "divert"


def test_previously_consumed_but_unapplied_human_choice_still_defaults_to_divert_at_deadline(initial_state):
    supervision, ledger = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    obs = observation(initial_state, 601.)
    handover = tick(supervision, obs, routing_proposal=proposal(request, route="human_review"))
    decision = ledger.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
        observed_evidence_sha256=obs["evidence_sha256"], latest_observation=tower._ledger_observation(obs),
        action="continue_capture", simulation_time_s=601., wall_time_s=1001., run_active=True)
    tick(supervision, observation(initial_state, 675.))
    assert supervision.receipt()["human_resolution"]["decision"] == decision
    assert tick(supervision, observation(initial_state, 675.1)).directive["action"] == "divert"


@pytest.mark.parametrize("simulation,wall", [(603.001, 1003.), (603., 1003.001)])
def test_consumed_stale_human_choice_is_rejected_without_killing_observation_loop(initial_state, simulation, wall):
    supervision, ledger = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    old = observation(initial_state, 601.)
    handover = tick(supervision, old, routing_proposal=proposal(request, route="human_review"))
    signed = ledger.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
        observed_evidence_sha256=old["evidence_sha256"], latest_observation=tower._ledger_observation(old),
        action="continue_capture", simulation_time_s=601., wall_time_s=1001., run_active=True)
    latest = observation(initial_state, simulation, wall=wall)
    received = tick(supervision, latest, human_decision=signed)
    assert received.directive is None
    record = supervision.receipt()
    assert record["status"] == "awaiting_current_rules_after_stale_human"
    assert record["human_resolution"]["decision"] == signed
    assert record["accepted_decision"] is None and record["directive"] is None
    assert record["events"] == [{"event": "stale_human_decision_rejected", "decision_sha256": tower._digest(signed),
        "decision_observed_evidence_sha256": old["evidence_sha256"], "latest_observed_evidence_sha256": latest["evidence_sha256"],
        "simulation_time_s": simulation, "wall_time_s": wall, "decision_observation_simulation_age_s": simulation-601.,
        "decision_observation_wall_age_s": wall-1001., "maximum_observation_age_s": 2.,
        "request_slot_consumed": True, "reapproval_created": False, "executor_command_issued": False}]
    with pytest.raises(OperatorResolutionError, match="already_consumed"):
        ledger.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
            observed_evidence_sha256=latest["evidence_sha256"], latest_observation=tower._ledger_observation(latest),
            action="continue_capture", simulation_time_s=simulation, wall_time_s=wall, run_active=True)
    assert tick(supervision, observation(initial_state, 604., wall=1004.)).directive is None
    # Current Rules retain their own authority and cannot resurrect the old
    # human go: a new unavailable observation forces the approved diversion.
    tick(supervision, observation(initial_state, 605., ready=False, wall=1005.))
    assert supervision.receipt()["accepted_decision"]["source"] == "deterministic_tower_unavailable"
    assert tick(supervision, observation(initial_state, 605.1, ready=False, wall=1005.1)).directive["action"] == "divert"


def test_stale_signed_human_choice_can_reach_original_deadline_fallback_without_reapproval(initial_state):
    supervision, ledger = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    old = observation(initial_state, 601.)
    handover = tick(supervision, old, routing_proposal=proposal(request, route="human_review"))
    signed = ledger.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
        observed_evidence_sha256=old["evidence_sha256"], latest_observation=tower._ledger_observation(old),
        action="continue_capture", simulation_time_s=601., wall_time_s=1001., run_active=True)
    tick(supervision, observation(initial_state, 604.), human_decision=signed)
    tick(supervision, observation(initial_state, 675.))
    assert supervision.receipt()["accepted_decision"]["source"] == "original_deadline_fallback"
    assert supervision.receipt()["human_resolution"]["decision"] == signed
    assert ledger.records(CONTEXT)["request"] == handover.human_request
    assert tick(supervision, observation(initial_state, 675.1)).directive["action"] == "divert"


@pytest.mark.parametrize("change", [{"source_sha256": "f"*64}, {"plan_sha256": "f"*64}, {"run_active": False}])
def test_source_plan_or_run_change_blocks_later_consumption(initial_state, change):
    supervision, _ = supervisor()
    accepted(supervision, initial_state)
    assert tick(supervision, observation(initial_state, 602.), **change).directive is None
    assert supervision.receipt()["directive"] is None


def test_stale_state_and_same_time_tick_never_emit_a_capture_directive(initial_state):
    supervision, _ = supervisor()
    accepted(supervision, initial_state)
    obs = observation(initial_state, 601.)
    assert tick(supervision, obs).directive is None
    assert tick(supervision, obs, simulation_time_s=604., wall_time_s=1004.).directive is None
    assert tick(supervision, observation(initial_state, 605.)).directive["action"] == "continue_capture"


def test_later_executor_report_records_mode_change_without_claiming_physical_improvement(initial_state):
    supervision, _ = supervisor()
    accepted(supervision, initial_state, action="replan")
    directive = tick(supervision, observation(initial_state, 602.)).directive
    with pytest.raises(tower.TowerSupervisionError, match="lacks_later_mode"):
        supervision.record_application(directive_sha256=directive["sha256"], observed_evidence_sha256=directive["observed_evidence_sha256"])
    later = observation(initial_state, 603., mode="divert")
    tick(supervision, later)
    report = supervision.record_application(directive_sha256=directive["sha256"], observed_evidence_sha256=later["evidence_sha256"])
    assert report["executor_application_reported"] is True
    assert report["observed_guidance_mode_change"] is True
    assert report["physical_effect_verified"] is False
    assert report["flight_outcome_improvement"] is False
    assert supervision.receipt()["ditch_trajectory_implemented"] is False
    with pytest.raises(tower.TowerSupervisionError, match="not_pending"):
        supervision.record_application(directive_sha256=directive["sha256"], observed_evidence_sha256=later["evidence_sha256"])


def test_continue_capture_report_does_not_invent_a_mode_change(initial_state):
    supervision, _ = supervisor()
    accepted(supervision, initial_state)
    directive = tick(supervision, observation(initial_state, 602.)).directive
    later = observation(initial_state, 603.)
    tick(supervision, later)
    assert supervision.record_application(directive_sha256=directive["sha256"],
        observed_evidence_sha256=later["evidence_sha256"])["observed_guidance_mode_change"] is False


def test_classifier_fixture_is_bounded_uses_existing_prompt_contract_and_has_no_authority(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    judge = JudgeFixture("deep_reasoning", "replan")
    adapter = tower.TowerRoutingAdapter(judge)
    routed = adapter.assess(request, run_active=True, source_current=True)
    assert routed["route"] == "deep_reasoning"
    assert routed["proposed_action"] is None
    assert routed["classifier_evidence"]["model_inference_invoked"] is False
    assert routed["classifier_evidence"]["llm_calls"] == 0
    prompt = judge.prompts[0]
    assert set(prompt["decision_contract"]["allowed_response_kinds"]) == {"continue", "replan", "operator_escalation"}
    assert "state" not in prompt["mission_situation"]["observations"]
    assert "session_id" not in json.dumps(prompt)
    with pytest.raises(tower.TowerSupervisionError, match="budget_exhausted"):
        adapter.assess(request, run_active=True, source_current=True)
    handover = tick(supervision, observation(initial_state, 601.), routing_proposal=routed)
    assert handover.human_request is not None
    assert supervision.receipt()["human_resolution"]["llm_connected"] is False


@pytest.mark.parametrize("field,value", [("proposed_action", "set_thrust"), ("request_sha256", "f"*64),
    ("context_sha256", "f"*64), ("source_sha256", "f"*64), ("observed_evidence_sha256", "f"*64),
    ("approval_granted", True), ("executor_command_issued", True), ("numeric_flight_authority", True)])
def test_routing_cannot_expand_authority_or_cross_evidence_context(initial_state, field, value):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    routed = proposal(request)
    routed[field] = value
    with pytest.raises(tower.TowerSupervisionError, match="unbound_tower_routing_proposal"):
        tick(supervision, observation(initial_state, 601.), routing_proposal=routed)
    assert supervision.receipt()["accepted_decision"] is None


def test_scope_observation_and_returned_receipt_copies_are_immutable(initial_state):
    approved = scope()
    ledger = OperatorResolutionLedger(KEY)
    supervision = tower.TowerSupervision(approved, resolution_ledger=ledger, resolution_signing_key=KEY)
    approved["context"]["run_id"] = "changed"
    obs = observation(initial_state)
    request = tick(supervision, obs).routing_request
    obs["state"]["propellant_kg"] = 0.
    request["original_simulation_deadline_s"] = 1200.
    receipt = supervision.receipt()
    assert receipt["approved_scope"]["context"] == CONTEXT
    assert receipt["routing_requests"][0]["original_simulation_deadline_s"] == 675.
    assert receipt["observations"][0]["state"]["propellant_kg"] == initial_state.propellant_kg
    receipt["observations"].clear()
    assert len(supervision.receipt()["observations"]) == 1


def test_terminal_receipt_remains_separate_from_mode_application_and_success(initial_state):
    supervision, _ = supervisor()
    tick(supervision, observation(initial_state))
    final = observation(initial_state, 610.)
    result = supervision.finish({"state": final["state"], "simulation_time_s": 610., "wall_time_s": 1010.,
        "termination": "fixture_failure", "outcome_sha256": "f"*64, "independent_verification_sha256": None})
    assert result["terminal_outcome"]["termination"] == "fixture_failure"
    assert result["mission_completed"] is False
    assert result["physical_effect_verified"] is False
    assert result["model_value_established"] is False
    with pytest.raises(tower.TowerSupervisionError, match="already_finished"):
        tick(supervision, observation(initial_state, 611.))


@pytest.mark.parametrize("changes", [{"scope": "physical_capture"}, {"approval_record_sha256": "invalid"},
    {"original_simulation_deadline_s": 590.}, {"original_wall_deadline_s": 990.},
    {"original_simulation_deadline_s": 1800.}, {"maximum_observation_age_s": 0.},
    {"maximum_observation_age_s": 76.}, {"observation_collection_allowed": 1},
    {"human_resolution_allowed": 1}, {"extra": "thrust_override"}])
def test_preapproved_scope_is_closed_and_cannot_widen_authority(changes):
    with pytest.raises(tower.TowerSupervisionError):
        supervisor(**changes)


@pytest.mark.parametrize("field,value", [("state_sha256", "f"*64), ("evidence_sha256", "f"*64),
    ("simulation_time_s", 601.), ("context", {**CONTEXT, "plan_id": "other"}),
    ("tower_ready", 1), ("return_mode", "force_engines_on")])
def test_exact_observation_evidence_state_and_readiness_are_bound(initial_state, field, value):
    supervision, _ = supervisor()
    obs = observation(initial_state)
    obs[field] = value
    with pytest.raises(tower.TowerSupervisionError):
        tick(supervision, obs)
    assert supervision.receipt()["accepted_decision"] is None


def test_changed_or_reused_observation_clock_cannot_count_as_new_sampling(initial_state):
    supervision, _ = supervisor()
    original = observation(initial_state)
    tick(supervision, original)
    changed = observation(initial_state, ready=None)
    with pytest.raises(tower.TowerSupervisionError, match="reused_observation_changed"):
        tick(supervision, changed)
    earlier = observation(initial_state, 599.)
    with pytest.raises(tower.TowerSupervisionError, match="clock_regressed"):
        tick(supervision, earlier)


def test_classifier_cannot_receive_extra_hidden_truth_or_arbitrary_actions(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    judge = JudgeFixture()
    adapter = tower.TowerRoutingAdapter(judge)
    for change in ({"hidden_recovery_time_s": 123.}, {"allowed_actions": ["set_thrust"]}):
        changed = {**request, **change}
        changed["sha256"] = tower._digest({k: v for k, v in changed.items() if k != "sha256"})
        with pytest.raises(tower.TowerSupervisionError, match="invalid_tower_routing_request"):
            adapter.assess(changed, run_active=True, source_current=True)
    assert judge.prompts == []


def test_classifier_confidence_and_fixture_receipts_cannot_claim_native_value(initial_state):
    supervision, _ = supervisor()
    request = tick(supervision, observation(initial_state)).routing_request
    routed = proposal(request)
    routed["classifier_evidence"]["confidence_used_for_rules"] = True
    with pytest.raises(tower.TowerSupervisionError, match="invalid_tower_classifier_evidence"):
        tick(supervision, observation(initial_state, 601.), routing_proposal=routed)
    assert supervision.receipt()["accepted_decision"] is None


def test_stale_executor_application_report_cannot_count_as_observed_mode_change(initial_state):
    supervision, _ = supervisor()
    accepted(supervision, initial_state, action="replan")
    directive = tick(supervision, observation(initial_state, 602.)).directive
    later = observation(initial_state, 603., mode="divert")
    tick(supervision, later, simulation_time_s=606., wall_time_s=1006.)
    with pytest.raises(tower.TowerSupervisionError, match="lacks_later_mode"):
        supervision.record_application(directive_sha256=directive["sha256"], observed_evidence_sha256=later["evidence_sha256"])
    assert supervision.receipt()["application_report"] is None


def test_fixture_mode_refuses_an_undeclared_provider_judge_before_it_can_invoke():
    calls = []
    class NativeJudge:
        def judge(self, prompt):
            calls.append(prompt)
            raise AssertionError("A provider must not be called in fixture mode")
    with pytest.raises(tower.TowerSupervisionError, match="fixture_judge_declaration_required"):
        tower.TowerRoutingAdapter(NativeJudge(), mode="fixture")
    assert calls == []
