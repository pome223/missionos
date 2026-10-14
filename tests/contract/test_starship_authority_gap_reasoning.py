"""Authority-gap reasoning admission, no models or numerical propagation."""
from copy import deepcopy
import json

import pytest

from src.runtime.starship_inflight_runtime import amendment_window
from src.runtime.starship_orbital_window import (
    AUTHORITY_GAP_POLICY, decision_window, prepared_reasoning_eligible, reserved_decision_s,
)
from src.runtime.starship_replanning import MODE_ENV, contract, digest, notices


def inputs():
    envelope = contract("live", "op_scope_01", authority_gap_reasoning=True)
    inflight = {"schema": "missionos.inflight_executor_contract.v1",
        "context": {"run_id": "run1-return", "mission_id": "run1", "vehicle_id": "ship"},
        "initial_scope": {"commands": ["select_nominal", "keep_reserved_return"],
            "resource_limits": {"maximum_return_delay_s": 6000.}},
        "bounds": {"simulation_deadline_s": 2200., "wall_deadline_s": 5000.,
            "maximum_observation_age_s": 5., "reservation_cutoff_s": 2220.}}
    notice = notices("op_scope_01", 1700.)
    authority = {"schema": "missionos.inflight_active_authority.v2", "contract_sha256": digest(inflight),
        "scope": deepcopy(inflight["initial_scope"]), "scope_revision": 0,
        "scope_sha256": digest(inflight["initial_scope"]), "amendment_status": "idle",
        "technical_candidates_are_not_authorization": True,
        "select_next_orbit_requires_candidate_bound_operator_approval": True,
        "request_window": amendment_window(inflight["bounds"], simulation_time_s=1700., wall_time_s=1000.,
            model_response_reserve_s=150., status="idle", scope_revision=0)}
    request = {"schema": "missionos.m1_decision.v1", "request_id": "run1-return-0",
        "envelope_sha256": digest(envelope), "revision": 1, "time_s": 1700., "deadline_s": 1850.,
        "stage": "orbital_event", "notice": notice, "resources": {}, "selected": None,
        "allowed_actions": ["select_next_orbit", "wait_for_update"], "context_generation": 1,
        "trigger_event_ids": ["event-1"], "comparison_context": {"ready": True,
            "generation_sha256": "a"*64, "missing_candidate_ids": [],
            "prepared_candidate_ids": ["nominal", "next_orbit"]}, "active_authority": authority,
        "candidates": {}}
    for name, when, errors in (("nominal", 2280., ["area_or_availability"]), ("next_orbit", 7731., [])):
        fingerprint = ("b" if name == "nominal" else "c")*64
        request["candidates"][name] = {"candidate_sha256": fingerprint, "return_time_s": when,
            "constraint_rejections": errors, "areas": [] if errors else ["west"],
            "comparison_material": {"candidate_sha256": fingerprint, "notice_sha256": digest(notice),
                "return_time_s": when, "waiting_at_burn": {"energy_remaining_kwh": 12.9}}}
    request["planning_window"] = decision_window(envelope, request)
    request["provider_budget"] = {"provider": "deepseek", "used_slots": 0, "maximum_slots": None,
        "generation_sha256": "a"*64, "previous_reservations_sha256": digest([])}
    return envelope, inflight, request


def test_opt_in_preserves_legacy_and_admits_reasoning_not_action():
    envelope, _inflight, request = inputs()
    assert envelope["authority_gap_reasoning"] == AUTHORITY_GAP_POLICY
    assert decision_window(envelope, request) == {
        "schema": "missionos.orbital_planning_window_decision.v1", "duration_s": 150.,
        "provider_timeout_s": 120., "admitted": True, "reason": "authority_gap", "protected_until_s": 2200.}
    assert prepared_reasoning_eligible(envelope, request)
    assert not prepared_reasoning_eligible(envelope, request, {"a"*64})
    assert request["allowed_actions"] == ["select_next_orbit", "wait_for_update"]
    assert request["active_authority"]["scope_revision"] == 0
    legacy = contract("live", "op_scope_01")
    assert legacy == contract("live", "op_scope_01", authority_gap_reasoning=False)
    assert "authority_gap_reasoning" not in legacy
    assert decision_window(legacy, request)["reason"] == "insufficient_admissible_candidates"
    assert not prepared_reasoning_eligible(legacy, request)


