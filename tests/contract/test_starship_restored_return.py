"""Invented finite-step/controller contracts; no real flight or forecast run."""
from dataclasses import asdict, FrozenInstanceError, replace
from hashlib import sha256
import importlib.util
import json
import math
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_restored_return as restored
from src.runtime.starship_restored_contract import ActualRestoredOrigin, RestoredCandidateSchedule

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("_restored_return_origin_fixture", Path(__file__).with_name("test_starship_restored_contract.py"))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


@pytest.fixture(autouse=True)
def no_real_dynamics_or_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Restored return contracts cannot run dynamics, optimizers or providers")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
            (restored, "advance_plant"), (restored, "control"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr(restored.ReturnController, "update", forbidden)
    monkeypatch.setattr(restored.ReturnController, "command", forbidden)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def make_origin_seed(*, retained=26, outage=True, near_return=True):
    record, profile_raw, source = FIXTURE.fixture_record(retained=retained, outage=outage)
    if near_return:
        record["final_state"]["time_s"] = 3599.95
        record["state_history"][-1]["state"]["time_s"] = 3599.95
        record["inventory"]["observed_time_s"] = 3599.95
    raw = encode(record)
    return ActualRestoredOrigin(sha256(raw).hexdigest(), raw, profile_raw, source), profile_raw, source, raw


def make_origin(**kwargs):
    return make_origin_seed(**kwargs)[:3]


def request(*, retained=26, outage=True, candidate="nominal", maximum_control_ticks=20, near_return=True):
    origin, profile_raw, source = make_origin(retained=retained, outage=outage, near_return=near_return)
    schedule = RestoredCandidateSchedule(origin)
    return restored.RestoredReturnRequest(origin, schedule, candidate, profile_raw, source,
        maximum_control_ticks=maximum_control_ticks)


@pytest.mark.parametrize("retained", [0, 13, 26])
@pytest.mark.parametrize("candidate", ["nominal", "next_orbit"])
def test_request_binds_actual_inventory_source_profile_schedule_and_candidate(retained, candidate):
    req = request(retained=retained, candidate=candidate)
    origin, binding = req.origin.record(), req.record()
    assert binding["candidate_id"] == candidate and binding["return_time_s"] == req.schedule.record()[candidate+"_s"]
    assert binding["source_record_sha256"] == req.origin.source_record_sha256
    assert binding["origin_sha256"] == sha256(encode(origin)).hexdigest()
    assert binding["inventory_generation"] == 26-retained and binding["retained_count"] == retained
    assert binding["source_sha256"] == sha256(encode(origin["source_sha256"])).hexdigest()
    assert binding["maximum_simulation_time_s"] == 16000. and binding["controller_policy"] == "trimmed_state_terminal_v4"
    assert binding["old_certificate_admitted"] is binding["registered_return"] is binding["dispatch_authorized"] is False
    before = req.record()
    req.record()["inventory_generation"] = 999
    req.profile()["ship"]["return_reserve_kg"] = 0.
    assert req.record() == before and req.profile()["ship"]["return_reserve_kg"] == 28000.
    with pytest.raises(FrozenInstanceError):
        req.candidate_id = "other"


@pytest.mark.parametrize("mutation", ["raw_origin", "raw_schedule", "other_origin", "source", "profile", "candidate", "ticks_bool", "ticks_zero", "ticks_over"])
def test_stale_untyped_changed_source_profile_or_unbounded_request_rejected(mutation):
    origin, profile, source = make_origin()
    schedule, candidate, ticks = RestoredCandidateSchedule(origin), "nominal", 20
    if mutation == "raw_origin":
        origin = origin.record()
    elif mutation == "raw_schedule":
        schedule = schedule.record()
    elif mutation == "other_origin":
        other, _, _ = make_origin(retained=13)
        schedule = RestoredCandidateSchedule(other)
    elif mutation == "source":
        source["src/runtime/starship_ship_return.py"] = "f"*64
    elif mutation == "profile":
        profile += b" "
    elif mutation == "candidate":
        candidate = "reported_by_model"
    elif mutation == "ticks_bool":
        ticks = True
    elif mutation == "ticks_zero":
        ticks = 0
    else:
        ticks = restored.MAXIMUM_CONTROL_TICKS+1
    with pytest.raises(ValueError):
        restored.RestoredReturnRequest(origin, schedule, candidate, profile, source, maximum_control_ticks=ticks)


def fake_boundary(monkeypatch, *, contact=True, bad_endpoint=None, transition=True):
    calls = {"coast": [], "start": [], "update": [], "command": [], "plant": []}

    def coast(state, craft, target, throttle, count, profile, **kwargs):
        calls["coast"].append((state, craft, target, throttle, count, profile, kwargs))
        return dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad), {"fake_coast": True}

    class Controller:
        def __init__(self, *args, **kwargs):
            calls["init"] = args, kwargs
            self.phase, self.first = "orbital_coast", True

        def start(self, state, craft, profile, samples, event):
            calls["start"].append(state)
            self.phase = "deorbit_slew"
            event("return_requested")

        def update(self, state, craft, profile, samples, event):
            calls["update"].append(state)
            if self.first and transition:
                self.first = False
                self.phase = "deorbit_burn"
                event("deorbit_ignition_command")
                return SimpleNamespace(state=state, transition_only=True)
            self.phase = "landing_burn"
            return SimpleNamespace(state=state, transition_only=False, throttle=.4)

        def command(self, step, craft, profile, altitude):
            calls["command"].append((step.state, craft, profile, altitude))
            return dyn.Command6DOF(tuple(dyn.EngineCommand(True, .4) if i < 3 else dyn.EngineCommand()
                for i in range(len(craft.engines))), step.state.flap_angles_rad), {"fake_return": True}

        def to_dict(self):
            return {"schema": "fixture_controller_history", "phase": self.phase, "actual_history_preserved": True}

    def plant(state, craft, command, dt, length, radius, altitude, air_speed):
        calls["plant"].append((state, craft, command, dt, length, radius, altitude, air_speed))
        actual = tuple(replace(item, throttle=item.throttle+.000001 if item.available else item.throttle)
            for item in state.engine_states)
        endpoint = replace(state, time_s=state.time_s+dt, propellant_kg=state.propellant_kg-.1,
            engine_states=actual)
        if bad_endpoint == "clock":
            endpoint = replace(endpoint, time_s=state.time_s)
        elif bad_endpoint == "fuel":
            endpoint = replace(endpoint, propellant_kg=state.propellant_kg+1.)
        elif bad_endpoint == "availability":
            engines = list(endpoint.engine_states)
            engines[3] = replace(engines[3], available=True)
            endpoint = replace(endpoint, engine_states=tuple(engines))
        elif bad_endpoint == "exception":
            raise ValueError("synthetic_plant_failure")
        if len(calls["plant"]) > 1 and contact:
            normal = env.unit(endpoint.r_eci_m)
            receipt = {"contact": True, "initial_overlap": False, "surface_relative_speed_mps": 4.,
                "surface_normal_speed_mps": -1., "surface_normal_eci": normal,
                "point_eci_m": endpoint.r_eci_m, "event_time_s": endpoint.time_s}
            return endpoint, receipt
        return endpoint, None

    monkeypatch.setattr(restored, "control", coast)
    monkeypatch.setattr(restored, "ReturnController", Controller)
    monkeypatch.setattr(restored, "advance_plant", plant)
    return calls


