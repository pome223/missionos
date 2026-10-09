"""Authority, provider isolation, delayed dispatch and physically applied scope."""
import json
import os

import pytest

from src.runtime.starship_mission_director import (
    FIELDS, MODE_ENV, POINTS, SCENARIOS, SCOPE, MissionDirector, check_action, contract, digest, validate_observation,
    fuel_sensor,
)
from src.intelligence.starship_mission_director import MissionAgent, fixture_decision
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control


def row(t=100., **updates):
    result = {"time_s": t, "phase": "orbital_coast", "released_count": 0,
        "release_acknowledged": False, "sequencer_state": "running", "fuel_kg": 50000.,
        "return_deadline_s": 2000., "tower_ready": True, "operations_notice": "", "hold_expires_at_s": None,
        "numerical_tools": {"orbit_release_feasible": True, "retained_payload_present": True,
            "capture_corridor_certified": False, "mechanism_status": "not_collected"}}
    result.update(updates)
    return result


def request(point="deployment_start", observation=None, mode="fixture"):
    return {"schema": "missionos.starship_director_request.v5", "request_id": "run:1", "point": point,
            "observation": observation or row(), "allowed_actions": POINTS[point],
            "decision_deadline_s": (observation or row())["time_s"]+75.,
            "envelope_sha256": digest(contract(mode))}


@pytest.mark.parametrize("field,value", [("scenario", "normal"), ("true_mass", 1234),
    ("recovery_time", 120.), ("inertia_kg_m2", [[1, 0, 0]]), ("future_fault", True)])
def test_hidden_truth_is_rejected(field, value):
    with pytest.raises(ValueError, match="invalid_director_observation"):
        validate_observation({**row(), field: value})


def test_scope_choices_are_not_mutable_global_authority():
    granted = contract("fixture")
    granted["decision_points"]["booster_selection"].append("teleport")
    assert "teleport" not in POINTS["booster_selection"]
    assert check_action(granted, "booster_selection", "divert", row(), elapsed_s=0,
                        observation_requests=0, hold_used_s=0) == "envelope_mismatch"


@pytest.mark.parametrize("point,action,observation,elapsed,reads,holds,reason", [
    ("deployment_start", "launch_missile", row(), 0, 0, 0, "outside_approved_choices"),
    ("deployment_start", "continue", row(fuel_kg=27000), 0, 0, 0, "release_envelope_not_met"),
    ("deployment_start", "continue", row(sequencer_state="inhibited"), 0, 0, 0, "release_envelope_not_met"),
    ("deployment_start", "hold", row(return_deadline_s=120.), 0, 0, 0, "hold_budget_exhausted"),
    ("deployment_monitor", "collect_status", row(), 0, 1, 0, "observation_budget_exhausted"),
    ("deployment_start", "hold", row(), 0, 0, 30, "hold_budget_exhausted"),
    ("booster_selection", "capture", row(tower_ready=False), 0, 0, 0, "capture_not_certified"),
    ("booster_selection", "capture", row(), 0, 0, 0, "capture_not_certified"),
    ("deployment_start", "continue", row(), 75, 0, 0, "decision_expired"),
    ("deployment_start", "continue", row(), float("nan"), 0, 0, "invalid_decision_clock"),
])
def test_independent_limits(point, action, observation, elapsed, reads, holds, reason):
    assert check_action(contract("fixture"), point, action, observation, elapsed_s=elapsed,
                        observation_requests=reads, hold_used_s=holds) == reason


def test_normal_monitor_is_called_without_alarm_or_interlock():
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision)
    assert actor.update("deployment_start", row()) == "continue"
    assert actor.records[0]["request"]["observation"]["sequencer_state"] == "running"
    assert actor.records[0]["later_observation"] is None
    actor.confirm("ship", row(101.))
    assert actor.records[0]["later_observation"]["time_s"] == 101.
    assert actor.records[0]["response"]["model_inference_invoked"] is False


def test_synthetic_gauge_noise_is_repeatable_bounded_and_not_case_dependent():
    readings = [fuel_sensor(50123., t, "ship") for t in range(40)]
    assert len(set(readings)) > 1
    assert all(abs(value-50123.) <= 100 for value in readings)
    assert readings == [fuel_sensor(50123., t, "ship") for t in range(40)]


