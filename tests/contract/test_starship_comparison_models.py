"""New routing stays within one finite provider attempt and visible inputs."""

from copy import deepcopy
import json
import pytest

from src.intelligence import starship_replanning as model
from src.runtime.starship_replanning import contract, digest, notices, MODE_ENV


def request(envelope):
    return {
        "schema": "missionos.m1_decision.v1",
        "request_id": "test-1",
        "envelope_sha256": digest(envelope),
        "revision": 1,
        "time_s": 1700.0,
        "deadline_s": 1735.0,
        "stage": "event_reassessment",
        "notice": notices("comparison_update", 1700),
        "resources": {},
        "selected": "nominal",
        "allowed_actions": ["keep_plan", "select_next_orbit"],
        "context_generation": 1,
        "trigger_event_ids": ["event-1"],
        "candidates": {
            n: {"constraint_rejections": [], "comparison_material": {"waiting_at_burn": {"energy_remaining_kwh": x}}}
            for n, x in (("nominal", 22.0), ("next_orbit", 12.9))
        },
        "comparison_context": {"generation_sha256": "a" * 64, "ready": True},
    }


def evidence_for(value):
    return {
        "schema": model.EVIDENCE_SCHEMA,
        "request_sha256": digest(value),
        "references": [
            {"candidate_id": name, "field_path": "/waiting_at_burn/energy_remaining_kwh",
             "value": row["comparison_material"]["waiting_at_burn"]["energy_remaining_kwh"],
             "unit": "kWh"}
            for name, row in value["candidates"].items()
        ],
    }


def setup_agent(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setattr(model, "_deepseek_model", lambda: "deepseek-test")
    envelope = contract("live", "comparison_update")
    agent = model.ReplanningAgent(envelope, lambda: True)
    calls = []

    def invoke(provider, payload, receipt, decode):
        calls.append((provider, payload))
        if provider == "deepseek":
            value = {
                "model": payload["model"],
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "keep_plan",
                                    "compared_candidate_ids": ["nominal", "next_orbit"],
                                    "summary": "Retain the earlier return because the later option leaves little energy.",
                                    "numeric_evidence": evidence_for(request(envelope)),
                                }
                            ),
                            "reasoning_content": "private reasoning never retained",
                        },
                    }
                ],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 123}},
            }
        else:
            options = payload["questions"]["decision"]["criteria"]
            value = {
                "model": model.JEV_MODEL,
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": "keep_plan",
                        "probabilities": {k: 1.0 if k == "keep_plan" else 0.0 for k in options},
                        "confidence": 0.9,
                    }
                },
            }
        result, _ = decode(value)
        receipt.update(call_attempted=True, model_inference_invoked=True, call_succeeded=True)
        return result

    monkeypatch.setattr(agent.transport, "_invoke", invoke)
    return agent, request(envelope), calls


def test_ready_uncertain_comparison_routes_one_provider_with_real_thinking(monkeypatch):
    agent, r, calls = setup_agent(monkeypatch)
    response = agent.assess(r)
    assert [c[0] for c in calls] == ["deepseek"]
    payload = calls[0][1]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high" and payload["max_tokens"] == 4096
    assert "temperature" not in payload
    assert response["action"] == "keep_plan"
    assert response["decision_basis"]["compared_candidate_ids"] == ["nominal", "next_orbit"]
    assert response["llm_invocation"]["reasoning_tokens"] == 123
    assert "private reasoning" not in json.dumps(response)
    public = json.loads(payload["messages"][1]["content"])
    assert "comparison_update" not in json.dumps(public)
    assert "evaluation_conditions" not in json.dumps(public)
    assert public["candidates"] == r["candidates"]


def test_generation_uses_reasoning_once_and_never_serial_provider_waits(monkeypatch):
    agent, r, calls = setup_agent(monkeypatch)
    agent.assess(r)
    response = agent.assess(r)
    assert [c[0] for c in calls] == ["deepseek", "typesafe"]
    assert response["routing"]["route"] == "jev"
    assert "deep_reasoning" not in calls[1][1]["questions"]["decision"]["criteria"]
    r["comparison_context"]["generation_sha256"] = "b" * 64
    agent.assess(r)
    r["comparison_context"]["generation_sha256"] = "c" * 64
    agent.assess(r)
    assert [c[0] for c in calls] == ["deepseek", "typesafe", "deepseek", "typesafe"]
    assert agent.calls == 4 and agent.llm_calls == 2


@pytest.mark.parametrize(
    "change", ["one_candidate", "blocked_candidate", "certain_estimate", "not_ready"]
)
def test_no_reasoning_for_missing_or_noncompeting_material(monkeypatch, change):
    agent, r, calls = setup_agent(monkeypatch)
    if change == "one_candidate":
        r["candidates"].pop("next_orbit")
    elif change == "blocked_candidate":
        r["candidates"]["next_orbit"]["constraint_rejections"] = ["energy"]
    elif change == "not_ready":
        r["comparison_context"]["ready"] = False
    else:
        forecast = r["notice"]["recovery_service_forecast"]
        for k, v in forecast.items():
            if type(v) is list:
                forecast[k] = [v[0], v[0]]
    agent.assess(r)
    assert [c[0] for c in calls] == ["typesafe"]


def test_comparison_material_required_and_unknown_fields_rejected(monkeypatch):
    agent, r, _ = setup_agent(monkeypatch)
    malformed = deepcopy(r)
    malformed["candidates"]["nominal"].pop("comparison_material")
    with pytest.raises(ValueError, match="missing_m1_candidate_costs"):
        agent.assess(malformed)
    r["hidden_future"] = 1350
    with pytest.raises(ValueError, match="invalid_m1_request"):
        agent.assess(r)


