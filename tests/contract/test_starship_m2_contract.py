"""M2 authority/continuation input checks; no model or numerical flight."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from src.runtime.starship_deployment_handoff import DeploymentTracker
from src.runtime.starship_m2_contract import (
    CHECKPOINT_TIME_S, PROFILE, m2_contract, validate_deployment_origin,
)
from src.runtime.starship_replanning import ROOT, contract as m1_contract, digest
from src.runtime.starship_sixdof import EngineState, State6DOF
from src.runtime.starship_sixdof_mission import vehicle


def copy_json(value):
    return json.loads(json.dumps(value, allow_nan=False))


def row(when, count, sequence="running"):
    return {"time_s": when, "released_count": count, "sequencer_state": sequence}


def record(action, when, later):
    return {"clock_id": "ship", "request": {"point": "deployment_monitor",
            "observation": {"time_s": when}}, "dispatch": {"action": action, "time_s": when},
            "later_observation": {"time_s": later}}


def fixture(released=1, *, mode="fixture", case="operations_notice"):
    profile = json.loads((ROOT / PROFILE).read_text())
    envelope = m2_contract(mode, case)
    tracker = DeploymentTracker(profile["payload"]["count"], digest(envelope))
    tracker.observe(row(500., 0))
    tracker.command("continue", 500.)
    tracker.observe(row(500.25, 0))
    events = [{"event": "orbit_cutoff_command", "time_s": 480.7},
              {"event": "managed_mission_command", "point": "deployment_start", "action": "continue", "time_s": 500.}]
    records = [record("continue", 500., 500.25)]
    now = 500.25
    for count in range(1, released + 1):
        now = 500. + count * 15.
        events.append({"event": "payload_released", "time_s": now - .125})
        tracker.observe(row(now, count))
    sequence = "running"
    if released < profile["payload"]["count"]:
        tracker.command("stop_deployment", now)
        events.append({"event": "managed_mission_command", "point": "deployment_monitor",
                       "action": "stop_deployment", "time_s": now})
        records.append(record("stop_deployment", now, now + .25))
        sequence = "skipped"
        tracker.observe(row(now + .25, released, sequence))
    tracker.observe(row(CHECKPOINT_TIME_S - .25, released, sequence))
    craft = vehicle(profile, payload_count=26 - released)
    state = State6DOF(CHECKPOINT_TIME_S, (6678137.123, 0., 0.), (0., 7725.0023, 0.),
        (.5, -.5, .5, .5), (.0010012, 0., 0.), 100001.5,
        tuple(EngineState(throttle=.01) for _ in craft.engines),
        tuple(.003 for _ in craft.aero_panels))
    handoff = tracker.freeze(copy_json(asdict(state)), digest(profile))
    launch = {
        "scenario": "launch", "mission_management_case": envelope["deployment_case"],
        "mission_director": {"envelope": deepcopy(envelope["deployment_authority"]),
                             "response_fault": envelope["deployment_response_fault"], "records": records},
        "events": events, "samples": [{**copy_json(asdict(state)), "phase": "orbital_coast"}],
        "final_state": copy_json(asdict(state)), "final_vehicle": copy_json(asdict(craft)),
        "outcome": {"orbit_gate_reached": True, "phase": "orbital_coast", "termination": "time_limit",
                    "duration_s": CHECKPOINT_TIME_S, "payload_released_count": released},
    }
    return profile, envelope, handoff, launch


@pytest.mark.parametrize("case", ("normal", "operations_notice", "release_fault", "hold_resume"))
def test_one_grant_reuses_fixed_m1_event_normal_operations(case):
    envelope = m2_contract("fixture", case)
    assert envelope["return_case"] == "event_normal"
    assert envelope["return_authority"] == m1_contract("fixture", "event_normal")
    assert envelope["maximum_model_calls"] == 0
    assert envelope["phase_provider_policy"]["deployment"] == "jev_only_with_remaining_deadline"
    assert envelope["ship_return_dispatch_authorized"] is True
    assert envelope["physical_execution_authorized"] is False
    if case == "hold_resume":
        assert envelope["deployment_case"] == "normal"
        assert envelope["deployment_response_fault"] == "hold_deployment_start"
    # Nested mutable returns never change the next approval contract or M1.
    envelope["return_authority"]["operations"]["areas"]["east"]["radius_m"] = 1.
    assert m2_contract("fixture", case)["return_authority"] == m1_contract("fixture", "event_normal")


def test_live_bounds_exclude_deployment_deepseek_even_though_inner_legacy_allows_it():
    envelope = m2_contract("live", "normal")
    assert envelope["deployment_authority"]["maximum_llm_calls"] == 6
    assert envelope["maximum_llm_calls"] is envelope["return_authority"]["maximum_llm_calls"] is None
    assert envelope["maximum_jev_calls"] == 22
    assert envelope["maximum_model_calls"] is None
    historical = m2_contract("live", "normal", legacy_call_limits=True)
    assert historical["maximum_llm_calls"] == 2 and historical["maximum_model_calls"] == 24
    assert envelope["return_authority"]["decision_timeout_s"] == 35.
    assert "orbital_planning_window" not in envelope["return_authority"]


@pytest.mark.parametrize("mode,case", [("off", "normal"), ("live", "hold_resume"), ("fixture", "arbitrary"), (True, "normal")])
def test_unregistered_grants_rejected(mode, case):
    with pytest.raises(ValueError):
        m2_contract(mode, case)


@pytest.mark.parametrize("released", [0, 1, 11, 25, 26])
def test_retained_inventory_exact_state_and_original_schedule_are_carried_forward(released):
    profile, envelope, handoff, launch = fixture(released)
    result = validate_deployment_origin(profile, envelope, handoff, launch)
    assert result["released_count"] == released
    assert result["retained_count"] == 26 - released
    assert result["execution_state"] == launch["final_state"]
    assert result["final_vehicle"] == launch["final_vehicle"]
    assert result["observation_origin"] == handoff["origin"]
    assert result["observation_origin"]["retained_count"] == 26 - released
    assert result["scheduled_s"] == 480.7 + profile["guidance"]["coast_before_return_s"]
    assert result["original_deadline_s"] == result["scheduled_s"] + 6000.
    assert result["generation"] == handoff["generation"]
    result["execution_state"]["engine_states"][0]["throttle"] = .99
    result["observation_origin"]["retained_count"] = -1
    assert launch["final_state"]["engine_states"][0]["throttle"] == .01
    assert handoff["origin"]["retained_count"] == 26 - released


@pytest.mark.parametrize("mutation", [
    "scope", "return_authority", "deployment_authority", "profile", "foreign_handoff",
    "final_state", "final_sample", "vehicle_mass", "vehicle_inertia", "count",
    "boolean_count", "phase", "termination", "outcome_time", "missing_release", "release_time",
    "missing_command", "wrong_command", "missing_confirmation", "early_confirmation",
    "duplicate_cutoff", "late_cutoff", "missing_cutoff", "prior_return", "case", "response_fault",
])
def test_changed_or_incomplete_origin_is_rejected(mutation):
    profile, envelope, handoff, launch = fixture()
    if mutation == "scope":
        envelope["scope"] = "arbitrary_flight"
    elif mutation == "return_authority":
        envelope["return_authority"]["maximum_delay_s"] += 1
    elif mutation == "deployment_authority":
        launch["mission_director"]["envelope"]["maximum_hold_s"] += 1
    elif mutation == "profile":
        profile["payload"]["mass_each_kg"] += 1
    elif mutation == "foreign_handoff":
        handoff["authority_sha256"] = "c" * 64
        handoff["handoff_sha256"] = digest({k: v for k, v in handoff.items() if k != "handoff_sha256"})
    elif mutation == "final_state":
        launch["final_state"]["r_eci_m"][0] += .001
    elif mutation == "final_sample":
        launch["samples"][-1]["propellant_kg"] += .001
    elif mutation == "vehicle_mass":
        launch["final_vehicle"]["dry_mass_kg"] += 1700.
    elif mutation == "vehicle_inertia":
        launch["final_vehicle"]["dry_inertia_kg_m2"][0][0] += 1.
    elif mutation == "count":
        launch["outcome"]["payload_released_count"] = 0
    elif mutation == "boolean_count":
        launch["outcome"]["payload_released_count"] = True
    elif mutation == "phase":
        launch["outcome"]["phase"] = "ballistic_return"
    elif mutation == "termination":
        launch["outcome"]["termination"] = "surface_impact"
    elif mutation == "outcome_time":
        launch["outcome"]["duration_s"] -= 1.
    elif mutation == "missing_release":
        launch["events"] = [e for e in launch["events"] if e["event"] != "payload_released"]
    elif mutation == "release_time":
        next(e for e in launch["events"] if e["event"] == "payload_released")["time_s"] = 1.
    elif mutation == "missing_command":
        launch["events"] = [e for e in launch["events"] if e["event"] != "managed_mission_command"]
    elif mutation == "wrong_command":
        launch["mission_director"]["records"][-1]["dispatch"]["action"] = "hold"
    elif mutation == "missing_confirmation":
        launch["mission_director"]["records"][-1]["later_observation"] = None
    elif mutation == "early_confirmation":
        item = launch["mission_director"]["records"][-1]
        item["later_observation"]["time_s"] = item["dispatch"]["time_s"]
    elif mutation == "duplicate_cutoff":
        launch["events"].append(deepcopy(launch["events"][0]))
    elif mutation == "late_cutoff":
        launch["events"][0]["time_s"] = CHECKPOINT_TIME_S + 1.
    elif mutation == "missing_cutoff":
        launch["events"] = launch["events"][1:]
    elif mutation == "prior_return":
        launch["events"].append({"event": "return_requested", "time_s": 900.})
    elif mutation == "case":
        launch["mission_management_case"] = "normal"
    else:
        launch["mission_director"]["response_fault"] = "hold_deployment_start"
    with pytest.raises(ValueError):
        validate_deployment_origin(profile, envelope, handoff, launch)


def test_live_origin_contract_does_not_claim_any_model_or_flight_executed():
    result = validate_deployment_origin(*fixture(mode="live"))
    assert result["retained_count"] == 25
    assert "model_calls" not in result and "passed" not in result


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_orbital_update_is_a_distinct_grant_with_normal_deployment(mode):
    authority = m2_contract(mode, "orbital_update")
    assert authority["deployment_case"] == "normal"
    assert authority["deployment_response_fault"] is None
    assert authority["deployment_authority"] == m2_contract(mode, "normal")["deployment_authority"]
    assert authority["return_case"] == "comparison_orbital_update"
    assert authority["return_authority"] == m1_contract(mode, "comparison_orbital_update")
    assert authority["return_authority"]["maximum_llm_calls"] == (None if mode == "live" else 1)
    assert authority["return_authority"]["orbital_planning_window"]["decision_timeout_s"] == 150.0
    assert authority["return_authority"]["comparison_preparation"]["deep_reasoning"]["protocol"] == "ids_orbital_low_16384"
    assert authority["maximum_model_calls"] == (None if mode == "live" else 0)
    assert digest(authority) != digest(m2_contract(mode, "normal"))
    result = validate_deployment_origin(*fixture(26, mode=mode, case="orbital_update"))
    assert result["retained_count"] == 0 and result["released_count"] == 26


def test_orbital_return_authority_cannot_be_added_to_existing_normal_grant():
    profile, authority, handoff, launch = fixture(26, case="normal")
    authority["return_authority"] = m1_contract("fixture", "comparison_orbital_update")
    with pytest.raises(ValueError, match="m2_authority_not_current"):
        validate_deployment_origin(profile, authority, handoff, launch)


def test_scope_reasoning_is_explicit_and_historical_authority_not_upgraded():
    from src.runtime.starship_m2_contract import m2_contract_matches
    from src.runtime.starship_m3_live import contract as m3_contract
    from src.runtime.starship_operational_trials import SCOPE_CLOSURE_CASE
    current = m2_contract("live", SCOPE_CLOSURE_CASE)
    historical = m2_contract("live", SCOPE_CLOSURE_CASE, authority_gap_reasoning=False)
    assert "authority_gap_reasoning" in current["return_authority"]
    assert "authority_gap_reasoning" not in historical["return_authority"]
    assert current != historical
    assert m2_contract_matches(current) and m2_contract_matches(historical)
    for case in ("normal", "release_fault", "op_trial_05", "op_trial_07"):
        assert "authority_gap_reasoning" not in m2_contract("live", case)["return_authority"]
        with pytest.raises(ValueError, match="not_registered"):
            m2_contract("live", case, authority_gap_reasoning=True)
    forged = deepcopy(current)
    forged["return_authority"]["authority_gap_reasoning"]["schema"] = "unregistered"
    assert not m2_contract_matches(forged)
    outer = m3_contract("live", "inflight_op_scope_01")
    old_outer = m3_contract("live", "inflight_op_scope_01", authority_gap_reasoning=False)
    assert outer["m2_authority"] == outer["prefix_authority"]["m2_authority"] == current
    assert old_outer["m2_authority"] == old_outer["prefix_authority"]["m2_authority"] == historical
    assert outer["m2_authority_sha256"] != old_outer["m2_authority_sha256"]
