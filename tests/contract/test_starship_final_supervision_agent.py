"""Supplemental host judgments; synthetic/loopback receipts, zero native calls."""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from src.intelligence import starship_final_supervision_agent as agent


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def request(phase="booster", *, identity="booster-1", window=180.0):
    return agent.decision_request(
        identity,
        phase,
        500.0,
        500.0 + window,
        scope_sha256="a" * 64,
        facts={
            "time_s": 500.0,
            "source_time_s": 499.75,
            "available_time_s": 500.0,
            "altitude_m": 71000.0,
            "ground_speed_mps": 1400.0,
            "propellant_kg": 260000.0,
            "body_rate_rad_s": 0.01,
            **(
                {
                    "tower_ready": True,
                    "recovery_control_available": True,
                    "observed_available_engine_count": 32,
                }
                if phase == "booster"
                else {"deployment_allowed": True}
            ),
        },
    )


def jev(choice="splashdown", phase="booster", *, delegate=True):
    actions = list(agent.ACTIONS[phase]) + (
        [agent.DELEGATE] if phase == "booster" and delegate else []
    )
    return {
        "model": agent.JEV_MODEL,
        "answers": {
            "decision": {
                "type": "choice",
                "choice": choice,
                "probabilities": {action: float(action == choice) for action in actions},
                "confidence": 0.8,
            }
        },
    }


def deep(action="splashdown", refs=None):
    return {
        "model": agent.DEEPSEEK_MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "action": action,
                            "summary": "Judge current observations within the available recovery scope.",
                            "reference_ids": refs or ["propellant_kg", "history.supported_catches"],
                        }
                    ),
                },
            }
        ],
    }


class FixtureTransport:
    def __init__(self, answers, *, after=None):
        self.answers, self.calls, self.sent, self.after = list(answers), 0, [], after

    def _invoke(self, provider, payload, receipt, decode):
        self.sent.append(deepcopy(payload))
        self.calls += 1
        try:
            result, model = decode(self.answers.pop(0))
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
            receipt["status"] = "invalid_response"
            return None
        receipt.update(status="succeeded", model_id=model)
        if self.after:
            self.after(receipt)
        return result


def assess(answer, *, phase="booster", window=180.0, clock=None, **kw):
    clock = Clock() if clock is None else clock
    transport = FixtureTransport([answer])
    judge = agent.FinalSupervisionAgent(transport=transport, clock=clock, **kw)
    return judge.assess(
        request(phase, window=window), remaining_s=window, outer_deadline_monotonic=100.0 + window
    ), transport


@pytest.mark.parametrize(
    "phase,choice",
    [
        ("ascent", "continue_mission"),
        ("ascent", "withhold_deployment"),
        ("booster", "splashdown"),
        ("booster", "inhibit_recovery"),
        ("booster", agent.DELEGATE),
    ],
)
def test_finite_choices_stay_fixture_judgments_not_execution(phase, choice):
    result, transport = assess(jev(choice, phase), phase=phase)
    assert result["status"] == "fixture_valid" and result["action"] == choice
    assert (
        result["model_inference_invoked"] is False
        and result["execution_permission_granted"] is False
    )
    assert result["provider_receipt"]["mode"] == "fixture"
    assert result["provider_receipt"]["invocation_kind"] == "not_invoked"
    assert result["maximum_instance_calls"] is None and not result["automatic_retry"]
    assert transport.calls == 1


def test_explicit_booster_delegation_keeps_same_original_epoch_and_exact_model():
    clock = Clock()
    transport = FixtureTransport([jev(agent.DELEGATE), deep()])
    judge = agent.FinalSupervisionAgent(transport=transport, clock=clock)
    value = request()
    first = judge.assess(value, remaining_s=180.0, outer_deadline_monotonic=280.0)
    assert first["action"] == agent.DELEGATE
    clock.value += 0.25
    second = judge.assess(
        value, provider="deepseek", remaining_s=179.75, outer_deadline_monotonic=280.0
    )
    assert second["status"] == "fixture_valid" and second["action"] == "splashdown"
    assert second["provider_receipt"]["request_model"] == agent.DEEPSEEK_MODEL == "deepseek-flash"
    assert second["provider_receipt"]["original_deadline_monotonic"] == 280.0
    assert second["provider_receipt"]["decision_deadline_monotonic"] == 250.25
    assert second["references"] == [
        {"reference_id": "propellant_kg", "value": 260000.0},
        {"reference_id": "history.supported_catches", "value": 0},
    ]
    assert (
        second["reference_values_verified"] is True
        and second["internal_reasoning_verified"] is False
    )
    public = json.loads(transport.sent[1]["messages"][1]["content"])
    assert agent.DELEGATE not in public["allowed_actions"]
    assert public["evidence"]["authority.catch_allowed"] is False
    assert public["evidence"]["evidence.current_state_catch_qualification_available"] is False
    assert "does not certify" in public["recovery_history"]
    assert not second["free_prose_verified"] and not second["model_inference_invoked"]
    assert len(transport.sent) == 2


