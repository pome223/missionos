"""Malformed provider output yields bounded categories; history stays evidence-bound."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.intelligence import starship_flight_supervisor as transport
from src.intelligence import starship_replanning as host
from src.runtime.starship_model_diagnostics import summarize_model_diagnostics
from src.runtime.starship_replanning import MODE_ENV, contract, digest, notices


def _request(envelope):
    return {
        "schema": "missionos.m1_decision.v1", "request_id": "diagnostics-request",
        "envelope_sha256": digest(envelope), "revision": 1, "time_s": 1700.,
        "deadline_s": 1735., "stage": "event_reassessment",
        "notice": notices("comparison_update", 1700), "resources": {},
        "selected": "nominal", "allowed_actions": ["keep_plan", "select_next_orbit"],
        "context_generation": 1, "trigger_event_ids": ["event-1"],
        "candidates": {"nominal": {"constraint_rejections": [], "comparison_material": {}}},
        "comparison_context": {"generation_sha256": "a" * 64, "ready": False},
    }


def _answer():
    return {"model": host.JEV_MODEL, "answers": {"decision": {
        "type": "choice", "choice": "keep_plan",
        "probabilities": {"keep_plan": .7, "select_next_orbit": .3}, "confidence": .8,
    }}}


def _assess(monkeypatch, body):
    monkeypatch.setenv(MODE_ENV, "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "never-persist-this-test-secret")
    calls = []

    def send(*_):
        calls.append(True)
        return "response", body

    monkeypatch.setattr(transport, "_send_once", send)
    envelope = contract("live", "comparison_update")
    result = host.ReplanningAgent(envelope, lambda: True).assess(_request(envelope))
    assert len(calls) == 1
    return result


@pytest.mark.parametrize(("field", "value", "category"), [
    ("model", "never-persist-this-test-secret", "model_mismatch"),
    ("answers", [], "answer_shape"),
    ("decision", None, "answer_shape"),
    ("type", "free-text", "answer_type"),
    ("choice", {"untrusted": "never-persist-this-test-secret"}, "choice"),
    ("choice", "unauthorized", "choice"),
    ("probabilities", [], "probability_keys"),
    ("probabilities", {"keep_plan": 1.}, "probability_keys"),
    ("probabilities", {"keep_plan": True, "select_next_orbit": 0}, "probability_values"),
    ("probabilities", {"keep_plan": "bad", "select_next_orbit": .3}, "probability_values"),
    ("probabilities", {"keep_plan": .7, "select_next_orbit": .7}, "probability_sum"),
    ("confidence", True, "confidence"),
    ("confidence", -1., "confidence"),
    ("confidence", 10**400, "confidence"),
    ("probabilities", {"keep_plan": 10**400, "select_next_orbit": 0}, "probability_values"),
])
def test_jev_decoder_classifies_without_recording_provider_text(monkeypatch, field, value, category):
    answer = _answer()
    if field in ("model", "answers"):
        answer[field] = value
    elif field == "decision":
        answer["answers"][field] = value
    else:
        answer["answers"]["decision"][field] = value
    result = _assess(monkeypatch, json.dumps(answer).encode())
    receipt = result["jev_invocation"]
    assert receipt["status"] == "invalid_response"
    assert receipt["validation_error"] == category
    assert receipt["call_attempted"] is True
    assert receipt["model_inference_invoked"] is False
    assert result["action"] is None and result["llm_invocation"]["call_attempted"] is False
    assert "never-persist-this-test-secret" not in json.dumps(result)


@pytest.mark.parametrize(("body", "category"), [
    (b"{", "json_decode"), (b"\xff", "json_decode"),
    (b'{"model":"a","model":"b"}', "json_decode"),
    (b'{"confidence":NaN}', "json_decode"),
    (b"[]", "response_shape"), (b"null", "response_shape"), (b"42", "response_shape"),
])
def test_transport_json_failures_have_fixed_cause(monkeypatch, body, category):
    result = _assess(monkeypatch, body)
    receipt = result["jev_invocation"]
    assert receipt["status"] == "invalid_response" and receipt["validation_error"] == category
    assert result["action"] is None


def test_valid_jev_response_retains_existing_authority_boundary(monkeypatch):
    result = _assess(monkeypatch, json.dumps(_answer()).encode())
    receipt = result["jev_invocation"]
    assert result["action"] == "keep_plan" and receipt["status"] == "succeeded"
    assert receipt["model_inference_invoked"] is True
    assert receipt["approval_granted"] is False and receipt["dispatch"] is False
    assert "validation_error" not in receipt


def _receipt(status, *, attempted=True, reason=None):
    receipt = {"status": status, "call_attempted": attempted,
               "call_succeeded": status == "succeeded",
               "model_inference_invoked": status == "succeeded"}
    if reason is not None:
        receipt["validation_error"] = reason
    return receipt


def _study(receipts, *, source="a"):
    return {"source_sha256": {"safe-file.py": source * 64},
            "decisions": [{"response": {"jev_invocation": row}} for row in receipts]}


def test_cumulative_deduplicates_preserves_unknown_and_separates_preflight_failure():
    old = _study([_receipt("succeeded"), _receipt("invalid_response")])
    new = _study([
        _receipt("invalid_response", reason="probability_sum"),
        _receipt("transport_timeout"), _receipt("credential_unavailable", attempted=False),
        _receipt("not_routed", attempted=False),
    ], source="b")
    new["decisions"].append({"response": None})
    summary = summarize_model_diagnostics([old, deepcopy(old), new])
    jev = summary["providers"]["jev"]
    assert summary["input_studies"] == 3 and summary["unique_studies"] == 2
    assert summary["duplicate_studies_ignored"] == 1
    assert len({row["source_manifest_sha256"] for row in summary["evidence"]}) == 2
    assert jev["attempts"] == 4 and jev["valid_responses"] == 1 and jev["failed_attempts"] == 3
    assert jev["failure_reason_counts"] == {"probability_sum": 1, "transport_timeout": 1, "unknown": 1}
    assert jev["blocked_before_attempt_counts"] == {"credential_unavailable": 1}
    assert summary["missing_decision_responses"] == 1
    assert summary["historical_unknown_causes_inferred"] is False


def test_unsafe_error_strings_and_inconsistent_success_are_not_trusted():
    study = _study([_receipt("invalid_response", reason="NEVER_OUTPUT_SECRET"),
                    _receipt("NEVER_OUTPUT_SECRET")])
    forged = _receipt("succeeded")
    forged["call_succeeded"] = False
    study["decisions"].append({"response": {"llm_invocation": forged}})
    summary = summarize_model_diagnostics([study])
    assert "NEVER_OUTPUT_SECRET" not in json.dumps(summary)
    assert summary["providers"]["jev"]["failure_reason_counts"] == {"unknown": 2}
    assert summary["providers"]["deepseek"]["valid_responses"] == 0
    assert summary["providers"]["deepseek"]["failure_reason_counts"] == {"inconsistent_success_flags": 1}


def test_fixture_and_missing_responses_do_not_invent_provider_attempts():
    study = {"decisions": [{"response": {"mode": "fixture", "jev_invocation": None}},
                            {"response": None}]}
    summary = summarize_model_diagnostics([study])
    assert summary["providers"]["jev"]["attempts"] == 0
    assert summary["providers"]["deepseek"]["attempts"] == 0
    assert summary["missing_decision_responses"] == 1


def test_diagnostics_cli_exercises_saved_json_boundary(tmp_path):
    source = tmp_path / "study.json"
    source.write_text(json.dumps(_study([_receipt("invalid_response")])))
    target = tmp_path / "model-diagnostics.json"
    result = subprocess.run([sys.executable, "scripts/summarize_starship_models.py",
                             "--study", str(source), "--study", str(source),
                             "--output", str(target)], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    summary = json.loads(target.read_text())
    assert summary["duplicate_studies_ignored"] == 1
    assert summary["providers"]["jev"]["failure_reason_counts"] == {"unknown": 1}
    assert str(tmp_path) not in target.read_text()


def test_malformed_row_is_not_an_observed_missing_model_reply():
    result = summarize_model_diagnostics([{"decisions": [42, {"response": None}]}])
    assert result["malformed_records_or_receipts"] == 1
    assert result["missing_decision_responses"] == 1
