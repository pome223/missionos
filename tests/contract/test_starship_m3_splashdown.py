"""Body authority and recorded goal wiring; no numerical flight or model calls."""
from copy import deepcopy
import json

import pytest

from src.intelligence.starship_mission_director import fixture_decision
from src.runtime.starship_deployment_handoff import DeploymentTracker
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_m3_directors import M3Directors
from src.runtime import starship_m3_prefix as prefix
from src.runtime import starship_m3_prefix_verifier as verifier
from src.runtime import starship_m3_fixture as composition
from src.runtime.starship_mission_director import contract
from src.runtime.starship_replanning import ROOT, digest
from src.runtime.starship_splashdown import SplashdownGoal


def observation(phase="orbital_coast", when=10.):
    return {"time_s": when, "phase": phase, "released_count": 0,
        "release_acknowledged": False, "sequencer_state": "running", "fuel_kg": 100000.,
        "return_deadline_s": 9000., "tower_ready": True, "operations_notice": "", "hold_expires_at_s": None,
        "numerical_tools": {"orbit_release_feasible": True, "retained_payload_present": False,
            "capture_corridor_certified": False, "mechanism_status": "not_collected"}}


def router():
    ship, water = contract("fixture"), contract("fixture", splashdown=True)
    return M3Directors(ship, water, DeploymentTracker(26, digest(m2_contract("fixture", "normal"))),
                       fixture_decider=fixture_decision, run_id="scope-test")


def test_ship_and_booster_decisions_keep_distinct_hashes_and_namespaces():
    actor = router()
    assert actor.update("deployment_start", observation()) == "continue"
    assert actor.update("booster_selection", observation("booster_return")) == "splashdown"
    actor.confirm("ship", observation(when=10.1))
    actor.confirm("booster", observation("booster_return", 10.1))
    ship, water = actor.finish(), actor.finish_booster()
    assert ship["envelope"] == contract("fixture")
    assert water["envelope"] == contract("fixture", splashdown=True)
    assert len(ship["records"]) == len(water["records"]) == 1
    assert ship["records"][0]["request"]["request_id"] == "scope-test-deployment:1"
    assert water["records"][0]["request"]["request_id"] == "scope-test-booster:1"
    assert ship["records"][0]["request"]["envelope_sha256"] == digest(ship["envelope"])
    assert water["records"][0]["request"]["envelope_sha256"] == digest(water["envelope"])
    assert "splashdown_goal" not in ship["envelope"]
    assert actor.pending is actor.ship.pending and actor.finished is actor.ship.finished
    assert actor.trace is actor.ship.trace and actor.tracker is actor.ship.tracker
    assert actor.observation_requests == actor.ship.observation_requests
    assert actor.hold_used_s == actor.ship.hold_used_s


@pytest.mark.parametrize("point,phase", [("return_selection", "orbital_coast"),
    ("booster_selection", "orbital_coast"), ("deployment_start", "booster_return"), ("unknown", "orbital_coast")])
def test_cross_body_or_unapproved_point_never_reaches_an_actor(point, phase):
    actor = router()
    with pytest.raises(ValueError):
        actor.update(point, observation(phase))
    assert not actor.finish()["records"] and not actor.finish_booster()["records"]


def test_registered_goal_is_required_by_real_simulator_entry_guard(monkeypatch):
    from src.runtime import starship_sixdof_mission as physics
    actor = router()
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    def reached(*args, **kwargs):
        raise RuntimeError("after_goal_guard_before_plant_initialization")
    monkeypatch.setattr(physics, "vehicle", reached)
    with pytest.raises(ValueError, match="unapproved_splashdown_goal"):
        next(physics._simulate_steps(profile, mission_director=actor, mission_case="normal"))
    goal = SplashdownGoal.from_dict(actor.envelope["splashdown_goal"])
    with pytest.raises(RuntimeError, match="after_goal_guard_before_plant_initialization"):
        next(physics._simulate_steps(profile, mission_director=actor, mission_case="normal", splashdown_goal=goal))


def test_outer_scope_addition_does_not_change_m2_authority():
    for case in prefix.CASES:
        ordinary = m2_contract("fixture", case)
        current, old = prefix.m3_prefix_contract(case), prefix.m3_prefix_contract(case, splashdown=False)
        assert current["m2_authority"] == old["m2_authority"] == ordinary
        assert current["m2_authority_sha256"] == old["m2_authority_sha256"] == digest(ordinary)
        assert current["schema"].endswith(".v2") and old["schema"].endswith(".v1")
        assert current["booster_authority"] == contract("fixture", splashdown=True)
        full = composition.contract(case)
        assert full["prefix_authority"] == current and full["m2_authority"] == ordinary