@pytest.mark.parametrize("retained", [0, 13, 26])
def test_continuation_preserves_exact_actual_state_vehicle_availability_and_finite_history(monkeypatch, retained):
    req = request(retained=retained)
    before = req.origin.record()
    calls = fake_boundary(monkeypatch)
    ticks = []
    result = restored.continue_restored_return(req, on_tick=ticks.append)
    record = result.record()
    assert record["initial_state"] == before["state"] and record["vehicle"] == before["vehicle"]
    assert json.loads(encode(asdict(calls["plant"][0][0]))) == before["state"]
    assert json.loads(encode(asdict(calls["plant"][0][1]))) == before["vehicle"]
    assert calls["init"] == (("trimmed_state_terminal_v4", 3600., 26-retained),
        {"allocation_enabled": True, "registered": False})
    assert len(calls["start"]) == 1 and calls["start"][0].time_s >= 3600.
    assert len(calls["coast"]) == 1 and calls["coast"][0][-1]["control_interval_s"] == req.profile()["integration"]["coast_dt_s"]
    assert len(calls["plant"]) == 2 and len(calls["update"]) == 2 and len(calls["command"]) == 1
    assert calls["plant"][1][0].engine_states[0].throttle == before["state"]["engine_states"][0]["throttle"]+.000001
    assert all(not row[0].engine_states[3].available for row in calls["plant"])
    assert record["final_state"]["engine_states"][3]["available"] is False
    assert record["final_state"]["propellant_kg"] < before["state"]["propellant_kg"]
    assert record["outcome"]["termination"] == "low_speed_surface_contact" and record["outcome"]["integration_steps"] == 2
    assert record["retained_count"] == retained and record["payload_releases_after_origin"] == 0
    assert record["prediction_only"] is False and record["actual_state_initialized_from_prediction"] is False
    assert all(tick.body_id == "ship" and tick.deployment_withheld for tick in ticks)
    assert ticks[0].time_s == before["state"]["time_s"] and len(ticks) == 4
    assert req.origin.record() == before


