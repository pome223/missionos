"""Actual dispatch-clock resource gates over analytic saved fixture evidence.

No plant, controller, provider, forecast or saved flight reexecution is allowed.
The invented due-tick witnesses exercise the production typed admission seam;
they do not establish a new physical recovery or resource subsystem model.
"""
from dataclasses import replace
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_restored_admission as admission
from src.runtime import starship_restored_return as returning
from tests.contract.test_starship_restored_admission import case, candidate, current, check


@pytest.fixture(autouse=True)
def no_plant_provider_or_external_execution(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("resource dispatch contracts cannot execute a flight, forecast or provider")
    for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
            (mission, "advance_plant"), (returning, "advance_plant"), (returning, "control"),
            (returning, "forecast_restored_return"), (returning, "continue_restored_return"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def dispatch_fixture(*, slip=.2127750659226586, candidate_id="next_orbit"):
    def due_witness(record):
        record["return_start_state"]["time_s"] += slip
        record["events"][1]["time_s"] += slip
        record["samples"][-2]["time_s"] += slip
        record["return_controller"]["record"]["activation"]["time_s"] += slip
        record["return_controller"]["record"]["activation"]["state"]["time_s"] += slip
    values = case(candidate_id=candidate_id, mutate=due_witness)
    when = values[9].record()["return_time_s"]
    now = when+slip
    observation = current(values, time_s=now)
    return values, when, now, observation


def admitted(values, observation, resources, *, stage="dispatch"):
    selected = candidate(values, resources=resources)
    result = check(values, selected, observation, resources=resources, stage=stage)
    return selected, result


@pytest.mark.parametrize("candidate_id", ["nominal", "next_orbit"])
@pytest.mark.parametrize("slip", [0., .1, .2127750659226586, .25])
def test_actual_current_resource_observation_is_not_future_at_valid_first_due_tick(candidate_id, slip):
    values, when, now, observation = dispatch_fixture(slip=slip, candidate_id=candidate_id)
    resources = replace(values[8], observed_s=now, energy_kwh=10.625,
        temperature_k=301.825, modeled_propellant_loss_kg=136.5)
    selected, result = admitted(values, observation, resources)
    assert result["accepted"] is True, result
    assert "resource_observation_is_future" not in result["reasons"]
    assert result["fresh_state_match"]["method"] == "exact_precontroller_return_start_state"
    assert result["resource_accounting"]["prospective_orbital_wait_s"] == 0.
    assert result["resource_accounting"]["energy_at_return_kwh"] == resources.energy_kwh
    assert result["resource_accounting"]["temperature_at_return_k"] == resources.temperature_k
    assert result["resource_accounting"]["modeled_loss_budget_kg"] == resources.modeled_propellant_loss_kg
    assert selected.record()["resources"]["observed_s"] == now
    assert observation.record()["state"]["time_s"] == now
    assert observation.record()["binding"]["return_time_s"] == when
    assert selected.record()["proposal"]["return_time_s"] == when
    assert result["dispatch_authorized"] is result["mission_authority_granted"] is False


@pytest.mark.parametrize("offset", [-.001, .25001, .5])
def test_resource_current_time_never_bypasses_existing_first_due_dispatch_gate(offset):
    values, when, _now, _observation = dispatch_fixture(slip=0.)
    now = when+offset
    observation = current(values, time_s=now)
    resources = replace(values[8], observed_s=now)
    _selected, result = admitted(values, observation, resources)
    assert result["accepted"] is False
    assert "dispatch_not_first_due_reserved_tick" in result["reasons"]
    assert "resource_observation_is_future" not in result["reasons"]
    assert observation.record()["binding"]["maximum_start_clock_slip_s"] == .25


@pytest.mark.parametrize("ahead_s", [.000001, 1.])
def test_dispatch_resource_observation_after_actual_now_always_rejected(ahead_s):
    values, _when, now, observation = dispatch_fixture()
    resources = replace(values[8], observed_s=now+ahead_s)
    _selected, result = admitted(values, observation, resources)
    assert result["accepted"] is False
    assert "resource_observation_is_future" in result["reasons"]
    assert "dispatch_not_first_due_reserved_tick" not in result["reasons"]


def test_selection_still_rejects_observation_after_reserved_epoch_even_when_not_after_actual_now():
    values, when, now, observation = dispatch_fixture()
    resources = replace(values[8], observed_s=when+.1)
    _selected, dispatch = admitted(values, observation, resources)
    _selected, selection = admitted(values, observation, resources, stage="selection")
    assert dispatch["accepted"] is True and when < resources.observed_s < now
    assert selection["accepted"] is False
    assert "resource_observation_is_future" in selection["reasons"]
    assert "original_nominal_booking_guard" in selection["reasons"]


def test_selection_resource_observation_after_actual_now_remains_rejected():
    values = case()
    observation = current(values)
    resources = replace(values[8], observed_s=values[1].time_s+.1)
    _selected, result = admitted(values, observation, resources, stage="selection")
    assert result["accepted"] is False and "resource_observation_is_future" in result["reasons"]


def test_before_reserved_epoch_remaining_wait_is_accounted_without_rewriting_observation():
    values, when, _now, observation = dispatch_fixture()
    resources = replace(values[8], observed_s=when-1., energy_kwh=11.,
        temperature_k=300., modeled_propellant_loss_kg=120.)
    selected, result = admitted(values, observation, resources)
    account = result["resource_accounting"]
    assert result["accepted"] is True, result
    assert account["prospective_orbital_wait_s"] == 1.
    assert account["energy_at_return_kwh"] == pytest.approx(11.-6./3600.)
    assert account["temperature_at_return_k"] == pytest.approx(300.001)
    assert account["modeled_loss_budget_kg"] == pytest.approx(120.02)
    assert resources.observed_s == selected.record()["resources"]["observed_s"] == when-1.


@pytest.mark.parametrize("updates,reason", [({"energy_kwh":9.999}, "synthetic_energy_budget"),
    ({"temperature_k":310.001}, "synthetic_temperature_budget"),
    ({"modeled_propellant_loss_kg":2000.001}, "synthetic_reserve_after_modeled_loss_budget")])
def test_budget_breach_at_actual_dispatch_time_still_rejects(updates, reason):
    values, _when, now, observation = dispatch_fixture()
    resources = replace(values[8], observed_s=now, **updates)
    selected, result = admitted(values, observation, resources)
    assert selected.record()["numerical_contact"]["capability_met"] is True
    assert result["accepted"] is False and reason in result["reasons"]
    assert "resource_observation_is_future" not in result["reasons"]
    assert result["resource_accounting"]["prospective_orbital_wait_s"] == 0.
    assert selected.record()["resources"]["observed_s"] == now


def test_exact_resource_thresholds_remain_the_registered_values_at_dispatch():
    values, _when, now, observation = dispatch_fixture()
    resources = replace(values[8], observed_s=now, energy_kwh=10., temperature_k=310., modeled_propellant_loss_kg=2000.)
    _selected, result = admitted(values, observation, resources)
    assert result["accepted"] is True, result
    assert result["resource_accounting"]["reserve_after_modeled_loss_kg"] == 28000.
    policy = admission.RestoredAdmissionPolicy().record()
    assert policy["waiting"]["minimum_energy_kwh"] == 10.
    assert policy["waiting"]["maximum_temperature_k"] == 310.
    assert policy["contact"]["minimum_propellant_kg"] == 28000.


def test_rebound_resources_keep_proposal_identity_but_old_context_proof_is_ineligible():
    values, _when, now, observation = dispatch_fixture()
    original = candidate(values)
    resources = replace(values[8], generation=1, observed_s=now, energy_kwh=10.625)
    selected, result = admitted(values, observation, resources)
    stale = check(values, original, observation, resources=resources, stage="dispatch")
    assert result["accepted"] is True
    assert selected.record()["proposal_sha256"] == original.record()["proposal_sha256"]
    assert selected.record()["forecast_proof_sha256"] != original.record()["forecast_proof_sha256"]
    assert stale["accepted"] is False
    assert "resource_generation_or_content_changed_rebind_saved_prediction" in stale["reasons"]


def test_wrong_reserved_candidate_or_source_cannot_borrow_valid_current_resources():
    values, _when, now, _observation = dispatch_fixture()
    resources = replace(values[8], observed_s=now)
    nominal = values[2].record()["nominal_s"]
    observation = current(values, time_s=now,
        binding_change=lambda binding:binding.update(candidate_id="nominal", return_time_s=nominal))
    _selected, result = admitted(values, observation, resources)
    assert result["accepted"] is False and "dispatch_not_first_due_reserved_tick" in result["reasons"]
    with pytest.raises(ValueError, match="source_inventory_reservation_or_actual_state_mismatch"):
        current(values, time_s=now, binding_change=lambda binding:binding.update(source_sha256="f"*64))
    changed_source = {**values[4], "src/runtime/starship_sixdof.py":"f"*64}
    with pytest.raises(ValueError, match="host_source_mismatch"):
        candidate(values, resources=resources, expected_source_sha256=changed_source)


def test_dispatch_clock_rule_does_not_relax_finite_actuator_freshness():
    values, _when, now, _observation = dispatch_fixture()
    resources = replace(values[8], observed_s=now)
    observation = current(values, time_s=now,
        state_change=lambda row:row["state"]["engine_states"][0].update(
            throttle=row["state"]["engine_states"][0]["throttle"]+.0011))
    _selected, result = admitted(values, observation, resources)
    assert result["accepted"] is False and "fresh_throttle" in result["reasons"]
    assert "resource_observation_is_future" not in result["reasons"]