@pytest.mark.parametrize(
    "change",
    ["no_delegation", "renewed_epoch", "changed_request", "insufficient_remaining", "ascent"],
)
def test_deepseek_cannot_borrow_or_renew_an_undelegated_request(change):
    phase = "ascent" if change == "ascent" else "booster"
    first_answer = (
        jev("continue_mission", "ascent")
        if phase == "ascent"
        else jev("splashdown" if change == "no_delegation" else agent.DELEGATE)
    )
    transport = FixtureTransport([first_answer, deep()])
    clock = Clock()
    judge = agent.FinalSupervisionAgent(transport=transport, clock=clock)
    value = request(phase)
    judge.assess(value, remaining_s=180.0, outer_deadline_monotonic=280.0)
    clock.value += 0.5
    remaining, outer = 179.5, 280.0
    if change == "renewed_epoch":
        outer = 280.25
    elif change == "changed_request":
        value["observation"]["facts"]["propellant_kg"] -= 1
    elif change == "insufficient_remaining":
        remaining = 121.0
    with pytest.raises(ValueError):
        judge.assess(
            value, provider="deepseek", remaining_s=remaining, outer_deadline_monotonic=outer
        )
    assert transport.calls == 1


def test_under150s_booster_does_not_offer_delegation():
    value = request(window=149.0)
    assert value["observation"]["allowed_actions"] == ["splashdown", "inhibit_recovery"]
    value["observation"]["allowed_actions"].append(agent.DELEGATE)
    with pytest.raises(ValueError, match="eligible_actions"):
        agent.validate_request(value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("fault_label", "engine_shutdown"),
        ("physical_factor", 0.8),
        ("true_thrust", 0.8),
        ("seed", 5),
        ("source_sha256", "a" * 64),
        ("r_eci_m", [1.0, 2.0, 3.0]),
        ("engine_states", []),
        ("private_path", "/Users/person/private"),
        ("api_key", "secret"),
    ],
)
def test_truth_private_or_unselected_plant_fields_never_reach_transport(key, value):
    incoming = request()
    incoming["observation"]["facts"][key] = value
    transport = FixtureTransport([jev()])
    with pytest.raises(ValueError):
        agent.FinalSupervisionAgent(transport=transport).assess(
            incoming, remaining_s=180.0, outer_deadline_monotonic=280.0
        )
    assert transport.calls == 0