def test_forecast_and_actual_share_policy_but_manifest_is_prediction_only(monkeypatch):
    req = request(retained=13)
    fake_boundary(monkeypatch)
    prediction = restored.forecast_restored_return(req)
    fake_boundary(monkeypatch)
    actual = restored.continue_restored_return(req)
    a, p = actual.record(), prediction.record()
    assert a["final_state"] == p["final_state"] and a["return_controller"] == p["return_controller"]
    assert p["prediction_only"] is True and a["prediction_only"] is False
    manifest = restored.forecast_candidate_manifest(prediction)
    assert manifest["prediction_record_sha256"] == prediction.sha256
    assert manifest["candidate_id"] == "nominal" and manifest["inventory_generation"] == 13
    assert manifest["origin_sha256"] == req.record()["origin_sha256"]
    assert manifest["dispatch_authorized"] is manifest["old_certificate_admitted"] is False
    with pytest.raises(ValueError, match="cannot_borrow_actual"):
        restored.forecast_candidate_manifest(actual)
    for borrowed in (prediction, p, p["final_state"], manifest):
        with pytest.raises(ValueError, match="typed_request_not_forecast"):
            restored.continue_restored_return(borrowed)
    p["final_state"]["propellant_kg"] = 0.
    assert prediction.record()["final_state"]["propellant_kg"] > 0.


def test_next_orbit_coast_does_not_retime_candidate_or_start_controller_early(monkeypatch):
    req = request(candidate="next_orbit", maximum_control_ticks=2)
    calls = fake_boundary(monkeypatch, contact=False)
    record = restored.continue_restored_return(req).record()
    assert not calls["start"] and "init" not in calls
    assert record["binding"]["return_time_s"] == req.schedule.record()["next_orbit_s"]
    assert record["outcome"]["termination"] == "control_tick_limit"
    assert record["outcome"]["integration_steps"] == record["outcome"]["control_ticks"] == 2
    assert record["outcome"]["contact_metrics"]["contact_observed"] is False
    assert record["return_start_state"] is None


def test_forecast_preserves_exact_due_tick_before_controller_start(monkeypatch):
    req = request()
    calls = fake_boundary(monkeypatch)
    row = restored.forecast_restored_return(req).record()
    from dataclasses import asdict
    assert row["return_start_state"] == json.loads(encode(asdict(calls["start"][0])))
    assert row["return_start_state"]["time_s"] >= req.record()["return_time_s"]
    assert row["return_start_state"] != row["final_state"]
    assert row["actual_state_initialized_from_prediction"] is False


