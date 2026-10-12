"""Independent authority-gap, transport and reference audits; no model/physics IO."""

from copy import deepcopy
import json

import pytest

from src.intelligence import starship_reference_protocol as reference
from src.runtime.starship_m2_attempts import provider_timeout_limit
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_replanning_verifier import (
    _expected_planning_window, checksum, orbital_provider_budget_reasons, planning_window_reasons,
)
from src.runtime.starship_reference_ids import catalog_for


def inputs():
    authority = m2_contract("live", "op_scope_01", authority_gap_reasoning=True)
    envelope = authority["return_authority"]
    scope = {"commands": ["select_nominal", "keep_reserved_return"],
             "resource_limits": {"maximum_return_delay_s": 6000.0}}
    notice = {"issued_at_s": 1600.0, "expires_at_s": 3000.0}
    request = {
        "request_id": "authority-gap-0", "time_s": 1700.0, "stage": "orbital_event",
        "resources": {}, "selected": None, "notice": notice,
        "envelope_sha256": checksum(envelope), "trigger_event_ids": ["notice-1"],
        "allowed_actions": ["select_next_orbit", "wait_for_update"],
        "comparison_context": {"ready": True, "generation_sha256": "a" * 64,
                               "missing_candidate_ids": [],
                               "prepared_candidate_ids": ["nominal", "next_orbit"]},
        "active_authority": {
            "schema": "missionos.inflight_active_authority.v2", "contract_sha256": "b" * 64,
            "scope": scope, "scope_sha256": checksum(scope), "scope_revision": 0,
            "amendment_status": "idle", "technical_candidates_are_not_authorization": True,
            "select_next_orbit_requires_candidate_bound_operator_approval": True,
            "request_window": {
                "schema": "missionos.inflight_request_window.v1",
                "simulation_deadline_s": 2200.0, "remaining_simulation_s": 500.0,
                "remaining_wall_s": 600.0, "model_response_reserve_s": 150.0,
                "operator_response_reserve_s": 20.0, "coast_guard_s": 4.002,
                "latest_decision_start_s": 2200.0 - (150.0 + 20.0 + 4.002),
                "request_attempted": False, "trusted_telemetry_available": True,
                "request_available": True, "reason": "request_window_open",
            },
        },
        "candidates": {},
    }
    for name, when, rejections in (("nominal", 2280.0, ["area_or_availability"]),
                                    ("next_orbit", 7731.0, [])):
        candidate_hash = ("c" if name == "nominal" else "d") * 64
        request["candidates"][name] = {
            "candidate_sha256": candidate_hash, "return_time_s": when,
            "constraint_rejections": rejections,
            "comparison_material": {"candidate_sha256": candidate_hash,
                "notice_sha256": checksum(notice), "return_time_s": when,
                "waiting_at_burn": {"energy_remaining_kwh": 12.9}},
        }
    request["planning_window"] = {
        "schema": "missionos.orbital_planning_window_decision.v1", "duration_s": 150.0,
        "provider_timeout_s": 120.0, "admitted": True,
        "reason": "authority_gap", "protected_until_s": 2200.0,
    }
    request["deadline_s"] = 1850.0
    return authority, envelope, request


def recorded(envelope, request, *, provider="deepseek", used=0, prior=()):
    reservation = {
        "provider": provider, "used_slots": used,
        "maximum_slots": envelope["maximum_llm_calls" if provider == "deepseek" else "maximum_jev_calls"],
        "generation_sha256": request["comparison_context"]["generation_sha256"] if provider == "deepseek" else None,
        "previous_reservations_sha256": checksum(list(prior)),
    }
    request["provider_budget"] = reservation
    return {"request": request, "provider_budget": deepcopy(reservation),
            "request_disposition": "submitted", "accepted_action": "wait_for_update",
            "response": {"request_sha256": checksum(request),
                         "routing": {"route": "authority_gap" if provider == "deepseek" else "jev"}}}


