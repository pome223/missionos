"""Real inherited FDs and production decoding with synthetic responses only."""
from copy import deepcopy
import json
import socket
import subprocess
import threading

import pytest

from src.intelligence import starship_restored_agent as agent
from src.runtime import starship_restored_decision_pipe as pipe
from src.runtime.starship_restored_return_review import DEFAULT_POLICY, policy_digest
from src.runtime.starship_inflight_runtime import approval_pipe

CONTEXT = {"run_id": "synthetic-review", "mission_id": "synthetic-review", "vehicle_id": "ship"}
BINDINGS = {"context": CONTEXT, "plan_sha256": "b"*64, "source_sha256": "c"*64}


def request(phase="return"):
    return {"schema": agent.SCHEMA, "request_id": "synthetic-review-1", "phase": phase,
        "scope_sha256": "a"*64, "time_s": 2000., "deadline_s": 2180. if phase == "return" else 2035.,
        "facts": {"time_s": 2000., "released_count": 13, "retained_count": 13},
        "notice_summary": "Synthetic observation; no real flight.",
        "allowed_actions": ["keep_reserved_return", "select_next_orbit", "request_deep_reasoning"]
            if phase == "return" else ["continue_deployment", "withhold_deployment"]}


def jev(*, action="keep_reserved_return", confidence=.2, probabilities=None):
    return {"model": agent.JEV_MODEL, "answers": {"decision": {"type": "choice", "choice": action,
        "confidence": confidence, "probabilities": probabilities or
            {"keep_reserved_return": .4, "select_next_orbit": .35, "request_deep_reasoning": .25}}}}


def deep(action="select_next_orbit"):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"action": action,
            "summary": "Synthetic comparison of decision-time inputs.", "reference_ids": ["time_s"]})}}]}


