"""Finite-clock orbital budget tests; no provider, propagation or flight runs."""

from copy import deepcopy
import json

import pytest

from src.runtime.starship_orbital_window import CONFIG, decision_window, reserved_decision_s
from src.runtime.starship_replanning import contract, digest, notices
from src.runtime.starship_replanning_events import EventMonitor
from src.runtime.starship_replanning_verifier import (
    _expected_planning_window, case_reasons, comparison_quality_record, planning_window_reasons,
)
from src.runtime.starship_reference_ids import catalog_for
from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate


def inputs():
    envelope = contract("fixture", "comparison_update")
    envelope["orbital_planning_window"] = deepcopy(CONFIG)
    request = {
        "time_s": 1700.0,
        "notice": notices("comparison_update", 1700.0),
        "selected": "nominal",
        "allowed_actions": ["keep_plan", "select_nominal", "select_next_orbit"],
        "comparison_context": {"ready": True},
        "candidates": {
            name: {"return_time_s": when, "constraint_rejections": [],
                   "comparison_material": {"waiting_at_burn": {"energy_remaining_kwh": energy}}}
            for name, when, energy in (("nominal", 2280.0, 22.0), ("next_orbit", 7730.0, 12.9))
        },
    }
    return envelope, request


def recorded(envelope, request):
    request["planning_window"] = decision_window(envelope, request)
    request["deadline_s"] = request["time_s"] + request["planning_window"]["duration_s"]
    return {"request": request, "response": None}


def test_long_window_uses_observed_inputs_and_preserves_legacy():
    envelope, request = inputs()
    window = decision_window(envelope, request)
    assert window["admitted"] and window["duration_s"] == 150
    assert window["provider_timeout_s"] == 120
    assert window == _expected_planning_window(envelope, request)
    request.update(deadline_s=999999, planning_window={"admitted": False})
    assert decision_window(envelope, request) == window
    del envelope["orbital_planning_window"]
    assert reserved_decision_s(envelope) == 35
    assert decision_window(envelope, request)["duration_s"] == 35


@pytest.mark.parametrize("fault", ["not_ready", "single", "unauthorized", "certain", "late", "expiry", "nan", "bool"])
def test_incomplete_or_urgent_comparison_does_not_earn_long_wait(fault):
    envelope, request = inputs()
    if fault == "not_ready":
        request["comparison_context"]["ready"] = False
    elif fault == "single":
        request["candidates"]["next_orbit"]["constraint_rejections"] = ["energy"]
    elif fault == "unauthorized":
        request["allowed_actions"].remove("select_next_orbit")
    elif fault == "certain":
        request["notice"]["recovery_service_forecast"] = {"east": [1, 1], "west": [2, 2]}
    elif fault == "late":
        request["time_s"] = 2100
    elif fault == "expiry":
        request["notice"]["expires_at_s"] = 1800
    elif fault == "nan":
        request["candidates"]["nominal"]["return_time_s"] = float("nan")
    else:
        request["time_s"] = True
    actual = decision_window(envelope, request)
    assert actual == _expected_planning_window(envelope, request)
    assert actual["admitted"] is False and actual["duration_s"] == 35


def test_long_window_nearest_booking_boundary_and_step_margin():
    envelope, request = inputs()
    threshold = request["time_s"] + 150 + 60 + 2.001
    request["candidates"]["nominal"]["return_time_s"] = threshold
    assert decision_window(envelope, request)["admitted"]
    request["candidates"]["nominal"]["return_time_s"] -= 0.001
    assert decision_window(envelope, request)["reason"] == "insufficient_booking_margin"


@pytest.mark.parametrize("fault", [None, "duration", "timeout", "scope", "base"])
def test_unapproved_contract_cannot_expand_scope(fault):
    envelope, request = inputs()
    if fault is None:
        envelope["orbital_planning_window"] = None
    elif fault == "base":
        envelope["decision_timeout_s"] = 150
    else:
        key = {"duration": "decision_timeout_s", "timeout": "provider_timeout_s", "scope": "scope"}[fault]
        envelope["orbital_planning_window"][key] = 999
    with pytest.raises(ValueError):
        decision_window(envelope, request)
    assert planning_window_reasons({"request": request}, envelope, 0.25)


