"""Registered notice/accounting tools only; no flight, forecast or model."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_restored_return as returning
from src.runtime import starship_restored_operations as tools
from src.runtime import starship_restored_native_verifier as checker
from tests.contract.test_starship_restored_admission import case, current


@pytest.fixture(autouse=True)
def pure_scope(monkeypatch):
    from src.intelligence.starship_restored_agent import RestoredAgent
    def forbidden(*_a, **_k):
        raise AssertionError("operations tools cannot run dynamics, forecasts, subprocesses or models")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
            (mission, "advance_plant"), (returning, "advance_plant"), (returning, "forecast_restored_return"),
            (returning, "control"), (RestoredAgent, "assess"), (socket, "socket"),
            (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def definition(values):
    start = values[1].time_s
    notices = [replace(values[7], generation=generation, issued_s=issued, expires_s=expiry)
        for generation, issued, expiry in ((0, 0., 16000.), (3, start+10., start+20.), (7, start+30., 16000.))]
    return {"schema": tools.SCHEMA, "sites": [row.record() for row in values[6]],
        "notice_schedule": [{"notice": value.record(), "summary": f"Registered generation {value.generation}."} for value in notices],
        "deployment_notices": {"0": "Registered first deployment notice.", "13": "Registered halfway deployment notice."},
        "resource_accounting": {"epoch": "closed_origin", "initial_energy_kwh": 22., "power_kw": 6.,
            "initial_temperature_k": 295., "warming_k_per_s": .001,
            "initial_modeled_propellant_loss_kg": 0., "modeled_loss_kg_per_s": .02}}


def snapshot(values, when=None):
    row = current(values, time_s=values[1].time_s if when is None else when).record()
    return {"state": row["state"], "vehicle": row["vehicle"], "binding": row["binding"],
        "phase": "orbital_coast", "return_controller": None, "completed": False}


def test_definition_matches_independent_expected_operations_without_shared_validator():
    values = case()
    registered = tools.RegisteredRestoredOperations(definition(values))
    assert checker._operations(registered.record()) == registered.record()
    assert registered.sites == values[6]
    assert registered.notice_generations == (0, 3, 7)


def test_definition_bound_tool_and_returned_records_are_detached_and_immutable():
    values = case()
    data = definition(values)
    registered = tools.RegisteredRestoredOperations(data)
    bound = registered.bind(values[1])
    data["resource_accounting"]["initial_energy_kwh"] = 100.
    record = registered.record()
    record["notice_schedule"][0]["summary"] = "Changed after registration."
    assert registered.record()["resource_accounting"]["initial_energy_kwh"] == 22.
    notice, _ = bound(snapshot(values))
    assert bound.notice_summary(notice) == "Registered generation 0."
    with pytest.raises(FrozenInstanceError):
        registered._json = b"{}"
    with pytest.raises(FrozenInstanceError):
        bound._origin = None
    assert bound.record()["prediction_used"] is bound.record()["fault_truth_exposed"] is False
    assert bound.record()["physical_state_modified"] is bound.record()["mission_authority_granted"] is False


@pytest.mark.parametrize("elapsed,expected", [(0., 0), (9.999, 0), (10., 3), (25., 3), (30., 7)])
def test_latest_absolute_issued_notice_and_resources_use_one_actual_closed_epoch(elapsed, expected):
    values = case()
    registered = tools.RegisteredRestoredOperations(definition(values))
    bound = tools.RestoredOperations(registered, values[1])
    actual = snapshot(values, values[1].time_s+elapsed)
    before = deepcopy(actual)
    notice, resources = bound(actual)
    assert notice.generation == resources.generation == expected
    assert resources.observed_s == actual["state"]["time_s"]
    assert resources.energy_kwh == pytest.approx(22.-elapsed*6./3600.)
    assert resources.temperature_k == pytest.approx(295.+elapsed*.001)
    assert resources.modeled_propellant_loss_kg == pytest.approx(elapsed*.02)
    if elapsed == 25.:
        assert notice.expires_s < resources.observed_s  # Preserve stale, do not resurrect gen0.
    checker._operations_row({"sites": [value.record() for value in registered.sites], "notice": notice.record(),
        "resources": resources.record()}, resources.observed_s, registered.record(), values[1])
    assert actual == before and bound.record()["resource_epoch_s"] == values[1].time_s


def test_new_query_never_rebases_resources_at_notice_change_or_call_wall_time():
    values = case()
    bound = tools.RegisteredRestoredOperations(definition(values)).bind(values[1])
    first = bound(snapshot(values, values[1].time_s+10.))[1]
    second = bound(snapshot(values, values[1].time_s+30.))[1]
    assert first.generation == 3 and second.generation == 7
    assert second.energy_kwh < first.energy_kwh < 22.
    assert second.modeled_propellant_loss_kg > first.modeled_propellant_loss_kg > 0.
    assert bound.record()["time_epoch_rebased"] is False


@pytest.mark.parametrize("mutation", ["schema", "extra", "missing", "bool_generation", "duplicate_generation", "reverse_issue",
    "future_expiry", "unknown_site", "duplicate_site", "site_truth", "summary_type", "long_summary", "deployment_slot", "accounting", "epoch"])
def test_unregistered_or_ill_typed_definition_fails_before_origin_or_plant(mutation):
    values = case()
    data = definition(values)
    if mutation == "schema":
        data["schema"] = "other"
    elif mutation == "extra":
        data["fault_truth"] = "engine_failure"
    elif mutation == "missing":
        del data["notice_schedule"]
    elif mutation == "bool_generation":
        data["notice_schedule"][0]["notice"]["generation"] = False
    elif mutation == "duplicate_generation":
        data["notice_schedule"][1]["notice"]["generation"] = 0
    elif mutation == "reverse_issue":
        data["notice_schedule"][1]["notice"]["issued_s"] = values[1].time_s+50.
    elif mutation == "future_expiry":
        data["notice_schedule"][0]["notice"]["expires_s"] = 16001.
    elif mutation == "unknown_site":
        data["notice_schedule"][0]["notice"]["available_site_ids"] = ["unregistered"]
    elif mutation == "duplicate_site":
        data["sites"].append(deepcopy(data["sites"][0]))
    elif mutation == "site_truth":
        data["sites"][0]["real_ocean_clearance_verified"] = True
    elif mutation == "summary_type":
        data["notice_schedule"][0]["summary"] = True
    elif mutation == "long_summary":
        data["deployment_notices"]["0"] = "x"*601
    elif mutation == "deployment_slot":
        data["deployment_notices"]["26"] = "unregistered"
    elif mutation == "accounting":
        data["resource_accounting"]["initial_energy_kwh"] = 100.
    else:
        data["resource_accounting"]["epoch"] = "each_query"
    with pytest.raises(ValueError):
        tools.RegisteredRestoredOperations(data)


@pytest.mark.parametrize("mutation", ["prediction_origin", "dict_origin", "untyped_registration", "state_time_bool", "before_epoch", "added_fuel",
    "future_notice", "vehicle", "source", "inventory", "controller", "phase"])
def test_bound_tool_rejects_predictions_source_inventory_or_nonactual_coast(mutation):
    values = case()
    definition_row = definition(values)
    registered = tools.RegisteredRestoredOperations(definition_row)
    if mutation in ("prediction_origin", "dict_origin", "untyped_registration"):
        with pytest.raises(ValueError):
            tools.RestoredOperations(definition_row if mutation == "untyped_registration" else registered,
                values[0] if mutation == "prediction_origin" else values[1].record() if mutation == "dict_origin" else values[1])
        return
    if mutation == "future_notice":
        definition_row["notice_schedule"] = definition_row["notice_schedule"][1:]
        registered = tools.RegisteredRestoredOperations(definition_row)
    bound = registered.bind(values[1])
    actual = snapshot(values)
    if mutation == "state_time_bool":
        actual["state"]["time_s"] = True
    elif mutation == "before_epoch":
        actual["state"]["time_s"] -= 1.
    elif mutation == "added_fuel":
        actual["state"]["propellant_kg"] += 1.
    elif mutation == "vehicle":
        actual["vehicle"]["dry_mass_kg"] += 1.
    elif mutation == "source":
        actual["binding"]["source_sha256"] = "f"*64
    elif mutation == "inventory":
        actual["binding"]["retained_count"] += 1
    elif mutation == "controller":
        actual["return_controller"] = {}
    elif mutation == "phase":
        actual["phase"] = "landing_burn"
    with pytest.raises(ValueError):
        bound(actual)


def test_ground_deployment_text_uses_only_registered_slot_and_notice_summary_exact_typed_content():
    values = case()
    registered = tools.RegisteredRestoredOperations(definition(values))
    assert registered.deployment_notice({"released_count": 0, "retained_count": 26}) == "Registered first deployment notice."
    assert registered.deployment_notice({"released_count": 13, "retained_count": 13}) == "Registered halfway deployment notice."
    for bad in (True, 12, 26):
        with pytest.raises(ValueError):
            registered.deployment_notice({"released_count": bad, "retained_count": 26-int(bad)})
    bound = registered.bind(values[1])
    with pytest.raises(ValueError):
        bound.notice_summary(values[7].record())
    with pytest.raises(ValueError):
        bound.notice_summary(replace(values[7], expires_s=15000.))


def test_real_prefix_readonly_summary_to_deployment_client_uses_registered_notice_without_forced_withhold():
    from src.runtime import starship_restored_decision_pipe as pipe
    from src.runtime.starship_inflight_runtime import approval_pipe
    from src.runtime.starship_restored_deployment import RestoredDeploymentJudgments
    from src.runtime.starship_restored_prefix import PENDING_DEPLOYMENT_DECISION
    from tests.contract.test_starship_restored_prefix import setup, runtime, state_at
    values, registered = case(), None
    registered = tools.RegisteredRestoredOperations(definition(values))
    current_setup = setup.__wrapped__()
    request_reader, request_writer = approval_pipe()
    response_reader, response_writer = approval_pipe()
    now = [100.]
    context = {"run_id": "operations-boundary", "mission_id": "synthetic", "vehicle_id": "ship"}
    client = pipe.RestoredDecisionClient(context=context, plan_sha256="a"*64, source_sha256="b"*64,
        request_writer=request_writer, response_reader=response_reader, clock=lambda: now[0])
    adapter = RestoredDeploymentJudgments(client, scope_sha256="c"*64,
        notice=registered.deployment_notice, clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0]+delay))
    driver = runtime(current_setup, adapter)
    profile, settings = current_setup[1:3]
    try:
        driver.begin(profile, settings, 7500.)
        driver.settled(state_at(profile, 1805.), {"final_gate_time_s": 1800.,
            "first_release_time_s": 1845., "return_request_time_s": 3600.})
        assert driver.decide(state_at(profile, 1845.), 0, release_ready=True,
            sequencer_state="running") is PENDING_DEPLOYMENT_DECISION
        frame = request_reader.poll()
        req = frame["request"]
        assert req["notice_summary"] == registered.record()["deployment_notices"]["0"]
        judgment = {"schema": "missionos.starship_restored_judgment.v1", "request_id": req["request_id"],
            "request_sha256": pipe._digest(req), "provider": "typesafe", "action": "continue_deployment", "status": "valid",
            "summary": "Synthetic pipe only.", "model_inference_invoked": False, "execution_permission_granted": False}
        attempt = {"provider": "typesafe", "remaining_s": 35., "decision_deadline_s": req["deadline_s"],
            "request_sha256": pipe._digest(req), "judgment": judgment}
        result = pipe._result(req, [attempt], judgment)
        response_writer.send({"schema": pipe.RESPONSE_FRAME, **client.binding,
            **{key: frame[key] for key in ("sequence", "request_id", "request_sha256")},
            "result": result, "result_sha256": pipe._digest(result)})
        now[0] += .25
        assert driver.decide(state_at(profile, 1845.25), 0, release_ready=True,
            sequencer_state="running") == "continue_deployment"
        evidence = driver.finish({})
        assert evidence["decisions"][0]["callback_error_type"] is None
        assert evidence["decisions"][0]["action"] == "continue_deployment"
        assert not evidence["closed"] and client.record()["provider_calls_by_worker"] == 0
    finally:
        for endpoint in (request_reader, request_writer, response_reader, response_writer):
            endpoint.close()
