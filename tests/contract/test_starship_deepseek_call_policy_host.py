"""No hosted calls: repeated reasoning keeps phase deadlines and finite grants."""

import json

import pytest

from src.intelligence import starship_m2_broker as broker
from src.intelligence import starship_replanning as host
from src.runtime.starship_m2_attempts import provider_timeout_limit
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_orbital_window import decision_window
from src.runtime.starship_replanning import MODE_ENV, contract, digest
from tests.contract.test_starship_comparison_models import request
from tests.contract.test_starship_protocol_campaign import response_for


def envelope_with_limit(limit):
    envelope = contract("live", "comparison_orbital_update")
    envelope["maximum_llm_calls"] = limit
    return envelope


def reasoning_request(envelope, index, *, used=None, previous=()):
    value = request(envelope)
    value["request_id"] = f"call-policy-return-{index}"
    value["allowed_actions"].append("select_nominal")
    for name, candidate in value["candidates"].items():
        candidate["return_time_s"] = 2281.0 if name == "nominal" else 7732.0
    value["comparison_context"]["generation_sha256"] = digest({"observed_update": index})
    value["planning_window"] = decision_window(envelope, value)
    value["deadline_s"] = value["time_s"] + value["planning_window"]["duration_s"]
    value["provider_budget"] = {
        "provider": "deepseek", "used_slots": index if used is None else used,
        "maximum_slots": envelope["maximum_llm_calls"],
        "generation_sha256": value["comparison_context"]["generation_sha256"],
        "previous_reservations_sha256": digest(list(previous)),
    }
    return value


def fake_decode_transport(monkeypatch, agent):
    calls = []

    def invoke(provider, payload, receipt, decode, **kwargs):
        assert provider == "deepseek"
        assert kwargs["timeout_s"] == 120.0
        calls.append(payload)
        result, _ = decode(json.loads(response_for(payload)))
        receipt.update(call_attempted=True, call_succeeded=True, model_inference_invoked=True)
        return result

    monkeypatch.setattr(agent.transport, "_invoke", invoke)
    return calls


def test_no_count_cap_allows_new_evidence_after_jev_slots_are_used(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "live")
    envelope = envelope_with_limit(None)
    agent = host.ReplanningAgent(envelope, lambda: True)
    agent.calls = envelope["maximum_jev_calls"]
    calls = fake_decode_transport(monkeypatch, agent)
    for index in range(4):
        result = agent.assess(reasoning_request(envelope, index))
        assert result["action"] == "keep_plan"
        assert result["llm_invocation"]["maximum_instance_calls"] is None
        assert result["llm_invocation"]["reserved_call_slot"] == index + 1
        public = json.loads(calls[-1]["messages"][1]["content"])
        assert public["delegation"]["remaining_deepseek_calls"] is None
        assert public["delegation"]["remaining_jev_calls"] == 0
        assert "no_per_flight_count_cap" in public["delegation"]["deepseek_call_count_policy"]
    assert len(calls) == agent.llm_calls == 4
    assert agent.calls == envelope["maximum_jev_calls"]
    with pytest.raises(ValueError, match="invalid_provider_reservation"):
        agent.assess(reasoning_request(envelope, 3, used=4))
    assert len(calls) == 4


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_explicit_finite_grant_remains_finite(monkeypatch, limit):
    monkeypatch.setenv(MODE_ENV, "live")
    envelope = envelope_with_limit(limit)
    agent = host.ReplanningAgent(envelope, lambda: True)
    calls = fake_decode_transport(monkeypatch, agent)
    for index in range(limit):
        result = agent.assess(reasoning_request(envelope, index))
        assert result["llm_invocation"]["maximum_instance_calls"] == limit
        public = json.loads(calls[-1]["messages"][1]["content"])
        assert "deepseek_call_count_policy" not in public["delegation"]
        assert type(public["delegation"]["remaining_deepseek_calls"]) is int
    with pytest.raises(ValueError, match="m1_inference_not_authorized"):
        agent.assess(reasoning_request(envelope, limit))
    assert len(calls) == agent.llm_calls == limit


@pytest.mark.parametrize("bad_limit", [True, -1, 1.0, "unlimited", float("inf")])
def test_invalid_reservation_limit_never_reaches_transport(bad_limit):
    envelope = envelope_with_limit(None)
    value = reasoning_request(envelope, 0)
    value["provider_budget"]["maximum_slots"] = bad_limit
    with pytest.raises(ValueError, match="invalid_provider_reservation"):
        host.validate(value, envelope)


