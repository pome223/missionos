"""Scripted incremental-actor wiring only: no actual dynamics or model calls."""
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path

import pytest

from src.runtime import starship_final_sensor_execution as execution
from src.runtime import starship_final_sensor_prediction as prediction
from src.runtime import starship_ship_capability_observation as obs
from src.runtime import starship_ship_sensor_observation as sensors
from src.runtime import starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_final_faults import FinalFaultHarness, selection
from src.runtime.starship_final_handoff import FinalActualReturnHandoff, _OWNER_SEAL
from src.runtime.starship_return_feasibility import backend
from src.runtime.starship_ship_return import GuidanceStep

ROOT = Path(__file__).resolve().parents[2]


def ready(value):
    future = Future()
    future.set_result(value)
    return future


def estimate(when, factor=.8):
    value = {"schema": "fixture_numeric_estimate", "observed_at_s": when, "factor_estimate": factor,
        "band": [factor-.01, factor+.01], "accepted_samples": 100, "anomaly": factor != 1.}
    return {**value, "estimate_id": obs.digest(value)}


def judgment(request, action):
    value = {"schema": execution.JUDGMENT_SCHEMA, "request_sha256": obs.digest(request),
        "action": action, "mode": "fixture", "provenance": {"model_inference_invoked": False}}
    return {**value, "decision_id": obs.digest(value)}


def install_scripted_world(monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("actor contract attempted actual propagation")
    monkeypatch.setattr(dyn, "step", prohibited)
    monkeypatch.setattr(obs, "propagate", prohibited)
    monkeypatch.setattr(execution, "control_interval", lambda *args: 1.)
    monkeypatch.setattr(dyn, "observe", lambda *args, **kwargs: {"altitude_m": 200000., "air_speed_mps": 7500.})
    monkeypatch.setattr(obs, "estimate", lambda prepared, when: estimate(when))
    monkeypatch.setattr(sensors, "estimate_tracking", lambda delivery, nominal, when: estimate(when))
    monkeypatch.setattr(obs, "specific_force_row", lambda state, plant, phase: {
        "time_s": state.time_s, "phase": phase, "true_specific_force_body_mps2": [0., 0., 5.],
        "engine_throttle_states": [e.throttle for e in state.engine_states],
        "engine_available": [e.available for e in state.engine_states], "propellant_kg": state.propellant_kg})

    def update(self, state, samples, event):
        self.audit.inspect("update", state, self.model)
        profile = deepcopy(self.profile)
        profile["ship"]["engine_thrust_n"] = self.model.engines[0].max_thrust_n
        self.audit.inspect("capability_profile", state, self.model, profile)
        self.controller.record["status"] = "active"
        if state.time_s < 5002.:
            self.controller.phase = "deorbit_burn"
        elif self.phase != "ballistic_return":
            self.controller.phase = "ballistic_return"
            event("deorbit_cutoff_command")
        return GuidanceStep(state, dyn.IDENTITY, 1. if self.phase == "deorbit_burn" else 0., 3)

    def command(self, step, altitude):
        profile = deepcopy(self.profile)
        profile["ship"]["engine_thrust_n"] = self.model.engines[0].max_thrust_n
        for boundary in ("command", "capability_profile", "tvc_control"):
            self.audit.inspect(boundary, step.state, self.model,
                profile if boundary != "command" else None)
        return dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in self.model.engines), step.state.flap_angles_rad), {}

    monkeypatch.setattr(obs.SwitchingController, "update", update)
    monkeypatch.setattr(obs.SwitchingController, "command", command)
    monkeypatch.setattr(execution, "record_sample", lambda state, plant, phase, command, diagnostics,
        samples, next_sample, profile: samples.append({**asdict(state), "phase": phase}) or state.time_s)
    monkeypatch.setattr(mission, "_sample", lambda state, craft, phase: {**asdict(state), "phase": phase})
    advances = []
    def advance(state, plant, command, dt, *args):
        advances.append((state, plant, dt))
        later = replace(state, time_s=state.time_s+dt, propellant_kg=state.propellant_kg-.1)
        contact = {"contact": True, "surface_relative_speed_mps": 3.5} if later.time_s >= 5610. else None
        return later, contact
    monkeypatch.setattr(execution, "advance_plant", advance)
    monkeypatch.setattr(prediction, "forecast_preview", lambda rows, snapshot: {"status": "fixture_only"})
    monkeypatch.setattr(prediction, "forecast_evidence", lambda rows, snapshot: {
        f"{action}.{label}.{key}": value for action in prediction.ACTIONS for label in prediction.POINT_LABELS
        for key, value in {"sampled_factor": .8, "speed_mps": 3.5, "tilt_deg": .3,
            "body_rate_rad_s": .001, "reserve_kg": 58000., "limits_met": True}.items()})
    return advances