def test_diagnostic_cannot_disable_a_blocked_mechanism_interlock():
    observed = row(sequencer_state="held")
    observed["numerical_tools"]["mechanism_status"] = "blocked"
    assert check_action(contract("fixture"), "deployment_diagnostic", "continue", observed,
                        elapsed_s=0, observation_requests=1, hold_used_s=0) == "release_envelope_not_met"


def test_fresh_state_overrides_the_model_request_state(tmp_path):
    actor = MissionDirector(contract("fixture"), tmp_path, "fresh")
    assert actor.update("deployment_start", row()) is None
    req = json.loads((tmp_path/"request.json").read_text())
    (tmp_path/"response.json").write_text(json.dumps(fixture_decision(req)))
    assert actor.update("deployment_start", row(101., fuel_kg=26000.)) == "stop_deployment"
    assert actor.records[0]["dispatch"]["rejection"] == "release_envelope_not_met"


def test_request_response_binding_later_state_and_timeout(tmp_path):
    actor = MissionDirector(contract("fixture"), tmp_path, "test-run")
    assert actor.update("deployment_start", row()) is None
    req = json.loads((tmp_path/"request.json").read_text())
    assert set(req["observation"]) == FIELDS
    (tmp_path/"response.json").write_text(json.dumps({**fixture_decision(req), "request_sha256": "0"*64}))
    assert actor.update("deployment_start", row(101.)) == "continue"
    assert actor.records[0]["dispatch"]["rejection"] == "response_binding_mismatch"
    actor.confirm("ship", row(102., sequencer_state="running"))
    # Adapter records what it sees; it cannot forge a skipped sequencer.
    assert actor.records[0]["later_observation"]["sequencer_state"] == "running"
    timeout = MissionDirector(contract("fixture"), tmp_path, "timeout-run")
    assert timeout.update("deployment_start", row()) is None
    assert timeout.update("deployment_start", row(175.)) == "continue"
    assert timeout.records[0]["dispatch"]["rejection"] == "decision_deadline_reached"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_plan_and_single_consumption_scope(tmp_path, monkeypatch, scenario):
    from src.runtime import starship_return_feasibility as qualification
    # This test isolates approval consumption; qualification has its own checks.
    monkeypatch.setattr(qualification, "readiness", lambda *a, **k: ({}, "fixture", None))
    monkeypatch.setenv(MODE_ENV, "fixture")
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("operator", "Starship "+scenario)
    plan = state["plan"]
    assert plan["backend"] == "starship_mission_management"
    expected = contract("fixture", splashdown=scenario == "sixdof_managed_splashdown")
    assert plan["mission_envelope"] == expected
    assert plan["model_authority"] == "bounded_mission_decision_candidate"
    assert state["approval"] is None
    ref = ("operator", plan["id"], plan["sha256"])
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    state = service.approve(*ref)
    assert state["approval"]["scope"] == SCOPE
    assert state["approval"]["mission_envelope"] == plan["mission_envelope"]
    service._valid_grant(service._load("operator"))
    changed = service._load("operator")
    changed["plan"]["mission_envelope"]["maximum_hold_s"] = 9999
    with pytest.raises(control.StarshipMissionError, match="approval_binding_expired_or_consumed"):
        service._valid_grant(changed)


def test_live_provider_can_decide_from_normal_observation(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "live")
    agent = MissionAgent(contract("live"))
    seen = []
    def invoke(provider, payload, receipt, decode):
        seen.append(payload)
        receipt.update(model_inference_invoked=True)
        return "continue"
    monkeypatch.setattr(agent.transport, "_invoke", invoke)
    answer = agent.assess(request(mode="live"))
    assert answer["action"] == "continue" and answer["mode"] == "live"
    assert answer["model_inference_invoked"] is True
    assert "scenario" not in json.dumps(seen[0]["state"])
    assert len(seen) == 1
    for _ in range(5):
        agent.assess(request(mode="live"))
    with pytest.raises(ValueError, match="call_budget_exhausted"):
        agent.assess(request(mode="live"))


def test_worker_environment_never_contains_director_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-never-disclose")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-jev-never-disclose")
    monkeypatch.setenv(MODE_ENV, "live")
    env = control.process_environment({"scenario": "sixdof_managed_normal", "mission_envelope": contract("live")})
    assert not {"DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", MODE_ENV} & set(env)