def test_policy_is_dormant_without_bound_inflight_context(monkeypatch):
    from src.intelligence.starship_replanning import ReplanningAgent, validate
    envelope, _inflight, request = inputs()
    ReplanningAgent(envelope, lambda: False)  # Registered standalone M2 remains constructible.
    with pytest.raises(ValueError, match="invalid_m1_request"):
        validate(request, envelope)  # It cannot inject an unbound authority block.
    request.pop("active_authority")
    assert decision_window(envelope, request)["reason"] != "authority_gap"
    assert not prepared_reasoning_eligible(envelope, request)


@pytest.mark.parametrize("fault", ["unsafe", "normal", "authorized", "not_allowed", "not_ready",
    "missing", "stale_notice", "stale_material", "mismatched_candidate", "expired", "short_sim", "short_wall",
    "prior_request", "telemetry", "no_approval_capability", "short_burn", "short_notice"])
def test_authority_gap_never_bypasses_scope_evidence_or_deadlines(fault):
    envelope, inflight, request = inputs()
    authority = request["active_authority"]
    if fault == "unsafe":
        request["candidates"]["next_orbit"]["constraint_rejections"] = ["fuel"]
    elif fault == "normal":
        request["candidates"]["nominal"]["constraint_rejections"] = []
    elif fault == "authorized":
        authority["scope"]["commands"].append("select_next_orbit")
        authority["scope_sha256"] = digest(authority["scope"])
    elif fault == "not_allowed":
        request["allowed_actions"].remove("select_next_orbit")
    elif fault == "not_ready":
        request["comparison_context"]["ready"] = False
    elif fault == "missing":
        request["comparison_context"]["missing_candidate_ids"] = ["next_orbit"]
    elif fault == "stale_notice":
        request["notice"]["expires_at_s"] = 1700.
    elif fault == "stale_material":
        request["candidates"]["next_orbit"]["comparison_material"]["notice_sha256"] = "0"*64
    elif fault == "mismatched_candidate":
        request["candidates"]["next_orbit"]["candidate_sha256"] = "0"*64
    elif fault in {"expired", "short_sim", "short_wall"}:
        if fault != "short_wall":
            request["time_s"] = 2200. if fault == "expired" else 2026.
        authority["request_window"] = amendment_window(inflight["bounds"], simulation_time_s=request["time_s"],
            wall_time_s=4826. if fault == "short_wall" else 1000., model_response_reserve_s=150.,
            status="idle", scope_revision=0)
    elif fault == "prior_request":
        authority["amendment_status"] = "awaiting_operator"
    elif fault == "telemetry":
        authority["request_window"]["trusted_telemetry_available"] = False
    elif fault == "no_approval_capability":
        authority["select_next_orbit_requires_candidate_bound_operator_approval"] = False
    elif fault == "short_burn":
        request["candidates"]["next_orbit"]["return_time_s"] = 1850.
        request["candidates"]["next_orbit"]["comparison_material"]["return_time_s"] = 1850.
    else:
        request["notice"]["expires_at_s"] = 1850.
        for candidate in request["candidates"].values():
            candidate["comparison_material"]["notice_sha256"] = digest(request["notice"])
    assert decision_window(envelope, request)["reason"] != "authority_gap"
    # An ordinary two-feasible-candidate comparison is independent of this route.
    if fault != "normal":
        assert not prepared_reasoning_eligible(envelope, request)


@pytest.mark.parametrize("fault", ["policy", "missing_window", "budget_policy"])
def test_bad_policy_cannot_increase_a_model_window(fault):
    envelope, _inflight, request = inputs()
    if fault == "policy":
        envelope["authority_gap_reasoning"]["operator_response_reserve_s"] = 0.
    elif fault == "missing_window":
        envelope.pop("orbital_planning_window")
    else:
        envelope.pop("provider_budget_policy")
    with pytest.raises(ValueError, match="invalid_authority_gap"):
        reserved_decision_s(envelope)
    with pytest.raises(ValueError, match="invalid_authority_gap"):
        decision_window(envelope, request)


