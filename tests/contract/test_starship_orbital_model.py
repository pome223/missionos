"""Exercise real model framing/decoding with fake transport, never hosted IO."""

from copy import deepcopy
import json

import pytest

from src.intelligence import starship_replanning as host
from src.runtime.starship_replanning import contract, digest, MODE_ENV
from src.runtime.starship_orbital_window import decision_window
from tests.contract.test_starship_comparison_models import request
from tests.contract.test_starship_protocol_campaign import response_for


def reserve_provider(authority, value, provider, used_slots=0):
    value["provider_budget"] = {
        "provider": provider, "used_slots": used_slots,
        "maximum_slots": authority["maximum_llm_calls" if provider == "deepseek" else "maximum_jev_calls"],
        "generation_sha256": value["comparison_context"]["generation_sha256"] if provider == "deepseek" else None,
        "previous_reservations_sha256": digest([]),
    }


def orbital_request():
    authority = contract("live", "comparison_orbital_update")
    value = request(authority)
    value["allowed_actions"].append("select_nominal")
    for name, candidate in value["candidates"].items():
        candidate["return_time_s"] = 2281.0 if name == "nominal" else 7732.0
    value["planning_window"] = decision_window(authority, value)
    value["deadline_s"] = value["time_s"] + value["planning_window"]["duration_s"]
    reserve_provider(authority, value, "deepseek")
    return authority, value


def test_new_contract_is_explicit_and_legacy_unchanged():
    old = contract("live", "comparison_update")
    new, context = orbital_request()
    assert "orbital_planning_window" not in old
    assert old["decision_timeout_s"] == new["decision_timeout_s"] == 35
    assert old["maximum_llm_calls"] == 2 and new["maximum_llm_calls"] is None
    assert context["planning_window"]["admitted"]
    assert context["deadline_s"] == 1850
    host.validate(context, new)


def test_real_decoder_resolves_ids_once_and_preserves_no_raw_reasoning(monkeypatch):
    authority, context = orbital_request()
    monkeypatch.setenv(MODE_ENV, "live")
    agent = host.ReplanningAgent(authority, lambda: True)
    calls = []

    def invoke(provider, payload, receipt, decode, **kwargs):
        calls.append((provider, payload, kwargs))
        assert provider == "deepseek"
        assert kwargs["timeout_s"] == 120 and kwargs["active"]()
        action, model = decode(json.loads(response_for(payload)))
        receipt.update(call_attempted=True, call_succeeded=True, model_inference_invoked=True)
        return action

    monkeypatch.setattr(agent.transport, "_invoke", invoke)
    response = agent.assess(context)
    assert response["action"] == "keep_plan"
    assert response["llm_invocation"]["reference_protocol"] == "ids_orbital_low_16384"
    basis = response["decision_basis"]
    assert set(basis) == {"summary", "compared_candidate_ids", "reference_ids"}
    from src.runtime.starship_reference_ids import verify_reference_ids
    assert verify_reference_ids(context, basis["reference_ids"], basis["compared_candidate_ids"])["status"] == "valid"
    assert "raw-reasoning" not in json.dumps(response)
    payload = calls[0][1]
    assert payload["max_tokens"] == 16384 and payload["reasoning_effort"] == "low"
    assert json.loads(payload["messages"][1]["content"])["planning_window"] == context["planning_window"]


def test_deadline_near_booking_never_calls_long_model(monkeypatch):
    authority, context = orbital_request()
    context["time_s"] = 2220.0
    context["planning_window"] = decision_window(authority, context)
    context["deadline_s"] = context["time_s"] + context["planning_window"]["duration_s"]
    reserve_provider(authority, context, "jev")
    assert not context["planning_window"]["admitted"]
    monkeypatch.setenv(MODE_ENV, "live")
    agent = host.ReplanningAgent(authority, lambda: True)
    calls = []

    def invoke(provider, payload, receipt, decode, **kwargs):
        calls.append(provider)
        assert kwargs["active"]() is True
        assert "timeout_s" not in kwargs
        assert "deep_reasoning" not in payload["questions"]["decision"]["criteria"]
        return None

    monkeypatch.setattr(agent.transport, "_invoke", invoke)
    agent.assess(context)
    assert calls == ["typesafe"] and agent.llm_calls == 0