@pytest.mark.parametrize("kind", ["invalid", "timeout"])
@pytest.mark.parametrize("point,expected", [
    ("deployment_start", "continue"), ("deployment_monitor", "continue"),
    ("return_selection", "halt_unresolved_return"), ("booster_selection", "divert"),
])
def test_response_failures_preserve_preapproved_nominal_actions(kind, point, expected):
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision, response_fault=kind+"_"+point)
    observed = row()
    if point == "booster_selection":
        observed["phase"] = "booster_return"
    result = actor.update(point, observed)
    if kind == "timeout":
        assert result is None
        observed["time_s"] = 175.
        result = actor.update(point, observed)
    assert result == expected
    assert actor.records[0]["dispatch"]["rules_accepted"] is False


@pytest.mark.parametrize("updates", [
    {"sequencer_state": "inhibited"}, {"sequencer_state": "skipped"}, {"sequencer_state": "held"},
    {"fuel_kg": 27000.}, {"operations_notice": "Unresolved operations notice"},
])
def test_invalid_response_never_overrides_inhibit_or_new_notice(updates):
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision,
                            response_fault="invalid_deployment_monitor")
    assert actor.update("deployment_monitor", row(**updates)) == "stop_deployment"


def test_malformed_reply_does_not_crash_or_select_new_return():
    actor = MissionDirector(contract("fixture"), fixture_decider=lambda _: ["retained_return"])
    assert actor.update("return_selection", row()) == "halt_unresolved_return"
    assert actor.records[0]["dispatch"]["rules_accepted"] is False


def test_execution_recheck_normalizes_native_integrator_scalars(monkeypatch):
    import numpy as np
    from src.runtime import starship_return_feasibility_verifier as guard
    seen = []
    def inspect_proof(proof, *args, **kwargs):
        seen.append(proof)
        assert all(type(x) is float for x in proof["inputs"]["r_eci_m"])
        return {"passed": True, "reason": None}
    monkeypatch.setattr(guard, "verify", inspect_proof)
    observation = row()
    observation["numerical_tools"]["return_feasibility"] = {
        "inputs": {"r_eci_m": [np.float64(1.), np.float64(2.), np.float64(3.)]},
        "return_admitted": True, "bounded_coast_admitted": True}
    assert check_action(contract("fixture"), "return_selection", "state_return", observation,
        elapsed_s=0, observation_requests=0, hold_used_s=0) is None
    assert seen


def test_initial_deadline_is_bound_and_cannot_wait_past_release_slot():
    actor = MissionDirector(contract("fixture"), fixture_decider=lambda _: None)
    assert actor.update("deployment_start", row(), decision_deadline_s=120.) is None
    assert actor.update("deployment_start", row(120.)) == "continue"
    assert actor.records[0]["request"]["decision_deadline_s"] == 120.
    assert actor.records[0]["dispatch"]["rejection"] == "decision_deadline_reached"


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), 99., 100.])
def test_invalid_local_deadline_is_rejected(deadline):
    actor = MissionDirector(contract("fixture"), fixture_decider=lambda _: None)
    with pytest.raises(ValueError, match="invalid_decision_deadline"):
        actor.update("deployment_start", row(), decision_deadline_s=deadline)


def test_response_injection_cannot_enable_live_model_spend():
    with pytest.raises(ValueError, match="fixture_response_fault_only"):
        MissionDirector(contract("live"), response_fault="invalid_deployment_monitor")


def test_temporary_release_gate_does_not_permanently_abort_running_plan():
    observed = row()
    observed["numerical_tools"]["orbit_release_feasible"] = False
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision,
                            response_fault="invalid_deployment_monitor")
    assert actor.update("deployment_monitor", observed) == "continue"
    # Only keeping the sequence queued is approved. The command itself cannot
    # override the instantaneous physical release gate.
    assert check_action(contract("fixture"), "deployment_monitor", "continue", observed,
                        elapsed_s=0, observation_requests=0, hold_used_s=0) is None


def test_valid_diagnostic_resume_survives_temporary_release_gate():
    observed = row(sequencer_state="held")
    observed["numerical_tools"].update(mechanism_status="clear", orbit_release_feasible=False)
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision)
    assert actor.update("deployment_diagnostic", observed) == "continue"
    assert actor.records[0]["dispatch"]["rules_accepted"] is True