@pytest.fixture
def scripted(monkeypatch):
    return install_scripted_world(monkeypatch)


def actor_fixture(*, decide=None, forecast=None, guard=None, policy=None, retained=13,
        family="return_propulsion_80", integration_policy=None):
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    craft = mission.vehicle(profile, payload_count=retained)
    state = dyn.State6DOF(5000., (6600000., 0., 0.), (0., 7400., 0.), dyn.IDENTITY,
        (.001, -.001, .002), 100000., tuple(dyn.EngineState(throttle=.5 if i < 3 else 0., available=i != 3)
            for i in range(len(craft.engines))), tuple(0. for _ in craft.aero_panels))
    inventory = {"schema": prediction.INVENTORY_SCHEMA, "total_count": 26,
        "released_count": 26-retained, "retained_count": retained, "generation": 26-retained,
        "sequencer_state": "skipped" if retained else "running", "pending_action": None,
        "observed_time_s": 4900., "release_receipts": [{"time_s": 4800.+i} for i in range(26-retained)]}
    handoff_record = {"snapshot": {"state": asdict(state), "vehicle": asdict(craft),
        "binding": {"return_time_s": 5000.}}, "inventory": inventory, "fixture_only": True}
    handoff = FinalActualReturnHandoff(_seal=_OWNER_SEAL, state=state, vehicle=craft, record=handoff_record)
    selected = next(value for value in (selection(seed) for seed in range(100)) if value["selected_family"] == family)
    harness = FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    now, submissions = [1000.], []
    def decide_default(request, **kwargs):
        submissions.append(("decision", deepcopy(request), kwargs))
        return ready(judgment(request, execution.TRACK if request["stage"] == "initial" else execution.ADOPT))
    def forecast_default(payload, **kwargs):
        submissions.append(("forecast", deepcopy(payload), kwargs))
        return ready([{"fixture_only": True} for _ in range(6)])
    def guard_default(payload, **kwargs):
        submissions.append(("guard", deepcopy(payload), kwargs))
        bundle, authority = payload["bundle"], payload["authority"]
        receipt = {"schema": execution.GUARD_SCHEMA_V2 if "integration_policy" in bundle else execution.GUARD_SCHEMA,
            "bundle_sha256": obs.digest(bundle),
            "authority_sha256": obs.digest(authority), "snapshot_id": authority["snapshot_id"],
            "selected_action": bundle["selected_action"], "application_time_s": authority["application_time_s"],
            "factor_estimate": bundle["request"]["facts"]["accel_factor"] if bundle["selected_action"] == execution.ADOPT else 1.,
            "checks": {"fixture_only": True}, "passed": True}
        receipt["receipt_id"] = obs.digest(receipt)
        return ready({"receipt": receipt, "process_evidence": {"checker_pid": os.getpid()+100000,
            "exit_code": 0, "credential_free": True, "fixture_only": True}})
    actor = execution.FinalSensorExecution(handoff, harness, profile, run_id="final-actor-fixture",
        source_sha256=prediction.source_inventory(["src/runtime/starship_final_sensor_execution.py",
            "src/runtime/starship_final_sensor_guard.py"]),
        backend=backend(), policy=policy or execution.FinalSensorExecutionPolicy(),
        decide=decide or decide_default, forecast=forecast or forecast_default, guard=guard or guard_default,
        clock=lambda: now[0], integration_policy=integration_policy)
    return actor, now, submissions, state, craft


