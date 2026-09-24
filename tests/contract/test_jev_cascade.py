from copy import deepcopy

import pytest

from src.intelligence.jev_assurance import JevAssuranceJudge
from src.intelligence.jev_cascade import JevCascadeJudge, JevCascadeShadowJudge
from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    ModelJudgment,
    build_mission_assurance_prompt,
    configured_mission_assurance_agent,
)
from tests.contract.test_jev_assurance import situation, response


class Reasoner:
    def __init__(self, fail=False):
        self.calls, self.fail, self.prompt = 0, fail, None

    def judge(self, prompt):
        self.calls += 1
        self.prompt = deepcopy(prompt)
        if self.fail:
            raise TimeoutError()
        return ModelJudgment(
            {
                "proposed_response_kind": "replan",
                "parameters": {},
                "rationale": "reasoner",
                "expected_outcome": "bounded proposal",
                "uncertainty": "fixture",
                "operator_question": "Review?",
            },
            {"model_id": "fixture-reasoner"},
        )


def jev(route="bounded", confidence=0.1, fail=False, review="bounded"):
    def transport(payload):
        if fail:
            raise TimeoutError()
        result = response(payload)
        result["answers"]["response"]["confidence"] = confidence
        result["answers"]["review"]["choice"] = review
        result["answers"]["assessment_route"] = {
            "type": "choice",
            "choice": route,
            "confidence": confidence,
            "probabilities": {
                k: float(k == route) for k in payload["questions"]["assessment_route"]["criteria"]
            },
        }
        return result

    return JevAssuranceJudge(transport=transport, include_routing=True)


def prompt():
    p = build_mission_assurance_prompt(situation())
    p["mission_situation"]["observations"] = {
        "runtime_recovery_agent_result": {
            "assessment": {"selected_bounded_action": "avoid_obstacle"}
        },
        "source_action_feasibility": {
            "feasibility_status": "verified_feasible",
            "action": "avoid_obstacle",
        },
        "runtime_telemetry": {"telemetry": {"stale": False, "dropout": False}},
    }
    return p


@pytest.mark.parametrize("confidence", [0.01, 0.99])
def test_explicit_fixture_fast_path_skips_reasoner_independent_of_confidence(confidence):
    slow = Reasoner()
    result = JevCascadeJudge(
        slow, jev=jev(confidence=confidence), fast_path="fixture_verified_detour_v1"
    ).judge(prompt())
    assert slow.calls == 0
    assert result.output["proposed_response_kind"] == "hold"
    assert result.invocation_evidence["jev_cascade"]["route"] == "jev"


@pytest.mark.parametrize("change", ["disabled", "simulator", "stale", "unverified", "review"])
def test_outside_fast_scope_uses_reasoner_with_original_prompt(change):
    slow, p = Reasoner(), prompt()
    profile, review = "fixture_verified_detour_v1", "bounded"
    if change == "disabled":
        profile = "disabled"
    if change == "simulator":
        p["mission_situation"]["execution_scope"] = "simulator"
    if change == "stale":
        p["mission_situation"]["observations"]["runtime_telemetry"]["telemetry"]["stale"] = True
    if change == "unverified":
        p["mission_situation"]["observations"]["source_action_feasibility"][
            "feasibility_status"
        ] = "unknown"
    if change == "review":
        review = "review"
    result = JevCascadeJudge(slow, jev=jev(review=review), fast_path=profile).judge(p)
    assert slow.calls == 1 and slow.prompt == p
    assert result.output["rationale"] == "reasoner"
    assert result.invocation_evidence["jev_cascade"]["route"] == "reasoner"


def test_deep_reasoning_route_invokes_reasoner():
    slow = Reasoner()
    result = JevCascadeJudge(
        slow, jev=jev("deep_reasoning"), fast_path="fixture_verified_detour_v1"
    ).judge(prompt())
    assert slow.calls == 1
    assert result.invocation_evidence["jev_cascade"]["reason"] == "jev_requested_reasoning"


@pytest.mark.parametrize("route", ["need_observation", "human_review"])
def test_missing_evidence_or_human_decision_never_goes_to_reasoner(route):
    slow = Reasoner()
    result = JevCascadeJudge(slow, jev=jev(route)).judge(prompt())
    assert slow.calls == 0
    assert result.output["proposed_response_kind"] == "operator_escalation"
    assert result.invocation_evidence["jev_cascade"]["route"] == route