def test_null_deepseek_limit_cannot_silently_remove_jev_limit():
    envelope = envelope_with_limit(None)
    value = reasoning_request(envelope, 0)
    envelope["maximum_jev_calls"] = None
    value["envelope_sha256"] = digest(envelope)
    value["provider_budget"].update(provider="jev", generation_sha256=None)
    with pytest.raises(ValueError, match="invalid_provider_reservation"):
        host.validate(value, envelope)


def test_existing_deadline_and_evidence_eligibility_still_block_reasoning(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "live")
    envelope = envelope_with_limit(None)
    agent = host.ReplanningAgent(envelope, lambda: True)
    calls = fake_decode_transport(monkeypatch, agent)
    value = reasoning_request(envelope, 0)
    value["time_s"] = 2220.0
    value["planning_window"] = decision_window(envelope, value)
    value["deadline_s"] = value["time_s"] + value["planning_window"]["duration_s"]
    assert value["planning_window"]["admitted"] is False
    with pytest.raises(ValueError, match="invalid_provider_reservation"):
        agent.assess(value)
    assert calls == []


@pytest.mark.parametrize("limit", [None, 1])
def test_host_ledger_preserves_finite_cap_and_duplicate_request_guard(tmp_path, limit):
    authority = {"return_authority": {"maximum_jev_calls": 2, "maximum_llm_calls": limit}}
    current = {"request_id": "one", "sha256": "a" * 64, "request_age_s": 0.1}
    ledger = broker.HostAttempts(tmp_path, authority, "run", "return", current)
    receipt = {"request_sha256": "b" * 64, "timeout_seconds": 120.0, "remaining_budget_s": 149.0}
    ledger.event("started", "deepseek", receipt)
    with pytest.raises(ValueError, match="host_attempt_budget_exhausted"):
        ledger.event("started", "deepseek", receipt)
    current["request_id"] = "two"
    if limit is None:
        ledger.event("started", "deepseek", receipt)
        assert len(ledger.record["attempts"]) == 2
    else:
        with pytest.raises(ValueError, match="host_attempt_budget_exhausted"):
            ledger.event("started", "deepseek", receipt)
        assert len(ledger.record["attempts"]) == 1


def test_three_reasoning_decisions_cross_real_loopback_and_durable_host_ledger(monkeypatch, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            body = response_for(payload)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(broker, "DEEPSEEK_ENDPOINT", f"http://127.0.0.1:{server.server_port}/completion")
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-loopback-test-not-provider-secret")
    authority = m2_contract("live", "orbital_update")
    envelope = authority["return_authority"]
    assert envelope["maximum_llm_calls"] is None
    assert authority["maximum_model_calls"] is None
    current = {}
    ledger = broker.HostAttempts(tmp_path, authority, "call-policy", "return", current)
    agent = host.ReplanningAgent(envelope, lambda: True)
    agent.calls = envelope["maximum_jev_calls"]
    agent.transport = broker.DeadlineTransport(
        lambda: True, lambda: 149.0, audit=ledger.event,
        approved_provider_timeout_s=lambda provider: provider_timeout_limit(
            authority, "return", provider, current["request"]),
    )
    previous = []
    try:
        for index in range(3):
            value = reasoning_request(envelope, index, previous=previous)
            current.update(request=value, request_id=value["request_id"], sha256=digest(value), request_age_s=1.0)
            result = agent.assess(value)
            assert result["action"] == "keep_plan"
            assert result["llm_invocation"]["call_succeeded"] is True
            assert result["llm_invocation"]["timeout_seconds"] == 120.0
            ledger.published()
            previous.append({"request_id": value["request_id"], "request_sha256": digest(value)})
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
    saved = json.loads((tmp_path / "host-attempts.json").read_text())
    assert len(received) == len(saved["attempts"]) == agent.llm_calls == 3
    assert all(row["provider"] == "deepseek" and row["response_published"] and row["valid_response"] for row in saved["attempts"])
    assert all(row["timeout_s"] == 120.0 for row in saved["attempts"])
    assert agent.calls == envelope["maximum_jev_calls"]
    assert all(payload["max_tokens"] == 16384 for payload in received)