@pytest.fixture(autouse=True)
def no_model_network_or_physics(monkeypatch):
    from src.runtime import starship_sixdof as dyn

    def forbidden(*_a, **_kw):
        raise AssertionError("Synthetic review tests cannot invoke network, subprocess, or physics")

    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (socket, "socket"),
            (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def endpoints():
    host_reader, client_writer = approval_pipe()
    client_reader, host_writer = approval_pipe()
    now, calls = [100.], []

    def build(*, answer=None, deep_answer=None, delay=0., deep_delay=0., policy=DEFAULT_POLICY,
            mutate_judgment=None):
        class Transport:
            def _invoke(self, provider, payload, receipt, decode):
                calls.append((provider, deepcopy(payload)))
                now[0] += delay if provider == "typesafe" else deep_delay
                value = answer if provider == "typesafe" else deep_answer
                receipt["status"] = "synthetic_decoder_only"
                try:
                    decoded, model = decode(deepcopy(value))
                except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
                    receipt["status"] = "invalid_response"
                    return None
                receipt.update(status="succeeded", model_id=model)
                return decoded

        class Agent:
            def __init__(self, *, active, clock):
                self.adapter = agent.RestoredAgent(transport=Transport(), active=active, clock=clock)

            def assess(self, req, **kwargs):
                result = self.adapter.assess(req, **kwargs)
                if mutate_judgment is not None and kwargs["provider"] == "typesafe":
                    mutate_judgment(result)
                return result

        client = pipe.RestoredDecisionClient(**BINDINGS, request_writer=client_writer,
            response_reader=client_reader, clock=lambda: now[0], return_review_policy=policy)
        host = pipe.RestoredDecisionHost(**BINDINGS, reader=host_reader, writer=host_writer,
            agent_factory=Agent, clock=lambda: now[0], return_review_policy=policy)
        return client, host

    yield {"build": build, "now": now, "calls": calls, "host_writer": host_writer,
        "client_writer": client_writer}
    for endpoint in (host_reader, client_writer, client_reader, host_writer):
        endpoint.close()


def pump(client, host, sim=2000.):
    for _ in range(2000):
        host.tick()
        result = client.poll(sim)
        if result is not None:
            return result
        threading.Event().wait(.001)
    pytest.fail("Synthetic review did not finish")


def test_rules_review_preserves_original_jev_receipt_and_same_evidence_in_original_epoch(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep(), delay=20.)
    original = request()
    frame = client.begin(original)
    result = pump(client, host)
    assert frame["schema"] == pipe.REQUEST_FRAME_V2
    assert frame["return_review_policy_sha256"] == policy_digest(DEFAULT_POLICY)
    assert result["schema"] == pipe.EXCHANGE_V2 and result["status"] == "valid"
    assert result["action"] == "select_next_orbit"
    assert result["attempts"][0]["judgment"]["action"] == "keep_reserved_return"
    assert result["attempts"][0]["judgment"]["status"] == "valid"
    assert result["return_review"]["route"] == "rules_uncertainty_review"
    assert result["return_review"]["confidence"] == .2
    assert [row["provider"] for row in result["attempts"]] == ["typesafe", "deepseek"]
    assert result["attempts"][1]["remaining_s"] == 160.
    assert result["attempts"][1]["decision_deadline_s"] == 2160.
    first, second = [payload for _, payload in endpoints["calls"]]
    public = json.loads(second["messages"][1]["content"])
    assert public["evidence"] == first["state"]["evidence"] == original["facts"]
    assert public["effects"] == {key: value for key, value in first["state"]["effects"].items()
        if key != "request_deep_reasoning"}
    assert public["allowed_actions"] == ["keep_reserved_return", "select_next_orbit"]
    expected_deep = deepcopy(original)
    expected_deep["allowed_actions"].remove("request_deep_reasoning")
    expected_deep["deadline_s"] = 2160.
    assert public == agent.public_input(expected_deep, provider="deepseek")
    assert "jev" not in json.dumps(public).lower() and "correct" not in json.dumps(public).lower()
    assert host.record()["requests"][0]["frame"]["wall_deadline_s"] == 280.
    assert client.record()["schema"] == pipe.CLIENT_V2
    assert host.record()["schema"] == pipe.HOST_V2
    assert client.record()["return_review_policy"] == host.record()["return_review_policy"] == DEFAULT_POLICY
    assert result["execution_permission_granted"] is False
    assert result["model_inference_invoked"] is False


@pytest.mark.parametrize("action", ["keep_reserved_return", "select_next_orbit"])
def test_valid_deep_choice_including_contrary_keep_is_preserved(endpoints, action):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep(action))
    client.begin(request())
    result = pump(client, host)
    assert result["action"] == result["judgment"]["action"] == action
    assert result["judgment"] == result["attempts"][-1]["judgment"]


def test_high_confidence_but_selected_action_margin_small_requires_review(endpoints):
    client, host = endpoints["build"](answer=jev(confidence=.9), deep_answer=deep())
    client.begin(request())
    result = pump(client, host)
    assert result["return_review"]["low_confidence"] is False
    assert result["return_review"]["small_selected_margin"] is True
    assert len(endpoints["calls"]) == 2


def test_threshold_boundaries_leave_valid_jev_choice_unchanged(endpoints):
    values = {"keep_reserved_return": .5, "select_next_orbit": .4, "request_deep_reasoning": .1}
    client, host = endpoints["build"](answer=jev(confidence=.5, probabilities=values))
    client.begin(request())
    result = pump(client, host)
    assert result["return_review"]["route"] == "jev_final"
    assert result["action"] == "keep_reserved_return" and len(endpoints["calls"]) == 1


def test_explicit_jev_delegation_is_distinct_from_rules_review(endpoints):
    client, host = endpoints["build"](answer=jev(action="request_deep_reasoning"), deep_answer=deep())
    client.begin(request())
    result = pump(client, host)
    assert result["return_review"]["route"] == "explicit_jev_delegation"
    assert result["return_review"]["low_confidence"] is False
    assert len(endpoints["calls"]) == 2