@pytest.mark.parametrize("kind", [None, "invalid", "timeout"])
def test_hold_has_one_bounded_reassessment_and_no_implicit_restart(kind):
    fault = "hold_deployment_start" if kind is None else kind+"_deployment_reassessment"
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision, response_fault=fault)
    assert actor.update("deployment_start", row()) == "hold"
    held = row(105., sequencer_state="held", hold_expires_at_s=130.)
    result = actor.update("deployment_reassessment", held, decision_deadline_s=130.)
    if kind == "timeout":
        assert result is None
        result = actor.update("deployment_reassessment", row(130., sequencer_state="skipped"))
    assert result == ("continue" if kind is None else "stop_deployment")
    assert actor.hold_used_s == 30.
    assert actor.records[-1]["request"]["decision_deadline_s"] == 130.
    assert actor.update("deployment_reassessment", held) is None
    if kind == "invalid":
        assert actor.records[-1]["dispatch"]["escalation_requested"] is False
        assert actor.records[-1]["dispatch"]["response_invalid"] is True


@pytest.mark.parametrize("updates", [
    {"time_s": 130.}, {"hold_expires_at_s": None}, {"sequencer_state": "inhibited"},
    {"sequencer_state": "skipped"},
])
def test_reassessment_cannot_resume_an_expired_or_inhibited_hold(updates):
    observed = row(105., sequencer_state="held", hold_expires_at_s=130.)
    observed.update(updates)
    assert check_action(contract("fixture"), "deployment_reassessment", "continue", observed,
                        elapsed_s=0, observation_requests=0, hold_used_s=30) == "hold_not_active"


def test_verifier_rejects_an_unearned_reassessment(monkeypatch):
    from src.runtime import starship_mission_director_verifier as verifier
    study = stub_study(monkeypatch)
    item = study["runs"][1]["mission_director"]["records"][0]
    item["request"]["point"] = "deployment_reassessment"
    item["request"]["allowed_actions"] = POINTS["deployment_reassessment"]
    item["request"]["observation"].update(sequencer_state="held", hold_expires_at_s=130.)
    item["response"]["request_sha256"] = digest(item["request"])
    result = verifier.verify(study, expected_case="normal", expected_envelope=contract("fixture"))
    assert {"code": "hold_reassessment_binding"} in result["issues"]


def test_verifier_rejects_a_fabricated_human_referral(monkeypatch):
    from src.runtime import starship_mission_director_verifier as verifier
    study = stub_study(monkeypatch)
    study["runs"][1]["mission_director"]["records"][0]["dispatch"]["escalation_requested"] = True
    result = verifier.verify(study, expected_case="normal", expected_envelope=contract("fixture"))
    assert {"code": "referral_semantics_mismatch"} in result["issues"]


@pytest.mark.parametrize("state", ["held", "inhibited", "skipped"])
def test_hold_cannot_launder_a_stopped_sequence_into_a_resumable_state(state):
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision,
                            response_fault="hold_deployment_monitor")
    assert actor.update("deployment_monitor", row(sequencer_state=state)) == "stop_deployment"
    assert actor.records[0]["dispatch"]["rejection"] == "hold_cannot_override_sequence_state"


def test_actor_rejects_a_reassessment_without_an_actual_granted_hold():
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision)
    with pytest.raises(ValueError, match="hold_reassessment_not_approved"):
        actor.update("deployment_reassessment", row(105., sequencer_state="held", hold_expires_at_s=130.),
                     decision_deadline_s=130.)


def test_exhausted_or_too_late_diagnostics_are_not_offered():
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision, response_fault="hold_deployment_start")
    actor.update("deployment_start", row())
    actor.observation_requests = 1
    actor.update("deployment_reassessment", row(105., sequencer_state="held", hold_expires_at_s=130.), decision_deadline_s=130.)
    assert "collect_status" not in actor.records[-1]["request"]["allowed_actions"]
    assert check_action(contract("fixture"), "deployment_reassessment", "collect_status",
        row(128., sequencer_state="held", hold_expires_at_s=130.), elapsed_s=0,
        observation_requests=0, hold_used_s=30) == "diagnostic_window_exhausted"


def test_hold_reserves_the_return_decision_window():
    assert check_action(contract("fixture"), "deployment_monitor", "hold", row(return_deadline_s=200.),
        elapsed_s=0, observation_requests=0, hold_used_s=0) == "hold_budget_exhausted"


