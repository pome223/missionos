"""Failure receipts and machine-readable candidate semantics at the real adapters."""
import json
from copy import deepcopy

import pytest

from src.intelligence import mission_assurance_agent as assurance
from src.intelligence.jev_assurance import JevAssuranceJudge, JevShadowJudge
from src.intelligence.jev_cascade import JevCascadeJudge, JevCascadeShadowJudge
from tests.contract.test_jev_assurance import response, situation
from tests.contract.test_jev_cascade import Reasoner


@pytest.mark.parametrize("mode", ["primary", "shadow", "cascade", "cascade_shadow"])
def test_failed_jev_receipt_survives_every_composition(mode):
    def invalid(payload):
        raw = response(payload)
        raw["answers"]["response"]["probabilities"]["hold"] = 0.5
        raw["untrusted_text"] = "SENSITIVE_PROVIDER_ECHO"
        return raw

    jev = JevAssuranceJudge(transport=invalid)
    judge = {
        "primary": jev,
        "shadow": JevShadowJudge(Reasoner(), jev),
        "cascade": JevCascadeJudge(Reasoner(), jev=jev),
        "cascade_shadow": JevCascadeShadowJudge(Reasoner(), jev=jev),
    }[mode]
    result = assurance.MissionAssuranceAgent(judge).evaluate(situation())
    evidence = result.model_invocation_evidence
    serialized = json.dumps(evidence)
    assert "invalid_jev_probability_sum" in serialized
    assert "response_sha256" in serialized and '"response": 0.5' in serialized
    assert "SENSITIVE_PROVIDER_ECHO" not in serialized
    assert result.proposed_response_kind == ("replan" if "shadow" in mode else "operator_escalation")
    assert result.model_inference_invoked is True


@pytest.mark.parametrize("defect", ["missing", "nan", "root", "routing"])
def test_jev_schema_failures_have_safe_diagnostic_identifiers(defect):
    def invalid(payload):
        raw = response(payload)
        raw["answers"]["assessment_route"] = {
            "type": "choice", "choice": "bounded", "confidence": 0.9,
            "probabilities": {"bounded": 0.5, "deep_reasoning": 0.1, "need_observation": 0.1, "human_review": 0.1},
        }
        if defect == "missing":
            del raw["answers"]["response"]["confidence"]
        elif defect == "nan":
            raw["answers"]["response"]["confidence"] = float("nan")
        elif defect == "root":
            return ["SENSITIVE_PROVIDER_ECHO"]
        return raw

    result = assurance.MissionAssuranceAgent(JevAssuranceJudge(transport=invalid, include_routing=True)).evaluate(situation())
    assert result.judgment_status == "failed"
    evidence = result.model_invocation_evidence
    assert evidence["validation_reason"].startswith("invalid_jev_")
    assert len(evidence["response_sha256"]) == 64
    assert evidence["raw_response_recorded"] is False
    assert "SENSITIVE_PROVIDER_ECHO" not in json.dumps(evidence, allow_nan=False)


def _adk_output(assessment, label="replan"):
    return {
        "candidate_assessment": assessment, "proposed_response_kind": label,
        "parameters": {}, "rationale": "The existing candidate conflicts; a different plan is needed.",
        "expected_outcome": "Proposal only.", "uncertainty": "Fixture.", "operator_question": "Review?",
    }


def test_non_json_jev_response_is_fingerprinted_without_echoing_body(monkeypatch):
    import io
    import src.intelligence.jev_assurance as module
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")
    monkeypatch.setattr(module, "urlopen", lambda *a, **k: io.BytesIO(b"SENSITIVE_PROVIDER_ECHO"))
    result = assurance.MissionAssuranceAgent(JevAssuranceJudge()).evaluate(situation())
    assert result.judgment_status == "failed"
    assert result.blocking_reasons == ("invalid_jev_json",)
    assert len(result.model_invocation_evidence["response_sha256"]) == 64
    assert "SENSITIVE_PROVIDER_ECHO" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("assessment", ["conflicting", "unresolved", "not_applicable", None, ["aligned"]])
