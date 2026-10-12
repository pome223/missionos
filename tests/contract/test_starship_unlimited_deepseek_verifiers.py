"""Approved call-count semantics; no models, network, propagation or source reruns."""
from copy import deepcopy

import pytest

from src.runtime.starship_m2_attempts import authority_call_limits_valid, empty, verify_attempts
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_m2_decision_verifier import verify_decisions
from src.runtime.starship_replanning import digest
from src.runtime.starship_replanning_verifier import decision_budget_reasons
from tests.contract.test_starship_orbital_model import orbital_request


def reservation_rows(limit=None, count=2):
    envelope, source = orbital_request()
    envelope["maximum_llm_calls"] = limit
    rows, prior = [], []
    for index in range(count):
        request = deepcopy(source)
        request["request_id"] = f"run-return-{index}"
        request["envelope_sha256"] = digest(envelope)
        request["comparison_context"]["generation_sha256"] = f"{index + 1:064x}"
        budget = {"provider": "deepseek", "used_slots": index, "maximum_slots": limit,
                  "generation_sha256": request["comparison_context"]["generation_sha256"],
                  "previous_reservations_sha256": digest(prior)}
        request["provider_budget"] = deepcopy(budget)
        rows.append({"request": request, "provider_budget": budget,
                     "request_disposition": "submitted", "response": None,
                     "accepted_action": None, "fallback": True})
        prior.append({"request_id": request["request_id"], "request_sha256": digest(request)})
    return envelope, rows


def host_study(*, count=2, legacy=False, provider="deepseek"):
    authority = m2_contract("live", "orbital_update", legacy_call_limits=legacy)
    _, source = orbital_request()
    envelope = authority["return_authority"]
    ledgers = {phase: empty(authority, "run", phase) for phase in ("deployment", "return")}
    records = []
    for index in range(count):
        request = deepcopy(source)
        request.update(request_id=f"run-return-{index}", envelope_sha256=digest(envelope))
        provider_sha = f"{index + 1:064x}"
        timeout = 120.0 if provider == "deepseek" else 30.0
        receipt = {"call_attempted": True, "request_sha256": provider_sha,
                   "status": "succeeded", "timeout_seconds": timeout,
                   "application_margin_s": 2.0, "retries": 0, "model_inference_invoked": True}
        records.append({"request": request, "response": {
            "llm_invocation" if provider == "deepseek" else "jev_invocation": receipt},
            "accepted_action": "keep_plan"})
        ledgers["return"]["attempts"].append({"sequence": index + 1,
            "request_id": request["request_id"], "request_sha256": digest(request),
            "provider": provider, "provider_request_sha256": provider_sha,
            "timeout_s": timeout, "remaining_s": request["deadline_s"] - request["time_s"],
            "request_file_age_s": 0.0, "status": "succeeded", "completed": True,
            "valid_response": True, "response_published": True})
    return {"authority": authority, "run_id": "run",
            "deployment_run": {"mission_director": {"records": []}},
            "return_study": {"decisions": records}, "provider_attempt_ledger": ledgers,
            "runtime": {"model_calls": count, "actor_record_model_calls": count}}


def counts_for(count, provider="deepseek"):
    return {"deployment_jev": 0, "deployment_llm": 0,
            "return_jev": count if provider == "typesafe" else 0,
            "return_llm": count if provider == "deepseek" else 0}


@pytest.mark.parametrize("legacy", [False, True])
def test_canonical_grant_keeps_explicit_current_or_historical_count_semantics(legacy):
    authority = m2_contract("live", "orbital_update", legacy_call_limits=legacy)
    assert authority_call_limits_valid(authority)
    assert authority["maximum_llm_calls"] == (1 if legacy else None)
    assert authority["maximum_model_calls"] == (23 if legacy else None)