def test_clear_diagnostic_cannot_resume_a_latched_interlock():
    observed=row(sequencer_state="inhibited")
    observed["numerical_tools"]["mechanism_status"]="clear"
    actor=MissionDirector(contract("fixture"),fixture_decider=fixture_decision)
    assert actor.update("deployment_diagnostic",observed)=="stop_deployment"
    assert actor.records[0]["dispatch"]["rejection"]=="release_envelope_not_met"


def hold_study(monkeypatch, timeout=False):
    from src.runtime import starship_mission_director_verifier as verifier
    study=stub_study(monkeypatch)
    actor=MissionDirector(contract("fixture"),fixture_decider=fixture_decision,
        response_fault="timeout_deployment_reassessment" if timeout else "hold_deployment_start")
    actor.update("deployment_start",row(),decision_deadline_s=125.)
    actor.update("deployment_reassessment",row(105.,sequencer_state="held",hold_expires_at_s=130.),decision_deadline_s=130.)
    if timeout:
        actor.update("deployment_reassessment",row(130.,sequencer_state="skipped"))
    actor.confirm("ship",row(131. if timeout else 106.,sequencer_state="skipped" if timeout else "running"))
    managed=study["runs"][1]
    managed["mission_director"]=actor.finish()
    managed["final_state"]["time_s"]=2100.
    managed["events"][1]["action"]="hold"
    if timeout:
        managed["events"].extend([{"event":"managed_hold_expired","time_s":130.},
            {"event":"managed_mission_command","time_s":130.,"action":"stop_deployment"}])
    else:
        managed["events"].extend([{"event":"managed_mission_command","time_s":105.,"action":"continue"},
            {"event":"managed_deployment_resumed","time_s":105.}])
    study["response_fault"]=actor.response_fault
    study["comparison"]=verifier.comparison("normal",*study["runs"])
    return study


@pytest.mark.parametrize("mutation,code", [("during","release_during_hold_or_diagnostic"),
    ("after","release_after_hold_expiry"),("no_expiry","hold_expiry_event_missing"),
    ("after_inhibit","release_after_inhibit"),("no_resume","resume_effect_not_observed")])
def test_verifier_checks_pause_intervals_not_only_snapshot_counts(monkeypatch,mutation,code):
    from src.runtime import starship_mission_director_verifier as verifier
    study=hold_study(monkeypatch,timeout=mutation in ("after","no_expiry"))
    fault=study["response_fault"]
    assert verifier.verify(study,expected_case="normal",expected_envelope=contract("fixture"),expected_response_fault=fault)["passed"]
    managed=study["runs"][1]
    if mutation in ("during","after","after_inhibit"):
        t=103. if mutation=="during" else 131.
        managed["events"].append({"event":"payload_released","time_s":t})
        managed["outcome"]["payload_released_count"]=1
        if mutation=="after_inhibit":
            managed["events"].append({"event":"deployment_interlock_inhibited","time_s":110.})
        for item in managed["mission_director"]["records"]:
            for observed in (item["request"]["observation"],item["dispatch"]["observation"],item["later_observation"]):
                observed["released_count"]=int(observed["time_s"]>t)
            item["response"]["request_sha256"]=digest(item["request"])
    elif mutation=="no_expiry":
        managed["events"]=[e for e in managed["events"] if e["event"]!="managed_hold_expired"]
    else:
        managed["mission_director"]["records"][-1]["later_observation"]["sequencer_state"]="held"
    study["comparison"]=verifier.comparison("normal",*study["runs"])
    result=verifier.verify(study,expected_case="normal",expected_envelope=contract("fixture"),expected_response_fault=fault)
    assert {"code":code} in result["issues"]


def test_verdict_identity_separates_different_valid_runs(monkeypatch):
    from src.runtime import starship_mission_director_verifier as verifier
    a=stub_study(monkeypatch)
    first=verifier.verify(a,expected_case="normal",expected_envelope=contract("fixture"))
    assert first["passed"]
    item=a["runs"][1]["mission_director"]["records"][0]
    item["request"]["request_id"]=item["response"]["request_id"]="other:1"
    item["response"]["request_sha256"]=digest(item["request"])
    second=verifier.verify(a,expected_case="normal",expected_envelope=contract("fixture"))
    assert second["passed"]
    assert first["study_canonical_sha256"]!=second["study_canonical_sha256"]
    assert first["observed_run_id"]!=second["observed_run_id"]
    assert first["verifier_source_sha256"]==second["verifier_source_sha256"]