@pytest.mark.parametrize("label", ["replan", "return", "abort"])
def test_adk_cannot_endorse_candidate_it_did_not_assess_as_aligned(monkeypatch, assessment, label):
    async def invoke(_): return json.dumps(_adk_output(assessment, label))
    monkeypatch.setattr(assurance, "_invoke_adk_response", invoke)
    result = assurance.MissionAssuranceAgent(assurance._ADKJudge()).evaluate(situation())
    assert result.judgment_status == "failed"
    assert result.proposed_response_kind == "operator_escalation"
    assert result.model_inference_invoked
    assert result.model_invocation_evidence["candidate_semantics"]["free_text_consistency_verified"] is False


@pytest.mark.parametrize("assessment,label", [("aligned", "replan"), ("conflicting", "hold"), ("unresolved", "operator_escalation")])
def test_adk_internal_assessment_is_audited_without_changing_proposal_schema(monkeypatch, assessment, label):
    async def invoke(_): return json.dumps(_adk_output(assessment, label))
    monkeypatch.setattr(assurance, "_invoke_adk_response", invoke)
    result = assurance._ADKJudge().judge(assurance.build_mission_assurance_prompt(situation()))
    assert "candidate_assessment" not in result.output
    assert result.invocation_evidence["candidate_semantics"]["assessment"] == assessment
    assert result.output["proposed_response_kind"] == label


@pytest.mark.parametrize("nested", [False, True])
def test_custom_response_mapping_does_not_inherit_builtin_endorsement_semantics(monkeypatch, nested):
    async def invoke(_): return json.dumps(_adk_output("conflicting"))
    monkeypatch.setattr(assurance, "_invoke_adk_response", invoke)
    prompt = deepcopy(assurance.build_mission_assurance_prompt(situation()))
    mapping = {"response_mapping": {"replan": "Request a different candidate; do not endorse this one."}}
    prompt["mission_situation"]["mission_contract"] = {"mission_context": mapping} if nested else mapping
    result = assurance._ADKJudge().judge(prompt)
    assert result.output["proposed_response_kind"] == "replan"
    assert result.invocation_evidence["candidate_semantics"]["built_in_response_guard_applied"] is False


def test_cascade_preserves_reasoner_semantics_failure(monkeypatch):
    async def invoke(_): return json.dumps(_adk_output("conflicting"))
    monkeypatch.setattr(assurance, "_invoke_adk_response", invoke)
    s = situation().to_dict()
    s["uncertainty"] = {"mission_context": {"requires_additional_reasoning": True}}
    result = assurance.MissionAssuranceAgent(JevCascadeJudge(assurance._ADKJudge())).evaluate(assurance.MissionSituation.from_dict(s))
    assert result.judgment_status == "failed"
    audit = result.model_invocation_evidence["jev_cascade"]
    assert audit["jev_invoked"] is False and audit["reasoner_invoked"] is True
    assert audit["reasoner_failure"]["reason"] == "candidate_response_assessment_mismatch"


def test_shadow_retains_incumbent_failure_even_when_candidate_stops_before_models(monkeypatch):
    async def invoke(_): return json.dumps(_adk_output("conflicting"))
    monkeypatch.setattr(assurance, "_invoke_adk_response", invoke)
    s = situation().to_dict()
    s["uncertainty"] = {"mission_context": {"operator_decision_required": True}}
    result = assurance.MissionAssuranceAgent(JevCascadeShadowJudge(assurance._ADKJudge())).evaluate(assurance.MissionSituation.from_dict(s))
    assert result.judgment_status == "failed" and result.model_inference_invoked
    audit = result.model_invocation_evidence["jev_cascade_shadow"]
    assert audit["model_inference_invoked"] is False
    assert audit["incumbent_failure"]["reason"] == "candidate_response_assessment_mismatch"