@pytest.mark.parametrize("field", ["deadline_s", "duration_s", "admitted", "protected_until_s", "provider_timeout_s"])
def test_independent_verifier_rejects_claimed_window_tampering(field):
    envelope, request = inputs()
    record = recorded(envelope, request)
    assert planning_window_reasons(record, envelope, 0.25) == []
    if field == "deadline_s":
        request[field] += 1
    else:
        request["planning_window"][field] = False if field == "admitted" else 99999
    assert planning_window_reasons(record, envelope, 0.25)


def test_independent_verifier_checks_current_inputs_and_provider_limit():
    envelope, request = inputs()
    record = recorded(envelope, request)
    record["response"] = {"llm_invocation": {"call_attempted": True, "call_succeeded": True,
                                              "timeout_seconds": 120.0, "latency_ms": 110000.0}}
    assert planning_window_reasons(record, envelope, 0.25) == []
    record["response"]["llm_invocation"]["latency_ms"] = 120001
    assert planning_window_reasons(record, envelope, 0.25) == ["orbital_provider_deadline"]
    record["response"]["llm_invocation"]["latency_ms"] = 110000
    request["candidates"]["nominal"]["return_time_s"] = 1750
    assert planning_window_reasons(record, envelope, 0.25) == ["orbital_planning_window_binding"]


def test_preparation_reserves_long_window_and_events_warn_earlier(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1300.0, case="comparison_update")
    actor.envelope["orbital_planning_window"] = deepcopy(CONFIG)
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    assert actor.monitor.lead_s == 600 + 150 + 60
    actor.s = __import__("dataclasses").replace(actor.s, time_s=2100)
    actor.candidates["nominal"] = make_candidate(actor, "nominal")
    actor.selected = "nominal"
    actor.comparison_generations = {actor.comparison_generation(notices(actor.case, 1000))}
    actor.comparison_preparations = []
    actor.calculate = lambda *args, **kwargs: pytest.fail("late optional forecast must not start")
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    row = actor.comparison_preparations[-1]
    assert row["budget"]["decision_timeout_s"] == 150
    assert row["budget"]["opportunity_cutoff_time_s"] == 2068
    assert row["reason"] == "booking_or_candidate_deadline"
    assert actor.selected == "nominal"


def test_actor_long_wait_advances_plant_and_retires_mailbox(monkeypatch, tmp_path):
    actor, clock = actor_harness(monkeypatch, tmp_path, now=1700, case="comparison_update")
    actor.envelope["orbital_planning_window"] = deepcopy(CONFIG)
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(actor.case, 1700), "nominal")
    actor.selected = "nominal"
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context = {"ready": True, "generation_sha256": "a" * 64}
    actor.mailbox = tmp_path
    published = []
    original_sleep = clock.sleep

    def delayed_response(duration):
        original_sleep(duration)
        if clock.now >= 110 and not published:
            request = json.loads((tmp_path / "request.json").read_text())
            (tmp_path / "response.json").write_text(json.dumps({
                "request_id": request["request_id"], "request_sha256": digest(request),
                "action": "keep_plan",
            }))
            published.append(True)

    clock.sleep = delayed_response
    action = actor.choose("orbital_event", ["keep_plan", "select_nominal", "select_next_orbit"])
    assert action == "keep_plan"
    record = actor.decisions[-1]
    assert record["request"]["planning_window"]["admitted"]
    assert record["later_time_s"] - record["request"]["time_s"] >= 110
    assert record["later_time_s"] <= record["request"]["deadline_s"]
    assert json.loads((tmp_path / "cancelled.json").read_text()) == {
        "request_id": record["request"]["request_id"], "request_sha256": digest(record["request"]),
    }
    assert planning_window_reasons(record, actor.envelope, 2.0) == []