@pytest.mark.parametrize("failure", ["length", "json", "action", "decision_basis"])
def test_invalid_reasoning_retains_bounded_usage_and_reason(monkeypatch, failure):
    from src.intelligence import starship_flight_supervisor as transport

    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    authority = contract("live", "comparison_update")
    agent = model.ReplanningAgent(authority, lambda: True)
    answer = {"action": "keep_plan", "compared_candidate_ids": ["nominal", "next_orbit"],
              "summary": "Preserve the earlier opportunity.", "numeric_evidence": evidence_for(request(authority))}
    if failure == "action":
        answer["action"] = "unauthorized"
    if failure == "decision_basis":
        answer["compared_candidate_ids"] = ["nominal"]
    content = "{" if failure in ("length", "json") else json.dumps(answer)
    value = {
        "model": authority["comparison_preparation"]["deep_reasoning"]["model"],
        "choices": [{"finish_reason": "length" if failure == "length" else "stop",
                     "message": {"content": content, "reasoning_content": "NEVER_STORE_ME"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 4096, "total_tokens": 5096,
                  "completion_tokens_details": {"reasoning_tokens": 4000}},
    }
    calls = []

    def send(*args):
        calls.append(True)
        return "response", json.dumps(value).encode()

    monkeypatch.setattr(transport, "_send_once", send)
    result = agent.assess(request(authority))
    receipt = result["llm_invocation"]
    assert len(calls) == 1 and result["action"] is None and result["decision_basis"] is None
    assert receipt["status"] == "invalid_response"
    assert receipt["validation_error"] == ("output_limit" if failure == "length" else failure)
    assert receipt["finish_reason"] == ("length" if failure == "length" else "stop")
    assert receipt["reasoning_tokens"] == 4000
    assert receipt["provider_usage"]["completion_tokens"] == 4096
    assert receipt["reasoning_content_observed"] is True
    assert "NEVER_STORE_ME" not in json.dumps(result)


@pytest.mark.parametrize("credential_name", ["DEEPSEEK_API_KEY", "TYPESAFE_API_KEY"])
def test_summary_rejects_credential_echo_before_persistence(monkeypatch, credential_name):
    from src.intelligence import starship_flight_supervisor as transport

    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "transport-test-key")
    secret = "private-test-value-never-persist"
    monkeypatch.setenv(credential_name, secret)
    authority = contract("live", "comparison_update")
    agent = model.ReplanningAgent(authority, lambda: True)
    value = {
        "model": authority["comparison_preparation"]["deep_reasoning"]["model"],
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "action": "keep_plan", "compared_candidate_ids": ["nominal", "next_orbit"],
            "summary": "Keep plan. " + secret,
            "numeric_evidence": evidence_for(request(authority)),
        })}}],
    }
    monkeypatch.setattr(transport, "_send_once", lambda *_: ("response", json.dumps(value).encode()))
    result = agent.assess(request(authority))
    assert result["action"] is None and result["decision_basis"] is None
    assert result["llm_invocation"]["validation_error"] == "credential_in_response"
    assert result["llm_invocation"]["status"] == "invalid_response"
    assert secret not in json.dumps(result)


def test_numeric_references_are_bound_to_full_request_and_audited_separately(monkeypatch):
    from src.runtime.starship_decision_evidence import verify_numeric_evidence

    agent, r, calls = setup_agent(monkeypatch)
    result = agent.assess(r)
    public = json.loads(calls[0][1]["messages"][1]["content"])
    assert public["numeric_evidence_contract"]["request_sha256"] == digest(r)
    basis = result["decision_basis"]
    audit = verify_numeric_evidence(r, basis["numeric_evidence"], basis["compared_candidate_ids"])
    assert audit["status"] == "valid"
    wrong = deepcopy(basis["numeric_evidence"])
    wrong["references"][0]["value"] = 999
    assert verify_numeric_evidence(r, wrong, basis["compared_candidate_ids"])["status"] == "invalid"
    assert result["action"] == "keep_plan"  # Evidence audit creates no command authority.


@pytest.mark.parametrize("mutation", ["missing", "nonfinite", "bool", "too_many", "unknown_candidate"])
def test_malformed_numeric_evidence_is_bounded_before_retention(monkeypatch, mutation):
    from src.intelligence import starship_flight_supervisor as transport

    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    authority = contract("live", "comparison_update")
    r = request(authority)
    evidence = evidence_for(r)
    if mutation == "missing":
        evidence = None
    elif mutation == "nonfinite":
        evidence["references"][0]["value"] = float("inf")
    elif mutation == "bool":
        evidence["references"][0]["value"] = True
    elif mutation == "too_many":
        evidence["references"] *= 9
    else:
        evidence["references"][0]["candidate_id"] = "hidden_future_candidate"
    answer = {"action": "keep_plan", "compared_candidate_ids": ["nominal", "next_orbit"],
              "summary": "Keep the booking.", "numeric_evidence": evidence}
    value = {"model": authority["comparison_preparation"]["deep_reasoning"]["model"],
             "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
    monkeypatch.setattr(transport, "_send_once", lambda *_: ("response", json.dumps(value).encode()))
    result = model.ReplanningAgent(authority, lambda: True).assess(r)
    assert result["action"] is None and result["decision_basis"] is None
    assert result["llm_invocation"]["validation_error"] == ("json" if mutation == "nonfinite" else "numeric_evidence")