@pytest.mark.parametrize("fault", ["prepared_string", "commands_string", "candidate_scalar"])
def test_malformed_authority_gap_inputs_do_not_get_a_long_window(fault):
    envelope, _inflight, request = inputs()
    if fault == "prepared_string":
        request["comparison_context"]["prepared_candidate_ids"] = "next_orbit"
    elif fault == "commands_string":
        authority = request["active_authority"]
        authority["scope"]["commands"] = "select_nominal"
        authority["scope_sha256"] = digest(authority["scope"])
    else:
        request["candidates"]["nominal"] = 1
    assert decision_window(envelope, request)["admitted"] is False


def test_authority_gap_host_uses_one_candidate_reference_protocol_without_forcing_choice(monkeypatch):
    from src.intelligence.starship_replanning import ReplanningAgent
    from src.runtime.starship_reference_ids import catalog_for
    envelope, inflight, request = inputs()
    monkeypatch.setenv(MODE_ENV, "live")
    agent = ReplanningAgent(envelope, lambda: True, inflight_contract=inflight)
    invoked = []
    def transport(provider, payload, receipt, decoder, **kwargs):
        invoked.append((provider, payload))
        assert provider == "deepseek"
        answer = {"action": "wait_for_update", "compared_candidate_ids": ["next_orbit"],
            "summary": "Defer the scope request within the remaining authority window.",
            "reference_ids": [x["reference_id"] for x in catalog_for(request)["entries"] if x["candidate_id"] == "next_orbit"]}
        value = {"model": payload["model"], "usage": {"completion_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 50}}, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(answer)}}]}
        action, _model = decoder(value)
        return action
    monkeypatch.setattr(agent.transport, "_invoke", transport)
    result = agent.assess(request)
    assert result["action"] == "wait_for_update"
    assert result["routing"]["route"] == "authority_gap"
    assert result["decision_basis"]["compared_candidate_ids"] == ["next_orbit"]
    assert len(invoked) == 1 and agent.llm_calls == 1 and agent.calls == 0
    with pytest.raises(ValueError, match="invalid_provider_reservation"):
        agent.assess(request)  # Same generation cannot be promoted a second time.
    assert len(invoked) == 1


def test_executor_reserves_authority_gap_reasoning_once_and_keeps_original_deadline(monkeypatch, tmp_path):
    from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate
    from src.runtime.starship_replanning_events import EventMonitor

    actor, clock = actor_harness(monkeypatch, tmp_path, now=1700., case="op_scope_01")
    envelope, inflight, source = inputs()
    actor.envelope = envelope
    actor.monitor = EventMonitor(envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(actor.case, 1700.), None)
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context = source["comparison_context"]
    actor.decision_context = lambda: {"active_authority": deepcopy(source["active_authority"])}
    actor.mailbox = tmp_path
    original_sleep = clock.sleep
    sent = []
    def respond_after_100_seconds(duration):
        original_sleep(duration)
        if clock.now >= 100. and not sent:
            request = json.loads((tmp_path/"request.json").read_text())
            (tmp_path/"response.json").write_text(json.dumps({"request_id": request["request_id"],
                "request_sha256": digest(request), "action": "wait_for_update"}))
            sent.append(request)
    clock.sleep = respond_after_100_seconds
    assert actor.choose("orbital_event", ["select_next_orbit", "wait_for_update"]) == "wait_for_update"
    row = actor.decisions[-1]
    assert row["provider_budget"]["provider"] == "deepseek"
    assert row["request"]["planning_window"]["reason"] == "authority_gap"
    assert row["request"]["deadline_s"] == 1850.
    assert row["later_time_s"] >= 1800.
    assert row["later_time_s"] < inflight["bounds"]["simulation_deadline_s"]-20.
    assert actor.selected is None and actor.dispatch is None
    generation = row["request"]["comparison_context"]["generation_sha256"]
    assert not prepared_reasoning_eligible(envelope, row["request"], {generation})
