"""Protocol preparation exercises the real request builder without provider IO."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.intelligence import starship_replanning as host
from src.intelligence.starship_protocol_probe import (
    _CaptureOnly, prepare, audit_response,
)
from src.runtime.starship_reference_ids import catalog_for
from src.runtime.starship_replanning import contract, digest, MODE_ENV
from tests.contract.test_starship_comparison_models import request


@pytest.fixture
def saved_study(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "live")
    authority = contract("live", "comparison_update")
    context = request(authority)
    agent = host.ReplanningAgent(authority, lambda: False)
    agent.transport = captured = _CaptureOnly()
    agent.assess(context)
    payload = captured.payload
    from src.intelligence.space_ops_investigator import canonical_bytes
    return {"envelope": authority, "source_sha256": {}, "decisions": [{
        "request": context, "response": {
            "request_sha256": digest(context), "llm_invocation": {
                "request_sha256": digest(payload), "request_bytes": len(canonical_bytes(payload)),
            },
        },
        "later_observation": {"secret_future": "must not reach the model"},
    }]}


def answer_for(bundle):
    return {"action": "keep_plan", "compared_candidate_ids": ["nominal", "next_orbit"],
            "summary": "Keep the earlier booking with more remaining energy.",
            "reference_ids": [r["reference_id"] for r in catalog_for(bundle["request"])["entries"]]}


def provider_body(payload, answer, finish="stop"):
    return json.dumps({"model": payload["model"], "choices": [{"finish_reason": finish,
                       "message": {"content": json.dumps(answer), "reasoning_content": "private"}}],
                       "usage": {"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}}}).encode()


def test_exact_capture_is_offline_and_future_independent(saved_study, monkeypatch):
    monkeypatch.delenv(MODE_ENV)
    monkeypatch.setattr(host.FlightSupervisor, "_invoke", lambda *_a, **_k: pytest.fail("network transport reached"))
    bundle = prepare(saved_study, 0)
    assert bundle["original_wire_verified"]
    assert bundle["provider_calls"] == bundle["flight_runs"] == 0
    assert bundle["proposed_campaign"]["execution_authorized"] is False
    assert bundle["proposed_campaign"]["maximum_attempts"] == 4
    assert bundle["original_provider_request_sha256"] != bundle["variants"][0]["provider_request_sha256"]
    assert "secret_future" not in json.dumps(bundle)
    changed = deepcopy(saved_study)
    changed["decisions"][0]["later_observation"] = {"different_future": "crash"}
    assert prepare(changed, 0) == bundle
    import os
    assert MODE_ENV not in os.environ


@pytest.mark.parametrize("mutation", ["wire_hash", "wire_size", "request", "index"])
def test_original_identity_required(saved_study, mutation):
    decision = saved_study["decisions"][0]
    if mutation == "wire_hash":
        decision["response"]["llm_invocation"]["request_sha256"] = "0" * 64
    elif mutation == "wire_size":
        decision["response"]["llm_invocation"]["request_bytes"] += 1
    elif mutation == "request":
        decision["request"]["resources"]["energy_kwh"] = 100
    with pytest.raises(ValueError):
        prepare(saved_study, -1 if mutation == "index" else 0)


@pytest.mark.parametrize("action", ["keep_plan", "select_next_orbit"])
def test_both_allowed_choices_can_pass_without_numeric_copy(saved_study, action):
    bundle = prepare(saved_study, 0)
    payload = bundle["variants"][0]["payload"]
    answer = answer_for(bundle)
    answer["action"] = action
    audit = audit_response(bundle["request"], payload, provider_body(payload, answer), 1.2, expected_payload_sha256=bundle["variants"][0]["provider_request_sha256"])
    assert audit["status"] == "valid"
    assert audit["reference_audit"]["status"] == "valid"
    assert audit["free_prose_verified"] is False
    assert "private" not in json.dumps(audit)
    assert "numeric_evidence" not in audit["decision"]


@pytest.mark.parametrize("mutation,error", [
    ("limit", "output_limit"), ("late", "decision_deadline"),
    ("json", "malformed_response"), ("partial", "reference_ids"),
    ("stale", "reference_ids"), ("model", "model_mismatch"),
    ("wrong_action", "action"), ("duplicate", "reference_ids"),
    ("bad_budget", "output_budget_exceeded"),
])
def test_rejections_do_not_adopt_partial_answers(saved_study, mutation, error):
    bundle = prepare(saved_study, 0)
    payload = bundle["variants"][0]["payload"]
    answer = answer_for(bundle)
    if mutation == "partial":
        answer["reference_ids"].pop()
    if mutation == "stale":
        old = deepcopy(bundle["request"])
        old["time_s"] += 1
        answer["reference_ids"] = [r["reference_id"] for r in catalog_for(old)["entries"]]
    if mutation == "duplicate":
        answer["reference_ids"].append(answer["reference_ids"][0])
    if mutation == "wrong_action":
        answer["action"] = "fire_engine"
    body = provider_body(payload, answer, "length" if mutation == "limit" else "stop")
    if mutation == "json":
        body = b'{"model":'
    if mutation in ("model", "bad_budget"):
        decoded = json.loads(body)
        if mutation == "model":
            decoded["model"] = "wrong"
        else:
            decoded["usage"]["completion_tokens"] = 8193
        body = json.dumps(decoded).encode()
    audit = audit_response(bundle["request"], payload, body, 36 if mutation == "late" else 1, expected_payload_sha256=bundle["variants"][0]["provider_request_sha256"])
    assert audit["status"] == "invalid"
    assert error in audit["errors"]
    assert audit["decision"] is None


def test_cli_refuses_to_overwrite_evidence(saved_study, tmp_path):
    source = tmp_path / "study.json"
    source.write_text(json.dumps(saved_study))
    output = tmp_path / "probe"
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "scripts/prepare_starship_protocol_probe.py", "--study", str(source),
               "--decision-index", "0", "--output-dir", str(output)]
    first = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    data = (output / "probe.json").read_bytes()
    second = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert second.returncode != 0
    assert (output / "probe.json").read_bytes() == data


@pytest.mark.parametrize("mutation,error", [
    ("payload", "provider_request_mismatch"), ("request", "decision_input_mismatch"),
    ("ids", "reference_catalog_mismatch"), ("usage", "usage_unavailable"),
    ("bad_usage", "usage_unavailable"), ("thinking", "reasoning_usage_unavailable"),
    ("time", "decision_deadline"),
])
def test_send_binding_and_missing_budget_evidence(saved_study, mutation, error):
    bundle = prepare(saved_study, 0)
    context = deepcopy(bundle["request"])
    payload = deepcopy(bundle["variants"][0]["payload"])
    expected_hash = bundle["variants"][0]["provider_request_sha256"]
    answer = answer_for(bundle)
    response = json.loads(provider_body(payload, answer))
    if mutation == "payload":
        payload["messages"][0]["content"] += " altered"
    if mutation == "request":
        context["notice"] = {}
    if mutation == "ids":
        public = json.loads(payload["messages"][1]["content"])
        public["reference_ids"] = {}
        payload["messages"][1]["content"] = json.dumps(public)
        expected_hash = digest(payload)  # Even a newly registered but inconsistent map fails.
    if mutation == "usage":
        response.pop("usage")
    if mutation == "bad_usage":
        response["usage"]["completion_tokens"] = False
    if mutation == "thinking":
        response["usage"].pop("completion_tokens_details")
    result = audit_response(context, payload, json.dumps(response).encode(),
                            10**1000 if mutation == "time" else 1,
                            expected_payload_sha256=expected_hash)
    assert result["status"] == "invalid"
    assert error in result["errors"]
    assert result["decision"] is None
