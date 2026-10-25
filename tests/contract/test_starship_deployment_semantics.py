"""Deployment input semantics and real loopback transport; no model or flight."""

from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from src.intelligence import starship_m2_broker as broker
from src.runtime.starship_mission_director import MODE_ENV, contract
from src.runtime.starship_replanning import digest


def request():
    envelope = contract("live")
    observation = {
        "time_s": 480.7, "phase": "orbital_coast", "released_count": 0,
        "release_acknowledged": False, "sequencer_state": "running",
        "fuel_kg": 112500.0, "return_deadline_s": 2280.7,
        "hold_expires_at_s": None, "tower_ready": True, "operations_notice": "",
        "numerical_tools": {
            "orbit_release_feasible": True, "retained_payload_present": True,
            "capture_corridor_certified": False, "mechanism_status": "not_collected",
            "return_feasibility": {
                "within_tested_state_domain": False, "coast_evidence_match": None,
                "return_admitted": False, "return_fuel_margin_kg": 13578.9,
                "bounded_coast_admitted": True, "physical_recovery_certified": False,
            },
        },
    }
    return envelope, {
        "schema": "missionos.starship_director_request.v5",
        "request_id": "semantics-deployment:1", "point": "deployment_start",
        "observation": observation,
        "allowed_actions": ["continue", "hold", "stop_deployment", "collect_status"],
        "decision_deadline_s": 525.45, "envelope_sha256": digest(envelope),
    }


def provider_response(payload, action):
    criteria = payload["questions"]["decision"]["criteria"]
    return json.dumps({
        "model": broker.JEV_MODEL,
        "answers": {"decision": {
            "type": "choice", "choice": action, "confidence": 1.0,
            "probabilities": {key: float(key == action) for key in criteria},
        }},
    }).encode()


@pytest.mark.parametrize("action", ["continue", "hold", "stop_deployment", "collect_status"])
def test_tool_scope_does_not_rewrite_evidence_or_force_an_answer(monkeypatch, action):
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-host-secret-not-state")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-synthetic-secret")
    envelope, req = request()
    original = deepcopy(req)
    captured = []

    def send(_endpoint, wire, _key, **kwargs):
        captured.append((json.loads(wire), kwargs))
        return "response", provider_response(captured[-1][0], action)

    monkeypatch.setattr(broker, "_send_once", send)
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: 7.25, jev_only=True)
    )
    result = agent.assess(req)
    payload, transport = captured[0]
    state = payload["state"]
    assert req == original
    assert state["observation"] == original["observation"]
    assert state["allowed_actions"] == original["allowed_actions"]
    assert result["action"] == action
    assert result["jev_invocation"]["timeout_seconds"] == transport["timeout_s"] == 5.25
    assert result["llm_invocation"] is None
    semantics = state["decision_semantics"]
    assert "outside that sampled return domain" in semantics["observation_meaning"]["return_feasibility"]
    assert "grants no return" in semantics["observation_meaning"]["return_feasibility"]
    assert "not an observed blocked or clear" in semantics["observation_meaning"]["mechanism_status"]
    assert semantics["approved_limits"] == {
        key: envelope[key] for key in (
            "maximum_hold_s", "hold_reassessment_after_s", "hold_expiry",
            "maximum_observation_requests", "minimum_return_fuel_kg",
        )
    }
    assert "not remaining budgets" in semantics["limits_meaning"]
    assert set(state) == {
        "point", "observation", "allowed_actions", "decision_deadline_s", "scope",
        "decision_semantics",
    }
    serialized = broker.canonical_bytes(payload)
    assert b"synthetic-host-secret" not in serialized and b"unused-synthetic-secret" not in serialized
    assert b"scenario" not in serialized and b"future_fault" not in serialized
    assert len(serialized) < broker.MAX_REQUEST_BYTES


def test_semantics_does_not_add_an_ineligible_choice(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "loopback-fixture")
    envelope, req = request()
    req["allowed_actions"] = ["stop_deployment"]
    req["observation"]["sequencer_state"] = "inhibited"
    req["observation"]["numerical_tools"]["mechanism_status"] = "blocked"

    def send(_endpoint, wire, _key, **_kwargs):
        payload = json.loads(wire)
        assert list(payload["questions"]["decision"]["criteria"]) == ["stop_deployment"]
        assert payload["state"]["observation"] == req["observation"]
        return "response", provider_response(payload, "stop_deployment")

    monkeypatch.setattr(broker, "_send_once", send)
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: 7.25, jev_only=True)
    )
    assert agent.assess(req)["action"] == "stop_deployment"


def test_deployment_semantics_crosses_real_loopback_transport(monkeypatch):
    """Exercise production serialization, HTTP, decoder and receipt boundaries."""
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "local-only-fixture-key")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            wire = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(wire)
            body = provider_response(json.loads(wire), "stop_deployment")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(broker, "JEV_ENDPOINT", f"http://127.0.0.1:{server.server_port}/jev")
    envelope, req = request()
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: 7.25, jev_only=True)
    )
    try:
        response = agent.assess(req)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(received) == 1
    payload = json.loads(received[0])
    receipt = response["jev_invocation"]
    assert payload["state"]["observation"] == req["observation"]
    assert response["action"] == "stop_deployment" and receipt["status"] == "succeeded"
    assert receipt["timeout_seconds"] == 5.25 and receipt["retries"] == 0
    assert receipt["request_sha256"] == sha256(received[0]).hexdigest()
    assert receipt["public_input_sha256"] == digest(payload["state"])
    assert receipt["dispatch"] is False and receipt["physical_execution"] is False
