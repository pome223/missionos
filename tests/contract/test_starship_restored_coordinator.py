"""Scripted actual ticks and fake subprocess results; no physics or native AI."""
from copy import deepcopy
from hashlib import sha256
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from src.runtime import starship_restored_coordinator as control
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_return as returning
from src.runtime.starship_inflight_runtime import approval_pipe
from src.runtime.starship_restored_decision_pipe import RestoredDecisionClient
from tests.contract.test_starship_restored_admission import case


class Clock:
    def __init__(self):
        self.now = 100.
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def no_actual_physics_provider_or_subprocess(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    from src.runtime import starship_sixdof_mission as mission
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Coordinator tests cannot run physics, forecasts or native models")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
            (mission, "advance_plant"), (returning, "advance_plant"), (returning, "control"),
            (returning, "forecast_restored_return"), (socket, "socket"),
            (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    nominal, later = case(), case(candidate_id="next_orbit")
    sources = deepcopy(nominal[4])
    sources["src/runtime/starship_restored_coordinator.py"] = "c"*64
    raw = json.loads(nominal[1].source_record_bytes())
    raw["source_sha256"] = sources
    encoded = control._wire(raw)
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, nominal[3], sources)
    clock, pairs = Clock(), [approval_pipe() for _ in range(7)]
    context = {"run_id": "coordinator", "mission_id": "synthetic", "vehicle_id": "ship"}
    client = RestoredDecisionClient(context=context, plan_sha256="a"*64, source_sha256=control._hash(sources),
        request_writer=pairs[0][1], response_reader=pairs[1][0], clock=clock)
    requests, replies, forecasts = [], [], []
    def begin(request, *, started_wall_s=None):
        requests.append(deepcopy(request))
        started = clock.now if started_wall_s is None else started_wall_s
        return {"started_wall_s": started, "wall_deadline_s": started+request["deadline_s"]-request["time_s"]}
    monkeypatch.setattr(client, "begin", begin)
    monkeypatch.setattr(client, "poll", lambda _now: replies.pop(0) if replies else None)
    tools = {"notice": nominal[7], "resources": nominal[8]}
    class Batch:
        def __init__(self, items, **kwargs):
            self.requests, self.output_dir, self.policy = items, kwargs["output_dir"], kwargs["integration_policy"]
            self.deadline, self.state, self.ready = kwargs["wall_deadline_monotonic"], "created", False
            forecasts.append(self)
        def start(self):
            self.output_dir.mkdir()
            self.state = "running"
            profile = self.output_dir/"profile.json"
            profile.write_bytes(nominal[3])
            (self.output_dir/"origin-000.json").write_bytes(origin.source_record_bytes())
            control._save(self.output_dir/"job.json", {"profile_file": "profile.json",
                "input_sha256": {"profile.json": sha256(nominal[3]).hexdigest(),
                    "origin-000.json": origin.source_record_sha256}})
            control._save(self.output_dir/"batch.claim", {"synthetic_transport_fixture": True})
            return self
        def poll(self):
            if not self.ready:
                return None
            import gzip
            results = []
            for request, original in zip(self.requests, (nominal, later)):
                row = original[0].record()
                row.update(binding=request.record(), initial_binding=request.record())
                result = returning.RestoredReturnResult(control._wire(row))
                folder = self.output_dir/request.candidate_id
                folder.mkdir(exist_ok=True)
                (folder/"prediction.json.gz").write_bytes(gzip.compress(result._record_json))
                for filename in ("result.json", "integration.json", "started.json", "worker.claim"):
                    if not (folder/filename).exists():
                        control._save(folder/filename, {"synthetic_transport_fixture": True})
                results.append(result)
            self.state = "completed"
            return tuple(results)
        def close(self):
            self.state = "closed"
    monkeypatch.setattr(control, "RestoredForecastBatch", Batch)
    coordinator = control.RestoredReturnCoordinator(origin, profile_raw_bytes=nominal[3],
        expected_source_sha256=sources, integration_policy=control.RestoredIntegrationPolicy(2),
        context=context, plan_sha256="a"*64, output_dir=tmp_path/"mission", decision_client=client,
        ready_writer=pairs[2][1], ready_reader=pairs[3][0], approval_request_writer=pairs[4][1],
        approval_grant_reader=pairs[5][0], telemetry_writer=pairs[6][1], sites=nominal[6],
        notice_resources=lambda _snapshot: (tools["notice"], tools["resources"]),
        notice_generations=(0, 1), deployment_record={"schema": "missionos.starship_restored_deployment_judgments.v1"},
        clock=clock, sleep=clock.sleep)
    actual_steps, actual_owner = [], {}
    def install_scripted_steps():
        execution = coordinator.execution
        # A separate scripted actual-state owner replaces the generator only
        # in this fixture. It cannot silently follow a forged cached snapshot.
        actual_owner["snapshot"] = execution.snapshot()
        execution._context["actual_snapshot_provider"] = lambda: deepcopy(actual_owner["snapshot"])
        def step():
            row = execution.snapshot()
            row["state"]["time_s"] += .25
            execution._context["snapshot"] = row
            actual_owner["snapshot"] = deepcopy(row)
            execution._context["coast_history"].append({"state": deepcopy(row["state"]),
                "retained_count": origin.record()["retained_count"]})
            actual_steps.append(row["state"]["time_s"])
            return row
        monkeypatch.setattr(execution, "step_once", step)
        def stop(reason):
            # The actual stop boundary has its own production tests. This
            # coordinator fixture uses scripted ticks rather than mutating a
            # live generator's hidden local state and claiming it was physics.
            row = execution.snapshot()
            value = {"schema": "missionos.starship_restored_return_continuation.v1", "prediction_only": False,
                "final_state": deepcopy(row["state"]), "return_controller": None,
                "outcome": {"termination": "return_inhibited_by_governance", "reason": reason},
                "old_certificate_admitted": False, "qualification_refreshed": False,
                "mission_authority_granted": False, "dispatch_authorized": False, "physical_execution": False}
            execution._result = returning.RestoredReturnResult(control._wire(value))
            row["completed"] = True
            execution._context["snapshot"] = row
            return execution._result
        monkeypatch.setattr(execution, "stop_before_dispatch", stop)
    def start():
        coordinator.start()
        install_scripted_steps()
    result = SimpleNamespace(coordinator=coordinator, origin=origin, clock=clock, pairs=pairs,
        requests=requests, replies=replies, forecasts=forecasts, tools=tools, source=sources,
        actual_steps=actual_steps, actual_owner=actual_owner, start=start, nominal=nominal)
    yield result
    if coordinator.execution is not None:
        coordinator.execution._iterator.close()
    for pair in pairs:
        for endpoint in pair:
            endpoint.close()


def ack(s):
    c = s.coordinator
    frame = s.pairs[2][0].poll()
    assert frame == c.ready
    value = {"schema": control.ACK, "ready_sha256": control._hash(frame), "nonce": frame["nonce"],
        "context": c.context, "plan_sha256": c.plan, "source_sha256": control._hash(c.source),
        "binding_sha256": frame["binding_sha256"], "initial_reservation_sha256": c.initial_sha, "ledger_ready": True}
    s.pairs[3][1].send(value)
    return value


def judged(s, action="keep_reserved_return", *, accepted=True):
    s.start()
    ack(s)
    s.coordinator.tick()
    s.forecasts[0].ready = True
    # Real typed candidates use saved analytic fixture contacts. For isolated
    # coordinator state-machine tests only, fresh admission is an injected tool.
    original = control.admit_restored_candidate
    def result(candidate, observation, **kwargs):
        value = original(candidate, observation, **kwargs)
        value.update(accepted=accepted, reasons=[] if accepted else ["synthetic_inadmissible"])
        return value
    return original, result


def test_constructor_has_no_launch_files_or_actual_advance(scenario):
    s = scenario
    assert s.coordinator.stage == "created" and s.forecasts == [] and s.actual_steps == []
    assert not s.coordinator.output.exists()


def test_ready_packet_is_compact_nonce_bound_and_initial_reservation_is_request_not_forecast(scenario):
    s = scenario
    s.start()
    frame = s.coordinator.ready
    assert len(control._wire(frame)) < 16384 and "source_manifest" not in frame and "binding" not in frame
    assert frame["initial_reservation_sha256"] == control._hash(s.coordinator.requests["nominal"].record())
    assert sha256((s.coordinator.output/frame["origin_file"]).read_bytes()).hexdigest() == frame["source_record_sha256"]
    assert len(frame["nonce"]) == 64 and s.forecasts == []
    row = s.coordinator.record()
    assert row["origin_proves_numerical_policy"] is False and row["outer_completed_actual_integration_receipt_required"] is True


def test_actual_coast_advances_and_wall_is_paced_while_host_ack_pending(scenario):
    s = scenario
    s.start()
    for _ in range(3):
        s.coordinator.tick()
        while s.pairs[6][0].poll() is not None:
            pass
    assert len(s.actual_steps) == 3 and s.clock.now == pytest.approx(100.5)
    assert not s.forecasts and s.coordinator.stage == "await_host_ready"


@pytest.mark.parametrize("key", ["nonce", "ready_sha256", "binding_sha256", "initial_reservation_sha256", "ledger_ready"])
def test_host_ack_cannot_be_rebound_or_self_approved(scenario, key):
    s = scenario
    s.start()
    good = ack(s)
    # Replace the unconsumed actual ACK with one wrong field.
    s.pairs[3][0].poll()
    good[key] = False if key == "ledger_ready" else "0"*64
    s.pairs[3][1].send(good)
    s.coordinator.tick()
    assert s.coordinator.stage == "inhibited" and not s.forecasts and not s.actual_steps
    assert s.coordinator.result.record()["return_controller"] is None


def test_forecasts_start_only_after_exact_host_ack_both_candidates_before_model(scenario):
    s = scenario
    s.start()
    expected = ack(s)
    s.coordinator.tick()
    batch = s.forecasts[0]
    assert [x.candidate_id for x in batch.requests] == ["nominal", "next_orbit"]
    assert batch.policy.subdivisions == 2 and batch.deadline == 400.
    assert s.coordinator.ack == expected and not s.requests
    assert s.coordinator.stage == "forecasting"


def test_model_receives_each_candidate_tool_costs_with_next_orbit_wait_difference(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    facts = s.requests[0]["facts"]
    for candidate in ("nominal", "next_orbit"):
        _row, check = s.coordinator._admit(candidate, "selection")
        account = check["resource_accounting"]
        assert facts[f"candidate.{candidate}.power_kwh"] == account["energy_at_return_kwh"]
        assert facts[f"candidate.{candidate}.temperature_k"] == account["temperature_at_return_k"]
        assert facts[f"candidate.{candidate}.propellant_loss_kg"] == account["modeled_loss_budget_kg"]
    assert facts["candidate.next_orbit.power_kwh"] < facts["candidate.nominal.power_kwh"]
    assert facts["candidate.next_orbit.temperature_k"] > facts["candidate.nominal.temperature_k"]
    assert facts["candidate.next_orbit.propellant_loss_kg"] > facts["candidate.nominal.propellant_loss_kg"]


def _real_client_after_delayed_admission(s, monkeypatch, per_candidate_delay):
    """Exercise the real outgoing pipe with scripted ticks and tool duration."""
    judged(s)
    original = s.coordinator._admit
    def delayed(candidate, stage):
        value = original(candidate, stage)
        s.clock.now += per_candidate_delay
        return value
    monkeypatch.setattr(s.coordinator, "_admit", delayed)
    monkeypatch.setattr(s.coordinator.client, "begin", RestoredDecisionClient.begin.__get__(s.coordinator.client))


def test_admission_time_is_deducted_before_real_client_frame_and_pacer_share_epoch(scenario, monkeypatch):
    s = scenario
    _real_client_after_delayed_admission(s, monkeypatch, 10.)
    steps = len(s.actual_steps)
    s.coordinator._pump()
    frame = s.pairs[0][0].poll()
    epoch = s.coordinator.pending.epoch
    assert s.clock.now == 120. and frame["started_wall_s"] == epoch.wall_start_s == 100.
    assert frame["wall_deadline_s"] == epoch.wall_deadline_s == 280.
    assert frame["request"]["deadline_s"] == epoch.simulation_deadline_s
    assert s.coordinator.stage == "judging" and len(s.actual_steps) == steps


@pytest.mark.parametrize("delay,send", [(72.5, True), (72.500001, False), (100., False)])
def test_expensive_admission_reserves_initial_budget_or_stops_before_any_real_pipe_send(scenario, monkeypatch, delay, send):
    s = scenario
    _real_client_after_delayed_admission(s, monkeypatch, delay)
    steps = len(s.actual_steps)
    s.coordinator._pump()
    frame = s.pairs[0][0].poll()
    assert len(s.actual_steps) == steps
    if send:
        assert frame["wall_deadline_s"]-s.clock.now == 35.
        assert s.coordinator.stage == "judging" and s.coordinator.client.pending is not None
    else:
        assert frame is None and s.coordinator.client.pending is None
        assert s.coordinator.client.sequence == 0 and not s.coordinator.client.seen
        assert s.coordinator.stage == "inhibited" and s.coordinator.pending is None
        assert s.coordinator.dispatch["refusal_reason"] == "judgment_window_unavailable"


@pytest.mark.parametrize("delay", [72.500001, 100.])
def test_exhausted_judgment_window_reaches_real_execution_stop_without_a_plant_step(scenario, monkeypatch, delay):
    s = scenario
    _real_client_after_delayed_admission(s, monkeypatch, delay)
    # Replace the scripted seam with a fresh production execution at its first
    # yielded coast tick. No control or dynamics step is needed to reject work.
    s.coordinator.execution._iterator.close()
    actual = returning.RestoredReturnExecution(s.coordinator.requests["nominal"],
        origin_record_bytes=s.coordinator.origin.source_record_bytes())
    s.coordinator.execution = actual
    before = actual.snapshot()
    s.coordinator._pump()
    result = actual.result.record()
    assert s.coordinator.stage == "inhibited" and s.pairs[0][0].poll() is None
    assert s.coordinator.client.pending is None and s.coordinator.client.sequence == 0
    assert result["outcome"]["termination"] == "return_inhibited_by_governance"
    assert result["outcome"]["integration_steps"] == 0
    assert result["final_state"] == before["state"]
    assert result["return_controller"] is result["return_start_state"] is None
    assert actual.snapshot()["completed"] is True
    assert any(event["event"] == "return_inhibited_by_governance"
        and event["reason"] == "judgment_window_unavailable" for event in result["events"])


@pytest.mark.parametrize("delay,send", [(0., True), (10., False)])
def test_original_global_wall_cap_clips_request_and_cannot_be_restarted_after_admission(scenario, monkeypatch, delay, send):
    s = scenario
    _real_client_after_delayed_admission(s, monkeypatch, delay)
    # Narrow only this synthetic binding's global wall window. No registration,
    # real operator ledger, flight or previously retained evidence is modified.
    bound = s.coordinator.binding.record()
    bound["bounds"]["wall_deadline_s"] = 150.
    object.__setattr__(s.coordinator.binding, "_wire", control._wire(bound))
    s.coordinator._pump()
    frame = s.pairs[0][0].poll()
    if send:
        epoch = s.coordinator.pending.epoch
        assert frame["started_wall_s"] == epoch.wall_start_s == 100.
        assert frame["wall_deadline_s"] == epoch.wall_deadline_s == 150.
        assert frame["request"]["deadline_s"]-frame["request"]["time_s"] == 50.
    else:
        assert frame is None and s.coordinator.client.pending is None
        assert s.coordinator.stage == "inhibited"
        assert s.coordinator.dispatch["refusal_reason"] == "judgment_window_unavailable"


def test_outer_exception_cleanup_closes_forecast_peer_batch_without_an_actual_step(scenario):
    s = scenario
    s.start()
    ack(s)
    s.coordinator.tick()
    count = len(s.actual_steps)
    s.coordinator.close()
    assert s.forecasts[0].state == "closed" and len(s.actual_steps) == count


def test_model_wait_is_not_overwritten_and_same_generation_is_not_reasked(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    assert len(s.requests) == 1 and s.coordinator.stage == "judging"
    s.replies.append({"status": "valid", "action": "wait"})
    s.coordinator.tick()
    assert s.coordinator.stage == "waiting_reserved"
    assert s.coordinator.decisions[0]["original_action"] == "wait"
    assert s.coordinator.decisions[0]["disposition"] == "valid_wait_reservation_unchanged"
    s.coordinator.tick()
    assert len(s.requests) == 1 and not s.coordinator.changes


def test_only_registered_new_notice_can_trigger_new_judgment(scenario, monkeypatch):
    from dataclasses import replace
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    s.replies.append({"status": "valid", "action": "wait"})
    s.coordinator.tick()
    s.tools["notice"] = replace(s.tools["notice"], generation=1)
    s.coordinator.tick()
    assert len(s.requests) == 2 and s.requests[0]["request_id"] != s.requests[1]["request_id"]
    assert s.requests[1]["time_s"] > s.requests[0]["time_s"]
    assert s.requests[1]["deadline_s"] <= s.coordinator.binding.record()["bounds"]["reservation_cutoff_s"]


def test_wrong_but_valid_choice_is_recorded_rejected_without_fallback_or_retry(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s, accepted=False)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    s.replies.append({"status": "valid", "action": "select_nominal"})
    s.coordinator.tick()
    row = s.coordinator.decisions[0]
    assert row["original_action"] == "select_nominal" and row["disposition"] == "valid_choice_rejected_not_overwritten"
    assert s.coordinator.stage == "waiting_reserved" and not s.coordinator.changes
    s.coordinator.tick()
    assert len(s.requests) == 1


@pytest.mark.parametrize("accepted", [True, False])
def test_unavailable_model_preserves_only_fresh_preapproved_nominal(scenario, monkeypatch, accepted):
    s = scenario
    _, numerical = judged(s, accepted=accepted)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    s.replies.append({"status": "no_valid_judgment", "action": None})
    s.coordinator.tick()
    assert not s.coordinator.changes
    assert s.coordinator.stage == ("waiting_reserved" if accepted else "inhibited")
    assert s.coordinator.decisions[0]["original_action"] is None
    if not accepted:
        assert s.coordinator.dispatch["refusal_reason"] == "model_no_valid_judgment"


@pytest.mark.parametrize("change", ["unknown", "replayed", "same_generation_content"])
def test_unregistered_replayed_or_mutated_notice_fails_closed(scenario, change):
    from dataclasses import replace
    s = scenario
    s.start()
    s.coordinator.tick()
    if change == "unknown":
        s.tools["notice"] = replace(s.tools["notice"], generation=99)
    elif change == "same_generation_content":
        s.tools["notice"] = replace(s.tools["notice"], expires_s=15999.)
    else:
        s.tools["notice"] = replace(s.tools["notice"], generation=1)
        s.coordinator.tick()
        s.tools["notice"] = replace(s.tools["notice"], generation=0)
    s.coordinator.tick()
    assert s.coordinator.stage == "inhibited"


def test_handshake_timeout_is_consumed_without_starting_forecast_or_igniting(scenario):
    s = scenario
    s.start()
    s.clock.now = 400.
    s.coordinator.tick()
    assert s.coordinator.stage == "inhibited" and not s.forecasts and not s.actual_steps
    assert s.coordinator.dispatch["refusal_reason"] == "host_ready_unavailable"
    with pytest.raises(ValueError, match="single_use"):
        s.coordinator.start()


def test_forecast_deadline_does_not_reset_after_host_ack(scenario):
    s = scenario
    s.start()
    s.clock.now = 105.
    ack(s)
    s.coordinator.tick()
    assert s.forecasts[0].deadline == 400.
    s.clock.now = 400.
    s.coordinator.tick()
    assert s.coordinator.stage == "inhibited" and s.forecasts[0].state == "closed"


def test_due_dispatch_is_checked_before_generator_resume_and_never_uses_forecast_state(scenario, monkeypatch):
    s = scenario
    production_admission, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    s.replies.append({"status": "valid", "action": "keep_reserved_return"})
    s.coordinator.tick()
    execution = s.coordinator.execution
    snapshot = execution.snapshot()
    # An analytic saved due witness, explicitly scripted as this actual actor's
    # observed tick for a production dispatch-admission seam test; no trajectory
    # is propagated or claimed as a physical flight here.
    snapshot["state"] = deepcopy(s.coordinator.predictions[0].record()["return_start_state"])
    execution._context["snapshot"] = snapshot
    before = deepcopy(snapshot["state"])
    monkeypatch.setattr(control, "admit_restored_candidate", production_admission)
    s.coordinator.tick()
    assert s.coordinator.dispatch["gate_met"] is True
    assert s.coordinator.dispatch["actual_snapshot"]["state"] == before
    assert s.coordinator.dispatch["admission"]["fresh_state_match"]["method"] == "exact_precontroller_return_start_state"
    assert s.coordinator.stage == "returning" and s.actual_steps[-1] == before["time_s"]+.25


def test_rejected_due_dispatch_keeps_exact_current_state_no_controller_and_no_extra_step(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    s.replies.append({"status": "valid", "action": "wait"})
    s.coordinator.tick()
    snapshot = s.coordinator.execution.snapshot()
    snapshot["state"]["time_s"] = snapshot["binding"]["return_time_s"]
    s.coordinator.execution._context["snapshot"] = snapshot
    count, before = len(s.actual_steps), deepcopy(snapshot["state"])
    monkeypatch.setattr(control, "admit_restored_candidate", lambda *_a, **_k: {"accepted": False, "reasons": ["synthetic"]})
    s.coordinator.tick()
    assert s.coordinator.stage == "inhibited" and len(s.actual_steps) == count
    assert s.coordinator.result.record()["final_state"] == before
    assert s.coordinator.result.record()["return_controller"] is None


def test_telemetry_preserves_exact_worker_observation_for_host_history(scenario):
    s = scenario
    s.start()
    frame = s.pairs[6][0].poll()
    assert frame["runtime_state"]["observation"]["state_sha256"] == control._hash(frame["actual_state"])
    assert frame["runtime_state"]["observation"]["wall_time_s"] == 100.
    assert frame["runtime_state"]["reservation_sha256"] == s.coordinator.initial_sha
    assert "source_manifest" not in frame and len(control._wire(frame)) <= 16384


def test_scope_request_is_stable_proposal_and_approved_change_uses_actual_origin_then_later_confirmation(scenario, monkeypatch):
    from src.runtime import starship_restored_admission as admission
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    monkeypatch.setattr(admission, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    while s.pairs[6][0].poll() is not None:
        pass
    s.replies.append({"status": "valid", "action": "select_next_orbit"})
    s.coordinator.tick()
    c = s.coordinator
    assert c.stage == "awaiting_operator" and not c.changes
    request = s.pairs[4][0].poll()
    assert request["candidate_sha256"] == c.decisions[0]["candidate"]["proposal_sha256"]
    assert request["candidate_sha256"] != c.decisions[0]["candidate"]["prediction_record_sha256"]
    assert c._snapshot()["binding"]["candidate_id"] == "nominal"
    # Grant parsing/HMAC is independently tested by the real host/worker seam.
    # Here inject only that seam's already-validated result, then execute the
    # actual coordinator command, typed-origin and reservation callback.
    monkeypatch.setattr(c.actor, "_grant", lambda *_a: {"revision": 1,
        "scope": c.binding.record()["registry"], "sha256": "f"*64})
    s.pairs[5][1].send({"synthetic_prevalidated_grant": True})
    while s.pairs[6][0].poll() is not None:
        pass
    c.tick()
    assert c.stage == "confirming_reservation" and len(c.changes) == 1
    change = c.changes[0]
    raw = (c.output/change["origin"]["path"]).read_bytes()
    actual_origin = contract.ActualRestoredOrigin(sha256(raw).hexdigest(), raw, c.profile, c.source)
    assert actual_origin.record()["state"] == change["actual_snapshot"]["state"]
    assert c._snapshot()["binding"]["candidate_id"] == "next_orbit"
    assert c._snapshot()["binding"]["return_time_s"] == c.anchor.record()["next_orbit_s"]
    assert change["confirmation_snapshot"] is None
    assert c.actor.status == "reservation_changed_waiting_observation"
    c.tick()
    assert c.actor.status == "reservation_confirmed" and c.stage == "waiting_reserved"
    assert change["confirmation_snapshot"]["state"]["time_s"] > change["actual_snapshot"]["state"]["time_s"]
    assert change["confirmation_snapshot"]["binding"] == c._snapshot()["binding"]
    with pytest.raises(ValueError, match="budget_exhausted"):
        c.actor.request_change(request_id="again", candidate_sha256="e"*64, state=c._state())


def test_current_origin_cannot_be_created_after_original_nominal(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    s.coordinator.tick()
    c = s.coordinator
    state = c._state()
    state["simulation_time_s"] = c.anchor.record()["nominal_s"]
    calls = []
    monkeypatch.setattr(c.execution, "current_origin", lambda: calls.append("forbidden"))
    with pytest.raises(ValueError, match="one_pre_nominal"):
        c._command("select_next_orbit", state, {"revision": 1})
    assert calls == [] and not c.changes


@pytest.mark.parametrize('mutation', [None, 'missing_expected', 'hash', 'client_policy', 'source_pin'])
def test_real_coordinator_requires_same_external_policy_and_real_v2_client(scenario, tmp_path, mutation):
    from pathlib import Path
    from src.runtime import starship_restored_return_review as review
    old = scenario.coordinator
    source = dict(old.source)
    source['src/runtime/starship_restored_return_review.py'] = sha256(Path(review.__file__).read_bytes()).hexdigest()
    if mutation == 'source_pin':
        source.pop('src/runtime/starship_restored_return_review.py')
    raw = json.loads(old.origin.source_record_bytes())
    raw['source_sha256'] = source
    encoded = control._wire(raw)
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, old.profile, source)
    policy = deepcopy(review.DEFAULT_POLICY)
    client = RestoredDecisionClient(context=old.context, plan_sha256=old.plan, source_sha256=control._hash(source),
        request_writer=old.client.writer, response_reader=old.client.reader, clock=old.clock,
        return_review_policy=policy)
    if mutation == 'hash':
        client.binding['return_review_policy_sha256'] = '0'*64
    if mutation == 'client_policy':
        client.return_review_policy['confidence_floor'] = .4
    args = {'profile_raw_bytes': old.profile, 'expected_source_sha256': source,
        'integration_policy': old.policy, 'context': old.context, 'plan_sha256': old.plan,
        'output_dir': tmp_path/'real-v2-coordinator', 'decision_client': client,
        'ready_writer': old.ready_writer, 'ready_reader': old.ready_reader,
        'approval_request_writer': old.approval_writer, 'approval_grant_reader': old.approval_reader,
        'telemetry_writer': old.telemetry, 'sites': old.sites, 'notice_resources': old.tools,
        'notice_generations': old.generations, 'deployment_record': old.deployment,
        'clock': old.clock, 'sleep': old.sleep,
        'return_review_policy': None if mutation == 'missing_expected' else policy}
    if mutation is None:
        result = control.RestoredReturnCoordinator(origin, **args)
        assert result.client is client and client.record()['return_review_policy'] == policy
    else:
        with pytest.raises(ValueError, match='return_review_source_opt_in|decision_pipe_binding_changed'):
            control.RestoredReturnCoordinator(origin, **args)
    assert not args['output_dir'].exists() and not scenario.forecasts and not scenario.actual_steps


@pytest.mark.parametrize("kind", ["source", "policy", "notice", "client", "shared_channel"])
def test_constructor_rejects_incompatible_sources_policies_events_or_channels_without_output(scenario, tmp_path, kind):
    s, old = scenario, scenario.coordinator
    args = {"profile_raw_bytes": old.profile, "expected_source_sha256": old.source,
        "integration_policy": old.policy, "context": old.context, "plan_sha256": old.plan,
        "output_dir": tmp_path/"no-output", "decision_client": old.client,
        "ready_writer": old.ready_writer, "ready_reader": old.ready_reader,
        "approval_request_writer": old.approval_writer, "approval_grant_reader": old.approval_reader,
        "telemetry_writer": old.telemetry, "sites": old.sites,
        "notice_resources": old.tools, "notice_generations": old.generations,
        "deployment_record": old.deployment, "clock": old.clock, "sleep": old.sleep}
    if kind == "source":
        args["expected_source_sha256"] = old.source | {"src/runtime/starship_sixdof.py": "0"*64}
    elif kind == "policy":
        args["integration_policy"] = control.RestoredIntegrationPolicy(4)
    elif kind == "notice":
        args["notice_generations"] = (True,)
    elif kind == "client":
        old.client.binding["plan_sha256"] = "0"*64
    else:
        args["approval_request_writer"] = old.ready_writer
    with pytest.raises(ValueError):
        control.RestoredReturnCoordinator(old.origin, **args)
    assert not (tmp_path/"no-output").exists() and not s.forecasts and not s.actual_steps


def test_slow_admission_reacquires_current_actual_state_before_real_fd_approval_request(scenario, monkeypatch):
    from src.runtime.starship_inflight_runtime import _state as validate_runtime_state
    s = scenario
    _, numerical = judged(s)
    def slow_admission(*args, **kwargs):
        result = numerical(*args, **kwargs)
        s.clock.now += 6.
        return result
    monkeypatch.setattr(control, "admit_restored_candidate", slow_admission)
    c = s.coordinator
    c.tick()
    frames = []
    while (row := s.pairs[6][0].poll()) is not None:
        frames.append(row)
    original_bounds = deepcopy(c.binding.record()["bounds"])
    s.replies.append({"status": "valid", "action": "request_scope_amendment"})
    ordering = []
    old_telemetry_send, old_request_send = c.telemetry.send, c.approval_writer.send
    def telemetry_send(frame):
        ordering.append("telemetry")
        return old_telemetry_send(frame)
    def request_send(frame):
        ordering.append("approval_request")
        return old_request_send(frame)
    monkeypatch.setattr(c.telemetry, "send", telemetry_send)
    monkeypatch.setattr(c.approval_writer, "send", request_send)
    c.tick()
    while (row := s.pairs[6][0].poll()) is not None:
        frames.append(row)
    request = s.pairs[4][0].poll()
    assert c.stage == "awaiting_operator" and c.actor.status == "awaiting_operator"
    assert request is not None and request["observation"] == frames[-1]["runtime_state"]["observation"]
    assert ordering[-2:] == ["telemetry", "approval_request"]
    decision = c.decisions[0]
    read = decision["actual_observation_reacquisition"]
    fresh = next(row for row in frames if row["sequence"] == read["telemetry_sequence"])
    previous = frames[frames.index(fresh)-1]
    assert fresh["actual_state"] == previous["actual_state"] == decision["actual_snapshot"]["state"]
    assert fresh["runtime_state"]["observation"]["wall_time_s"] >= previous["runtime_state"]["observation"]["wall_time_s"]+6.
    validate_runtime_state(fresh["runtime_state"], s.clock.now, 5.)
    with pytest.raises(ValueError, match="runtime_observation_not_fresh"):
        validate_runtime_state(previous["runtime_state"], s.clock.now, 5.)
    assert read["provider"] == "current_yielded_plant_state" and read["plant_advanced"] is False
    assert decision["disposition"] == "scope_amendment_requested" and not c.changes
    assert c.binding.record()["bounds"] == original_bounds


@pytest.mark.parametrize("change", ["state", "availability", "vehicle", "binding", "notice"])
def test_actual_state_or_tool_change_after_admission_refuses_instead_of_relabelling_old_proof(scenario, monkeypatch, change):
    from dataclasses import replace
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    c = s.coordinator
    c.tick()
    while s.pairs[6][0].poll() is not None:
        pass
    def changed_after_admission(*args, **kwargs):
        result = numerical(*args, **kwargs)
        row = s.actual_owner["snapshot"]
        if change == "state":
            row["state"]["r_eci_m"][0] += 1.
        elif change == "availability":
            current = row["state"]["engine_states"][3]["available"]
            row["state"]["engine_states"][3]["available"] = not current
        elif change == "vehicle":
            row["vehicle"]["dry_mass_kg"] += 1.
        elif change == "binding":
            row["binding"]["return_time_s"] += 1.
        else:
            s.tools["notice"] = replace(s.tools["notice"], generation=1)
        c.execution._context["snapshot"] = deepcopy(row)
        return result
    monkeypatch.setattr(control, "admit_restored_candidate", changed_after_admission)
    before_steps = len(s.actual_steps)
    s.replies.append({"status": "valid", "action": "request_scope_amendment"})
    c.tick()
    assert c.stage == "inhibited" and c.actor.status == "idle"
    assert s.pairs[4][0].poll() is None and not c.changes
    assert c.decisions[0]["disposition"] is None
    assert len(s.actual_steps) == before_steps


def test_slow_actual_reacquisition_cannot_relax_five_second_freshness(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    c = s.coordinator
    c.tick()
    while s.pairs[6][0].poll() is not None:
        pass
    provider = c.execution._context["actual_snapshot_provider"]
    def slow_actual_read():
        actual = provider()
        s.clock.now += 6.
        return actual
    c.execution._context["actual_snapshot_provider"] = slow_actual_read
    s.replies.append({"status": "valid", "action": "request_scope_amendment"})
    c.tick()
    assert c.stage == "inhibited" and s.pairs[4][0].poll() is None
    assert c.binding.record()["bounds"]["maximum_observation_age_s"] == 5.
    assert c.decisions[0]["disposition"] is None


def test_request_sent_disposition_is_not_recorded_if_real_request_write_fails(scenario, monkeypatch):
    s = scenario
    _, numerical = judged(s)
    monkeypatch.setattr(control, "admit_restored_candidate", numerical)
    c = s.coordinator
    c.tick()
    while s.pairs[6][0].poll() is not None:
        pass
    def fail_send(_frame):
        raise OSError("synthetic closed approval pipe")
    monkeypatch.setattr(c.approval_writer, "send", fail_send)
    s.replies.append({"status": "valid", "action": "request_scope_amendment"})
    c.tick()
    assert c.stage == "inhibited" and c.decisions[0]["disposition"] is None
    assert c.actor.status == "idle" and s.pairs[4][0].poll() is None
    assert not any(row["event"] == "m3_scope_change_requested" for row in c.actor.events)
