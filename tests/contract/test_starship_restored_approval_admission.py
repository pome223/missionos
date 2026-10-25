"""Typed proposal/fresh-proof seam; invented saved states, no flight or model."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_restored_admission as admission
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_return as returning
from src.runtime.starship_restored_approval import RestoredApprovalBinding
from src.runtime.starship_restored_approval_admission import RestoredApprovalAdmissionBridge
from tests.contract.test_starship_restored_admission import case, candidate, current, raw


@pytest.fixture(autouse=True)
def no_integration_or_native_calls(monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("Approval admission tests must not integrate/call models")
    for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
            (mission, "advance_plant"), (returning, "advance_plant"), (returning, "control"),
            (returning, "forecast_restored_return"), (socket, "socket"),
            (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def scenario():
    nominal, next_orbit = case(), case(candidate_id="next_orbit")
    sites = (replace(nominal[6][0], site_id="nominal_area"), replace(next_orbit[6][0], site_id="next_area"))
    notice = replace(nominal[7], available_site_ids=("nominal_area", "next_area"))
    selected = {"select_next_orbit": candidate(next_orbit, sites=sites, notice=notice),
        "keep_reserved_return": candidate(nominal, sites=sites, notice=notice)}
    bound = RestoredApprovalBinding(nominal[1], context={"run_id": "typed", "mission_id": "m1", "vehicle_id": "ship"},
        plan_sha256="a"*64, expected_source_sha256=nominal[4], started_wall_s=100.)
    observation = current(nominal)
    inputs = {action: (row, observation, notice, nominal[8]) for action, row in selected.items()}
    calls = []
    def factory(action, state):
        calls.append((action, deepcopy(state)))
        state["simulation_time_s"] = -1.  # Caller state remains unchanged.
        return inputs[action]
    bridge = RestoredApprovalAdmissionBridge(bound, selected["select_next_orbit"],
        selected["keep_reserved_return"], current_inputs=factory)
    state = {"run_active": True, "simulation_time_s": nominal[1].time_s,
        "restored_origin_sha256": bound.record()["restored_origin_sha256"]}
    return bridge, bound, nominal, next_orbit, sites, notice, selected, inputs, state, calls


def test_stable_identity_is_reconstructed_from_typed_proposal_not_mutable_proof(scenario):
    bridge, _, _, _, _, _, selected, _, state, calls = scenario
    result = bridge("select_next_orbit", state)
    row = selected["select_next_orbit"].record()
    assert result["accepted"] and result["candidate_sha256"] == row["proposal_sha256"]
    assert result["current_candidate_sha256"] == row["candidate_sha256"] != row["proposal_sha256"]
    assert result["numerical_admission"]["candidate_sha256"] == row["candidate_sha256"]
    assert result["forecast_proof_sha256"] == row["forecast_proof_sha256"]
    assert bridge.proposal_sha256("select_next_orbit") == result["candidate_sha256"]
    assert state["simulation_time_s"] > 0 and len(calls) == 1
    assert all(result[key] is False for key in ("operator_approval_verified", "scope_amendment_applied",
        "mission_authority_granted", "dispatch_authorized", "reservation_changed", "physical_state_modified"))
    result["numerical_admission"]["accepted"] = False
    assert bridge("select_next_orbit", state)["accepted"]
    with pytest.raises(FrozenInstanceError):
        bridge._proposals_json = b"{}"


def test_notice_and_resource_refresh_change_proof_but_not_approved_opportunity(scenario):
    bridge, _, nominal, next_orbit, sites, notice, _, inputs, state, _ = scenario
    original = bridge("select_next_orbit", state)
    fresh_notice = replace(notice, generation=1)
    fresh_resources = replace(nominal[8], generation=1, energy_kwh=21.9)
    revised = candidate(next_orbit, sites=sites, notice=fresh_notice, resources=fresh_resources)
    inputs["select_next_orbit"] = revised, current(nominal), fresh_notice, fresh_resources
    result = bridge("select_next_orbit", state)
    assert result["accepted"] and result["candidate_sha256"] == original["candidate_sha256"]
    assert result["forecast_proof_sha256"] != original["forecast_proof_sha256"]
    assert result["numerical_admission"]["notice_sha256"] != original["numerical_admission"]["notice_sha256"]


def fresh_forecast(values):
    """New invented coast snapshot plus saved analytic contact, not propagation."""
    source = json.loads(values[1]._source_record_bytes)
    state = deepcopy(values[1].record()["state"])
    state["time_s"] += 1.
    state["propellant_kg"] -= .5
    state["v_eci_mps"][1] += .1
    source["final_state"] = state
    source["inventory"]["observed_time_s"] = state["time_s"]
    source["state_history"].append({"state": state, "retained_count": 13})
    encoded = raw(source)
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, values[3], values[4])
    schedule = contract.RestoredCandidateSchedule(origin, anchor=values[2])
    request = returning.RestoredReturnRequest(origin, schedule, "next_orbit", values[3], values[4])
    record = values[0].record()
    craft = dyn.vehicle_from_dict(origin.record()["vehicle"])
    actual = dyn.state_from_dict(state)
    times = (actual.time_s, actual.time_s+2., request.record()["return_time_s"])
    samples = json.loads(raw([mission._sample(replace(actual, time_s=t), craft, "orbital_coast") for t in times]))
    record.update(initial_state=state, binding=request.record(), initial_binding=request.record(),
        samples=samples+record["samples"][-1:])
    record["events"][0]["time_s"] = actual.time_s
    record["return_start_state"] = {key: samples[-1][key] for key in contract.STATE_KEYS}
    record["return_controller"]["record"]["activation"]["state"] = samples[-1]
    return returning.RestoredReturnResult(raw(record)), origin, state


def test_new_forecast_from_later_actual_origin_retains_original_approval_identity(scenario):
    bridge, _, nominal, next_orbit, sites, notice, _, inputs, state, _ = scenario
    before = bridge("select_next_orbit", state)
    prediction, origin, fresh_state = fresh_forecast(next_orbit)
    revised = candidate(next_orbit, prediction=prediction, forecast_origin=origin, sites=sites, notice=notice)
    def update(snapshot):
        snapshot["state"] = deepcopy(fresh_state)
    inputs["select_next_orbit"] = revised, current(nominal, state_change=update), notice, nominal[8]
    state["simulation_time_s"] = origin.time_s
    result = bridge("select_next_orbit", state)
    assert result["accepted"] and result["candidate_sha256"] == before["candidate_sha256"]
    assert result["forecast_proof_sha256"] != before["forecast_proof_sha256"]


def test_fallback_checks_nominal_instead_of_reusing_next_orbit_capability(scenario):
    bridge, _, nominal, _, sites, notice, selected, inputs, state, _ = scenario
    assert bridge("select_next_orbit", state)["accepted"]
    bad_nominal = candidate(case(speed=9.), sites=sites, notice=notice)
    assert bad_nominal.record()["proposal_sha256"] == selected["keep_reserved_return"].record()["proposal_sha256"]
    inputs["keep_reserved_return"] = bad_nominal, current(nominal), notice, nominal[8]
    fallback = bridge("keep_reserved_return", state)
    assert not fallback["accepted"]
    assert "numerical_contact_capability_unmet" in fallback["numerical_admission"]["reasons"]
    inputs["keep_reserved_return"] = inputs["select_next_orbit"]
    with pytest.raises(ValueError, match="changed_approved_proposal"):
        bridge("keep_reserved_return", state)


@pytest.mark.parametrize("change", ["source", "time", "inventory", "site_policy"])
def test_fresh_proposal_drift_is_rejected_before_rebinding_approved_identity(scenario, change):
    bridge, _, nominal, next_orbit, sites, notice, _, inputs, state, _ = scenario
    if change == "inventory":
        changed = candidate(case(candidate_id="next_orbit", retained=26), sites=sites, notice=notice)
    elif change == "site_policy":
        changed = candidate(next_orbit, sites=(replace(sites[0], radius_m=99999.), sites[1]), notice=notice)
    else:
        # Exact typed, immutable candidate instances are created by their trusted
        # factory only. This low-level mutation simulates corrupted saved memory
        # and must not become permission through a caller-supplied replacement hash.
        changed = candidate(next_orbit, sites=sites, notice=notice)
        row = changed.record()
        row["proposal"]["source_sha256" if change == "source" else "return_time_s"] = "b"*64 if change == "source" else row["proposal"]["return_time_s"]+1.
        row["proposal_sha256"] = admission._hash(row["proposal"])
        object.__setattr__(changed, "_json", raw(row))
    inputs["select_next_orbit"] = changed, current(nominal), notice, nominal[8]
    with pytest.raises(ValueError, match="changed_approved_proposal"):
        bridge("select_next_orbit", state)


@pytest.mark.parametrize("change", ["clock", "origin", "inactive"])
def test_actual_worker_observation_and_original_origin_cannot_be_replaced(scenario, change):
    bridge, _, _, _, _, _, _, _, state, _ = scenario
    state = deepcopy(state)
    if change == "clock":
        state["simulation_time_s"] += .25
    elif change == "origin":
        state["restored_origin_sha256"] = "e"*64
    else:
        state["run_active"] = False
    with pytest.raises(ValueError):
        bridge("select_next_orbit", state)


def test_stale_notice_is_rejected_without_remapping_a_negative_into_permission(scenario):
    bridge, _, nominal, _, _, notice, _, inputs, state, _ = scenario
    selected, observation, _, resources = inputs["select_next_orbit"]
    inputs["select_next_orbit"] = selected, observation, replace(notice, generation=1), resources
    result = bridge("select_next_orbit", state)
    assert not result["accepted"] and result["candidate_sha256"] == bridge.proposal_sha256("select_next_orbit")
    assert "notice_generation_or_content_changed_rebind_saved_prediction" in result["numerical_admission"]["reasons"]
    assert current(nominal).record()["state"]["time_s"] == state["simulation_time_s"]


@pytest.mark.parametrize("change", ["raw_binding", "raw_next", "wrong_next", "raw_nominal", "factory", "mismatched_scope"])
def test_constructor_cannot_borrow_untyped_candidates_or_another_registered_scope(scenario, change):
    _, bound, nominal, _, sites, notice, selected, inputs, _, _ = scenario
    args = [bound, selected["select_next_orbit"], selected["keep_reserved_return"]]
    def factory(action, _state):
        return inputs[action]
    if change == "raw_binding":
        args[0] = bound.record()
    elif change == "raw_next":
        args[1] = args[1].record()
    elif change == "wrong_next":
        args[1] = args[2]
    elif change == "raw_nominal":
        args[2] = args[2].record()
    elif change == "factory":
        factory = None
    else:
        args[2] = candidate(nominal, sites=(replace(sites[0], radius_m=99999.), sites[1]), notice=notice)
    with pytest.raises(ValueError):
        RestoredApprovalAdmissionBridge(*args, current_inputs=factory)


@pytest.mark.parametrize("change", ["action", "list", "raw_candidate", "raw_observation", "raw_notice", "raw_resource"])
def test_call_requires_allowed_action_and_fresh_exact_typed_factory_inputs(scenario, change):
    bridge, _, _, _, _, _, _, inputs, state, calls = scenario
    if change == "action":
        with pytest.raises(ValueError, match="action_outside_scope"):
            bridge("select_nominal", state)
        assert not calls
        return
    value = inputs["select_next_orbit"]
    if change == "list":
        inputs["select_next_orbit"] = list(value)
    else:
        index = {"raw_candidate": 0, "raw_observation": 1, "raw_notice": 2, "raw_resource": 3}[change]
        value = list(value)
        value[index] = value[index].record()
        inputs["select_next_orbit"] = tuple(value)
    with pytest.raises(ValueError, match="untyped_inputs"):
        bridge("select_next_orbit", state)