@pytest.mark.parametrize(
    "change",
    [
        "bool_numeric",
        "nonfinite",
        "negative",
        "stale",
        "future",
        "wrong_time",
        "extra_observation",
        "catch",
        "nested_action",
    ],
)
def test_observation_and_action_contract_rejects_invalid_inputs(change):
    value = request()
    obs, facts = value["observation"], value["observation"]["facts"]
    if change == "bool_numeric":
        facts["propellant_kg"] = True
    elif change == "nonfinite":
        facts["body_rate_rad_s"] = float("nan")
    elif change == "negative":
        facts["body_rate_rad_s"] = -1.0
    elif change == "stale":
        facts["source_time_s"] = 494.9
    elif change == "future":
        facts["available_time_s"] = 500.1
    elif change == "wrong_time":
        facts["time_s"] += 1.0
    elif change == "extra_observation":
        obs["hidden"] = 1.0
    elif change == "catch":
        obs["allowed_actions"].append("tower_catch")
    else:
        obs["allowed_actions"].append(["splashdown"])
    with pytest.raises(ValueError):
        agent.validate_request(value)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("model", "model_mismatch"),
        ("shape", "choice_shape"),
        ("distribution_keys", "choice_or_distribution_keys"),
        ("probability_sum", "probability_sum_or_range"),
        ("bool_probability", "probability_sum_or_range"),
        ("bool_confidence", "probability_sum_or_range"),
        ("choice", "choice_or_distribution_keys"),
    ],
)
def test_jev_receiver_finite_rejections_do_not_retry_or_save_raw_text(change, reason):
    answer = jev()
    decision = answer["answers"]["decision"]
    if change == "model":
        answer["model"] = "private-wrong-model"
    elif change == "shape":
        decision["untrusted"] = "private-output"
    elif change == "distribution_keys":
        decision["probabilities"]["private-output"] = 0.0
    elif change == "probability_sum":
        decision["probabilities"]["splashdown"] = 0.2
    elif change == "bool_probability":
        decision["probabilities"]["splashdown"] = True
    elif change == "bool_confidence":
        decision["confidence"] = True
    else:
        decision["choice"] = "command_engine"
    result, transport = assess(answer)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["provider_receipt"]["validation_error"] == reason
    assert (
        transport.calls == 1
        and "private-output" not in json.dumps(result)
        and "private-wrong-model" not in json.dumps(result)
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("model", "model_mismatch"),
        ("finish", "finish_reason"),
        ("unknown_ref", "answer_shape_or_reference_ids"),
        ("duplicate_ref", "answer_shape_or_reference_ids"),
        ("delegate", "answer_shape_or_reference_ids"),
        ("extra", "answer_shape_or_reference_ids"),
        ("json", "answer_json"),
    ],
)
def test_deep_receiver_keeps_rejections_and_drops_hidden_reasoning(change, reason):
    answer = deep()
    message = answer["choices"][0]["message"]
    message["reasoning_content"] = "private-internal-transcript"
    if change == "model":
        answer["model"] = "deepseek-v4-flash"
    elif change == "finish":
        answer["choices"][0]["finish_reason"] = "length"
    elif change == "json":
        message["content"] = "private-invalid-json"
    else:
        content = json.loads(message["content"])
        if change == "unknown_ref":
            content["reference_ids"] = ["true_thrust"]
        elif change == "duplicate_ref":
            content["reference_ids"] = ["time_s", "time_s"]
        elif change == "delegate":
            content["action"] = agent.DELEGATE
        else:
            content["private-field"] = "do-not-record"
        message["content"] = json.dumps(content)
    transport = FixtureTransport([jev(agent.DELEGATE), answer])
    judge = agent.FinalSupervisionAgent(transport=transport, clock=Clock())
    value = request()
    judge.assess(value, remaining_s=180.0, outer_deadline_monotonic=280.0)
    result = judge.assess(
        value, provider="deepseek", remaining_s=180.0, outer_deadline_monotonic=280.0
    )
    assert result["action"] is None and result["references"] == []
    assert result["provider_receipt"]["validation_error"] == reason
    assert all(
        word not in json.dumps(result)
        for word in ("private-internal-transcript", "do-not-record", "private-invalid-json")
    )
    assert transport.calls == 2


def test_valid_deep_external_summary_does_not_retain_internal_transcript():
    value = deep()
    value["choices"][0]["message"]["reasoning_content"] = "private-internal-transcript"
    transport = FixtureTransport([jev(agent.DELEGATE), value])
    judge = agent.FinalSupervisionAgent(transport=transport, clock=Clock())
    judge.assess(request(), remaining_s=180.0, outer_deadline_monotonic=280.0)
    result = judge.assess(
        request(), provider="deepseek", remaining_s=180.0, outer_deadline_monotonic=280.0
    )
    assert result["status"] == "fixture_valid" and "private-internal-transcript" not in json.dumps(
        judge.record()
    )


def test_same_request_never_resends_but_distinct_ids_have_no_permanent_call_cap():
    transport = FixtureTransport([jev() for _ in range(8)])
    judge = agent.FinalSupervisionAgent(transport=transport, clock=Clock())
    for index in range(8):
        value = request(identity=f"booster-{index}")
        assert (
            judge.assess(value, remaining_s=180.0, outer_deadline_monotonic=280.0)["status"]
            == "fixture_valid"
        )
        with pytest.raises(ValueError, match="already_consumed"):
            judge.assess(value, remaining_s=180.0, outer_deadline_monotonic=280.0)
    assert transport.calls == 8 and judge.record()["maximum_instance_calls"] is None


