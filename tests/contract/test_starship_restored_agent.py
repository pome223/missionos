"""Synthetic decoding only; no credentials, provider requests or plant steps."""
from copy import deepcopy
import json

import pytest

from src.intelligence import starship_restored_agent as agent


def request(provider="typesafe", phase="return"):
    return {"schema": agent.SCHEMA, "request_id": "restored-0", "phase": phase,
        "scope_sha256": "a"*64, "time_s": 2000., "deadline_s": 2150.,
        "facts": {"time_s": 2000., "released_count": 26, "retained_count": 0,
            "candidate.nominal.contact_speed_mps": 4.68,
            "candidate.next_orbit.additional_wait_s": 5500.},
        "notice_summary": "Synthetic test operations notice; no real recovery area.",
        "allowed_actions": (["continue_deployment", "withhold_deployment"] if phase == "deployment"
            else ["select_nominal", "select_next_orbit"] + (["request_deep_reasoning"] if provider == "typesafe" else []))}


class SyntheticTransport:
    def __init__(self, answer):
        self.answer, self.calls, self.sent = answer, 0, None

    def _invoke(self, provider, payload, receipt, decode):
        self.calls += 1
        self.sent = deepcopy(payload)
        receipt["status"] = "synthetic_decoder_only"
        try:
            result, model = decode(self.answer)
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
            receipt["status"] = "invalid_response"
            return None
        # Test transport does not claim actual provider inference occurred.
        receipt.update(status="succeeded", model_id=model)
        return result


def jev(choice="select_nominal"):
    return {"model": agent.JEV_MODEL, "answers": {"decision": {
        "type": "choice", "choice": choice,
        "probabilities": {"select_nominal": .2, "select_next_orbit": .3, "request_deep_reasoning": .5},
        "confidence": .5}}}


def deep(action="select_next_orbit", refs=None):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"action": action,
            "summary": "Compare the prepared resource tradeoffs.",
            "reference_ids": refs if refs is not None else ["candidate.next_orbit.additional_wait_s"]})}}]}


def assess(answer, provider="typesafe", **kwargs):
    transport = SyntheticTransport(answer)
    response = agent.RestoredAgent(transport=transport, **kwargs).assess(
        request(provider), provider=provider, remaining_s=180.)
    return response, transport


@pytest.mark.parametrize("choice", ["select_nominal", "select_next_orbit", "request_deep_reasoning"])
def test_valid_native_choice_preserved_not_approved(choice):
    response, transport = assess(jev(choice))
    assert response["action"] == choice and response["status"] == "valid"
    assert not response["execution_permission_granted"] and not response["model_inference_invoked"]
    assert transport.calls == 1


def test_deepseek_references_resolve_only_existing_tool_values():
    response, transport = assess(deep(), "deepseek")
    assert response["references"] == [{"reference_id": "candidate.next_orbit.additional_wait_s", "value": 5500.}]
    assert not response["free_prose_verified"] and not response["internal_reasoning_verified"]
    assert transport.sent["model"] == "deepseek-flash"
    assert transport.sent["thinking"] == {"type": "enabled"}
    assert "raw_reasoning" not in response


@pytest.mark.parametrize("bad,reason", [
    (lambda x: x.update(model="deepseek-v4-flash"), "model_mismatch"),
    (lambda x: x["answers"]["decision"]["probabilities"].update(select_nominal=.8), "probability_sum_or_range"),
    (lambda x: x["answers"]["decision"].update(confidence=True), "probability_sum_or_range"),
    (lambda x: x["answers"]["decision"].update(choice="dispatch_burn"), "choice_or_distribution_keys"),
    (lambda x: x["answers"]["decision"].update(extra="secret"), "choice_shape"),
])
def test_jev_invalid_has_fixed_diagnostic_no_retry_or_raw_content(bad, reason):
    answer = jev()
    bad(answer)
    response, transport = assess(answer)
    assert response["status"] == "no_valid_judgment" and response["action"] is None
    assert response["provider_receipt"]["validation_error"] == reason and transport.calls == 1
    assert "secret" not in json.dumps(response)