@pytest.mark.parametrize("field,value", [
    ("maximum_model_calls", 23), ("maximum_llm_calls", 1), ("maximum_jev_calls", None),
    ("maximum_jev_calls", True), ("maximum_model_calls", True),
    ("maximum_model_calls", float("inf")), ("maximum_llm_calls", -1),
])
def test_unlimited_is_not_inferred_from_malformed_or_mismatched_outer_grant(field, value):
    authority = m2_contract("live", "orbital_update")
    authority[field] = value
    assert not authority_call_limits_valid(authority)


@pytest.mark.parametrize("mutation", ["missing", "fixture", "no_policy", "null_jev", "finite_aggregate"])
def test_nested_null_is_only_an_explicit_live_deepseek_grant(mutation):
    authority = m2_contract("live", "orbital_update")
    returning = authority["return_authority"]
    if mutation == "missing":
        del returning["maximum_llm_calls"]
    elif mutation == "fixture":
        authority["mode"] = returning["mode"] = authority["deployment_authority"]["mode"] = "fixture"
    elif mutation == "no_policy":
        del returning["provider_budget_policy"]
    elif mutation == "null_jev":
        returning["maximum_jev_calls"] = None
    else:
        authority["maximum_model_calls"] = 1000
    assert not authority_call_limits_valid(authority)


def test_second_fresh_deepseek_reservation_is_valid_without_spending_jev_slots():
    envelope, rows = reservation_rows()
    assert decision_budget_reasons(rows, envelope["maximum_jev_calls"], envelope=envelope) == []
    assert [row["provider_budget"]["used_slots"] for row in rows] == [0, 1]


def test_unlimited_deepseek_cannot_claim_budget_exhaustion():
    envelope, rows = reservation_rows()
    rows[1]["request_disposition"] = "budget_exhausted"
    assert "invalid_budget_exhaustion" in decision_budget_reasons(rows, 16, envelope=envelope)


@pytest.mark.parametrize("mutation", ["null_under_finite", "missing", "missing_grant", "boolean", "fixture", "same_generation"])
def test_reservation_cannot_expand_or_disguise_approved_deepseek_limit(mutation):
    envelope, rows = reservation_rows()
    if mutation == "null_under_finite":
        envelope["maximum_llm_calls"] = 1
    elif mutation == "missing":
        del rows[1]["provider_budget"]["maximum_slots"]
    elif mutation == "missing_grant":
        del envelope["maximum_llm_calls"]
    elif mutation == "boolean":
        rows[1]["provider_budget"]["maximum_slots"] = True
    elif mutation == "fixture":
        envelope["mode"] = "fixture"
    else:
        rows[1]["request"]["comparison_context"]["generation_sha256"] = rows[0]["request"]["comparison_context"]["generation_sha256"]
    assert decision_budget_reasons(rows, 16, envelope=envelope)


def test_historical_finite_reservations_still_reject_second_dispatch_and_allow_exhaustion():
    envelope, rows = reservation_rows(limit=1)
    assert "model_budget" in decision_budget_reasons(rows, 16, envelope=envelope)
    rows[1]["request_disposition"] = "budget_exhausted"
    assert decision_budget_reasons(rows, 16, envelope=envelope) == []


def test_actor_receipts_and_host_ledger_allow_two_current_deepseek_calls():
    study = host_study()
    result = verify_decisions(study)
    assert result["return_llm"] == result["host_accounting"]["attempted"] == 2
    assert result["host_accounting"]["actor_rules_accepted"] == 2


def test_historical_host_and_actor_finite_limit_remains_enforced():
    study = host_study(count=1, legacy=True)
    assert verify_decisions(study)["return_llm"] == 1
    study = host_study(count=2, legacy=True)
    with pytest.raises(ValueError, match="m2_phase_model_budget"):
        verify_decisions(study)
    with pytest.raises(ValueError, match="m2_host_attempt_budget"):
        verify_attempts(study, counts_for(2))


def test_unlimited_deepseek_does_not_expand_jev_allowance():
    maximum = m2_contract("live", "orbital_update")["return_authority"]["maximum_jev_calls"]
    study = host_study(count=maximum + 1, provider="typesafe")
    with pytest.raises(ValueError, match="m2_phase_model_budget"):
        verify_decisions(study)
    with pytest.raises(ValueError, match="m2_host_attempt_budget"):
        verify_attempts(study, counts_for(maximum + 1, "typesafe"))


