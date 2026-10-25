"""Actual-owner handoff boundary using scripted yielded states, never a flight."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from src.runtime import starship_final_handoff as final
from src.runtime import starship_restored_admission as admission
from src.runtime import starship_restored_coordinator as coordinator
from src.runtime import starship_restored_return as returning
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_sixdof as dyn, starship_physics as env
from src.runtime.starship_inflight_runtime import approval_pipe
from src.runtime.starship_restored_decision_pipe import RestoredDecisionClient
from tests.contract.test_starship_restored_admission import case

ROOT = Path(final.__file__).resolve().parents[2]


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.fixture(autouse=True)
def no_physics_or_provider(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("Handoff contracts must not integrate or contact providers")

    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
        (returning, "advance_plant"), (returning, "control"), (socket, "socket"),
        (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def bound_fixture(candidate_id="nominal"):
    values = case(candidate_id=candidate_id)
    source = deepcopy(values[4])
    for name in (final.SOURCE, "src/runtime/starship_restored_return.py", "src/runtime/starship_restored_coordinator.py"):
        source[name] = sha256((ROOT/name).read_bytes()).hexdigest()
    original = json.loads(values[1].source_record_bytes())
    original["source_sha256"] = source
    raw = wire(original)
    origin = contract.ActualRestoredOrigin(sha256(raw).hexdigest(), raw, values[3], source)
    anchor = contract.RestoredCandidateSchedule(origin)
    request = returning.RestoredReturnRequest(origin, anchor, candidate_id, values[3], source)
    prediction = values[0].record()
    prediction.update(binding=request.record(), initial_binding=request.record())
    candidate = admission.RestoredCandidate(returning.RestoredReturnResult(wire(prediction)), origin, anchor,
        values[3], source, values[5], values[6], values[7], values[8])
    policy = final.FinalActualReturnHandoffPolicy("a"*64, source)
    return SimpleNamespace(values=values, source=source, origin=origin, request=request, anchor=anchor,
        candidate=candidate, policy=policy)


@pytest.fixture
def bound_case():
    return bound_fixture()


def due_owner(execution, *, slip=.1):
    """Install an explicitly scripted yielded owner; this is not propagation.

    The real generator is first opened at its original observed origin, then
    closed. A fixture owns one named future tick and retains its exact objects.
    Every production integration function remains forbidden by the fixture.
    """
    original = execution.snapshot()
    old_vehicle = execution._context.get("actual_owner_provider", lambda: (None, None, None))()[1]
    vehicle = old_vehicle if old_vehicle is not None else dyn.vehicle_from_dict(original["vehicle"])
    state = replace(dyn.state_from_dict(original["state"]),
        time_s=original["binding"]["return_time_s"]+slip, propellant_kg=original["state"]["propellant_kg"]-.1)
    snapshot = {**original, "state": json.loads(wire(asdict(state))), "vehicle": json.loads(wire(asdict(vehicle)))}
    execution._iterator.close()
    execution._context["snapshot"] = deepcopy(snapshot)
    execution._context["actual_snapshot_provider"] = lambda: deepcopy(snapshot)
    if execution._final_handoff_policy is not None:
        execution._context["actual_owner_provider"] = lambda: (state, vehicle, None)
    execution._context["coast_history"].append({"state": deepcopy(snapshot["state"]),
        "retained_count": execution._request.origin.record()["retained_count"]})

    def suspended():
        if False:
            yield
        raise AssertionError("The old scripted owner must not be resumed")

    execution._iterator = suspended()
    return state, vehicle, snapshot


def dispatch_proof(bound, snapshot, *, changed=None):
    row = bound.candidate.record()
    # The numerical acceptance is an injected test tool, not evidence that the
    # invented trajectory is physically feasible. Production obtains it from
    # the existing _admit(..., 'dispatch') path before sealing the handoff.
    result = {"schema": "missionos.starship_restored_candidate_admission.v1", "accepted": True,
        "stage": "dispatch", "reasons": [], "time_s": snapshot["state"]["time_s"],
        "candidate_sha256": row["candidate_sha256"], "proposal_sha256": row["proposal_sha256"]}
    if changed:
        result.update(changed)
    return final._accepted_actual_dispatch(bound.policy, candidate=bound.candidate, admission=result, snapshot=snapshot)


def actor(bound, *, opted_in=True):
    return returning.RestoredReturnExecution(bound.request, origin_record_bytes=bound.origin.source_record_bytes(),
        **({"final_handoff_policy": bound.policy} if opted_in else {}))


def test_actual_handoff_preserves_all_state_vehicle_inventory_and_exact_object_identity(bound_case):
    b = bound_case
    execution = actor(b)
    state, vehicle, snapshot = due_owner(execution)
    before = wire(snapshot)
    handoff = execution.handoff_actual_return(b.policy, dispatch=dispatch_proof(b, snapshot))
    assert type(handoff) is final.FinalActualReturnHandoff
    record = handoff.record()
    assert wire(record["snapshot"]) == before == wire(execution.snapshot())
    assert set(record["snapshot"]["state"]) == contract.STATE_KEYS
    assert record["snapshot"]["state"]["engine_states"][0]["throttle"] > 0
    assert record["origin"]["retained_count"] == record["inventory"]["retained_count"] == 13
    assert len(record["inventory"]["release_receipts"]) == 13
    assert handoff.origin is b.origin and handoff.request is b.request
    assert record["coast_history"][-1]["state"] == record["snapshot"]["state"]
    assert record["initial_binding"] == b.request.record() and record["reservation_changes"] == []
    assert all(record[name] is False for name in ("actual_state_assigned", "plant_advanced", "return_controller_started",
        "forecast_state_used", "completed_flight", "source_provenance_authenticated", "old_certificate_admitted",
        "qualification_refreshed", "mission_authority_granted", "dispatch_authorized", "physical_execution"))
    actual_state, actual_vehicle = handoff.consume()
    assert actual_state is state and actual_vehicle is vehicle
    assert handoff.consumed is True and handoff.record() == record
    assert execution.result is None and execution.snapshot()["completed"] is False
    with pytest.raises(ValueError, match="already_consumed"):
        handoff.consume()


@pytest.mark.parametrize("candidate_id", ["nominal", "next_orbit"])
def test_handoff_preserves_selected_opportunity_without_recasting_due_state_as_pre_dispatch_origin(candidate_id):
    b = bound_fixture(candidate_id)
    execution = actor(b)
    _, _, snapshot = due_owner(execution)
    handoff = execution.handoff_actual_return(b.policy, dispatch=dispatch_proof(b, snapshot))
    evidence = handoff.record()
    assert handoff.origin is b.origin
    assert evidence["snapshot"]["binding"]["candidate_id"] == candidate_id
    assert evidence["snapshot"]["state"]["time_s"] > evidence["origin"]["state"]["time_s"]
    assert evidence["snapshot"]["state"]["time_s"] >= b.anchor.record()[candidate_id+"_s"]
    assert evidence["origin"]["state"]["time_s"] < b.anchor.record()["nominal_s"]


@pytest.mark.parametrize("method", ["pull", "step", "advance", "replace", "reacquire", "origin", "stop", "handoff"])
def test_old_actor_cannot_advance_reacquire_change_reservation_or_reuse_after_handoff(bound_case, method):
    b = bound_case
    execution = actor(b)
    _, _, snapshot = due_owner(execution)
    proof = dispatch_proof(b, snapshot)
    execution.handoff_actual_return(b.policy, dispatch=proof)
    calls = {"pull": execution._pull, "step": execution.step_once,
        "advance": lambda: execution.advance_until(snapshot["state"]["time_s"]),
        "replace": lambda: execution.replace_reservation(b.request), "reacquire": execution.reacquire_actual_snapshot,
        "origin": execution.current_origin, "stop": lambda: execution.stop_before_dispatch("run_budget_exhausted"),
        "handoff": lambda: execution.handoff_actual_return(b.policy, dispatch=proof)}
    with pytest.raises(ValueError):
        calls[method]()
    assert execution.result is None


@pytest.mark.parametrize("mutation", ["snapshot_state", "snapshot_vehicle", "owner_missing", "owner_controller",
    "early", "late", "binding", "history", "denied", "selection", "candidate", "time"])
def test_changed_owner_snapshot_or_unaccepted_dispatch_cannot_become_a_handoff(bound_case, mutation):
    b = bound_case
    execution = actor(b)
    state, vehicle, snapshot = due_owner(execution,
        slip=-.1 if mutation == "early" else 1. if mutation == "late" else .1)
    changes = {"denied": {"accepted": False}, "selection": {"stage": "selection"},
        "candidate": {"candidate_sha256": "0"*64}, "time": {"time_s": state.time_s+1.}}
    proof = dispatch_proof(b, snapshot, changed=changes.get(mutation))
    if mutation == "snapshot_state":
        execution._context["snapshot"]["state"]["engine_states"][0]["throttle"] = 0.
    elif mutation == "snapshot_vehicle":
        execution._context["snapshot"]["vehicle"]["dry_mass_kg"] += 1.
    elif mutation == "owner_missing":
        execution._context["actual_owner_provider"] = None
    elif mutation == "owner_controller":
        execution._context["actual_owner_provider"] = lambda: (state, vehicle, object())
    elif mutation == "binding":
        execution._context["snapshot"]["binding"]["candidate_id"] = "next_orbit"
    elif mutation == "history":
        execution._context["coast_history"][-1]["state"]["flap_angles_rad"][0] += .01
    with pytest.raises(ValueError):
        execution.handoff_actual_return(b.policy, dispatch=proof)
    assert execution._final_handoff is None and execution.result is None


@pytest.mark.parametrize("bad", [None, {}, {"accepted": True}, {"schema": "missionos.starship_restored_return_prediction.v1"}])
def test_no_saved_record_or_boolean_creates_actual_ownership(bound_case, bad):
    with pytest.raises(ValueError, match="live_actual_owner"):
        final.FinalActualReturnHandoff(record=bad)
    execution = actor(bound_case)
    due_owner(execution)
    with pytest.raises(ValueError):
        execution.handoff_actual_return(bound_case.policy, dispatch=bad)
    execution._iterator.close()


def test_default_actor_never_exposes_actual_owner_or_admits_handoff(bound_case):
    execution = actor(bound_case, opted_in=False)
    assert "actual_owner_provider" not in execution._context
    with pytest.raises(ValueError, match="not_opted_in"):
        execution.handoff_actual_return(bound_case.policy, dispatch={})
    execution._iterator.close()


def test_mutated_actual_object_is_not_accepted_when_new_owner_consumes(bound_case):
    execution = actor(bound_case)
    state, _, snapshot = due_owner(execution)
    handoff = execution.handoff_actual_return(bound_case.policy, dispatch=dispatch_proof(bound_case, snapshot))
    object.__setattr__(state, "propellant_kg", state.propellant_kg+1.)
    with pytest.raises(ValueError, match="objects_changed"):
        handoff.consume()
    assert handoff.consumed is False


def test_arbitrary_reservation_change_cannot_be_added_to_actual_handoff(bound_case):
    execution = actor(bound_case)
    _, _, snapshot = due_owner(execution)
    execution._context["reservation_changes"] = [{"reservation_change_sha256": "0"*64, "accepted": True}]
    try:
        with pytest.raises(ValueError, match="reservation_change_not_actual"):
            execution.handoff_actual_return(bound_case.policy, dispatch=dispatch_proof(bound_case, snapshot))
        assert execution._final_handoff is None
    finally:
        execution._iterator.close()


@pytest.mark.parametrize("mutation", ["plan", "source", "file", "raw"])
def test_handoff_requires_exact_explicit_source_and_plan_policy(bound_case, mutation):
    b = bound_case
    if mutation == "plan":
        with pytest.raises(ValueError):
            final.FinalActualReturnHandoffPolicy("invalid", b.source)
    elif mutation == "source":
        with pytest.raises(ValueError, match="source_or_plan"):
            b.policy.validate_binding({**b.source, "src/runtime/other.py": "1"*64}, "a"*64)
    elif mutation == "file":
        with pytest.raises(ValueError, match="current_source"):
            final.FinalActualReturnHandoffPolicy("a"*64, {**b.source, final.SOURCE: "0"*64})
    else:
        with pytest.raises(ValueError):
            returning.RestoredReturnExecution(b.request, final_handoff_policy=b.policy.record())


def test_coordinator_takes_the_opt_in_before_controller_start_and_emits_distinct_incomplete_record(bound_case, tmp_path, monkeypatch):
    b = bound_case
    pairs = [approval_pipe() for _ in range(7)]
    context = {"run_id": "final-handoff-contract", "mission_id": "synthetic", "vehicle_id": "ship"}
    client = RestoredDecisionClient(context=context, plan_sha256="a"*64, source_sha256=final._digest(b.source),
        request_writer=pairs[0][1], response_reader=pairs[1][0])
    control = coordinator.RestoredReturnCoordinator(b.origin, profile_raw_bytes=b.values[3],
        expected_source_sha256=b.source, integration_policy=coordinator.RestoredIntegrationPolicy(2),
        context=context, plan_sha256="a"*64, output_dir=tmp_path/"actual", decision_client=client,
        ready_writer=pairs[2][1], ready_reader=pairs[3][0], approval_request_writer=pairs[4][1],
        approval_grant_reader=pairs[5][0], telemetry_writer=pairs[6][1], sites=b.values[6],
        notice_resources=lambda _snapshot: (b.values[7], b.values[8]), notice_generations=(0,),
        deployment_record={"schema": "missionos.starship_restored_deployment_judgments.v1"},
        final_handoff_policy=b.policy)
    try:
        control.start()
        state, vehicle, snapshot = due_owner(control.execution)
        # Only the prior accepted governance state and numerical verdict are
        # scripted. The production coordinator dispatch/handoff branch runs.
        control.stage = "waiting_reserved"
        control.pending = None
        control.actor = SimpleNamespace(status="reservation_confirmed", revision=0,
            scope=control.binding.record()["initial_scope"], record=lambda: {"fixture": True})
        monkeypatch.setattr(control, "_begin_judgment", lambda: None)
        proof = dispatch_proof(b, snapshot).record()
        monkeypatch.setattr(control, "_admit", lambda *_a: (b.candidate, proof["admission"]))
        returned = control.tick()
        assert control.stage == "handed_off" and control.result is None
        assert returned == snapshot and returned["return_controller"] is None
        assert control.final_handoff.consume() == (state, vehicle)
        evidence = control.record()
        assert evidence["schema"] == "missionos.starship_final_pre_dispatch_governance.v1"
        assert evidence["completed_v1_flight"] is False
        assert evidence["final_handoff"]["snapshot"] == snapshot
        assert evidence["final_handoff_telemetry_sequence"] == control._telemetry_sequence
        with pytest.raises(ValueError, match="permanently_handed_off"):
            control.tick()
    finally:
        control.close()
        control.execution._iterator.close()
        for pair in pairs:
            for endpoint in pair:
                endpoint.close()


@pytest.mark.parametrize("failure", ["due_frame_missing", "partial_pipe_write"])
def test_handoff_cannot_strand_the_due_state_telemetry(bound_case, monkeypatch, failure):
    b = bound_case
    execution = actor(b)
    _, _, snapshot = due_owner(execution)
    proof = dispatch_proof(b, snapshot).record()
    # Isolate the delivery guard with a prepared coordinator state. Its full
    # constructor/pipe boundary is exercised by the preceding production test.
    control = object.__new__(coordinator.RestoredReturnCoordinator)
    control._final_handoff, control._final_handoff_policy = None, b.policy
    control.execution, control.stage, control.pending = execution, "waiting_reserved", None
    control.actor = SimpleNamespace(status="reservation_confirmed", revision=0)
    control.events, stopped = [], []
    control._telemetry_key = (None if failure == "due_frame_missing" else
        (snapshot["state"]["time_s"], 0, coordinator._hash(snapshot["binding"])))
    control.telemetry = SimpleNamespace(flush=lambda: failure != "partial_pipe_write")
    monkeypatch.setattr(control, "_emit", lambda: None)
    monkeypatch.setattr(control, "_context", lambda: None)
    monkeypatch.setattr(control, "_begin_judgment", lambda: None)
    monkeypatch.setattr(control, "_admit", lambda *_a: (b.candidate, proof["admission"]))
    monkeypatch.setattr(control, "_stop", lambda reason: stopped.append(reason))
    before = execution.snapshot()
    try:
        assert control.tick() == before
        assert stopped == ["source_or_observation_changed"]
        assert control.final_handoff is None and execution._final_handoff is None
        assert execution.result is None and execution.snapshot() == before
    finally:
        execution._iterator.close()
