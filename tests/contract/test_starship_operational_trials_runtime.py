"""Seeded published inputs at real supervision and provider boundaries.

Finite clocks/forecast receipts stand in for propagation, and the provider
transport is intercepted. These checks call no model and establish no landing
or simulated-flight outcome.
"""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from src.runtime import starship_m3_live as m3
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_operational_trials import TRIALS, trial_manifest
from src.runtime.starship_replanning import MODE_ENV, contract, digest, notices
from src.runtime.starship_replanning_events import EventMonitor, verify_events
from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate


def trial_actor(monkeypatch, tmp_path, case, *, now=1000.7):
    actor, clock = actor_harness(monkeypatch, tmp_path, now=now, case=case)
    actor.run_id = "ordinary-flight"
    actor.comparison_context = {
        "ready": False,
        "generation_sha256": actor.comparison_generation(notices(case, now)),
    }
    return actor, clock


def publication(case, *, after_initial=True):
    rows = trial_manifest(case)["publications"]
    return rows[1 if after_initial else 0]


def test_received_correction_interrupts_pending_decision_before_action(monkeypatch, tmp_path):
    case = "op_trial_03"
    update = publication(case)
    actor, _ = trial_actor(monkeypatch, tmp_path, case, now=update["delivery_at_s"] - 1.0)
    old_notice = notices(case, actor.s.time_s)
    assert old_notice != update["notice"]
    assert actor.choose("selected_plan_monitoring", ["wait_for_update"]) is None
    record = actor.decisions[-1]
    assert record["request"]["notice"] == old_notice
    assert record["interrupted_by_events"] is True
    assert record["accepted_action"] is None
    assert record["later_generation"] > record["request"]["context_generation"]
    assert record["later_time_s"] >= update["delivery_at_s"]
    assert any(e["kind"] == "notice_updated" for e in actor.monitor.pending)
    assert actor.pending[-1]["end_time_s"] > actor.pending[-1]["start_time_s"]


def test_clearance_withdrawal_changes_actual_candidate_admission(monkeypatch, tmp_path):
    case = "op_trial_02"
    withdrawn = next(p for p in trial_manifest(case)["publications"] if "east" not in p["notice"]["areas"])
    actor, _ = trial_actor(monkeypatch, tmp_path, case, now=withdrawn["delivery_at_s"] - 1.0)
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    before = actor.checks(notices(case, actor.s.time_s))
    assert before["nominal"]["accepted"] and before["next_orbit"]["accepted"]
    actor.step()
    after = actor.checks(notices(case, actor.s.time_s))
    assert after["nominal"]["accepted"] is False
    assert after["nominal"]["reasons"] == ["area_or_availability"]
    assert after["next_orbit"]["accepted"] is True
    assert actor.monitor.pending[-1]["kind"] == "notice_updated"
    # Physical prediction was unchanged: published operations changed admission.
    assert before["nominal"]["candidate_sha256"] == after["nominal"]["candidate_sha256"]
    assert before["nominal"]["notice_sha256"] != after["nominal"]["notice_sha256"]


def test_late_delivery_expires_old_notice_without_revealing_replacement(monkeypatch, tmp_path):
    case = "op_trial_04"
    initial, replacement = publication(case, after_initial=False), publication(case)
    expiry = initial["notice"]["expires_at_s"]
    assert expiry + 2.0 < replacement["delivery_at_s"]
    actor, _ = trial_actor(monkeypatch, tmp_path, case, now=expiry - 1.0)
    before = notices(case, actor.s.time_s)
    actor.step()
    delivered = notices(case, actor.s.time_s)
    assert delivered == before == initial["notice"]
    assert delivered != replacement["notice"]
    assert [e["kind"] for e in actor.monitor.pending] == ["notice_expired"]
    # Independently delivered data resumes only at its receipt time.
    actor.s = replace(actor.s, time_s=replacement["delivery_at_s"])
    delivered = notices(case, actor.s.time_s)
    actor.monitor.poll(actor.observation(), delivered, actor.selected)
    assert delivered == replacement["notice"]
    assert any(e["kind"] == "notice_updated" and e["detected_at_s"] == replacement["delivery_at_s"]
               for e in actor.monitor.pending)
    # A candidate deadline may be coalesced with the newly delivered message.
    assert any(e["kind"] == "candidate_deadline" for e in actor.monitor.pending)