def test_governed_refusal_retains_due_tick_without_starting_or_stepping(monkeypatch):
    req = request()
    calls = fake_boundary(monkeypatch, contact=False)
    actor = restored.RestoredReturnExecution(req)
    actor.advance_until(req.record()["return_time_s"])
    before = actor.snapshot()
    count = len(calls["plant"])
    assert before["return_controller"] is None and before["state"]["time_s"] >= req.record()["return_time_s"]
    result = actor.stop_before_dispatch("dispatch_not_admitted")
    assert result is actor.result
    assert actor.snapshot()["state"] == before["state"]
    assert actor.snapshot()["completed"] is True
    assert len(calls["plant"]) == count and calls["start"] == []
    assert result.record()["return_start_state"] is None
    assert result.record()["return_controller"] is None
    assert result.record()["outcome"]["termination"] == "return_inhibited_by_governance"
    assert result.record()["outcome"]["contact_metrics"]["contact_observed"] is False
    with pytest.raises(ValueError, match="unstarted_actual_coast"):
        actor.stop_before_dispatch("dispatch_not_admitted")


def test_governed_refusal_after_return_activation_cannot_roll_back(monkeypatch):
    req = request()
    calls = fake_boundary(monkeypatch, contact=False)
    actor = restored.RestoredReturnExecution(req)
    actor.advance_until(req.record()["return_time_s"])
    actor.step_once()
    before = actor.snapshot()
    count = len(calls["plant"])
    assert before["return_controller"] is not None
    with pytest.raises(ValueError, match="unstarted_actual_coast"):
        actor.stop_before_dispatch("dispatch_not_admitted")
    assert actor.snapshot() == before and len(calls["plant"]) == count


@pytest.mark.parametrize("failure", ["clock", "fuel", "availability", "exception"])
def test_invalid_finite_step_preserves_last_accepted_actual_state_and_negative_result(monkeypatch, failure):
    req = request()
    calls = fake_boundary(monkeypatch, bad_endpoint=failure)
    record = restored.continue_restored_return(req).record()
    assert len(calls["plant"]) == 1 and record["outcome"]["termination"] == "numerical_failure"
    assert record["final_state"] == req.origin.record()["state"]
    assert record["outcome"]["integration_steps"] == 0
    assert record["outcome"]["contact_receipt"] is None


def test_control_tick_budget_is_saved_negative_not_exception_or_retry(monkeypatch):
    req = request(maximum_control_ticks=1, near_return=False)
    calls = fake_boundary(monkeypatch, contact=False)
    record = restored.forecast_restored_return(req).record()
    assert len(calls["plant"]) == 1 and not calls["start"]
    assert record["outcome"]["termination"] == "control_tick_limit"
    assert record["events"][-1]["event"] == "control_tick_limit"
    assert record["final_state"]["time_s"] > record["initial_state"]["time_s"]


def test_absolute_horizon_is_explicit_without_changing_profile():
    origin, profile, source = make_origin()
    nominal = origin.record()["schedule"]["return_request_time_s"]
    period = origin.record()["orbit"]["period_s"]
    end = nominal+period+.05
    schedule = RestoredCandidateSchedule(origin, maximum_simulation_time_s=end)
    req = restored.RestoredReturnRequest(origin, schedule, "nominal", profile, source)
    assert req.record()["maximum_simulation_time_s"] == end
    assert req.profile()["integration"]["max_duration_s"] == json.loads(profile)["integration"]["max_duration_s"]