@pytest.mark.parametrize("mutation", ["late", "request_hash", "provider_hash", "actor_count", "total_count", "repeat_identity"])
def test_unlimited_calls_preserve_deadlines_identity_and_exact_accounting(mutation):
    study = host_study()
    attempt = study["provider_attempt_ledger"]["return"]["attempts"][1]
    if mutation == "late":
        attempt["remaining_s"] = 2.0
    elif mutation == "request_hash":
        attempt["request_sha256"] = "f" * 64
    elif mutation == "provider_hash":
        attempt["provider_request_sha256"] = "f" * 64
    elif mutation == "actor_count":
        study["runtime"]["actor_record_model_calls"] = 1
    elif mutation == "total_count":
        study["runtime"]["model_calls"] = 1
    else:
        attempt["request_id"] = study["provider_attempt_ledger"]["return"]["attempts"][0]["request_id"]
    with pytest.raises(ValueError):
        verify_attempts(study, counts_for(2))


def test_new_event_normal_null_cap_is_canonical_and_old_grant_remains_finite():
    from src.runtime.starship_call_limits import canonical_uncapped_jev_escalations
    current = m2_contract("live", "release_fault")
    historical = m2_contract("live", "release_fault", legacy_call_limits=True)
    assert authority_call_limits_valid(current) and authority_call_limits_valid(historical)
    assert current["maximum_llm_calls"] is None and historical["maximum_llm_calls"] == 2
    assert canonical_uncapped_jev_escalations(current["return_authority"])
    assert decision_budget_reasons([], 16, envelope=current["return_authority"]) == []
    assert not canonical_uncapped_jev_escalations(historical["return_authority"])


@pytest.mark.parametrize("mutation", ["missing", "marker_missing", "fake_marker", "deadline", "jev", "old_null"])
def test_event_normal_unlimited_cannot_be_inferred_or_widen_authority(mutation):
    current = m2_contract("live", "release_fault")
    envelope = current["return_authority"]
    if mutation == "missing":
        del envelope["maximum_llm_calls"]
    elif mutation == "marker_missing":
        del envelope["deepseek_call_budget_policy"]
    elif mutation == "fake_marker":
        envelope["deepseek_call_budget_policy"] = "arbitrary"
    elif mutation == "deadline":
        envelope["decision_timeout_s"] = 999.
    elif mutation == "jev":
        envelope["maximum_jev_calls"] = None
    else:
        envelope = m2_contract("live", "release_fault", legacy_call_limits=True)["return_authority"]
        envelope["maximum_llm_calls"] = None
        current["return_authority"] = envelope
    assert not authority_call_limits_valid(current)
    assert decision_budget_reasons([], 16, envelope=envelope)


def test_event_normal_multiple_escalations_keep_jev_and_original_deadline():
    study = host_study(count=3)
    authority = m2_contract("live", "release_fault")
    study["authority"] = authority
    for phase, ledger in study["provider_attempt_ledger"].items():
        ledger["authority_sha256"] = digest(authority)
    for record, attempt in zip(study["return_study"]["decisions"], study["provider_attempt_ledger"]["return"]["attempts"]):
        request = record["request"]
        request["envelope_sha256"] = digest(authority["return_authority"])
        request["deadline_s"] = request["time_s"]+35.
        request.pop("planning_window", None)
        record["response"]["llm_invocation"]["timeout_seconds"] = 30.
        attempt.update(timeout_s=30., remaining_s=35., request_sha256=digest(request))
    assert verify_attempts(study, counts_for(3))["attempted"] == 3
    assert verify_decisions(study)["return_llm"] == 3
    study["provider_attempt_ledger"]["return"]["attempts"][0]["timeout_s"] = 31.
    with pytest.raises(ValueError):
        verify_attempts(study, counts_for(3))
