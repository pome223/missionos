from copy import deepcopy
import pytest

from src.intelligence.jev_assurance import JevAssuranceJudge, JevShadowJudge
from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionSituation,
    ModelJudgment,
)


def situation():
    return MissionSituation(
        "fixture",
        "2026-09-20T00:00:00Z",
        {},
        {},
        {},
        {},
        {},
        ("fixture",),
        "fixture.v1",
        "input",
        "fixture",
    )


def response(payload):
    labels = payload["questions"]["response"]["criteria"]
    return {
        "model": "fixture-jev",
        "answers": {
            "response": {
                "type": "choice",
                "choice": "hold",
                "confidence": 0.9,
                "probabilities": {k: float(k == "hold") for k in labels},
            },
            "review": {"type": "choice", "choice": "bounded"},
        },
    }


def test_jev_bounded_choice_never_creates_authority():
    result = (
        MissionAssuranceAgent(JevAssuranceJudge(transport=response)).evaluate(situation()).to_dict()
    )
    assert result["proposed_response_kind"] == "hold"
    assert result["judgment_status"] == "proposal_guardrail_passed"
    assert result["parameters"] == {}
    assert result["model_invocation_evidence"]["rationale_source"] == "adapter_template"
    for key in ("dispatch_authority_created", "physical_execution_invoked", "progress_counted"):
        assert result[key] is False


@pytest.mark.parametrize("defect", ["choice", "nan", "missing", "probability_sum", "timeout"])
def test_jev_invalid_or_failed_response_escalates(defect):
    def broken(payload):
        if defect == "timeout":
            raise TimeoutError("provider timeout")
        result = response(payload)
        answer = result["answers"]["response"]
        if defect == "choice":
            answer["choice"] = "approved"
        if defect == "nan":
            answer["confidence"] = float("nan")
        if defect == "missing":
            del answer["probabilities"]["continue"]
        if defect == "probability_sum":
            answer["probabilities"]["hold"] = 0.1
        return result

    result = MissionAssuranceAgent(JevAssuranceJudge(transport=broken)).evaluate(situation())
    assert result.proposed_response_kind == "operator_escalation"
    assert result.judgment_status == "failed"


@pytest.mark.parametrize("fail", [True, False])
def test_shadow_cannot_change_primary_output(fail):
    primary_output = {
        "proposed_response_kind": "continue",
        "parameters": {},
        "rationale": "primary",
        "expected_outcome": "primary",
        "uncertainty": "primary",
        "operator_question": "primary",
    }

    class Primary:
        def judge(self, prompt):
            return ModelJudgment(deepcopy(primary_output), {"provider": "fixture"})

    def transport(payload):
        if fail:
            raise TimeoutError()
        return response(payload)

    result = MissionAssuranceAgent(
        JevShadowJudge(Primary(), JevAssuranceJudge(transport=transport))
    ).evaluate(situation())
    assert result.proposed_response_kind == "continue"
    assert result.rationale == "primary"
    shadow = result.model_invocation_evidence["jev_shadow"]
    assert shadow["used_for_decision"] is False
    assert shadow["status"] == ("failed" if fail else "observed")
    if not fail:
        assert shadow["agrees_with_primary"] is False