def test_scalar_window_and_transport_do_not_use_producer_classifier(monkeypatch):
    from src.runtime import starship_orbital_window as producer
    authority, envelope, request = inputs()
    def unexpected(*_args, **_kwargs):
        pytest.fail("Independent verifier called production eligibility")
    monkeypatch.setattr(producer, "decision_window", unexpected)
    monkeypatch.setattr(producer, "prepared_reasoning_eligible", unexpected)
    assert _expected_planning_window(envelope, request) == request["planning_window"]
    assert planning_window_reasons({"request": request}, envelope, 2.0) == []
    assert provider_timeout_limit(authority, "return", "deepseek", request) == 120.0
    assert provider_timeout_limit(authority, "return", "jev", request) == 30.0
    assert provider_timeout_limit(authority, "deployment", "deepseek", request) == 30.0


def test_historical_scope_contract_cannot_acquire_authority_gap_route():
    _authority, _envelope, request = inputs()
    historical = m2_contract("live", "op_scope_01", authority_gap_reasoning=False)
    envelope = historical["return_authority"]
    expected = _expected_planning_window(envelope, request)
    assert expected["duration_s"] == 35.0 and not expected["admitted"]
    assert expected["reason"] == "insufficient_admissible_candidates"
    assert planning_window_reasons({"request": request}, envelope, 2.0)
    with pytest.raises(ValueError, match="m2_orbital_window_request"):
        provider_timeout_limit(historical, "return", "deepseek", request)


@pytest.mark.parametrize("field,value", [
    ("schema", "missionos.authority_gap_reasoning.v2"), ("scope", "all_decisions"),
    ("operator_response_reserve_s", 0.0), ("coast_guard_s", 0.0),
    ("retry_policy", "repeat_until_approval"), ("extra", True),
])
def test_scalar_policy_is_canonical(field, value):
    _authority, envelope, request = inputs()
    envelope["authority_gap_reasoning"][field] = value
    with pytest.raises(ValueError, match="invalid_authority_gap_reasoning_contract"):
        _expected_planning_window(envelope, request)


@pytest.mark.parametrize("field", ["orbital_planning_window", "provider_budget_policy"])
def test_scalar_policy_requires_registered_window_and_reservations(field):
    _authority, envelope, request = inputs()
    del envelope[field]
    with pytest.raises(ValueError, match="invalid_authority_gap_reasoning_contract"):
        _expected_planning_window(envelope, request)


@pytest.mark.parametrize("fault", [
    "not_ready", "generation", "missing", "unprepared", "scope_hash", "already_authorized",
    "revision", "prior_request", "attempted", "telemetry", "approval_flag", "available",
    "deadline", "remaining", "latest_start", "wall", "expired", "model_reserve", "operator_reserve",
    "guard", "unsafe", "allowed", "notice_hash", "candidate_hash", "return_time", "short_burn",
    "notice_expired", "malformed_candidate", "malformed_material", "malformed_authority",
])
def test_false_or_stale_authority_cannot_earn_reasoning_time(fault):
    authority, envelope, request = inputs()
    context, active = request["comparison_context"], request["active_authority"]
    window, candidate = active["request_window"], request["candidates"]["next_orbit"]
    if fault == "not_ready": context["ready"] = False
    elif fault == "generation": context["generation_sha256"] = "forged"
    elif fault == "missing": context["missing_candidate_ids"] = ["next_orbit"]
    elif fault == "unprepared": context["prepared_candidate_ids"] = ["nominal"]
    elif fault == "scope_hash": active["scope_sha256"] = "f" * 64
    elif fault == "already_authorized":
        active["scope"]["commands"].append("select_next_orbit")
        active["scope_sha256"] = checksum(active["scope"])
    elif fault == "revision": active["scope_revision"] = 1
    elif fault == "prior_request": active["amendment_status"] = "awaiting_operator"
    elif fault == "attempted": window["request_attempted"] = True
    elif fault == "telemetry": window["trusted_telemetry_available"] = False
    elif fault == "approval_flag": active["select_next_orbit_requires_candidate_bound_operator_approval"] = False
    elif fault == "available": window["request_available"] = False
    elif fault == "deadline": window["simulation_deadline_s"] += 100.0
    elif fault == "remaining": window["remaining_simulation_s"] += 100.0
    elif fault == "latest_start": window["latest_decision_start_s"] += 100.0
    elif fault == "wall": window["remaining_wall_s"] = 174.0
    elif fault == "expired":
        request["time_s"] = 2201.0
        window["remaining_simulation_s"] = 0.0
    elif fault == "model_reserve": window["model_response_reserve_s"] = 35.0
    elif fault == "operator_reserve": window["operator_response_reserve_s"] = 0.0
    elif fault == "guard": window["coast_guard_s"] = 0.0
    elif fault == "unsafe": candidate["constraint_rejections"] = ["propellant"]
    elif fault == "allowed": request["allowed_actions"].remove("select_next_orbit")
    elif fault == "notice_hash": candidate["comparison_material"]["notice_sha256"] = "f" * 64
    elif fault == "candidate_hash": candidate["comparison_material"]["candidate_sha256"] = "f" * 64
    elif fault == "return_time": candidate["comparison_material"]["return_time_s"] += 1.0
    elif fault == "short_burn":
        candidate["return_time_s"] = candidate["comparison_material"]["return_time_s"] = 1850.0
    elif fault == "notice_expired": request["notice"]["expires_at_s"] = 1700.0
    elif fault == "malformed_candidate": request["candidates"]["nominal"] = None
    elif fault == "malformed_material": candidate["comparison_material"] = None
    else: request["active_authority"] = None
    assert not _expected_planning_window(envelope, request)["admitted"]
    assert planning_window_reasons({"request": request}, envelope, 2.0)
    assert orbital_provider_budget_reasons([recorded(envelope, request)], envelope)
    with pytest.raises(ValueError, match="m2_orbital_window_request"):
        provider_timeout_limit(authority, "return", "deepseek", request)