def test_exhausted_deepseek_budget_cannot_spend_jev_for_new_prepared_comparison(monkeypatch):
    authority, context = orbital_request()
    authority = contract("live", "comparison_orbital_update", legacy_call_limits=True)
    context["envelope_sha256"] = digest(authority)
    monkeypatch.setenv(MODE_ENV, "live")
    agent = host.ReplanningAgent(authority, lambda: True)
    agent.llm_calls = authority["maximum_llm_calls"]
    reserve_provider(authority, context, "deepseek", agent.llm_calls)
    monkeypatch.setattr(agent.transport, "_invoke", lambda *_args, **_kwargs: pytest.fail(
        "An exhausted prepared-comparison budget must not invoke either provider."
    ))
    with pytest.raises(ValueError, match="m1_inference_not_authorized"):
        agent.assess(context)
    assert agent.calls == 0 and agent.llm_calls == authority["maximum_llm_calls"]
    assert agent.reasoned_generations == set()


@pytest.mark.parametrize("unsuitable", [
    "not_ready", "too_late", "same_generation", "certain_estimate", "blocked_candidate",
])
def test_exhausted_jev_does_not_force_reasoning_for_unsuitable_request(monkeypatch, unsuitable):
    authority, context = orbital_request()
    monkeypatch.setenv(MODE_ENV, "live")
    agent = host.ReplanningAgent(authority, lambda: True)
    agent.calls = authority["maximum_jev_calls"]
    if unsuitable == "not_ready":
        context["comparison_context"]["ready"] = False
    elif unsuitable == "too_late":
        context["time_s"] = 2220.0
    elif unsuitable == "same_generation":
        agent.reasoned_generations.add(context["comparison_context"]["generation_sha256"])
    elif unsuitable == "certain_estimate":
        context["notice"]["recovery_service_forecast"] = {"east": [1, 1], "west": [2, 2]}
    else:
        context["candidates"]["next_orbit"]["constraint_rejections"] = ["energy"]
    context["planning_window"] = decision_window(authority, context)
    context["deadline_s"] = context["time_s"] + context["planning_window"]["duration_s"]
    reserve_provider(authority, context, "jev", agent.calls)
    if unsuitable != "same_generation":
        assert context["planning_window"]["admitted"] is False
        assert context["planning_window"]["duration_s"] == 35.0
    monkeypatch.setattr(agent.transport, "_invoke", lambda *_args, **_kwargs: pytest.fail(
        "Unused DeepSeek budget does not make this request eligible."
    ))
    with pytest.raises(ValueError, match="m1_inference_not_authorized"):
        agent.assess(context)
    assert agent.calls == authority["maximum_jev_calls"] and agent.llm_calls == 0


def test_forged_extended_window_rejected_before_transport():
    authority, value = orbital_request()
    for field in ("deadline_s", "planning_window"):
        bad = deepcopy(value)
        if field == "deadline_s":
            bad[field] += 1
        else:
            bad[field]["duration_s"] += 1
        with pytest.raises(ValueError):
            host.validate(bad, authority)
    assert value["envelope_sha256"] == digest(authority)