@pytest.fixture
def water_study(monkeypatch):
    from src.runtime import starship_sixdof_verifier
    monkeypatch.setattr(starship_sixdof_verifier, "verify_study", lambda *_a, **_k: {"passed": True})
    actor = router()
    assert actor.update("booster_selection", observation("booster_return")) == "splashdown"
    actor.confirm("booster", observation("booster_return", 10.1))
    authority = prefix.m3_prefix_contract("normal")
    site = json.loads((ROOT / "examples/spaceflight/starship-return-sites-model-test.json").read_text())["sites"][1]
    goal = authority["booster_authority"]["splashdown_goal"]
    run = {"mission_director": actor.finish(), "booster_mission_director": actor.finish_booster(),
        "booster_run": {"final_state": {"time_s": 20.}, "active_return_site": site, "contact": None,
            "events": [{"event": "managed_booster_command", "time_s": 10., "action": "splashdown", "return_site_sha256": digest(site)}],
            "splashdown": {"goal": goal, "contact_observed": False, "controlled_water_entry_envelope_met": False,
                "water_response_verified": False, "physical_execution": False}}}
    study = {"profile": {}, "run_id": "scope-test", "authority": authority, "prefix_run": run}
    rows = [{"body_id": "booster", "time_s": t} for t in (10., 10.1, 20.)]
    return study, rows


def test_failed_water_entry_remains_negative_even_with_consistent_records(water_study):
    study, rows = water_study
    result = verifier._booster_splashdown(study, rows)
    assert result["passed"] and result["trajectory_record_verified"]
    assert result["controlled_water_entry_envelope_met"] is False
    assert result["physical_execution"] is result["independent_physics_validation"] is False


@pytest.mark.parametrize("mutate", [
    lambda s: s["prefix_run"]["booster_mission_director"]["envelope"]["splashdown_goal"].update(area_radius_m=9999.),
    lambda s: s["prefix_run"]["booster_mission_director"]["records"][0].update(clock_id="ship"),
    lambda s: s["prefix_run"]["booster_mission_director"]["records"][0]["request"].update(envelope_sha256="0"*64),
    lambda s: s["prefix_run"]["booster_mission_director"]["records"][0]["later_observation"].update(time_s=10.05),
    lambda s: s["prefix_run"]["booster_mission_director"]["records"].append(deepcopy(s["prefix_run"]["booster_mission_director"]["records"][0])),
    lambda s: s["prefix_run"]["booster_run"]["events"][0].update(return_site_sha256="0"*64),
    lambda s: s["prefix_run"]["booster_run"]["events"].append(deepcopy(s["prefix_run"]["booster_run"]["events"][0])),
    lambda s: s["prefix_run"]["booster_run"]["splashdown"].update(controlled_water_entry_envelope_met=True),
])
def test_booster_goal_dispatch_or_followup_corruption_rejected(water_study, mutate):
    study, rows = deepcopy(water_study)
    mutate(study)
    with pytest.raises(ValueError):
        verifier._booster_splashdown(study, rows)


def test_common_trajectory_failure_cannot_be_replaced_by_goal_check(water_study, monkeypatch):
    from src.runtime import starship_sixdof_verifier
    study, rows = water_study
    monkeypatch.setattr(starship_sixdof_verifier, "verify_study", lambda *_a, **_k: {"passed": False})
    with pytest.raises(ValueError, match="trajectory_record_invalid"):
        verifier._booster_splashdown(study, rows)


def test_continuation_rejects_other_full_prefix_scope_before_record_verification(monkeypatch):
    authority = composition.contract("normal")
    old = prefix.m3_prefix_contract("normal", splashdown=False)
    monkeypatch.setattr(composition, "verify_prefix", lambda _: pytest.fail("wrong outer grant must fail first"))
    with pytest.raises(ValueError, match="outer_authority_mismatch"):
        composition.validate_prefix({"authority": old}, authority["m2_authority"], {},
                                    expected_prefix_authority=authority["prefix_authority"])


def test_prefix_runner_passes_goal_and_saves_booster_record(tmp_path, monkeypatch):
    from src.runtime import starship_m3_prefix_verifier
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    seen = {}
    monkeypatch.setattr(prefix, "sources", lambda: {"fixture.py": "0"*64})
    monkeypatch.setattr(starship_m3_prefix_verifier, "verify", lambda _: {"passed": False, "handoff_ready": False})
    def simulate(_profile, **kwargs):
        seen.update(kwargs)
        actor = kwargs["mission_director"]
        assert kwargs["splashdown_goal"].to_dict() == actor.envelope["splashdown_goal"]
        actor.update("booster_selection", observation("booster_return"))
        actor.confirm("booster", observation("booster_return", 10.1))
        return {"mission_director": actor.finish(), "final_state": {"time_s": 20.},
            "outcome": {"phase": "ship_ascent", "payload_released_count": 0},
            "booster_run": {"outcome": {"termination": "time_limit"}}}
    monkeypatch.setattr(prefix, "simulate_concurrent", simulate)
    result = prefix.run_fixture(profile, "normal", tmp_path, run_id="integration-test")
    assert result["prefix_run"]["mission_director"]["envelope"] == m2_contract("fixture", "normal")["deployment_authority"]
    saved = json.loads((tmp_path / "prefix-run.json").read_text())
    assert saved["booster_mission_director"]["records"][0]["request"]["request_id"] == "integration-test-booster:1"
    assert saved["mission_director"]["records"] == []
    assert result["approval_binding"]["booster_authority_sha256"] == digest(contract("fixture", splashdown=True))
    assert result["run_id"] == result["approval_binding"]["run_id"] == "integration-test"
