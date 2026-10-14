"""Phase-specific attempt accounting; no provider, credentials or propagation."""

from copy import deepcopy

import pytest

from src.runtime.starship_m2_attempts import empty, provider_timeout_limit, verify_attempts
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_orbital_window import decision_window
from src.runtime.starship_replanning import digest, notices


def request(authority):
    value = {
        "request_id": "run-return-1", "time_s": 1700.0,
        "notice": notices("comparison_orbital_update", 1700.0),
        "selected": "nominal", "comparison_context": {"ready": True},
        "allowed_actions": ["keep_plan", "select_nominal", "select_next_orbit"],
        "candidates": {
            name: {"return_time_s": when, "constraint_rejections": [],
                   "comparison_material": {"waiting_at_burn": {"energy_remaining_kwh": energy}}}
            for name, when, energy in (("nominal", 2280.0, 22.0), ("next_orbit", 7730.0, 12.9))
        },
    }
    value["planning_window"] = decision_window(authority["return_authority"], value)
    value["deadline_s"] = value["time_s"] + value["planning_window"]["duration_s"]
    return value


def evidence(*, provider="deepseek", timeout=120.0, remaining=149.0, age=1.0, legacy=False):
    authority = m2_contract("live", "orbital_update", legacy_call_limits=legacy)
    req = request(authority)
    ledgers = {phase: empty(authority, "run", phase) for phase in ("deployment", "return")}
    # A timed-out actual attempt is retained without inventing an actor receipt.
    ledgers["return"]["attempts"] = [{
        "sequence": 1, "request_id": req["request_id"], "request_sha256": digest(req),
        "provider": provider, "provider_request_sha256": "a" * 64,
        "timeout_s": timeout, "remaining_s": remaining, "request_file_age_s": age,
        "status": "transport_timeout", "completed": True,
        "valid_response": False, "response_published": False,
    }]
    study = {
        "authority": authority, "run_id": "run",
        "deployment_run": {"mission_director": {"records": []}},
        "return_study": {"decisions": [{"request": req, "response": None}]},
        "provider_attempt_ledger": ledgers,
        "runtime": {"model_calls": 1, "actor_record_model_calls": 0},
    }
    counts = {phase + suffix: 0 for phase in ("deployment", "return") for suffix in ("_jev", "_llm")}
    return study, counts


def test_prepared_comparison_grants_only_return_deepseek_long_transport():
    authority = m2_contract("live", "orbital_update")
    req = request(authority)
    assert req["planning_window"]["admitted"] is True
    assert provider_timeout_limit(authority, "return", "deepseek", req) == 120.0
    assert provider_timeout_limit(authority, "return", "typesafe", req) == 30.0
    assert provider_timeout_limit(authority, "deployment", "deepseek", req) == 30.0
    assert provider_timeout_limit(authority, "ascent", "deepseek", req) == 30.0
    assert provider_timeout_limit(authority, "booster", "deepseek", req) == 30.0
    study, counts = evidence()
    result = verify_attempts(study, counts)
    assert result["attempted"] == 1
    assert result["actor_receipts"] == result["host_valid_responses"] == 0


@pytest.mark.parametrize("fault", ["deadline", "claimed_duration", "claimed_provider", "unready", "single",
                                    "certain", "late_opportunity", "expired_notice", "bool_time",
                                    "foreign_case", "expanded_contract"])
def test_long_attempt_requires_independent_current_window_recomputation(fault):
    study, counts = evidence()
    req = study["return_study"]["decisions"][0]["request"]
    authority = study["authority"]
    if fault == "deadline":
        req["deadline_s"] += .1
    elif fault == "claimed_duration":
        req["planning_window"]["duration_s"] += 1
    elif fault == "claimed_provider":
        req["planning_window"]["provider_timeout_s"] = 121.0
    elif fault == "unready":
        req["comparison_context"]["ready"] = False
    elif fault == "single":
        req["candidates"]["next_orbit"]["constraint_rejections"] = ["energy"]
    elif fault == "certain":
        req["notice"]["recovery_service_forecast"] = {"east": [1, 1], "west": [2, 2]}
    elif fault == "late_opportunity":
        req["candidates"]["nominal"]["return_time_s"] = 1800.0
    elif fault == "expired_notice":
        req["notice"]["expires_at_s"] = 1700.0
    elif fault == "bool_time":
        req["time_s"] = True
    elif fault == "foreign_case":
        authority["case"] = "normal"
    else:
        authority["return_authority"]["maximum_llm_calls"] = 2
    # Re-sign the simple ledger to ensure the independent window, not its hash,
    # rejects the forged public input or grant.
    for ledger in study["provider_attempt_ledger"].values():
        ledger["authority_sha256"] = digest(authority)
    study["provider_attempt_ledger"]["return"]["attempts"][0]["request_sha256"] = digest(req)
    with pytest.raises(ValueError, match="m2_authority_call_limits" if fault == "expanded_contract" else "m2_orbital_window"):
        verify_attempts(study, counts)


