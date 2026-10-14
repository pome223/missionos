"""Provider reservations and observation scheduling; no live models or physics."""
from copy import deepcopy

import pytest

from src.runtime.starship_orbital_window import CONFIG
from src.runtime.starship_replanning import MODE_ENV, contract, digest, notices
from src.runtime.starship_replanning_events import EventMonitor
from src.runtime.starship_replanning_verifier import decision_budget_reasons
from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate


def comparison_actor(monkeypatch, tmp_path):
    actor, clock = actor_harness(monkeypatch, tmp_path, now=1700., case="comparison_update")
    actor.envelope["orbital_planning_window"] = deepcopy(CONFIG)
    actor.envelope["provider_budget_policy"] = "independent_provider_reservations_v1"
    actor.envelope["maximum_jev_calls"] = 1
    actor.envelope["maximum_llm_calls"] = 1
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(actor.case, actor.s.time_s), None)
    actor.comparison_context = {"ready": False, "generation_sha256": "a"*64}
    return actor, clock


def test_reserved_jev_exhaustion_does_not_consume_independent_comparison(monkeypatch, tmp_path):
    actor, _ = comparison_actor(monkeypatch, tmp_path)
    assert actor.choose("orbital_event", ["wait_for_update"]) == "wait_for_update"
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context["ready"] = True
    allowed = ["select_nominal", "select_next_orbit"]
    assert actor.choose("orbital_event", allowed) in allowed
    assert [row["provider_budget"]["provider"] for row in actor.decisions] == ["jev", "deepseek"]
    assert actor.decisions[-1]["request"]["planning_window"]["admitted"]
    assert actor.decisions[-1]["provider_budget"]["used_slots"] == 0
    # Same generation cannot reserve DeepSeek again; neither budget resets.
    assert actor.choose("selected_plan_monitoring", allowed) is None
    assert actor.decisions[-1]["provider_budget"]["provider"] == "jev"
    actor.comparison_context["generation_sha256"] = "b"*64
    assert actor.choose("orbital_event", allowed) is None
    assert actor.decisions[-1]["provider_budget"]["provider"] == "deepseek"
    assert actor.decisions[-1]["request_disposition"] == "budget_exhausted"
    assert decision_budget_reasons(actor.decisions, 1, envelope=actor.envelope) == []
    forged = deepcopy(actor.decisions)
    forged[1]["provider_budget"]["used_slots"] = 1
    assert "provider_reservation_binding" in decision_budget_reasons(forged, 1, envelope=actor.envelope)
    forged = deepcopy(actor.decisions)
    forged[3]["request_disposition"] = "submitted"
    assert "model_budget" in decision_budget_reasons(forged, 1, envelope=actor.envelope)


def test_invalid_comparison_cannot_bypass_exhausted_jev(monkeypatch, tmp_path):
    actor, _ = comparison_actor(monkeypatch, tmp_path)
    actor.choose("orbital_event", ["wait_for_update"])
    actor.candidates = {"nominal": make_candidate(actor, "nominal")}
    actor.comparison_context["ready"] = True
    assert actor.choose("orbital_event", ["select_nominal"]) is None
    assert actor.decisions[-1]["provider_budget"]["provider"] == "jev"
    assert actor.decisions[-1]["request_disposition"] == "budget_exhausted"
    assert decision_budget_reasons(actor.decisions, 1, envelope=actor.envelope) == []


def test_refresh_does_not_reoffer_unchanged_channel_but_notice_update_wakes(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1163., case="comparison_update")
    seen = []
    class ObservedUpdate(Exception):
        pass

    def prepared(notice):
        actor.comparison_preparations = [{"end_time_s": actor.s.time_s, "status": "completed"}]

    def choose(stage, allowed):
        actor.monitor.drain()
        seen.append((actor.s.time_s, list(allowed)))
        if actor.s.time_s >= 1350.:
            raise ObservedUpdate
        return "refresh_observation" if "refresh_observation" in allowed else "wait_for_update"

    actor.prepare_comparison = prepared
    actor.choose = choose
    with pytest.raises(ObservedUpdate):
        actor.run_event_supervision()
    assert len(seen) == 3  # refresh once, decide to wait, then react to new publication
    assert "refresh_observation" in seen[0][1]
    assert "refresh_observation" not in seen[1][1]
    assert "refresh_observation" in seen[2][1]
    assert 1350. <= seen[2][0] <= 1352.
    assert len(actor.ground_observations) == 1
    assert actor.ground_observations[0]["information_changed"] is False
    assert actor.s.time_s > actor.ground_observations[0]["received_s"]
    assert any(event["kind"] == "notice_updated" for event in actor.monitor.events)


def test_legacy_orbital_authority_keeps_original_record_budget():
    old = contract("fixture", "comparison_orbital_update")
    old.pop("provider_budget_policy")
    # Old records have the same orbital window, but no reservation metadata.
    rows = [{"request": {}, "request_disposition": "submitted",
             "response": None, "accepted_action": None}]
    assert decision_budget_reasons(rows, old["maximum_jev_calls"], envelope=old) == []
    current = contract("fixture", "comparison_orbital_update")
    assert "provider_reservation_binding" in decision_budget_reasons(
        rows, current["maximum_jev_calls"], envelope=current)