def test_incremental_actor_initializes_without_plant_then_advances_every_finite_step(monkeypatch):
    origin, profile, source, seed = make_origin_seed(near_return=False)
    req = restored.RestoredReturnRequest(origin, RestoredCandidateSchedule(origin), "nominal", profile, source,
        maximum_control_ticks=20)
    calls = fake_boundary(monkeypatch, contact=False)
    actor = restored.RestoredReturnExecution(req, origin_record_bytes=seed)
    assert calls["plant"] == [] and actor.snapshot()["state"] == origin.record()["state"]
    first = actor.step_once()
    assert len(calls["plant"]) == 1 and first["state"]["time_s"] == origin.time_s+.25
    assert first["state"]["engine_states"][0]["throttle"] > origin.record()["state"]["engine_states"][0]["throttle"]
    advanced = actor.advance_until(origin.time_s+.7)
    assert len(calls["plant"]) == 3 and advanced["snapshot"]["state"]["time_s"] == origin.time_s+.75
    assert advanced["steps_skipped"] == 0 and advanced["state_projected"] is False
    current = actor.current_origin()
    assert current.record()["state"] == actor.snapshot()["state"]
    assert current.record()["inventory_receipt_chain_sha256"] == origin.record()["inventory_receipt_chain_sha256"]
    assert current.source_record_sha256 != origin.source_record_sha256
    assert actor.result is None


def test_hard_booking_guard_stops_before_overshoot_without_shortening_command(monkeypatch):
    req = request(near_return=False)
    calls = fake_boundary(monkeypatch, contact=False)
    actor = restored.RestoredReturnExecution(req)
    start = actor.snapshot()["state"]["time_s"]
    result = actor.advance_until(start+.3, not_after_s=start+.3)
    assert len(calls["plant"]) == 1 and calls["plant"][0][3] == .25
    assert result["snapshot"]["state"]["time_s"] == start+.25
    assert result["hard_guard_stopped_before_next_step"] is True
    with pytest.raises(ValueError, match="seed_or_coast_missing"):
        actor.current_origin()


def test_reservation_replacement_requires_current_actual_origin_and_never_installs_state(monkeypatch):
    origin, profile, source, seed = make_origin_seed(near_return=False)
    req = restored.RestoredReturnRequest(origin, RestoredCandidateSchedule(origin), "nominal", profile, source,
        maximum_control_ticks=20)
    calls = fake_boundary(monkeypatch, contact=False)
    actor = restored.RestoredReturnExecution(req, origin_record_bytes=seed)
    actor.step_once()
    before = actor.snapshot()
    with pytest.raises(ValueError, match="stale_actual"):
        actor.replace_reservation(req)
    fresh = actor.current_origin()
    unanchored = restored.RestoredReturnRequest(fresh, RestoredCandidateSchedule(fresh), "next_orbit", profile, source,
        maximum_control_ticks=20)
    with pytest.raises(ValueError, match="stale_actual"):
        actor.replace_reservation(unanchored)
    new = restored.RestoredReturnRequest(fresh, RestoredCandidateSchedule(fresh, anchor=req.schedule), "next_orbit", profile, source,
        maximum_control_ticks=20)
    actor.replace_reservation(new)
    assert actor.snapshot()["state"] == before["state"] and actor.snapshot()["vehicle"] == before["vehicle"]
    assert actor.snapshot()["binding"]["candidate_id"] == "next_orbit"
    assert actor.snapshot()["binding"]["opportunity_anchor"] == req.schedule.opportunity_anchor_record()
    actor.step_once()
    assert len(calls["plant"]) == 2 and not calls["start"]
    actor.advance_until(origin.time_s+100.)
    assert actor.result is not None
    result = actor.result.record()
    assert result["outcome"]["termination"] == "control_tick_limit" and len(result["reservation_changes"]) == 1
    assert result["reservation_changes"][0]["actual_state_assigned"] is False
    assert result["initial_binding"]["candidate_id"] == "nominal" and result["binding"]["candidate_id"] == "next_orbit"