def advance_to(actor, now, predicate, limit=1000):
    for _ in range(limit):
        now[0] += 1.
        result = actor.step_once()
        if predicate(actor):
            return result
        assert actor.termination is None, actor.error
    pytest.fail("scripted actor condition not reached")


@pytest.mark.parametrize("retained", [0, 13, 26])
def test_exact_handoff_and_shared_controller_without_old_controller_start(scripted, retained):
    actor, now, _, state, craft = actor_fixture(retained=retained)
    assert actor.state is state
    assert actor.state.engine_states[3].available is False
    assert actor.plant.dry_mass_kg == craft.dry_mass_kg
    assert actor.controller.controller.phase == "orbital_coast"
    assert actor.initial_controller["released_count"] == 26-retained
    assert actor.initial_state == asdict(state)
    assert actor.steps == 0 and scripted == []
    assert actor.step_once()["advanced"] is True
    assert scripted[0][0] is state and scripted[0][1] is actor.plant


def test_tracking_arrival_current_forecasts_guard_and_one_application_all_preserve_actual_chain(scripted):
    actor, now, calls, _, _ = actor_fixture()
    advance_to(actor, now, lambda a: a.stage == "tracking_wait")
    assert not actor.batches
    requested = actor.delivery["requested_at_s"]
    advance_to(actor, now, lambda a: a.batches)
    assert actor.state.time_s >= requested+30.
    batch = actor.batches[0]
    assert len(batch["jobs"]) == 6
    assert batch["snapshot"]["state"] == prediction.json_copy(asdict(scripted[-1][0]))
    assert batch["snapshot"]["controller"] == actor.controller.controller.to_dict()
    assert batch["snapshot"]["retained_count"] == 13
    advance_to(actor, now, lambda a: a.application is not None)
    assert actor.application["time_s"] == actor.complete_time+600.
    assert actor.application["consumed_receipt_id"] == actor.guards[0]["response"]["receipt"]["receipt_id"]
    assert actor.application["state_sha256"] == obs.digest(actor.application_witness["state"])
    assert actor.application["controller_sha256"] == obs.digest(actor.application_witness["controller"])
    assert actor.application_witness["inventory_sha256"] == obs.digest(actor.inventory)
    assert actor.controller.switch["factor"] == .8
    assert len(actor.controller.audit.switches) == 1
    assert actor.fallbacks == []
    assert actor.state.engine_states[3].available is False
    assert [kind for kind, *_ in calls] == ["decision", "forecast", "decision", "guard"]
    actor._apply()
    assert len(actor.controller.audit.switches) == 1
    advance_to(actor, now, lambda a: a.termination is not None)
    result = actor.result()
    assert result["rollout"]["initial_state"] == result["handoff_record"]["snapshot"]["state"]
    assert result["claim_limits"]["model_calls_by_worker"] == 0
    assert result["claim_limits"]["physical_execution"] is False
    assert result["application"]["mode"] == "fixture"
    assert result["sensor_source_rows"][0]["state"] == result["rollout"]["initial_state"]
    assert result["detection"]["anomaly"] is True
    actual_states = [row["state"] for row in result["actual_execution_trace"]]
    assert all(row["state"] in actual_states for row in result["sensor_source_rows"])
    assert result["application_witness"]["state"] in actual_states
    assert result["rollout"]["final_state"] == actual_states[-1]
    assert all(row["vehicle_sha256"] == obs.digest(result["actual_vehicle"])
        for row in result["actual_execution_trace"])
    burn_witnesses = [{"state": row["state"], "phase": row["phase"]}
        for row in result["actual_execution_trace"]
        if row["boundary"] == "before_finite_actual_plant_step" and row["phase"] == "deorbit_burn"]
    assert burn_witnesses == result["sensor_source_rows"]
    saved_batch = result["forecast_batches"][0]
    assert "response" not in saved_batch
    assert saved_batch["response_sha256"] == obs.digest(saved_batch["forecasts"])
    saved_bundle = result["guard_sessions"][0]["bundle"]
    assert "forecasts" not in saved_bundle
    assert saved_bundle["forecasts_sha256"] == obs.digest(saved_batch["forecasts"])
    assert saved_bundle["forecast_batch_snapshot_id"] == saved_batch["snapshot"]["snapshot_id"]
    rebuilt = {key: value for key, value in saved_bundle.items()
        if key not in ("forecasts_sha256", "forecast_batch_snapshot_id")}
    rebuilt["forecasts"] = saved_batch["forecasts"]
    assert obs.digest(rebuilt) == result["guard_sessions"][0]["response"]["receipt"]["bundle_sha256"]
    # Producing saved evidence must not mutate the process checker's preimage.
    assert "forecasts" in actor.guards[0]["bundle"] and "response" in actor.batches[0]