def test_actor_cancels_long_request_on_new_notice_before_next_forecast(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1300, case="comparison_update")
    actor.envelope["orbital_planning_window"] = deepcopy(CONFIG)
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(actor.case, 1300), "nominal")
    actor.selected = "nominal"
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context = {"ready": True, "generation_sha256": "a" * 64}
    actor.mailbox = tmp_path
    assert actor.choose("orbital_event", ["keep_plan", "select_nominal", "select_next_orbit"]) is None
    record = actor.decisions[-1]
    assert record["interrupted_by_events"] is True
    assert record["fallback"] is False
    assert record["later_time_s"] == 1350
    cancelled = json.loads((tmp_path / "cancelled.json").read_text())
    assert cancelled["request_sha256"] == digest(record["request"])
    assert actor.pending[-1]["wall_time_s"] < 150


def test_long_timeout_retires_request_and_preserves_current_booking(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1700, case="comparison_update")
    actor.envelope["orbital_planning_window"] = deepcopy(CONFIG)
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(actor.case, 1700), "nominal")
    actor.selected = "nominal"
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context = {"ready": True, "generation_sha256": "a" * 64}
    actor.mailbox = tmp_path
    assert actor.choose("orbital_event", ["keep_plan", "select_nominal", "select_next_orbit"]) is None
    record = actor.decisions[-1]
    assert record["request"]["planning_window"]["duration_s"] == 150
    assert record["later_time_s"] == 1850
    assert record["fallback"] is True and not record["interrupted_by_events"]
    assert actor.selected == "nominal"
    assert (tmp_path / "cancelled.json").is_file()


def test_new_live_case_requires_deepseek_and_bound_reference_evidence():
    envelope, request = inputs()
    envelope["mode"] = "live"
    request.update(request_id="comparison-1", revision=1)
    record = recorded(envelope, request)
    record.update(accepted_action="keep_plan", fallback=False, interrupted_by_events=False)
    response = {
        "model_inference_invoked": True,
        "routing": {"route": "prepared_uncertain_comparison"},
        "llm_invocation": {"call_attempted": True, "call_succeeded": True,
                           "model_inference_invoked": True},
        "decision_basis": {"summary": "Both opportunities were compared.",
                           "compared_candidate_ids": ["nominal", "next_orbit"],
                           "reference_ids": [e["reference_id"] for e in catalog_for(request)["entries"]]},
    }
    record["response"] = response
    study = {
        "envelope": envelope,
        "dispatch": {"candidate_id": "nominal", "revision": 1},
        "plan_revisions": [{"candidate_id": "nominal", "revision": 1, "time_s": 1600,
                            "decision_request_id": "earlier"}],
        "decisions": [record],
    }
    assert case_reasons(study, "comparison_orbital_update", "contact_and_recovery_area_met") == []
    for fault in ("no_llm", "bad_ids", "no_long_window", "different_dispatch"):
        changed = deepcopy(study)
        if fault == "no_llm":
            changed["decisions"][0]["response"]["llm_invocation"]["call_succeeded"] = False
        elif fault == "bad_ids":
            changed["decisions"][0]["response"]["decision_basis"]["reference_ids"] = []
        elif fault == "no_long_window":
            changed["decisions"][0]["request"]["planning_window"]["admitted"] = False
        else:
            changed["dispatch"]["candidate_id"] = "next_orbit"
        assert "prepared_comparison_decision_not_observed" in case_reasons(
            changed, "comparison_orbital_update", "contact_and_recovery_area_met",
        )


def test_quality_audit_resolves_ids_without_claiming_model_numeric_assertions():
    _, request = inputs()
    request["request_id"] = "quality-test"
    entries = catalog_for(request)["entries"]
    record = {"request": request, "accepted_action": "keep_plan", "response": {
        "decision_basis": {"summary": "Use the earlier opportunity.",
                           "compared_candidate_ids": ["nominal", "next_orbit"],
                           "reference_ids": [e["reference_id"] for e in entries]},
    }}
    report = comparison_quality_record(record, {}, [])
    assert report["reference_id_evidence"]["status"] == "valid"
    assert report["reference_id_evidence"]["host_resolved_values"] is True
    assert report["reference_id_evidence"]["model_numeric_values_verified"] is False
    assert report["numeric_evidence"]["status"] == "not_present"