def test_fresh_coast_period_drift_cannot_retime_execution_opportunity(monkeypatch):
    origin, profile, source, seed = make_origin_seed(near_return=False)
    initial_schedule = RestoredCandidateSchedule(origin)
    req = restored.RestoredReturnRequest(origin, initial_schedule, "nominal", profile, source,
        maximum_control_ticks=20)
    calls = fake_boundary(monkeypatch, contact=False)
    fake_plant = restored.advance_plant

    def changed_actual_velocity(*args):
        state, receipt = fake_plant(*args)
        return replace(state, v_eci_mps=(state.v_eci_mps[0], state.v_eci_mps[1]+.1, state.v_eci_mps[2])), receipt

    monkeypatch.setattr(restored, "advance_plant", changed_actual_velocity)
    actor = restored.RestoredReturnExecution(req, origin_record_bytes=seed)
    actor.step_once()
    current = actor.current_origin()
    fresh_schedule = RestoredCandidateSchedule(current)
    assert fresh_schedule.record()["next_orbit_s"] != initial_schedule.record()["next_orbit_s"]
    stale_clock = restored.RestoredReturnRequest(current, fresh_schedule, "next_orbit", profile, source,
        maximum_control_ticks=20)
    before = actor.snapshot()
    with pytest.raises(ValueError, match="stale_actual"):
        actor.replace_reservation(stale_clock)
    anchored_schedule = RestoredCandidateSchedule(current, anchor=initial_schedule)
    correct = restored.RestoredReturnRequest(current, anchored_schedule, "next_orbit", profile, source,
        maximum_control_ticks=20)
    actor.replace_reservation(correct)
    assert actor.snapshot()["state"] == before["state"]
    assert correct.record()["return_time_s"] == initial_schedule.record()["next_orbit_s"]
    assert len(calls["plant"]) == 1
    prediction = restored.RestoredReturnResult(encode({
        "schema": "missionos.starship_restored_return_prediction.v1", "prediction_only": True,
        "binding": correct.record(), "old_certificate_admitted": False, "qualification_refreshed": False,
        "mission_authority_granted": False, "dispatch_authorized": False, "physical_execution": False}))
    manifest = restored.forecast_candidate_manifest(prediction)
    assert manifest["opportunity_anchor"] == initial_schedule.opportunity_anchor_record()


def test_actual_actor_rejects_wrong_seed_and_late_reservation(monkeypatch):
    req = request()
    fake_boundary(monkeypatch)
    with pytest.raises(ValueError, match="seed_sha256"):
        restored.RestoredReturnExecution(req, origin_record_bytes=b"predicted state")
    actor = restored.RestoredReturnExecution(req)
    actor.step_once()  # Coast crosses nominal time; next pull would start return.
    with pytest.raises(ValueError, match="stale_actual"):
        actor.replace_reservation(req)
    actor.step_once()
    with pytest.raises(ValueError, match="stale_actual"):
        actor.replace_reservation(req)


def test_five_contact_gates_are_separate_and_point_epoch_is_preserved():
    req = request()
    state = dyn.state_from_dict(req.origin.record()["state"])
    state = replace(state, q_body_to_eci=(math.sqrt(.5), 0., math.sqrt(.5), 0.), omega_body_rad_s=(0., 0., 0.))
    contact = {"surface_normal_eci": (1., 0., 0.), "surface_relative_speed_mps": 4.,
        "surface_normal_speed_mps": -1., "initial_overlap": False,
        "point_eci_m": (6378137., 0., 0.), "event_time_s": state.time_s}
    metrics = restored._contact_metrics(state, contact, req.profile())
    assert len(metrics["gates"]) == 5 and metrics["contact_envelope_met"]
    assert metrics["event_time_s"] == state.time_s
    assert metrics["point_ecef_m"] == list(env.eci_to_ecef(contact["point_eci_m"], state.time_s))
    contact["surface_relative_speed_mps"] = 6.
    assert not restored._contact_metrics(state, contact, req.profile())["gates"]["contact_speed"]
    assert not restored._contact_metrics(state, None, req.profile())["contact_observed"]