@pytest.mark.parametrize("jev_exhausted", [False, True])
def test_production_orbital_host_over_real_loopback_http(monkeypatch, jev_exhausted):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from src.intelligence import starship_flight_supervisor as transport

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
    monkeypatch.setattr(transport, "DEEPSEEK_ENDPOINT", f"http://127.0.0.1:{server.server_port}/completion")
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-http-stub-only-no-provider-key")
    authority, context = orbital_request()
    agent = host.ReplanningAgent(authority, lambda: True)
    agent.calls = authority["maximum_jev_calls"] if jev_exhausted else 0
    jev_calls_before = agent.calls
    try:
        result = agent.assess(context)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    receipt = result["llm_invocation"]
    assert len(received) == 1
    assert receipt["status"] == "succeeded" and receipt["timeout_seconds"] == 120
    assert receipt["request_sha256"] == digest(received[0])
    assert result["action"] == "keep_plan"
    assert result["decision_basis"]["reference_ids"]
    assert result["routing"]["route"] == "prepared_uncertain_comparison"
    assert result["jev_invocation"]["call_attempted"] is False
    assert result["jev_invocation"]["reserved_call_slot"] is None
    assert receipt["reserved_call_slot"] == 1
    assert context["deadline_s"] - context["time_s"] == 150.0
    assert agent.calls == jev_calls_before and agent.llm_calls == 1
    delegation = json.loads(received[0]["messages"][1]["content"])["delegation"]
    assert delegation["remaining_jev_calls"] == authority["maximum_jev_calls"] - jev_calls_before
    assert delegation["remaining_deepseek_calls"] == authority["maximum_llm_calls"]
    assert delegation["provider_call_budgets_are_independent"] is True


def test_cancelled_host_callback_stays_bound_after_next_request(tmp_path, monkeypatch):
    authority, first = orbital_request()
    second = deepcopy(first)
    second["request_id"] = "next-request"
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    host.publish(mailbox / "request.json", first)
    callbacks = []
    stopped = False

    class Child:
        def poll(self):
            return 0 if stopped else None

    class Agent:
        def __init__(self, *_args):
            pass

        def assess(self, request):
            nonlocal stopped
            if callbacks:
                # The old callback must not rebind to the loop's next request.
                assert callbacks[0]() is False
                assert self.request_active() is True
                stopped = True
            else:
                callbacks.append(self.request_active)
                host.publish(mailbox / "request.json", second)
            return {"request_id": request["request_id"]}

    monkeypatch.setattr(host, "ReplanningAgent", Agent)
    host.serve(mailbox, authority, Child(), lambda: True)
    assert stopped and not (mailbox / "response.json").exists()


def test_cancelled_jev_does_not_block_next_request_over_http(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread
    import time
    from src.intelligence import starship_flight_supervisor as transport

    authority, first = orbital_request()
    first["comparison_context"]["ready"] = False
    first["planning_window"] = decision_window(authority, first)
    first["deadline_s"] = first["time_s"] + 35
    reserve_provider(authority, first, "jev")
    second = deepcopy(first)
    second["request_id"] = "next-jev-request"
    reserve_provider(authority, second, "jev", 1)
    arrived, release, stopped = Event(), Event(), Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(value)
            if len(calls) == 1:
                arrived.set()
                release.wait(5)
            criteria = value["questions"]["decision"]["criteria"]
            answer = {"model": host.JEV_MODEL, "answers": {"decision": {
                "type": "choice", "choice": "keep_plan", "confidence": 0.9,
                "probabilities": {key: float(key == "keep_plan") for key in criteria},
            }}}
            body = json.dumps(answer).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

    class Child:
        def poll(self):
            return 0 if stopped.is_set() else None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http = Thread(target=server.serve_forever, daemon=True)
    http.start()
    monkeypatch.setattr(transport, "JEV_ENDPOINT", f"http://127.0.0.1:{server.server_port}/jev")
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "local-only-stub-key")
    host.publish(tmp_path / "request.json", first)
    broker = Thread(target=host.serve, args=(tmp_path, authority, Child(), lambda: True), daemon=True)
    broker.start()
    try:
        assert arrived.wait(2)
        host.publish(tmp_path / "cancelled.json", {"request_id": first["request_id"], "request_sha256": digest(first)})
        started = time.monotonic()
        host.publish(tmp_path / "request.json", second)
        received = None
        while time.monotonic() - started < 2:
            path = tmp_path / "response.json"
            if path.exists():
                received = json.loads(path.read_text())
                if received.get("request_id") == second["request_id"]:
                    break
            time.sleep(0.01)
        assert received and received["request_id"] == second["request_id"]
        assert len(calls) == 2 and not release.is_set()
        assert received["jev_invocation"]["timeout_seconds"] == 30
        assert received["action"] == "keep_plan"
    finally:
        stopped.set()
        release.set()
        broker.join(timeout=3)
        server.shutdown()
        server.server_close()
        http.join(timeout=3)