def test_pending_future_paces_real_steps_without_advancing_time_by_assignment(scripted):
    pending = Future()
    actor, now, _, _, _ = actor_fixture(decide=lambda *a, **k: pending)
    advance_to(actor, now, lambda a: a.complete_time is not None)
    before, count = actor.snapshot(), len(scripted)
    assert actor.step_once() == {"advanced": False, "waiting": True, "complete": False}
    assert actor.snapshot() == before and len(scripted) == count
    now[0] += 36.
    actor.step_once()
    assert actor.fallbacks[-1]["action"] == execution.CONTINUE
    assert actor.controller.switch is None
    assert actor.decisions[0]["status"] == "expired"


def test_valid_continue_declines_tracking_without_rules_replacing_judgment(scripted):
    actor, now, _, _, _ = actor_fixture(decide=lambda request, **kwargs: ready(judgment(request, execution.CONTINUE)))
    advance_to(actor, now, lambda a: a.stage == "resolved")
    assert actor.declined_work is True
    assert actor.delivery is None and actor.batches == [] and actor.guards == []
    assert actor.fallbacks == [] and actor.controller.switch is None


@pytest.mark.parametrize("bad", ["unavailable", "unbound", "wrong_action"])
def test_invalid_or_missing_decisions_have_separate_nominal_fallback(scripted, bad):
    def decide(request, **kwargs):
        if bad == "unavailable":
            return ready({"status": "unavailable", "action": None})
        result = judgment(request, execution.CONTINUE)
        if bad == "unbound":
            result["request_sha256"] = "f"*64
        else:
            result["action"] = execution.ADOPT
        return ready(result)
    actor, now, _, _, _ = actor_fixture(decide=decide)
    advance_to(actor, now, lambda a: a.fallbacks)
    assert actor.fallbacks[0]["model_judgment"] is False
    assert actor.controller.switch is None
    assert actor.decisions[0]["accepted_action"] is None


@pytest.mark.parametrize("field", ["actual_vehicle", "physical_factor", "controller", "seed"])
def test_public_projection_is_strict_and_never_includes_private_capture(scripted, field):
    actor, now, _, _, _ = actor_fixture()
    advance_to(actor, now, lambda a: a.decisions)
    request = deepcopy(actor.decisions[0]["request"])
    request["facts"][field] = .8
    with pytest.raises(ValueError, match="projection"):
        execution.validate_public_request(request)


def test_guard_rejection_is_negative_without_nominal_fallback_replacement(scripted):
    def guard(payload, **kwargs):
        bundle, authority = payload["bundle"], payload["authority"]
        receipt = {"schema": execution.GUARD_SCHEMA_V2 if "integration_policy" in bundle else execution.GUARD_SCHEMA,
            "bundle_sha256": obs.digest(bundle),
            "authority_sha256": obs.digest(authority), "snapshot_id": authority["snapshot_id"],
            "selected_action": bundle["selected_action"], "application_time_s": authority["application_time_s"],
            "factor_estimate": bundle["request"]["facts"]["accel_factor"],
            "checks": {"selected_sampled_points_meet_limits": False}, "passed": False}
        receipt["receipt_id"] = obs.digest(receipt)
        return ready({"receipt": receipt, "process_evidence": {"checker_pid": os.getpid()+100000,
            "exit_code": 0, "credential_free": True, "fixture_only": True}})
    actor, now, _, _, _ = actor_fixture(guard=guard)
    advance_to(actor, now, lambda a: a.stage == "guard_rejected")
    assert actor.fallbacks == [] and actor.controller.switch is None and actor.approved is None
    assert actor.decisions[-1]["accepted_action"] == execution.ADOPT