@pytest.mark.parametrize("enabled,authorized", [(False, False), (False, True), (True, False)])
def test_live_requires_feature_opt_in_and_exact_request_authorization(
    monkeypatch, enabled, authorized
):
    monkeypatch.setenv(agent.LIVE_OPT_IN_ENV, "1" if enabled else "0")
    transport, seen = FixtureTransport([jev()]), []

    def permission(value, provider):
        seen.append((value, provider))
        return authorized

    judge = agent.FinalSupervisionAgent(
        mode="live", transport=transport, live_authorized=permission, clock=Clock()
    )
    with pytest.raises(ValueError, match="live_authorization_required"):
        judge.assess(request(), remaining_s=180.0, outer_deadline_monotonic=280.0)
    assert transport.calls == 0
    if enabled:
        assert seen == [(request(), "typesafe")]


def test_synthetic_decoder_cannot_claim_live_inference(monkeypatch):
    monkeypatch.setenv(agent.LIVE_OPT_IN_ENV, "1")
    result, transport = assess(jev(), mode="live", live_authorized=lambda _r, _p: True)
    assert result["status"] == "no_valid_judgment" and result["model_inference_invoked"] is False
    assert result["provider_receipt"]["validation_error"] == "incomplete_native_receipt"
    assert transport.calls == 1


def test_fixture_cannot_gain_live_claim_even_if_injected_transport_sets_flags():
    def forged(receipt):
        receipt.update(
            mode="live",
            invocation_kind="decision_api",
            call_attempted=True,
            call_succeeded=True,
            hosted_model_attempted=True,
            model_inference_invoked=True,
        )

    transport = FixtureTransport([jev()], after=forged)
    result = agent.FinalSupervisionAgent(transport=transport, clock=Clock()).assess(
        request(), remaining_s=180.0, outer_deadline_monotonic=280.0
    )
    assert result["status"] == "fixture_valid" and not result["model_inference_invoked"]
    receipt = result["provider_receipt"]
    assert receipt["mode"] == "fixture" and receipt["invocation_kind"] == "not_invoked"
    assert all(
        receipt[field] is False
        for field in (
            "call_attempted",
            "call_succeeded",
            "hosted_model_attempted",
            "model_inference_invoked",
        )
    )


def test_expired_original_epoch_and_late_answer_do_not_adopt_or_retry():
    clock = Clock()
    transport = FixtureTransport([jev()], after=lambda _: setattr(clock, "value", 135.0))
    judge = agent.FinalSupervisionAgent(transport=transport, clock=clock)
    result = judge.assess(request(), remaining_s=180.0, outer_deadline_monotonic=280.0)
    assert (
        result["action"] is None
        and result["provider_receipt"]["validation_error"] == "response_after_deadline"
    )
    with pytest.raises(ValueError):
        judge.assess(request(), remaining_s=145.0, outer_deadline_monotonic=280.0)
    assert transport.calls == 1


@contextmanager
def fixture_http(answer):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            assert self.headers.get("Authorization") is None
            observed.append(json.loads(self.rfile.read(size)))
            wire = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Transport:
        def _invoke(self, provider, payload, receipt, decode):
            wire = json.dumps(payload).encode()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2.0)
            try:
                connection.request(
                    "POST", "/fixture", wire, headers={"Content-Type": "application/json"}
                )
                response = connection.getresponse()
                raw = response.read(65537)
                assert response.status == 200 and len(raw) <= 65536
            finally:
                connection.close()
            result, model = decode(json.loads(raw))
            receipt.update(
                status="succeeded",
                model_id=model,
                request_sha256=sha256(wire).hexdigest(),
                response_sha256=sha256(raw).hexdigest(),
                response_received=True,
                complete_response_observed=True,
                request_bytes=len(wire),
                response_bytes_read=len(raw),
            )
            return result

    try:
        yield Transport(), observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.e2e
def test_real_loopback_fixture_serialization_receiver_and_provenance_boundary():
    with fixture_http(jev("withhold_deployment", "ascent")) as (transport, observed):
        value = request("ascent", window=35.0)
        result = agent.FinalSupervisionAgent(transport=transport, clock=Clock()).assess(
            value, remaining_s=35.0, outer_deadline_monotonic=135.0
        )
    assert len(observed) == 1 and observed[0]["model"] == agent.JEV_MODEL
    assert observed[0]["state"] == agent.public_input(value, "typesafe")
    assert result["action"] == "withhold_deployment" and result["status"] == "fixture_valid"
    assert (
        result["provider_receipt"]["request_bytes"] > 0
        and result["provider_receipt"]["response_bytes_read"] > 0
    )
    assert (
        result["provider_receipt"]["call_attempted"] is False
        and result["model_inference_invoked"] is False
    )
    assert not result["execution_permission_granted"]