def test_provider_route_is_audited_and_same_generation_cannot_repeat():
    _authority, envelope, request = inputs()
    first = recorded(envelope, request)
    assert orbital_provider_budget_reasons([first], envelope) == []
    changed = deepcopy(first)
    changed["response"]["routing"]["route"] = "prepared_uncertain_comparison"
    assert "provider_response_reservation" in orbital_provider_budget_reasons([changed], envelope)
    prior = [{"request_id": request["request_id"], "request_sha256": checksum(request)}]
    repeated = deepcopy(request)
    repeated["request_id"] = "authority-gap-1"
    second = recorded(envelope, repeated, provider="jev", prior=prior)
    assert orbital_provider_budget_reasons([first, second], envelope) == []
    repeated = recorded(envelope, repeated, used=1, prior=prior)
    assert "provider_reservation_binding" in orbital_provider_budget_reasons([first, repeated], envelope)


@pytest.mark.parametrize("action", ["wait_for_update", "select_next_orbit"])
def test_production_reference_protocol_accepts_single_candidate_without_forcing_action(action):
    _authority, _envelope, request = inputs()
    public = deepcopy(request)
    public["objective"] = "Evaluate the observed operational choices within the active authority."
    original = {"model": "fixture-model", "messages": [{"role": "system", "content": "fixture"},
                {"role": "user", "content": json.dumps(public)}]}
    policy = reference.policy_for("ids_orbital_low_16384")
    payload = reference.build_payload(request, original, policy=policy)
    ids = [entry["reference_id"] for entry in catalog_for(request)["entries"]
           if entry["candidate_id"] == "next_orbit"]
    answer = {"action": action, "compared_candidate_ids": ["next_orbit"],
              "summary": "Assess the available candidate within the remaining approval window.",
              "reference_ids": ids}
    def audit(value):
        body = json.dumps({"model": payload["model"], "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(value)}}], "usage": {"completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 30}}}).encode()
        return reference.audit_response(request, payload, body, 1.0,
            expected_payload_sha256=checksum(payload), policy=policy)
    accepted = audit(answer)
    assert accepted["status"] == "valid" and accepted["decision"]["action"] == action
    assert accepted["dispatch"] is False
    for fault, value in (("compared_candidate_ids", ["nominal", "next_orbit"]),
                         ("reference_ids", []), ("action", "select_nominal")):
        invalid = deepcopy(answer)
        invalid[fault] = value
        assert audit(invalid)["status"] == "invalid"