def test_policy_and_early_result_cannot_authorize_unregistered_execution():
    with pytest.raises(ValueError):
        execution.FinalSensorExecutionPolicy(forecast_s=400.)
    with pytest.raises(ValueError):
        execution.FinalSensorExecutionPolicy(fallback="adopt_estimate")
    actor, *_ = actor_fixture()
    with pytest.raises(ValueError, match="not_finished"):
        actor.result()


def test_six_field_runtime_backend_projects_only_named_numerical_fields():
    full = {"python": "3.11.fixture", "platform": "darwin", "machine": "arm64",
        "numpy": "fixture-numpy", "scipy": "fixture-scipy", "executable_sha256": "a"*64}
    assert execution.numerical_backend(full) == {"numpy": "fixture-numpy", "scipy": "fixture-scipy"}
    for value in (backend(), {k: v for k, v in full.items() if k != "executable_sha256"}, {**full, "extra": "value"}):
        with pytest.raises(ValueError):
            execution.numerical_backend(value)


def test_future_infrastructure_failure_is_recorded_and_uses_registered_fallback(scripted):
    class Broken:
        def done(self):
            raise TimeoutError("fixture child failed")
        def result(self):
            pytest.fail("failed task result was consumed")
        def cancel(self):
            return True
    actor, now, _, _, _ = actor_fixture(decide=lambda *a, **k: Broken())
    advance_to(actor, now, lambda a: a.fallbacks)
    assert actor.decisions[0]["status"] == "infrastructure_error"
    assert actor.decisions[0]["error_type"] == "TimeoutError"
    assert actor.fallbacks[0]["action"] == execution.CONTINUE and actor.controller.switch is None


def test_normal_continue_only_is_nonintervention_regression_not_diagnosis(scripted, monkeypatch):
    monkeypatch.setattr(obs, "estimate", lambda prepared, when: estimate(when, factor=1.))
    actor, now, _, _, _ = actor_fixture(family="normal",
        decide=lambda request, **kwargs: ready(judgment(request, execution.CONTINUE)))
    advance_to(actor, now, lambda a: a.stage == "resolved")
    assert actor.decisions[0]["request"]["allowed_actions"] == [execution.CONTINUE]
    assert actor.nonintervention_regression is True and actor.declined_work is False
    assert actor.detection is None and actor.delivery is None and actor.application is None



def test_explicit_actor_policy_binds_capture_jobs_guard_and_suffix_without_physics(scripted):
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    policy = RestoredIntegrationPolicy(2, fuel_event_mode="outer_bracket_v2")
    actor, now, _calls, _state, _craft = actor_fixture(integration_policy=policy)
    advance_to(actor, now, lambda item: item.application is not None)
    assert actor.complete_snapshot["schema"] == prediction.SNAPSHOT_SCHEMA_V2
    assert actor.complete_snapshot["integration_policy"] == policy.record()
    assert all(job["integration_policy"] == policy.record() for job in actor.batches[0]["jobs"])
    guard = actor.guards[0]
    assert guard["bundle"]["schema"] == "missionos.starship_final_sensor_guard_bundle.v2"
    assert guard["authority"]["schema"] == "missionos.starship_final_sensor_guard_authority.v2"
    assert guard["bundle"]["integration_policy"] == guard["authority"]["integration_policy"] == policy.record()
    assert guard["response"]["receipt"]["schema"] == execution.GUARD_SCHEMA_V2
    advance_to(actor, now, lambda item: item.termination is not None)
    result = actor.result()
    assert result["schema"] == execution.SCHEMA_V2 and result["integration_policy"] == policy.record()
    assert actor.fallbacks == []


def test_actor_none_policy_keeps_strict_v1_wire_shapes(scripted):
    actor, now, _calls, _state, _craft = actor_fixture()
    advance_to(actor, now, lambda item: item.termination is not None)
    result = actor.result()
    assert actor.integration_policy.fuel_event_mode == "strict_v1"
    assert result["schema"] == execution.SCHEMA and "integration_policy" not in result