@pytest.mark.parametrize("provider,timeout,remaining,age", [
    ("typesafe", 120.0, 149.0, 1.0),
    ("deepseek", 120.001, 149.0, 1.0),
    ("deepseek", 120.0, 121.99, 1.0),
    ("deepseek", 120.0, 149.0, 2.0),
    ("deepseek", True, 149.0, 1.0),
])
def test_attempt_cannot_expand_provider_or_consume_age_and_application_margin(provider, timeout, remaining, age):
    study, counts = evidence(provider=provider, timeout=timeout, remaining=remaining, age=age)
    with pytest.raises(ValueError, match="m2_host_attempt_deadline"):
        verify_attempts(study, counts)


def test_late_unprepared_update_keeps_legacy_jev_budget():
    study, counts = evidence(provider="typesafe", timeout=30.0, remaining=34.0)
    req = study["return_study"]["decisions"][0]["request"]
    req["candidates"]["nominal"]["return_time_s"] = 1800.0
    req["planning_window"] = decision_window(study["authority"]["return_authority"], req)
    req["deadline_s"] = req["time_s"] + req["planning_window"]["duration_s"]
    attempt = study["provider_attempt_ledger"]["return"]["attempts"][0]
    attempt["request_sha256"] = digest(req)
    assert req["planning_window"]["admitted"] is False
    assert provider_timeout_limit(study["authority"], "return", "deepseek", req) == 30.0
    assert verify_attempts(study, counts)["attempted"] == 1


def test_historical_deepseek_budget_remains_one_attempt_even_when_first_times_out():
    study, counts = evidence(legacy=True)
    record = deepcopy(study["return_study"]["decisions"][0])
    record["request"]["request_id"] = "run-return-2"
    study["return_study"]["decisions"].append(record)
    attempt = deepcopy(study["provider_attempt_ledger"]["return"]["attempts"][0])
    attempt.update(sequence=2, request_id="run-return-2", request_sha256=digest(record["request"]))
    study["provider_attempt_ledger"]["return"]["attempts"].append(attempt)
    study["runtime"]["model_calls"] = 2
    with pytest.raises(ValueError, match="m2_host_attempt_budget"):
        verify_attempts(study, counts)


def test_existing_m2_case_does_not_trust_a_claimed_long_window():
    study, counts = evidence()
    authority = study["authority"] = m2_contract("live", "normal")
    for ledger in study["provider_attempt_ledger"].values():
        ledger["authority_sha256"] = digest(authority)
    with pytest.raises(ValueError, match="m2_host_attempt_deadline"):
        verify_attempts(study, counts)


def test_actor_return_receipt_accepts_only_independently_admitted_long_deepseek(monkeypatch):
    from src.runtime.starship_m2_decision_verifier import verify_decisions
    from src.runtime import starship_m2_attempts
    study, _ = evidence()
    receipt = {"call_attempted": True, "timeout_seconds": 120., "application_margin_s": 2., "retries": 0}
    study["return_study"]["decisions"][0]["response"] = {"llm_invocation": receipt}
    study["runtime"]["actor_record_model_calls"] = 1
    monkeypatch.setattr(starship_m2_attempts, "verify_attempts", lambda *args: {})
    assert verify_decisions(study)["return_llm"] == 1
    study["return_study"]["decisions"][0]["response"] = {"jev_invocation": receipt}
    with pytest.raises(ValueError, match="return_provider_deadline"):
        verify_decisions(study)