@pytest.mark.parametrize(
    "declared", ["required_observations_missing", "operator_decision_required"]
)
def test_source_declared_missing_requirement_overrides_bounded_model_answer(declared):
    slow, p = Reasoner(), prompt()
    p["mission_situation"]["uncertainty"]["mission_context"] = {declared: True}
    result = JevCascadeJudge(slow, jev=jev(), fast_path="fixture_verified_detour_v1").judge(p)
    assert slow.calls == 0 and result.output["proposed_response_kind"] == "operator_escalation"


@pytest.mark.parametrize("failure", ["jev", "reasoner"])
def test_provider_failure_escalates_without_another_fallback(failure):
    slow = Reasoner(fail=failure == "reasoner")
    result = JevCascadeJudge(slow, jev=jev("deep_reasoning", fail=failure == "jev")).judge(prompt())
    assert slow.calls == int(failure == "reasoner")
    assert result.output["proposed_response_kind"] == "operator_escalation"
    assert result.invocation_evidence["jev_cascade"]["reason"] == failure + "_failed"


@pytest.mark.parametrize("route", ["bounded", "deep_reasoning", "need_observation", "human_review"])
def test_shadow_preserves_incumbent_and_reuses_single_reasoner_call(route):
    slow = Reasoner()
    p = prompt()
    result = JevCascadeShadowJudge(
        slow, jev=jev(route), fast_path="fixture_verified_detour_v1"
    ).judge(p)
    assert slow.calls == 1 and slow.prompt == p
    assert result.output["rationale"] == "reasoner"
    shadow = result.invocation_evidence["jev_cascade_shadow"]
    assert shadow["used_for_decision"] is False
    assert shadow["reasoner_reused_primary_call"] is (route == "deep_reasoning")


def test_shadow_incumbent_failure_is_not_replaced_by_jev():
    with pytest.raises(TimeoutError):
        JevCascadeShadowJudge(Reasoner(fail=True), jev=jev()).judge(prompt())


@pytest.mark.parametrize("defect", ["choice", "sum", "nan", "missing"])
def test_invalid_routing_answer_never_reaches_reasoner(defect):
    valid = jev().transport

    def invalid(payload):
        raw = valid(payload)
        route = raw["answers"]["assessment_route"]
        if defect == "choice":
            route["choice"] = "approve"
        if defect == "sum":
            route["probabilities"]["bounded"] = 0.2
        if defect == "nan":
            route["confidence"] = float("nan")
        if defect == "missing":
            del route["probabilities"]["bounded"]
        return raw

    slow = Reasoner()
    result = JevCascadeJudge(
        slow, jev=JevAssuranceJudge(transport=invalid, include_routing=True)
    ).judge(prompt())
    assert slow.calls == 0 and result.output["proposed_response_kind"] == "operator_escalation"


@pytest.mark.parametrize(
    "mode,kind", [("cascade", JevCascadeJudge), ("cascade_shadow", JevCascadeShadowJudge)]
)
def test_modes_select_cascade(monkeypatch, mode, kind):
    monkeypatch.setenv("MISSIONOS_JEV_MODE", mode)
    assert isinstance(configured_mission_assurance_agent()._judge, kind)


def test_cascade_proposal_has_no_authority():
    result = (
        MissionAssuranceAgent(JevCascadeJudge(Reasoner(), jev=jev("need_observation")))
        .evaluate(situation())
        .to_dict()
    )
    assert result["proposed_response_kind"] == "operator_escalation"
    assert result["model_invocation_evidence"]["jev_cascade"]["route"] == "need_observation"
    for key in [
        "operator_approved",
        "dispatch_authority_created",
        "physical_execution_invoked",
        "progress_counted",
    ]:
        assert result[key] is False


def test_source_can_require_reasoning_even_when_jev_says_bounded():
    slow, p = Reasoner(), prompt()
    p["mission_situation"]["uncertainty"]["mission_context"] = {
        "requires_additional_reasoning": True
    }
    result = JevCascadeJudge(slow, jev=jev(), fast_path="fixture_verified_detour_v1").judge(p)
    assert slow.calls == 1
    assert result.invocation_evidence["jev_cascade"]["reason"] == "declared_additional_reasoning"


def test_invalid_profile_fails_before_any_model(monkeypatch):
    monkeypatch.setenv("MISSIONOS_JEV_MODE", "cascade_shadow")
    monkeypatch.setenv("MISSIONOS_JEV_CASCADE_FAST_PATH", "typo")
    result = configured_mission_assurance_agent().evaluate(situation())
    assert result.proposed_response_kind == "operator_escalation"
    assert result.model_inference_invoked is False