def supervision_record(case, *, mutate=False):
    env = contract("fixture", case)
    now = 1000.7
    observed = {"state": {"time_s": now, "omega_body_rad_s": [0.0] * 3,
                          "engine_states": [{"available": True}]}}
    notice = notices(case, now)
    if mutate:
        notice["text"] += " Unregistered availability claim."
    times = {"nominal": 2280.7, "next_orbit": 7730.7}
    monitor = EventMonitor(env, times)
    monitor.poll(observed, notice, None)
    study = {
        "opportunities": times,
        "profile": {"integration": {"coast_dt_s": 2.0}},
        "execution_origin": {"time_s": now},
        "execution": {"final_state": {"time_s": now}, "events": []},
        "dispatch": None,
        "supervision_observations": monitor.observations,
        "supervision_events": monitor.events,
        "decisions": [],
    }
    return study, env


@pytest.mark.parametrize("case", TRIALS)
def test_independent_verifier_rejects_self_consistent_but_unpublished_notice(case):
    truthful, env = supervision_record(case)
    assert verify_events(truthful, env, expected_case=case) == []
    forged, env = supervision_record(case, mutate=True)
    # Its hashes and event chronology agree; source-of-information binding must
    # still reject it. A mere hash-consistency verifier would miss this change.
    assert verify_events(forged, env) == []
    assert verify_events(forged, env, expected_case=case) == ["supervision_registered_notice_mismatch"]


def dictionary_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(dictionary_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(dictionary_keys(v) for v in value))
    return set()


@pytest.mark.parametrize("provider", ["typesafe", "deepseek"])
def test_outbound_model_payload_contains_delivered_evidence_not_trial_answer(
    monkeypatch, tmp_path, provider
):
    from src.intelligence.starship_replanning import ReplanningAgent

    case = "op_trial_03"
    actor, _ = trial_actor(monkeypatch, tmp_path, case)
    if provider == "deepseek":
        actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
        assert all(row["accepted"] for row in actor.checks(notices(case, actor.s.time_s)).values())
        actor.comparison_context["ready"] = True
        allowed = ["select_nominal", "select_next_orbit"]
    else:
        allowed = ["wait_for_update"]
    actor.choose("orbital_event", allowed)
    request = deepcopy(actor.decisions[-1]["request"])
    envelope = deepcopy(actor.envelope)
    envelope["mode"] = "live"
    request["envelope_sha256"] = digest(envelope)
    monkeypatch.setenv(MODE_ENV, "live")
    host = ReplanningAgent(envelope, lambda: True)
    sent = []

    def capture(actual_provider, payload, *_args, **_kwargs):
        sent.append((actual_provider, deepcopy(payload)))
        return None  # Actual network transport never executes.

    monkeypatch.setattr(host.transport, "_invoke", capture)
    host.assess(request)
    assert len(sent) == 1 and sent[0][0] == provider
    payload = sent[0][1]
    public = payload["state"] if provider == "typesafe" else json.loads(payload["messages"][-1]["content"])
    assert public["notice"] == notices(case, request["time_s"])
    hidden = {"case", "seed", "random_seed", "trial_id", "profile", "manifest",
              "publications", "delivery_at_s", "operational_trial", "expected_action"}
    assert not dictionary_keys(public) & hidden
    encoded = json.dumps(payload, sort_keys=True)
    assert case not in encoded
    for future in trial_manifest(case)["publications"]:
        if future["delivery_at_s"] > request["time_s"]:
            assert future["notice"]["text"] not in encoded
    assert request["provider_budget"]["provider"] == ("jev" if provider == "typesafe" else "deepseek")


@pytest.mark.parametrize("case", TRIALS)
def test_m3_authority_binds_same_operational_trial_across_both_phases(case):
    authority = m3.contract("fixture", case)
    inner = authority["m2_authority"]
    assert authority["case"] == case
    assert inner == m2_contract("fixture", case)
    assert inner["deployment_case"] == "normal"
    assert inner["return_case"] == case
    assert inner["return_authority"] == contract("fixture", case)
    assert authority["m2_authority_sha256"] == digest(inner)
    assert authority["prefix_authority"]["m2_authority_sha256"] == digest(inner)
    m3.validate_worker(authority, mode="fixture", case=case, run_id="ordinary-flight", mailbox=None)
    other = next(candidate for candidate in TRIALS if candidate != case)
    with pytest.raises(ValueError, match="m3_control_authority_mismatch"):
        m3.validate_worker(authority, mode="fixture", case=other, run_id="ordinary-flight", mailbox=None)
    forged = deepcopy(authority)
    forged["m2_authority"]["return_authority"] = contract("fixture", other)
    with pytest.raises(ValueError, match="m3_control_authority_mismatch"):
        m3.validate_worker(forged, mode="fixture", case=case, run_id="ordinary-flight", mailbox=None)


@pytest.mark.parametrize("case", ["op_trial_00", "op_trial_09", "op_trial_1", "op_trial_01-extra"])
def test_unregistered_operational_case_cannot_fall_back_to_nominal(case):
    for builder in (contract, m2_contract, m3.contract):
        with pytest.raises(ValueError):
            builder("fixture", case)
    with pytest.raises(ValueError):
        notices(case, 1000.7)