@pytest.mark.parametrize("location", ["record", "request", "both", "prefix_hash"])
def test_new_policy_rejects_stripped_or_rebound_reservation(monkeypatch, tmp_path, location):
    actor, _ = comparison_actor(monkeypatch, tmp_path)
    actor.choose("orbital_event", ["wait_for_update"])
    rows = deepcopy(actor.decisions)
    if location in ("record", "both"):
        rows[0].pop("provider_budget")
    if location in ("request", "both"):
        rows[0]["request"].pop("provider_budget")
    if location == "prefix_hash":
        for budget in (rows[0]["provider_budget"], rows[0]["request"]["provider_budget"]):
            budget["previous_reservations_sha256"] = "f" * 64
    assert "provider_reservation_binding" in decision_budget_reasons(
        rows, 1, envelope=actor.envelope)


@pytest.mark.parametrize("policy", [None, "unknown", True])
def test_invalid_policy_cannot_enable_or_bypass_reservations(monkeypatch, tmp_path, policy):
    from src.intelligence.starship_replanning import validate

    actor, _ = comparison_actor(monkeypatch, tmp_path)
    actor.choose("orbital_event", ["wait_for_update"])
    actor.envelope["provider_budget_policy"] = policy
    request = deepcopy(actor.decisions[0]["request"])
    request["envelope_sha256"] = digest(actor.envelope)
    with pytest.raises(ValueError, match="invalid_provider_budget_policy"):
        validate(request, actor.envelope)
    with pytest.raises(ValueError, match="invalid_provider_budget_policy"):
        actor.choose("orbital_event", ["wait_for_update"])
    assert decision_budget_reasons(actor.decisions, 1, envelope=actor.envelope) == [
        "invalid_provider_budget_policy"]


def test_missed_first_comparison_does_not_promote_later_jev_reservation(monkeypatch, tmp_path):
    from src.intelligence.starship_replanning import ReplanningAgent

    actor, _ = comparison_actor(monkeypatch, tmp_path)
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context["ready"] = True
    allowed = ["select_nominal", "select_next_orbit"]
    actor.choose("orbital_event", allowed)
    actor.choose("orbital_event", allowed)
    first, second = actor.decisions
    assert first["provider_budget"]["provider"] == "deepseek"
    assert second["provider_budget"]["provider"] == "jev"
    assert second["request"]["provider_budget"] == second["provider_budget"]
    assert second["provider_budget"]["previous_reservations_sha256"] == digest([
        {"request_id": first["request"]["request_id"], "request_sha256": digest(first["request"])}])
    assert decision_budget_reasons(actor.decisions, 1, envelope=actor.envelope) == []

    envelope = deepcopy(actor.envelope)
    envelope["mode"] = "live"
    request = deepcopy(second["request"])
    request["envelope_sha256"] = digest(envelope)
    monkeypatch.setenv(MODE_ENV, "live")
    host = ReplanningAgent(envelope, lambda: True)
    calls = []
    # This fresh host never saw the first request. Only a stub receives the second.
    def invoke(provider, *_args, **_kwargs):
        calls.append(provider)
        return None

    monkeypatch.setattr(host.transport, "_invoke", invoke)
    response = host.assess(request)
    assert calls == ["typesafe"]
    assert host.calls == 1 and host.llm_calls == 0
    assert response["routing"]["route"] == "jev"
    assert response["request_sha256"] == digest(request)


def test_host_rejects_missing_and_ineligible_provider_metadata(monkeypatch, tmp_path):
    from src.intelligence.starship_replanning import ReplanningAgent, validate
    from src.runtime.starship_orbital_window import decision_window

    actor, _ = comparison_actor(monkeypatch, tmp_path)
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context["ready"] = True
    actor.choose("orbital_event", ["select_nominal", "select_next_orbit"])
    envelope = deepcopy(actor.envelope)
    envelope["mode"] = "live"
    request = deepcopy(actor.decisions[0]["request"])
    request["envelope_sha256"] = digest(envelope)
    missing = deepcopy(request)
    missing.pop("provider_budget")
    with pytest.raises(ValueError, match="invalid_m1_request"):
        validate(missing, envelope)
    request["comparison_context"]["ready"] = False
    request["planning_window"] = decision_window(envelope, request)
    request["deadline_s"] = request["time_s"] + request["planning_window"]["duration_s"]
    monkeypatch.setenv(MODE_ENV, "live")
    host = ReplanningAgent(envelope, lambda: True)
    monkeypatch.setattr(host.transport, "_invoke", lambda *_a, **_k: pytest.fail("must not invoke"))
    with pytest.raises(ValueError, match="invalid_provider_reservation"):
        host.assess(request)
