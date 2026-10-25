"""Restored-origin approval binding and real pipe contracts, no flight/model."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import socket
import subprocess

import pytest

from scripts.smoke_starship_restored_approval import Clock, Scenario, synthetic_origin
from src.gateway.starship_inflight_approval import InflightApprovalService
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_inflight_approval import InflightApprovalError, digest
from src.runtime.starship_restored_approval import (
    RestoredApprovalBinding, RestoredApprovalWorker, binding, validate_binding,
)


@pytest.fixture(autouse=True)
def no_plant_controller_process_or_network(monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("Approval contract tests cannot integrate, invoke models or contact networks")
    for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
            (mission, "advance_plant"), (mission, "control"), (socket, "socket"),
            (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def origin():
    return synthetic_origin()


def arguments(source):
    return {"context": {"run_id": "run1", "mission_id": "restored1", "vehicle_id": "ship"},
        "plan_sha256": "a"*64, "expected_source_sha256": source, "started_wall_s": 1000.}


def test_source_bound_immutable_handle_derives_actual_schedule_not_old_clocks(origin):
    actual, source = origin
    handle = RestoredApprovalBinding(actual, **arguments(source))
    record = handle.record()
    assert record["actual_cutoff_time_s"] == 1800. and record["closed_origin_time_s"] == 1810.
    assert record["nominal_burn_time_s"] == 3600.
    assert record["next_orbit_burn_time_s"] == 3600.+actual.record()["orbit"]["period_s"]
    assert record["bounds"] == {"simulation_deadline_s": 3010., "wall_deadline_s": 2200.,
        "maximum_observation_age_s": 5., "reservation_cutoff_s": 3540.}
    assert record["initial_scope"]["commands"] == ["select_nominal", "keep_reserved_return"]
    assert record["registry"]["commands"] == record["initial_scope"]["commands"]+["select_next_orbit"]
    assert record["maximum_amendment_requests"] == 1
    assert record["physical_execution_authorized"] is record["old_certificate_admitted"] is False
    assert validate_binding(record, actual, **arguments(source)).record() == record
    record["bounds"]["simulation_deadline_s"] = 9999.
    assert handle.record()["bounds"]["simulation_deadline_s"] == 3010.
    with pytest.raises(FrozenInstanceError):
        handle._wire = b"{}"


@pytest.mark.parametrize("change", [
    {"expected_source_sha256": {}}, {"plan_sha256": "invalid"}, {"started_wall_s": True},
    {"started_wall_s": float("nan")}, {"context": {"run_id": "run1"}},
])
def test_binding_rejects_bad_host_inputs(origin, change):
    actual, source = origin
    with pytest.raises(InflightApprovalError):
        RestoredApprovalBinding(actual, **(arguments(source) | change))


def test_binding_rejects_self_asserted_source_old_dict_origin_and_mutated_evidence(origin):
    actual, source = origin
    wrong = deepcopy(source)
    wrong["src/runtime/starship_sixdof.py"] = "c"*64
    with pytest.raises(InflightApprovalError, match="manifest_mismatch"):
        RestoredApprovalBinding(actual, **arguments(wrong))
    with pytest.raises(InflightApprovalError, match="typed_closed_origin"):
        RestoredApprovalBinding(actual.record(), **arguments(source))
    value = binding(actual, **arguments(source))
    for key, bad in (("plan_sha256", "b"*64), ("maximum_amendment_requests", 2),
            ("restored_origin_sha256", "0"*64), ("old_certificate_admitted", True)):
        with pytest.raises(InflightApprovalError, match="binding_mismatch"):
            validate_binding(value | {key: bad}, actual, **arguments(source))


@pytest.fixture
def scenario(tmp_path, origin):
    actual, source = origin
    clock = Clock()
    value = Scenario("pipe-contract", tmp_path, actual, source, InflightApprovalService(clock=clock), clock)
    try:
        yield value
    finally:
        value.close()


def approve(value):
    value.request()
    context = value.bound.record()["context"]
    value.host.service.resolve(**context, token="synthetic-http-smoke-principal-not-a-live-secret",
        request_id="change1", choice="approve", body=value.body())
    value.host.tick()


def test_real_pipes_approval_consumption_fresh_admission_and_later_confirmation(scenario):
    value = scenario
    approve(value)
    assert not value.updates
    result = value.worker.tick(value.state)
    assert result["reservation_changed"] is True and result["burn_dispatch_issued"] is False
    assert value.worker.status == "reservation_changed_waiting_observation"
    assert len(value.updates) == len(value.admissions) == 1
    assert value.worker.tick(value.state) is None
    value.observe(.1)
    value.worker.tick(value.state)
    assert value.worker.status == "reservation_confirmed"
    value.worker.tick(value.state)
    assert len(value.updates) == 1
    record = value.worker.record()
    assert record["host_signature_verified_by_worker"] is record["physical_return_verified"] is False
    assert record["restored_binding"]["restored_origin_sha256"] == value.state["restored_origin_sha256"]
    with pytest.raises(InflightApprovalError, match="budget_exhausted"):
        value.worker.request_change(request_id="again", candidate_sha256=value.candidate, state=value.state)


def test_changed_candidate_after_human_approval_is_not_applied(scenario):
    value = scenario
    approve(value)
    value.check_candidate = "0"*64
    value.worker.tick(value.state)
    assert value.worker.status == "fallback_preserved" and value.updates == []
    assert [row["action"] for row in value.admissions] == ["select_next_orbit", "keep_reserved_return"]


def test_fresh_fallback_failure_is_not_silently_authorized(scenario):
    value = scenario
    approve(value)
    value.check_candidate = "0"*64
    value.fallback_accepted = False
    value.worker.tick(value.state)
    assert value.worker.status == "fallback_not_admitted" and value.updates == []


@pytest.mark.parametrize("field,bad", [
    ("restored_origin_sha256", "0"*64), ("simulation_time_s", 1000.7),
    ("revision", True), ("revision", 2), ("reservation_sha256", "c"*64),
    ("scope", {"commands": ["select_next_orbit"], "resource_limits": {}}),
])
def test_wrong_origin_initial_reservation_and_scope_reject_before_request(scenario, field, bad):
    value = scenario
    state = deepcopy(value.state)
    state[field] = bad
    with pytest.raises(InflightApprovalError):
        value.worker.request_change(request_id="bad", candidate_sha256=value.candidate, state=state)
    assert value.worker.status == "idle" and value.host.bridge.reader.poll() is None


def test_raw_binding_cannot_construct_worker_and_missing_candidate_rejected(scenario):
    value = scenario
    with pytest.raises(InflightApprovalError, match="typed_approval_binding"):
        RestoredApprovalWorker(value.bound.record(), initial_reservation_sha256="a"*64,
            request_writer=None, grant_reader=None, admission=None, command=None)
    with pytest.raises(InflightApprovalError):
        value.worker.request_change(request_id="bad", candidate_sha256=None, state=value.state)
    assert value.worker.status == "idle"


def test_confirmation_uses_new_observation_and_reservation_digest(scenario):
    value = scenario
    approve(value)
    value.worker.tick(value.state)
    value.observe(.1)
    value.state["reservation_sha256"] = digest({"wrong": "reservation"})
    value.worker.tick(value.state)
    assert value.worker.status == "reservation_confirmation_failed"
    assert len(value.updates) == 1


def test_ledger_duplicate_consumption_rejected(scenario):
    value = scenario
    approve(value)
    with pytest.raises(InflightApprovalError, match="already_consumed"):
        value.host.ledger.consume(request_id="change1", mission_id="restored-smoke", vehicle_id="ship",
            revision=0, scope=value.bound.record()["initial_scope"], observation=value.state["observation"],
            simulation_time_s=value.state["simulation_time_s"], run_active=True)


def test_simulation_expiry_does_not_wait_for_wall_expiry_or_change_reservation(scenario):
    value = scenario
    value.request()
    value.state["simulation_time_s"] = value.bound.record()["bounds"]["simulation_deadline_s"]
    value.observe()
    value.host.tick()
    value.worker.tick(value.state)
    assert value.worker.status == "fallback_preserved" and not value.updates
    assert value.host.bridge.result["consumption"]["reason"] == "expired"


def test_stale_observation_rejects_before_sending_request(scenario):
    value = scenario
    value.clock.now += 6.
    with pytest.raises(InflightApprovalError, match="observation_not_fresh"):
        value.worker.request_change(request_id="bad", candidate_sha256=value.candidate, state=value.state)
    assert value.worker.status == "idle"


def test_context_registry_cannot_be_reused_by_raw_json_or_wrong_context(scenario):
    value = scenario
    request = value.worker.request_change(request_id="bad-context", candidate_sha256=value.candidate,
        state=value.state)
    # A different host context is rejected before a ledger request is created.
    value.host.bridge.context["mission_id"] = "another-mission"
    value.host.tick()
    assert value.host.bridge.request is None
    assert value.host.bridge.result["reason"] == "invalid_worker_scope_request"
    assert request["context"]["mission_id"] == "restored-smoke"