def test_initial_overlap_is_a_preserved_contact_negative_not_a_clock_reset():
    req = request()
    state = dyn.state_from_dict(req.origin.record()["state"])
    craft = dyn.vehicle_from_dict(req.origin.record()["vehicle"])
    receipt = {"contact": True, "initial_overlap": True, "surface_relative_speed_mps": 4.,
        "surface_normal_speed_mps": -1., "surface_normal_eci": (1., 0., 0.),
        "point_eci_m": state.r_eci_m, "event_time_s": state.time_s}
    restored._advanced(state, state, craft, .1, receipt)
    assert restored._contact_metrics(state, receipt, req.profile())["gates"]["nonoverlapping_descending_contact"] is False
    receipt["contact"] = False
    with pytest.raises(ValueError, match="changed_clock"):
        restored._advanced(state, state, craft, .1, receipt)


def test_result_is_immutable_finite_json_and_grants_nothing(monkeypatch):
    fake_boundary(monkeypatch)
    result = restored.forecast_restored_return(request())
    assert result.sha256 == sha256(result._record_json).hexdigest()
    with pytest.raises(FrozenInstanceError):
        result._record_json = b"{}"
    row = result.record()
    row["dispatch_authorized"] = True
    with pytest.raises(ValueError, match="scope_invalid"):
        restored.RestoredReturnResult(encode(row))
    with pytest.raises(ValueError):
        restored.RestoredReturnResult(b"{}")


def test_reacquisition_reads_actual_generator_objects_without_advancing_or_restamping_a_prior_receipt():
    execution = restored.RestoredReturnExecution(request(near_return=False))
    try:
        initial = execution.snapshot()
        first, receipt = execution.reacquire_actual_snapshot(clock=lambda: 100.)
        second, later = execution.reacquire_actual_snapshot(clock=lambda: 107.)
        assert first == second == initial == execution.snapshot()
        assert receipt["read_sequence"] == 1 and later["read_sequence"] == 2
        assert receipt["wall_time_s"] == 100. and later["wall_time_s"] == 107.
        assert receipt["simulation_time_s"] == later["simulation_time_s"] == initial["state"]["time_s"]
        assert receipt["state_sha256"] == sha256(encode(initial["state"])).hexdigest()
        assert receipt["vehicle_sha256"] == sha256(encode(initial["vehicle"])).hexdigest()
        assert receipt["binding_sha256"] == sha256(encode(initial["binding"])).hexdigest()
        assert receipt["provider"] == "current_yielded_plant_state"
        assert receipt["plant_advanced"] is receipt["actual_state_assigned"] is receipt["forecast_state_used"] is False
        first["state"]["propellant_kg"] = 1.
        assert execution.snapshot() == initial
    finally:
        execution._iterator.close()


@pytest.mark.parametrize("change", ["state", "availability", "vehicle", "phase", "binding"])
def test_reacquisition_does_not_refresh_a_forged_published_json_snapshot(change):
    execution = restored.RestoredReturnExecution(request(near_return=False))
    try:
        original = execution.snapshot()
        forged = execution.snapshot()
        if change == "state":
            forged["state"]["propellant_kg"] += 1.
        elif change == "availability":
            forged["state"]["engine_states"][3]["available"] = True
        elif change == "vehicle":
            forged["vehicle"]["dry_mass_kg"] += 1.
        elif change == "phase":
            forged["phase"] = "landing_burn"
        else:
            forged["binding"]["return_time_s"] += 1.
        execution._context["snapshot"] = forged
        with pytest.raises(ValueError, match="published_state_changed"):
            execution.reacquire_actual_snapshot(clock=lambda: 100.)
        assert execution.snapshot() == forged and execution._observation_reads == 0
        execution._context["snapshot"] = original
        actual, _receipt = execution.reacquire_actual_snapshot(clock=lambda: 101.)
        assert actual == original
    finally:
        execution._iterator.close()


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), "100"])
def test_reacquisition_requires_finite_actual_wall_clock(value):
    execution = restored.RestoredReturnExecution(request(near_return=False))
    try:
        with pytest.raises(ValueError, match="invalid_clock"):
            execution.reacquire_actual_snapshot(clock=lambda: value)
    finally:
        execution._iterator.close()