def test_insufficient_149_point_999_seconds_never_silently_adopts_provisional_jev(endpoints):
    client, host = endpoints["build"](answer=jev(), delay=30.001, deep_answer=deep())
    client.begin(request())
    result = pump(client, host)
    assert result["reason"] == "insufficient_deep_reasoning_window"
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["judgment"]["status"] == "valid" and result["judgment"]["action"] == "keep_reserved_return"
    assert len(endpoints["calls"]) == 1 and result["return_review"]["review_required"]


def test_exact_150_seconds_remain_is_eligible_without_new_epoch(endpoints):
    client, host = endpoints["build"](answer=jev(), delay=30., deep_answer=deep())
    client.begin(request())
    result = pump(client, host)
    assert result["action"] == "select_next_orbit"
    assert result["attempts"][1]["remaining_s"] == 150.


def test_deep_timeout_is_no_valid_and_does_not_restore_jev_provisional(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep(), deep_delay=180.)
    client.begin(request())
    host.tick()
    host.sessions[0]["thread"].join(1.)
    result = pump(client, host)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["judgment"] == result["attempts"][-1]["judgment"]
    assert result["judgment"]["status"] == "no_valid_judgment"
    assert len(endpoints["calls"]) == 2


def test_missing_metric_on_forged_valid_receipt_fails_closed_without_secondary(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep(),
        mutate_judgment=lambda row: row["provider_receipt"].pop("native_choice_confidence"))
    client.begin(request())
    result = pump(client, host)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["reason"] == "invalid_jev_review_metrics"
    assert result["return_review"]["route"] == "invalid_jev_metrics"
    assert len(endpoints["calls"]) == 1


def test_production_decoder_rejects_nonfinite_metric_without_review(endpoints):
    client, host = endpoints["build"](answer=jev(confidence=float("nan")), deep_answer=deep())
    client.begin(request())
    result = pump(client, host)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["return_review"]["route"] == "no_valid_jev_judgment"
    assert len(endpoints["calls"]) == 1


def test_deployment_decoding_and_action_unchanged_by_opt_in_return_policy(endpoints):
    answer = jev(action="continue_deployment", confidence=.1,
        probabilities={"continue_deployment": .5, "withhold_deployment": .5})
    client, host = endpoints["build"](answer=answer)
    client.begin(request("deployment"))
    result = pump(client, host)
    assert result["action"] == "continue_deployment" and len(endpoints["calls"]) == 1
    assert result["return_review"]["route"] == "not_applicable"


@pytest.mark.parametrize("change", [
    lambda row: row.pop("return_review"),
    lambda row: row.update(schema=pipe.EXCHANGE),
    lambda row: row["return_review"].update(route="jev_final"),
    lambda row: row["return_review"].update(policy_sha256="d"*64),
    lambda row: row["return_review"].update(confidence=.9),
    lambda row: row.update(judgment=row["attempts"][0]["judgment"]),
    lambda row: row.update(judgment=None),
    lambda row: row.update(action="keep_reserved_return"),
])
def test_v2_exchange_rejects_dropped_forged_route_or_nonlast_final(endpoints, change):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep())
    original = request()
    client.begin(original)
    result = pump(client, host)
    change(result)
    with pytest.raises(ValueError):
        pipe._validate_result(result, original, return_review_policy=DEFAULT_POLICY)


def test_v2_result_cannot_be_laundered_as_default_v1(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep())
    original = request()
    client.begin(original)
    result = pump(client, host)
    with pytest.raises(ValueError, match="invalid_exchange"):
        pipe._validate_result(result, original)


def test_v2_response_cannot_drop_policy_binding_or_schema(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep())
    original = request()
    frame = client.begin(original)
    result = pipe._result(original, [], reason="synthetic_no_call", return_review_policy=DEFAULT_POLICY)
    response = {"schema": pipe.RESPONSE_FRAME_V2, **client.binding,
        **{key: frame[key] for key in ("sequence", "request_id", "request_sha256")},
        "result": result, "result_sha256": pipe._digest(result)}
    response.pop("return_review_policy_sha256")
    with pytest.raises(ValueError, match="response_binding_mismatch"):
        client._response(response, frame)


