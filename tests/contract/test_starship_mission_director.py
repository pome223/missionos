"""Authority, provider isolation, delayed dispatch and physically applied scope."""
import json

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
        "return_deadline_s": 2000., "tower_ready": True, "operations_notice": "",
        "numerical_tools": {"orbit_release_feasible": True, "retained_payload_possible": True,
            "capture_corridor_certified": False, "mechanism_status": "not_collected"}}
    result.update(updates)
    return result


def request(point="deployment_start", observation=None, mode="fixture"):
    return {"schema": "missionos.starship_director_request.v1", "request_id": "run:1", "point": point,
            "observation": observation or row(), "allowed_actions": POINTS[point],
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
    assert actor.update("deployment_start", row(101.)) == "stop_deployment"
    assert actor.records[0]["dispatch"]["rejection"] == "response_binding_mismatch"
    actor.confirm("ship", row(102., sequencer_state="running"))
    # Adapter records what it sees; it cannot forge a skipped sequencer.
    assert actor.records[0]["later_observation"]["sequencer_state"] == "running"
    timeout = MissionDirector(contract("fixture"), tmp_path, "timeout-run")
    assert timeout.update("deployment_start", row()) is None
    assert timeout.update("deployment_start", row(175.)) == "stop_deployment"
    assert timeout.records[0]["dispatch"]["rejection"] == "decision_expired"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_plan_and_single_consumption_scope(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv(MODE_ENV, "fixture")
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("operator", "Starship "+scenario)
    plan = state["plan"]
    assert plan["backend"] == "starship_mission_management"
    assert plan["mission_envelope"] == contract("fixture")
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
    for _ in range(4):
        agent.assess(request(mode="live"))
    with pytest.raises(ValueError, match="call_budget_exhausted"):
        agent.assess(request(mode="live"))


def test_worker_environment_never_contains_director_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-never-disclose")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-jev-never-disclose")
    monkeypatch.setenv(MODE_ENV, "live")
    env = control.process_environment({"scenario": "sixdof_managed_normal", "mission_envelope": contract("live")})
    assert not {"DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", MODE_ENV} & set(env)


def test_pending_start_decision_keeps_coasting_without_releasing():
    from pathlib import Path
    from src.runtime.starship_return_sites import ReturnSites
    from src.runtime.starship_sixdof_mission import simulate
    root = Path(__file__).resolve().parents[2]
    profile = json.loads((root/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((root/"examples/spaceflight/starship-catch-profile.json").read_text())
    sites = ReturnSites.from_dict(json.loads((root/"examples/spaceflight/starship-return-sites-model-test.json").read_text()),
                                  profile=profile, catch_config=catch)
    actor = MissionDirector(contract("fixture"), fixture_decider=lambda _: None)
    # The first scheduled release is inside this horizon; the response expiry
    # is beyond it. Test actual integration, not a mocked sequencer predicate.
    run = simulate(profile, duration_s=540., mission_case="normal", mission_director=actor, return_sites=sites)
    assert run["outcome"]["orbit_gate_reached"] is True
    assert run["outcome"]["payload_released_count"] == 0
    assert run["final_state"]["time_s"] >= 540.
    assert actor.records[0]["dispatch"] is None


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
    actor.update("deployment_start", row(released_count=1))
    actor.confirm("ship", row(101., released_count=1))
    events = [{"event": "managed_mission_command", "time_s": 100., "action": "continue"},
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
        "profile": {}, "envelope": contract("fixture"), "return_sites": {"sites": [{}, {"site_id": "divert"}]},
        "runs": [run, managed], "comparison": verifier.comparison("normal", run, managed)}
    result = verifier.verify(study, expected_case="normal", expected_envelope=contract("fixture"))
    assert result["passed"] is False
    assert {"code": expected} in result["issues"]