def test_reacquisition_follows_actual_new_endpoint_not_the_initial_state(monkeypatch):
    calls = fake_boundary(monkeypatch, contact=False)
    execution = restored.RestoredReturnExecution(request(near_return=False))
    try:
        initial = execution.snapshot()
        endpoint = execution.step_once()
        before_count = len(calls["plant"])
        actual, receipt = execution.reacquire_actual_snapshot(clock=lambda: 100.)
        assert actual == endpoint and actual["state"]["time_s"] > initial["state"]["time_s"]
        assert actual["state"]["propellant_kg"] < initial["state"]["propellant_kg"]
        assert actual["state"]["engine_states"] != initial["state"]["engine_states"]
        assert len(calls["plant"]) == before_count == 1
        assert receipt["state_sha256"] == sha256(encode(endpoint["state"])).hexdigest()
    finally:
        execution._iterator.close()


def test_reacquisition_unavailable_after_governance_stop():
    execution = restored.RestoredReturnExecution(request(near_return=False))
    execution.stop_before_dispatch("candidate_inadmissible")
    with pytest.raises(ValueError, match="not_available"):
        execution.reacquire_actual_snapshot(clock=lambda: 100.)


def test_actual_yielded_state_read_crosses_real_approval_pipe_without_a_plant_step():
    """Affected worker boundary smoke: actual generator read -> inherited actor FD."""
    from src.runtime.starship_inflight_runtime import approval_pipe
    from src.runtime.starship_restored_approval import RestoredApprovalBinding, RestoredApprovalWorker
    req = request(near_return=False)
    execution = restored.RestoredReturnExecution(req)
    request_pipe, grant_pipe = approval_pipe(), approval_pipe()
    try:
        context = {"run_id": "reacquire-smoke", "mission_id": "synthetic", "vehicle_id": "ship"}
        binding = RestoredApprovalBinding(req.origin, context=context, plan_sha256="a"*64,
            expected_source_sha256=req.origin.record()["source_sha256"], started_wall_s=100.)
        actor = RestoredApprovalWorker(binding, initial_reservation_sha256=restored._digest(req.record()),
            request_writer=request_pipe[1], grant_reader=grant_pipe[0],
            admission=lambda *_a: {"accepted": False}, command=lambda *_a: None, clock=lambda: 107.)
        initial = execution.snapshot()
        old = {"run_active": True, "simulation_time_s": initial["state"]["time_s"],
            "restored_origin_sha256": binding.record()["restored_origin_sha256"], "revision": 0,
            "scope": binding.record()["initial_scope"], "reservation_sha256": restored._digest(initial["binding"]),
            "observation": {"state_sha256": restored._digest(initial["state"]),
                "simulation_time_s": initial["state"]["time_s"], "wall_time_s": 100.}}
        with pytest.raises(ValueError, match="runtime_observation_not_fresh"):
            actor.request_change(request_id="old", candidate_sha256="b"*64, state=old)
        assert request_pipe[0].poll() is None and actor.status == "idle"
        actual, receipt = execution.reacquire_actual_snapshot(clock=lambda: 107.)
        fresh = {**old, "observation": {"state_sha256": receipt["state_sha256"],
            "simulation_time_s": receipt["simulation_time_s"], "wall_time_s": receipt["wall_time_s"]}}
        actor.request_change(request_id="fresh", candidate_sha256="b"*64, state=fresh)
        frame = request_pipe[0].poll()
        assert frame["request_id"] == "fresh" and frame["observation"] == fresh["observation"]
        assert frame["candidate_sha256"] == "b"*64 and actor.status == "awaiting_operator"
        assert execution.snapshot() == actual == initial and execution.result is None
        assert receipt["plant_advanced"] is False and actor.record()["burn_dispatch_issued"] is False
    finally:
        execution._iterator.close()
        for pair in (request_pipe, grant_pipe):
            for endpoint in pair:
                endpoint.close()