def test_physical_hold_status_diagnostic_resume_chain():
    from pathlib import Path
    from src.runtime.starship_return_sites import ReturnSites
    from src.runtime.starship_sixdof_mission import simulate
    root=Path(__file__).resolve().parents[2]
    p=json.loads((root/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    c=json.loads((root/"examples/spaceflight/starship-catch-profile.json").read_text())
    sites=ReturnSites.from_dict(json.loads((root/"examples/spaceflight/starship-return-sites-model-test.json").read_text()),profile=p,catch_config=c)
    def decide(request):
        reply=fixture_decision(request)
        if request["point"]=="deployment_reassessment":
            reply["action"]="collect_status"
        return reply
    actor=MissionDirector(contract("fixture"),fixture_decider=decide,response_fault="hold_deployment_start")
    run=simulate(p,duration_s=550.,mission_case="normal",mission_director=actor,return_sites=sites)
    points={r["request"]["point"]:r for r in actor.records}
    assert points["deployment_reassessment"]["dispatch"]["action"]=="collect_status"
    assert points["deployment_diagnostic"]["dispatch"]["rules_accepted"] is True
    assert points["deployment_diagnostic"]["later_observation"]["sequencer_state"]=="running"
    collected=next(e for e in run["events"] if e["event"]=="managed_mechanism_diagnostic")
    assert collected["time_s"] >= points["deployment_reassessment"]["dispatch"]["time_s"]+2.
    assert run["outcome"]["payload_released_count"]>0
    assert not any(e["event"]=="managed_hold_expired" for e in run["events"])


def test_late_valid_mailbox_reply_is_rejected(tmp_path):
    actor = MissionDirector(contract("fixture"), tmp_path, "late")
    assert actor.update("deployment_start", row(), decision_deadline_s=120.) is None
    req = json.loads((tmp_path/"request.json").read_text())
    (tmp_path/"response.json").write_text(json.dumps(fixture_decision(req)))
    assert actor.update("deployment_start", row(120.)) == "continue"
    assert actor.records[0]["dispatch"]["rules_accepted"] is False
    assert actor.records[0]["dispatch"]["rejection"] == "decision_deadline_reached"


def test_old_contract_cannot_acquire_new_fallback_authority():
    old = contract("fixture")
    old["schema"] = "missionos.starship_mission_envelope.v1"
    old["no_response"] = {"deployment": "stop_deployment", "return": "retained_return", "booster": "divert"}
    with pytest.raises(ValueError, match="invalid_mission_envelope"):
        MissionDirector(old)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_approved_old_envelope_is_rejected_before_process_spawn(tmp_path, monkeypatch, version):
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setattr(qualification, "readiness", lambda *a, **k: ({}, "fixture", None))
    monkeypatch.setenv(MODE_ENV, "fixture")
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    service.plan("migration", "Starship sixdof_managed_normal")
    state = service._load("migration")
    plan = state["plan"]
    plan["mission_envelope"]["schema"] = f"missionos.starship_mission_envelope.v{version}"
    plan["sha256"] = control._digest({k:v for k,v in plan.items() if k != "sha256"})
    service._save("migration", state)
    # Even a locally signed approval of a correctly self-hashed old contract
    # cannot acquire the new worker's fallback permissions.
    ref = ("migration", plan["id"], plan["sha256"])
    service.approve(*ref)
    with pytest.raises(control.StarshipMissionError, match="mission_envelope_not_current"):
        service.execute(*ref)
    assert service._load("migration")["execution"] == {}


def stub_study(monkeypatch):
    from copy import deepcopy
    from src.runtime import starship_mission_director_verifier as verifier
    monkeypatch.setattr(verifier, "verify_study", lambda *a, **k: {"passed": True, "issues": []})
    monkeypatch.setattr(verifier, "verify_retained_return", lambda *a, **k: {"passed": True, "issues": []})
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision)
    actor.update("deployment_start", row(), decision_deadline_s=125.)
    actor.confirm("ship", row(101.))
    run = {"initial_state": {}, "mission_management_case": "normal",
        "events": [{"event": "orbit_cutoff_command", "time_s": 80.25},
                   {"event": "managed_mission_command", "time_s": 100., "action": "continue"},
                   {"event": "return_requested", "time_s": 2000.}],
        "outcome": {"payload_released_count": 0, "orbit_gate_reached": True, "termination": "time_limit"},
        "final_state": {"propellant_kg": 50000.},
        "booster_run": {"active_return_site": {"site_id": "divert"}, "events": []},
        "retained_return": {"policy_id": "fixed_v1"}}
    managed = {**deepcopy(run), "mission_director": actor.finish()}
    return {"schema": "missionos.starship_managed_study.v1", "case": "normal", "physical_execution": False,
        "profile": {"guidance": {"orbit_release_delay_s": 45.}, "integration": {"coast_dt_s": .25}},
        "envelope": contract("fixture"), "return_sites": {"sites": [{}, {"site_id": "divert"}]},
        "runs": [run, managed], "comparison": verifier.comparison("normal", run, managed)}


@pytest.mark.parametrize("mutation,expected", [
    ("deadline", "initial_deadline_schedule_mismatch"), ("early_timeout", "premature_timeout"),
    ("study_fault", "response_fault_binding"), ("record_fault", "response_fault_binding"),
    ("not_exercised", "response_fault_not_exercised"), ("live_fault", "response_fault_binding"),
])
def test_verifier_rejects_false_deadlines_and_fault_claims(monkeypatch, mutation, expected):
    from src.runtime import starship_mission_director_verifier as verifier
    study = stub_study(monkeypatch)
    assert verifier.verify(study, expected_case="normal", expected_envelope=contract("fixture"))["passed"]
    record = study["runs"][1]["mission_director"]
    item = record["records"][0]
    fault, envelope = None, contract("fixture")
    if mutation == "deadline":
        item["request"]["decision_deadline_s"] = 175.
        item["response"]["request_sha256"] = digest(item["request"])
    elif mutation == "early_timeout":
        item["response"]["mode"] = "timeout_fallback"
        item["dispatch"].update(rules_accepted=False, rejection="response_binding_mismatch")
    elif mutation == "study_fault":
        study["response_fault"] = "invalid_deployment_monitor"
    elif mutation == "record_fault":
        record["response_fault"] = "invalid_deployment_monitor"
    else:
        fault = "invalid_deployment_monitor"
        study["response_fault"] = record["response_fault"] = fault
        if mutation == "live_fault":
            envelope = study["envelope"] = contract("live")
    result = verifier.verify(study, expected_case="normal", expected_envelope=envelope, expected_response_fault=fault)
    assert result["passed"] is False
    assert {"code": expected} in result["issues"]


@pytest.mark.skipif(os.environ.get("MISSIONOS_STARSHIP_FULL_FALLBACK_TEST") != "1",
                    reason="Opt-in full 6DOF response-failure regressions")
@pytest.mark.parametrize("fault", ["invalid_deployment_monitor", "invalid_return_selection",
                                  "timeout_deployment_start", "timeout_deployment_monitor"])
def test_full_response_failure_preserves_nominal_trajectory(tmp_path, fault):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    output = tmp_path/"full-flight"
    subprocess.run([sys.executable, str(root/"scripts/run_starship_managed_mission.py"),
        "--approve-simulation", "--case", "normal", "--response-fault", fault,
        "--output-dir", str(output)], cwd=root, check=True, capture_output=True, timeout=1200)
    study = json.loads((output/"study.json").read_text())
    assert study["response_fault"] == fault
    assert study["comparison"]["normal_outcomes_equal"] is True
    assert study["comparison"]["comparison_accepted"] is True


def test_missing_start_decision_preserves_release_schedule_under_new_grant():
    from pathlib import Path
    from src.runtime.starship_return_sites import ReturnSites
    from src.runtime.starship_sixdof_mission import simulate
    root = Path(__file__).resolve().parents[2]
    profile = json.loads((root/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((root/"examples/spaceflight/starship-catch-profile.json").read_text())
    sites = ReturnSites.from_dict(json.loads((root/"examples/spaceflight/starship-return-sites-model-test.json").read_text()),
                                  profile=profile, catch_config=catch)
    actor = MissionDirector(contract("fixture"), fixture_decider=lambda _: None)
    # The preapproved fallback must resolve before the first release slot;
    # it cannot postpone it while waiting for the longer generic expiry.
    run = simulate(profile, duration_s=540., mission_case="normal", mission_director=actor, return_sites=sites)
    assert run["outcome"]["orbit_gate_reached"] is True
    assert run["outcome"]["payload_released_count"] > 0
    assert run["final_state"]["time_s"] >= 540.
    first_release = next(e["time_s"] for e in run["events"] if e["event"] == "payload_released")
    assert actor.records[0]["dispatch"]["time_s"] < first_release
    assert actor.records[0]["dispatch"]["action"] == "continue"
    assert actor.records[0]["dispatch"]["rules_accepted"] is False


@pytest.mark.parametrize("early_release,expected", [
    (False, "observed_release_inventory_mismatch"), (True, "release_before_start_decision"),
])
def test_verifier_rejects_forged_inventory_or_release_before_start(monkeypatch, early_release, expected):
    from copy import deepcopy
    from src.runtime import starship_mission_director_verifier as verifier
    # These stubs isolate the mission authority boundary; existing physics
    # verification has its own independent conservation/contact tests.
    monkeypatch.setattr(verifier, "verify_study", lambda *a, **k: {"passed": True, "issues": []})
    monkeypatch.setattr(verifier, "verify_retained_return", lambda *a, **k: {"passed": True, "issues": []})
    actor = MissionDirector(contract("fixture"), fixture_decider=fixture_decision)
    actor.update("deployment_start", row(released_count=1), decision_deadline_s=125.)
    actor.confirm("ship", row(101., released_count=1))
    events = [{"event": "orbit_cutoff_command", "time_s": 80.25},
              {"event": "managed_mission_command", "time_s": 100., "action": "continue"},
              {"event": "return_requested", "time_s": 2000.}]
    if early_release:
        events.insert(0, {"event": "payload_released", "time_s": 99.})
    run = {"initial_state": {}, "mission_management_case": "normal", "events": events,
        "outcome": {"payload_released_count": int(early_release), "orbit_gate_reached": True,
                    "termination": "time_limit"}, "final_state": {"propellant_kg": 50000.},
        "booster_run": {"active_return_site": {"site_id": "divert"}, "events": []},
        "retained_return": {"policy_id": "fixed_v1"}}
    managed = {**deepcopy(run), "mission_director": actor.finish()}
    study = {"schema": "missionos.starship_managed_study.v1", "case": "normal", "physical_execution": False,
        "profile": {"guidance": {"orbit_release_delay_s": 45.}, "integration": {"coast_dt_s": .25}},
        "envelope": contract("fixture"), "return_sites": {"sites": [{}, {"site_id": "divert"}]},
        "runs": [run, managed], "comparison": verifier.comparison("normal", run, managed)}
    result = verifier.verify(study, expected_case="normal", expected_envelope=contract("fixture"))
    assert result["passed"] is False
    assert {"code": expected} in result["issues"]


@pytest.mark.parametrize('reason', ['return_qualification_backend_mismatch', 'return_qualification_source_mismatch'])
def test_unqualified_environment_is_rejected_before_approval_and_worker(tmp_path, monkeypatch, reason):
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setenv(MODE_ENV, 'fixture')
    monkeypatch.setattr(qualification, 'readiness', lambda *a, **k: (None, 'fixture', reason))
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, 'fixture'))
    with pytest.raises(control.StarshipMissionError, match=reason):
        service.plan('blocked', 'Starship sixdof_managed_normal')
    assert service.current('blocked') is None
    assert not list(tmp_path.glob('run-*'))


def test_environment_change_after_approval_cannot_spawn_worker(tmp_path, monkeypatch):
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setenv(MODE_ENV, 'fixture')
    monkeypatch.setattr(qualification, 'readiness', lambda *a, **k: ({}, 'fixture', None))
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, 'fixture'))
    plan = service.plan('changed', 'Starship sixdof_managed_normal')['plan']
    ref = ('changed', plan['id'], plan['sha256'])
    service.approve(*ref)
    monkeypatch.setattr(qualification, 'readiness', lambda *a, **k: (None, 'fixture', 'return_qualification_backend_mismatch'))
    with pytest.raises(control.StarshipMissionError, match='return_qualification_backend_mismatch'):
        service.execute(*ref)
    assert not list(tmp_path.glob('run-*'))