def test_m3_gateway_plan_commits_trial_without_starting_flight(monkeypatch, tmp_path):
    from src.runtime import starship_mission_control as control
    from src.runtime import starship_return_feasibility
    from src.runtime.starship_sixdof_catalog import SIXDOF_PROFILE

    case = "op_trial_03"
    scenario = next(name for name, registered in m3.SCENARIOS.items() if registered == case)
    monkeypatch.setenv(MODE_ENV, "fixture")
    monkeypatch.setattr(control, "_sources", lambda _: {SIXDOF_PROFILE: "a" * 64})
    monkeypatch.setattr(starship_return_feasibility, "readiness", lambda _: (None, None, None))
    monkeypatch.setattr(control.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no worker permitted"))
    service = control.StarshipMissionService(tmp_path, planner=lambda _: {
        "proposal": {"scenario": scenario, "rationale": "Fixture boundary check", "uncertainties": ["No flight"]}})
    plan = service.plan("operator", "Starship operational trial")["plan"]
    assert plan["scenario"] == scenario
    assert plan["mission_envelope"] == m3.contract("fixture", case)
    assert plan["simulation"]["case"] == case
    ref = ("operator", plan["id"], plan["sha256"])
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    approval = service.approve(*ref)["approval"]
    assert approval["mission_envelope"] == plan["mission_envelope"]
    assert approval["physical_execution_authorized"] is False


def test_real_prefix_adapter_uses_canonical_normal_plant_case(monkeypatch, tmp_path):
    from src.runtime import starship_m3_prefix as prefix
    from src.runtime.starship_m2_contract import PROFILE
    from src.runtime.starship_replanning import ROOT

    class BoundaryReached(Exception):
        pass

    seen = []

    def reject_flight(profile, **kwargs):
        seen.append(kwargs)
        raise BoundaryReached  # Observe real adapter inputs; never propagate.

    monkeypatch.setattr(prefix, "simulate_concurrent", reject_flight)
    profile = json.loads((ROOT / PROFILE).read_text())
    with pytest.raises(BoundaryReached):
        prefix.run_prefix(profile, "op_trial_03", tmp_path / "prefix", mode="fixture")
    assert len(seen) == 1
    approved = prefix.m3_prefix_contract("op_trial_03", mode="fixture")
    assert seen[0]["mission_case"] == approved["m2_authority"]["deployment_case"] == "normal"
    assert seen[0]["scenario"] == "launch"
    assert seen[0]["duration_s"] == approved["checkpoint_time_s"]
    assert seen[0]["mission_director"].ship.envelope == approved["m2_authority"]["deployment_authority"]


@pytest.mark.parametrize("tamper", ["missing", "notice", "different_trial"])
def test_m3_verifier_rejects_unbound_schedule_before_accepting_flight_evidence(tamper):
    case = "op_trial_03"
    authority = m3.contract("fixture", case)
    manifest = trial_manifest(case)
    if tamper == "notice":
        manifest["publications"][-1]["notice"]["text"] += " Forged future status."
    elif tamper == "different_trial":
        manifest = trial_manifest("op_trial_07")
    study = {
        "schema": "missionos.starship_m3_control_study.v1",
        "authority": authority,
        "run_id": "ordinary-flight",
        "source_sha256": {},
        "sources_unchanged": True,
        "m3_completed": False,
        "human_inflight_commands": 0,
        "retry_performed": False,
        "physical_execution": False,
    }
    if tamper != "missing":
        study["operational_trial_manifest"] = manifest
    result = m3.verify(study, expected_authority=authority, expected_sources={},
                       expected_return_sources={}, expected_run_id="ordinary-flight")
    assert result["passed"] is False and result["case_accepted"] is False
    assert result["issues"] == ["m3_operational_trial_manifest_mismatch"]


@pytest.mark.parametrize("case", TRIALS)
def test_operational_trial_does_not_expand_existing_live_authority(case):
    legacy = contract("live", "comparison_orbital_update")
    experimental = contract("live", case)
    binding = experimental.pop("operational_trial")
    assert binding["manifest_sha256"] == digest(trial_manifest(case))
    assert experimental == legacy
    actual, existing = m3.contract("live", case), m3.contract("live", "orbital_update")
    for key in ("maximum_ascent_calls", "maximum_booster_calls", "maximum_jev_calls",
                "maximum_llm_calls", "maximum_model_calls", "maximum_attempts",
                "maximum_wall_time_s", "maximum_output_bytes", "minimum_free_bytes",
                "phase_provider_policy", "approval_policy", "inflight_scope_amendment_enabled",
                "maximum_human_inflight_commands", "physical_execution_authorized"):
        assert actual[key] == existing[key]