@pytest.mark.parametrize("answer,reason", [
    (deep(refs=["true_fault"]), "answer_shape_or_reference_ids"),
    (deep(refs=["time_s", "time_s"]), "answer_shape_or_reference_ids"),
    (deep(action="request_deep_reasoning"), "answer_shape_or_reference_ids"),
    ({"model": "deepseek-flash", "choices": []}, "choices_shape"),
    ({"model": "deepseek-v4-flash"}, "model_mismatch"),
])
def test_deepseek_invalid_not_replaced_by_desired_answer(answer, reason):
    response, transport = assess(answer, "deepseek")
    assert response["action"] is None and response["references"] == []
    assert response["provider_receipt"]["validation_error"] == reason and transport.calls == 1


def test_long_hidden_reasoning_not_saved():
    answer = deep()
    answer["choices"][0]["message"]["reasoning_content"] = "not-public"*100
    response, _ = assess(answer, "deepseek")
    assert response["status"] == "valid" and "not-public" not in json.dumps(response)


@pytest.mark.parametrize("fact", ["true_fault", "source_sha256", "r_eci_m", "engine_states", "operator_token"])
def test_unselected_truth_or_private_fields_not_sent(fact):
    value = request()
    value["facts"][fact] = 1.
    transport = SyntheticTransport(jev())
    with pytest.raises(ValueError, match="facts_or_actions"):
        agent.RestoredAgent(transport=transport).assess(value, provider="typesafe", remaining_s=180.)
    assert transport.calls == 0


@pytest.mark.parametrize("actions", [["select_nominal", "select_nominal"], ["dispatch_burn"], [["select_nominal"]], []])
def test_ineligible_or_malformed_actions_rejected(actions):
    value = request()
    value["allowed_actions"] = actions
    with pytest.raises(ValueError):
        agent.validate_request(value)


def test_deployment_cannot_borrow_deepseek_long_window():
    with pytest.raises(ValueError, match="physical_actions_only"):
        agent.public_input(request(phase="deployment"), provider="deepseek")


def test_deadline_after_valid_answer_fails_closed():
    times = iter([100., 251.])
    response, transport = assess(deep(), "deepseek", clock=lambda: next(times))
    assert response["action"] is None and response["provider_receipt"]["status"] == "response_after_deadline"
    assert transport.calls == 1


def test_no_call_if_remaining_budget_not_reserved():
    transport = SyntheticTransport(deep())
    with pytest.raises(ValueError, match="registered_decision_window"):
        agent.RestoredAgent(transport=transport).assess(request("deepseek"), provider="deepseek", remaining_s=149.)
    assert transport.calls == 0


def test_pipe_setup_consumes_original_deployment_deadline_without_extending_it():
    value = request(phase="deployment")
    value["deadline_s"] = value["time_s"]+35.
    answer = jev("continue_deployment")
    answer["answers"]["decision"]["probabilities"] = {
        "continue_deployment": .7, "withhold_deployment": .3}
    transport = SyntheticTransport(answer)
    times = iter([100.25, 100.25, 135.01])
    response = agent.RestoredAgent(transport=transport, clock=lambda: next(times)).assess(
        value, provider="typesafe", remaining_s=34.75, outer_deadline_monotonic=135.)
    assert transport.calls == 1
    assert response["action"] is None
    assert response["provider_receipt"]["decision_deadline_monotonic"] == 135.
    assert response["provider_receipt"]["status"] == "response_after_deadline"


def test_expired_original_pipe_deadline_never_sends():
    transport = SyntheticTransport(jev())
    with pytest.raises(ValueError, match="outer_decision_deadline_expired"):
        agent.RestoredAgent(transport=transport, clock=lambda: 135.).assess(
            request(), provider="typesafe", remaining_s=10., outer_deadline_monotonic=135.)
    assert transport.calls == 0


def test_no_call_when_run_inactive():
    transport = SyntheticTransport(jev())
    with pytest.raises(ValueError, match="run_inactive"):
        agent.RestoredAgent(transport=transport, active=lambda: False).assess(
            request(), provider="typesafe", remaining_s=180.)
    assert transport.calls == 0


def test_real_transport_requires_explicit_live_opt_in(monkeypatch):
    monkeypatch.delenv(agent.MODE_ENV, raising=False)
    with pytest.raises(ValueError, match="live_mode_opt_in"):
        agent.RestoredAgent().assess(request(), provider="typesafe", remaining_s=180.)