def test_default_is_exact_legacy_v1_even_with_low_metrics(endpoints):
    client, host = endpoints["build"](answer=jev(), policy=None)
    original = request()
    frame = client.begin(original)
    result = pump(client, host)
    assert frame["schema"] == pipe.REQUEST_FRAME
    assert result["schema"] == pipe.EXCHANGE and result["action"] == "keep_reserved_return"
    expected = pipe._result(original, result["attempts"], result["judgment"])
    assert pipe._wire(result) == pipe._wire(expected)
    assert len(endpoints["calls"]) == 1
    assert "return_review" not in result
    assert "return_review_policy_sha256" not in frame
    assert "return_review_policy" not in client.record()
    assert "return_review_policy" not in host.record()


def test_low_confidence_alone_requires_review_even_with_clear_selected_margin(endpoints):
    values = {"keep_reserved_return": .7, "select_next_orbit": .2, "request_deep_reasoning": .1}
    client, host = endpoints["build"](answer=jev(confidence=.2, probabilities=values), deep_answer=deep())
    client.begin(request())
    result = pump(client, host)
    assert result["return_review"]["low_confidence"] is True
    assert result["return_review"]["small_selected_margin"] is False
    assert len(endpoints["calls"]) == 2


def test_rules_review_can_remove_absent_delegate_without_changing_original_actions(endpoints):
    original = request()
    original["allowed_actions"].remove("request_deep_reasoning")
    client, host = endpoints["build"](answer=jev(confidence=.2,
        probabilities={"keep_reserved_return": .6, "select_next_orbit": .4}), deep_answer=deep())
    client.begin(original)
    result = pump(client, host)
    assert result["action"] == "select_next_orbit"
    assert json.loads(endpoints["calls"][1][1]["messages"][1]["content"])["allowed_actions"] == original["allowed_actions"]


def test_opt_in_host_rejects_v1_frame_downgrade_before_any_assessment(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep())
    original = request()
    frame = {"schema": pipe.REQUEST_FRAME, **client.binding, "sequence": 1,
        "request_id": original["request_id"], "request_sha256": pipe._digest(original),
        "request": original, "started_wall_s": 100., "wall_deadline_s": 280.}
    endpoints["client_writer"].send(frame)
    with pytest.raises(ValueError, match="frame_binding_mismatch"):
        host.tick()
    assert endpoints["calls"] == []


def test_required_review_cannot_add_deep_attempt_to_high_confidence_no_review_result(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep())
    original = request()
    client.begin(original)
    result = pump(client, host)
    first = result["attempts"][0]["judgment"]["provider_receipt"]
    first.update(native_choice_confidence=.9, native_choice_probabilities={
        "keep_reserved_return": .7, "select_next_orbit": .2, "request_deep_reasoning": .1})
    with pytest.raises(ValueError, match="not_required_or_epoch_extended"):
        pipe._validate_result(result, original, return_review_policy=DEFAULT_POLICY)


def test_opt_in_host_rejects_wrong_policy_digest_before_any_assessment(endpoints):
    client, host = endpoints["build"](answer=jev(), deep_answer=deep())
    original = request()
    frame = {"schema": pipe.REQUEST_FRAME_V2, **client.binding, "sequence": 1,
        "request_id": original["request_id"], "request_sha256": pipe._digest(original),
        "request": original, "started_wall_s": 100., "wall_deadline_s": 280.}
    frame["return_review_policy_sha256"] = "d"*64
    endpoints["client_writer"].send(frame)
    with pytest.raises(ValueError, match="frame_binding_mismatch"):
        host.tick()
    assert endpoints["calls"] == []
