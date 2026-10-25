"""Synthetic wiring/negative contracts; propagation, models and network absent."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from src.runtime import starship_final_sensor_prediction as prediction
from src.runtime import starship_ship_capability_observation as obs
from src.runtime import starship_ship_online_prediction as legacy
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_return_feasibility import backend
from src.runtime.starship_sixdof_mission import vehicle

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_physical_propagation(monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("final-sensor contracts must not advance a plant")
    monkeypatch.setattr(dyn, "step", prohibited)
    monkeypatch.setattr(obs, "advance_plant", prohibited)
    monkeypatch.setattr(obs, "propagate", prohibited)


def fixture(retained=13, actual_factor=.8):
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    nominal = vehicle(profile, payload_count=retained)
    actual = obs.model_vehicle(nominal, actual_factor)
    state = dyn.State6DOF(time_s=2430., r_eci_m=(6571000., 0., 0.),
        v_eci_mps=(0., 7500., 0.), q_body_to_eci=(1., 0., 0., 0.),
        omega_body_rad_s=(.0001, -.0002, .0003), propellant_kg=100000.,
        engine_states=tuple(dyn.EngineState(throttle=.02 if i == 0 else 0.,
            gimbal_x_rad=.001 if i == 0 else 0., available=i != 3)
            for i in range(len(actual.engines))),
        flap_angles_rad=tuple(min(.003, spec.max_deflection_rad) for spec in actual.aero_panels))
    model = obs.model_vehicle(nominal, 1.)
    cap = obs.SwitchingController(nominal, model, profile, 2300., 26-retained,
        obs.SwitchAudit(nominal, model, actual))
    history = cap.controller
    history.phase, history.record["status"] = "ballistic_return", "active"
    history.previous_budget = {"state": asdict(state), "budget": {"fuel_budget_estimate_kg": 12345.}}
    history.entry_prepared = {"status": "prepared", "basis": "legacy_geographic",
        "trim_angles_rad": [.01]*len(nominal.aero_panels)}
    history.trim_attempts, history.next_trim_attempt_s = 2, 2420.
    history.return_frame = ConditionedGeographicFrame(state.q_body_to_eci, 2429., maximum_roll_rate_rad_s=.01)
    history.return_frame.bridging = True
    inventory = {"schema": prediction.INVENTORY_SCHEMA, "total_count": 26,
        "released_count": 26-retained, "retained_count": retained, "generation": 26-retained,
        "sequencer_state": "skipped" if retained else "running", "pending_action": None,
        "observed_time_s": 2299.,
        "release_receipts": [{"time_s": 2000.+i, "fixture": "synthetic_release_witness"}
            for i in range(26-retained)]}
    snapshot = prediction.make_snapshot(state, actual, cap, profile, run_id="final-sensor-fixture",
        handoff_sha256="a"*64, inventory=inventory, cutoff_time_s=2398.,
        complete_time_s=2400., application_time_s=3000.)
    return snapshot, state, actual, nominal, cap


def estimate(point=.8):
    value = {"factor_estimate": point, "band": [point-.01, point+.01], "observed_at_s": 2430.}
    value["estimate_id"] = obs.digest(value)
    return value


def job_fixture(retained=13, actual_factor=.8, action=prediction.ADOPT, label="band_low"):
    snapshot, state, actual, nominal, cap = fixture(retained, actual_factor)
    points = {"band_low": .79, "point_estimate": .8, "band_high": .81}
    job = {"snapshot": snapshot, "action": action, "sample_label": label,
        "sampled_factor": points[label], "accelerometer_estimate": estimate(),
        "sampling_estimate": estimate(), "source_sha256": prediction.source_inventory(),
        "backend": backend(), "timeout_s": 30.}
    return job, state, actual, nominal, cap


def reseal(snapshot):
    snapshot["snapshot_id"] = obs.digest({k: v for k, v in snapshot.items() if k != "snapshot_id"})


@pytest.mark.parametrize("retained", prediction.RETAINED_COUNTS)
def test_roundtrip_preserves_actual_mass_actuators_reference_and_complete_history(retained):
    snapshot, state, actual, nominal, cap = fixture(retained)
    restored, reconstructed, history = prediction.validate_snapshot(snapshot)
    assert restored == state and restored.engine_states[3].available is False
    assert reconstructed == nominal and reconstructed is not actual
    assert history.to_dict() == cap.controller.to_dict()
    assert history.return_frame.bridging is True
    assert history.previous_budget["budget"]["fuel_budget_estimate_kg"] == 12345.
    assert history.entry_prepared["trim_angles_rad"][0] == .01
    assert snapshot["inventory_receipt_chain_sha256"] == obs.digest(snapshot["inventory"]["release_receipts"])
    assert snapshot["actuator_sha256"] == obs.digest({key: snapshot["state"][key]
        for key in ("engine_states", "flap_angles_rad")})
    empty = vehicle(snapshot["profile"], payload_count=0)
    assert reconstructed.dry_mass_kg-empty.dry_mass_kg == pytest.approx(
        retained*snapshot["profile"]["payload"]["mass_each_kg"])
    history.entry_prepared["trim_angles_rad"][0] = .3
    assert cap.controller.entry_prepared["trim_angles_rad"][0] == .01


def test_actual_main_thrust_never_controls_nominal_or_sampled_hypothesis():
    a, *_ = fixture(actual_factor=.6)
    b, *_ = fixture(actual_factor=1.1)
    _, nominal_a, _ = prediction.validate_snapshot(a)
    _, nominal_b, _ = prediction.validate_snapshot(b)
    assert nominal_a == nominal_b
    assert a["nominal_vehicle_sha256"] == b["nominal_vehicle_sha256"]
    assert a["actual_vehicle_sha256"] != b["actual_vehicle_sha256"]
    assert obs.model_vehicle(nominal_a, .79) == obs.model_vehicle(nominal_b, .79)


def test_actual_non_main_structure_is_preserved():
    snapshot, state, actual, _, cap = fixture()
    engines = list(actual.engines)
    engines[3] = replace(engines[3], max_thrust_n=engines[3].max_thrust_n*.7)
    actual = replace(actual, tank_center_body_m=(.01, .02, actual.tank_center_body_m[2]),
        engines=tuple(engines))
    nominal = prediction.nominal_from_actual(actual, snapshot["profile"], 13)
    cap.nominal, cap.model = nominal, replace(nominal)
    updated = prediction.make_snapshot(state, actual, cap, snapshot["profile"],
        run_id="actual-preserved", handoff_sha256="b"*64, inventory=snapshot["inventory"],
        cutoff_time_s=2398., complete_time_s=2400., application_time_s=3000.)
    _, rebuilt, _ = prediction.validate_snapshot(updated)
    assert rebuilt.tank_center_body_m == actual.tank_center_body_m
    assert rebuilt.engines[3] == actual.engines[3]


@pytest.mark.parametrize("mutation", ["snapshot_hash", "state_hash", "actuator_hash", "controller_hash",
    "vehicle_hash", "nominal_hash", "inventory_hash", "chain_hash", "profile_hash", "missing_history",
    "retained_count", "release_count", "inventory_count", "inventory_generation", "pending_release",
    "unclosed_inventory", "receipt_clock", "history_release_count", "wrong_phase", "clock",
    "missing_actuator", "mass", "nan", "extra_truth"])
def test_invalid_capture_rejected_even_if_outer_hash_resealed(mutation):
    snapshot, *_ = fixture()
    hash_fields = {"state_hash": "state_sha256", "actuator_hash": "actuator_sha256",
        "controller_hash": "controller_sha256", "vehicle_hash": "actual_vehicle_sha256",
        "nominal_hash": "nominal_vehicle_sha256", "inventory_hash": "inventory_sha256",
        "chain_hash": "inventory_receipt_chain_sha256", "profile_hash": "profile_sha256"}
    if mutation == "snapshot_hash":
        snapshot["snapshot_id"] = "b"*64
    elif mutation in hash_fields:
        snapshot[hash_fields[mutation]] = "b"*64
    elif mutation == "missing_history":
        del snapshot["controller"]["previous_budget"]
    elif mutation == "retained_count":
        snapshot["retained_count"] = 0
    elif mutation == "release_count":
        snapshot["released_count"] = 26
    elif mutation == "inventory_count":
        snapshot["inventory"]["retained_count"] = 0
    elif mutation == "inventory_generation":
        snapshot["inventory"]["generation"] += 1
    elif mutation == "pending_release":
        snapshot["inventory"]["pending_action"] = {"kind": "release"}
    elif mutation == "unclosed_inventory":
        snapshot["inventory"]["sequencer_state"] = "running"
    elif mutation == "receipt_clock":
        snapshot["inventory"]["release_receipts"][-1]["time_s"] = 3000.
    elif mutation == "history_release_count":
        snapshot["controller"]["released_count"] = 26
    elif mutation == "wrong_phase":
        snapshot["controller"]["phase"] = "landing_burn"
    elif mutation == "clock":
        snapshot["application_time_s"] += 1.
    elif mutation == "missing_actuator":
        snapshot["state"]["engine_states"].pop()
    elif mutation == "mass":
        snapshot["actual_vehicle"]["dry_mass_kg"] -= 1700.
    elif mutation == "nan":
        snapshot["controller"]["previous_budget"]["budget"]["fuel_budget_estimate_kg"] = float("nan")
    else:
        snapshot["physical_factor"] = .8
    if mutation not in ("snapshot_hash", "nan"):
        reseal(snapshot)
    with pytest.raises((ValueError, TypeError, KeyError)):
        prediction.validate_snapshot(snapshot)


@pytest.mark.parametrize("mutation", ["switched", "audit_switched", "wrong_model", "actual_as_model", "profile"])
def test_capture_refuses_changed_or_shared_control_model(mutation):
    snapshot, state, actual, nominal, cap = fixture()
    if mutation == "switched":
        cap.apply_switch(state.time_s, .8, {"kind": "fixture"})
    elif mutation == "audit_switched":
        cap.audit.switches.append({"kind": "fixture"})
    elif mutation == "wrong_model":
        cap.model = obs.model_vehicle(nominal, .6)
    elif mutation == "actual_as_model":
        actual = nominal
        cap.model = actual
    else:
        cap.profile = {**cap.profile, "extra": True}
    with pytest.raises(ValueError):
        prediction.make_snapshot(state, actual, cap, snapshot["profile"], run_id="capture-rejection",
            handoff_sha256="a"*64, inventory=snapshot["inventory"], cutoff_time_s=2398.,
            complete_time_s=2400., application_time_s=3000.)


def mock_propagation(monkeypatch, captured):
    def fake(state, plant, cap, profile, *, end_time_s, deadline, before_update, step_boundary_s):
        captured.update(state=state, plant=plant, cap=cap, profile=deepcopy(profile),
            history=cap.controller.to_dict(), deadline=deadline, end=end_time_s)
        assert cap.model is not plant
        assert cap.audit.plant_identity == id(plant)
        before_update(state, cap, [])
        assert cap.switch is None
        later = replace(state, time_s=step_boundary_s)
        before_update(later, cap, [])
        before_update(later, cap, [])  # Once-only switch, including repeated same-time hook.
        return {"state": later, "samples": [], "events": [], "termination": "execution_wall_budget",
            "contact": None, "error": None, "steps": 0}
    monkeypatch.setattr(obs, "propagate", fake)


@pytest.mark.parametrize("retained", prediction.RETAINED_COUNTS)
@pytest.mark.parametrize("action", prediction.ACTIONS)
@pytest.mark.parametrize("actual_factor", [.6, 1.])
def test_forecast_separate_branch_preserves_mass_availability_history_and_application(
        monkeypatch, retained, action, actual_factor):
    job, state, actual, nominal, original_cap = job_fixture(retained, actual_factor, action)
    before = deepcopy(job)
    captured = {}
    mock_propagation(monkeypatch, captured)
    result = prediction.forecast_job(job)
    assert job == before
    assert captured["state"] == state and captured["state"] is not state
    assert captured["state"].engine_states[3].available is False
    assert captured["plant"] is not actual and captured["cap"] is not original_cap
    assert captured["plant"].dry_mass_kg == actual.dry_mass_kg
    assert captured["plant"].dry_inertia_kg_m2 == actual.dry_inertia_kg_m2
    assert captured["plant"].engines[0].max_thrust_n == nominal.engines[0].max_thrust_n*.79
    assert captured["history"] == original_cap.controller.to_dict()
    assert captured["end"] == state.time_s+3600.
    assumed = .8 if action == prediction.ADOPT else 1.
    assert captured["cap"].model.engines[0].max_thrust_n == nominal.engines[0].max_thrust_n*assumed
    assert original_cap.switch is None and original_cap.model == nominal
    assert result["assumed_factor"] == assumed
    assert result["record"]["prediction_is_execution"] is False
    assert result["record"]["prediction_retained_count"] == retained
    assert result["verification"]["record_valid"] is False
    assert result["forecast_id"] == obs.digest({k: v for k, v in result.items() if k != "forecast_id"})


@pytest.mark.parametrize("mutation", ["source", "backend", "omitted_source", "timeout", "nan",
    "arbitrary_sample", "wrong_label", "truth_parameter", "estimate_hash", "stale_estimate", "bad_band"])
def test_forecast_refuses_mismatch_before_any_propagation(mutation):
    job, *_ = job_fixture()
    if mutation == "source":
        job["source_sha256"]["src/runtime/starship_final_sensor_prediction.py"] = "a"*64
    elif mutation == "backend":
        job["backend"] = {"numpy": "not-installed", "scipy": "not-installed"}
    elif mutation == "omitted_source":
        del job["source_sha256"]["src/runtime/starship_ship_return.py"]
    elif mutation == "timeout":
        job["timeout_s"] = 181.
    elif mutation == "nan":
        job["timeout_s"] = float("nan")
    elif mutation == "arbitrary_sample":
        job["sampled_factor"] = .785
    elif mutation == "wrong_label":
        job["sample_label"] = "point_estimate"
    elif mutation == "truth_parameter":
        job["physical_factor"] = .8
    elif mutation == "estimate_hash":
        job["sampling_estimate"]["factor_estimate"] = .7
    elif mutation == "stale_estimate":
        job["sampling_estimate"]["observed_at_s"] -= 1.
        job["sampling_estimate"]["estimate_id"] = obs.digest({k: v
            for k, v in job["sampling_estimate"].items() if k != "estimate_id"})
    else:
        job["sampling_estimate"]["band"] = [.85, .9]
        job["sampling_estimate"]["estimate_id"] = obs.digest({k: v
            for k, v in job["sampling_estimate"].items() if k != "estimate_id"})
    with pytest.raises(ValueError):
        prediction.forecast_job(job)


def test_source_inventory_is_new_root_and_detects_changed_or_escaped_sources(monkeypatch):
    source = prediction.source_inventory()
    assert prediction.ROOT == ROOT
    assert all(prediction.current_source_checks(source, backend()).values())
    assert not all(prediction.current_source_checks({**source, "../outside": "a"*64}, backend()).values())
    monkeypatch.setattr(obs, "__file__", "/unrelated/old-checkout/observation.py")
    assert prediction.current_source_checks(source, backend())["loaded_modules_from_current_root"] is False


@pytest.mark.parametrize("mutation", ["snapshot", "plant_factor", "switch", "stronger_summary",
    "state", "retained_count", "source", "nominal_limits"])
def test_saved_forecast_mismatch_is_refused_even_after_rehash(monkeypatch, mutation):
    job, *_ = job_fixture()
    mock_propagation(monkeypatch, {})
    result = prediction.forecast_job(job)
    if mutation == "snapshot":
        result["snapshot_id"] = "b"*64
    elif mutation == "plant_factor":
        result["record"]["plant_factor"] = .8
    elif mutation == "switch":
        result["record"]["switch"]["factor"] = .6
    elif mutation == "stronger_summary":
        result["stronger_record_checks"]["passed"] = True
    elif mutation == "state":
        result["record"]["initial_state"]["propellant_kg"] -= 1.
    elif mutation == "retained_count":
        result["record"]["prediction_retained_count"] = 0
    elif mutation == "source":
        result["source_sha256"]["src/runtime/starship_final_sensor_prediction.py"] = "b"*64
    else:
        result["record"]["nominal_engine_limits_n"][0] *= .8
    result["forecast_id"] = obs.digest({k: v for k, v in result.items() if k != "forecast_id"})
    with pytest.raises(ValueError):
        prediction.validate_forecast(result, job["snapshot"])


def test_provider_preview_contains_only_whitelisted_metrics(monkeypatch):
    snapshot, *_ = fixture()
    verified = {"record_valid": True, "contact_limits_met": True,
        "contact": {"speed_mps": 3.5, "tilt_deg": .3, "body_rate_rad_s": .001,
            "reserve_kg": 58000., "time_s": 5000.}}
    strong = {"passed": True, "checks": {"fixture_only": True}}
    rows = []
    for action in prediction.ACTIONS:
        for label, factor in zip(prediction.POINT_LABELS, [.79, .8, .81]):
            row = {"schema": prediction.FORECAST_SCHEMA, "snapshot_id": snapshot["snapshot_id"],
                "action": action, "sample_label": label, "sampled_factor": factor,
                "assumed_factor": .8 if action == prediction.ADOPT else 1.,
                "application_time_s": 3000., "accelerometer_estimate_id": "b"*64,
                "sampling_estimate_id": "c"*64, "source_sha256": {}, "backend": {},
                "record": {"actual_vehicle": snapshot["actual_vehicle"], "controller": snapshot["controller"],
                    "inventory": snapshot["inventory"], "physical_factor": .6},
                "verification": verified, "stronger_record_checks": strong}
            row["forecast_id"] = obs.digest(row)
            rows.append(row)
    monkeypatch.setattr(prediction, "validate_forecast", lambda *args: (verified, strong))
    preview = prediction.forecast_preview(rows[::-1], snapshot)
    assert set(preview) == {"status", "snapshot_id", "scope", "actions"}
    for group in preview["actions"].values():
        for point in group:
            assert set(point) == {"sample_label", "sampled_factor", "speed_mps", "tilt_deg",
                "body_rate_rad_s", "reserve_kg", "limits_met"}
    assert all(key not in json.dumps(preview) for key in
        ("actual_vehicle", "controller", "inventory", "physical_factor", "engine_states", "dry_mass_kg"))
    rows[0]["snapshot_id"] = "d"*64
    rows[0]["forecast_id"] = obs.digest({k: v for k, v in rows[0].items() if k != "forecast_id"})
    with pytest.raises(ValueError, match="capture_mismatch"):
        prediction.forecast_preview(rows, snapshot)


def test_old_zero_retained_schema_and_guards_not_relaxed():
    assert legacy.SNAPSHOT_SCHEMA == "missionos.ship_sensor_online_snapshot.v1"
    assert legacy.SNAPSHOT_SCHEMA != prediction.SNAPSHOT_SCHEMA
    source = (ROOT/"src/runtime/starship_ship_online_prediction.py").read_text()
    assert 'value["retained_count"] != 0' in source



@pytest.mark.parametrize("mode,subdivisions", [("strict_v1", 2), ("strict_v1", 4), ("outer_bracket_v2", 2)])
def test_explicit_integration_policy_is_fully_bound_without_changing_legacy_shape(mode, subdivisions):
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    old, state, actual, _nominal, cap = fixture()
    policy = RestoredIntegrationPolicy(subdivisions, fuel_event_mode=mode)
    current = prediction.make_snapshot(state, actual, cap, old["profile"], run_id=old["run_id"],
        handoff_sha256=old["handoff_sha256"], inventory=old["inventory"], cutoff_time_s=2398.,
        complete_time_s=2400., application_time_s=3000., integration_policy=policy)
    assert old["schema"] == prediction.SNAPSHOT_SCHEMA and "integration_policy" not in old
    assert current["schema"] == prediction.SNAPSHOT_SCHEMA_V2
    assert current["integration_policy"] == policy.record() and current["snapshot_id"] != old["snapshot_id"]
    assert prediction.policy_for_snapshot(current) == policy
    assert prediction.validate_snapshot(current)[0] == state
    assert current["state"] == old["state"] and current["controller"] == old["controller"]


def explicit_job():
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    job, *_ = job_fixture()
    policy = RestoredIntegrationPolicy(2, fuel_event_mode="outer_bracket_v2")
    job["snapshot"].update(schema=prediction.SNAPSHOT_SCHEMA_V2, integration_policy=policy.record())
    reseal(job["snapshot"])
    job["integration_policy"] = policy.record()
    return job, policy


def mocked_integration_counters(monkeypatch, *, failed=False):
    """A test-only counter witness for mocked propagation, no RK4 invocation."""
    from contextlib import contextmanager
    from src.runtime.starship_restored_integration import restored_integration
    @contextmanager
    def owned(policy):
        with restored_integration(policy) as scope:
            yield scope
            # Explicit synthetic receipt counters, not physical execution.
            scope._trace.update(rk4_calls=1, rk4_substeps=policy.subdivisions-(1 if failed else 0))
            if failed:
                scope._trace.update(failed_rk4_calls=1, incomplete_failure_substeps=1)
                scope._status, scope._failure_class = "failed", "ValueError"
    monkeypatch.setattr(prediction, "restored_integration", owned)


def test_forecast_owns_the_bound_integration_hook_and_hashes_its_receipt(monkeypatch):
    from src.runtime import starship_restored_integration as integration
    job, policy = explicit_job()
    captured = {}
    mock_propagation(monkeypatch, captured)
    mocked_integration_counters(monkeypatch)
    fake = obs.propagate
    def inside(*args, **kwargs):
        assert dyn._rk4 is not integration._BASELINE_RK4
        return fake(*args, **kwargs)
    monkeypatch.setattr(obs, "propagate", inside)
    result = prediction.forecast_job(job)
    assert dyn._rk4 is integration._BASELINE_RK4
    assert result["schema"] == prediction.FORECAST_SCHEMA_V2
    assert result["integration_policy"] == policy.record()
    assert integration.validate_execution_receipt(result["integration_execution"], policy) == result["integration_execution"]
    assert result["integration_execution"]["trace"]["rk4_calls"] == 1  # synthetic test-only counter witness
    assert result["forecast_id"] == obs.digest({k: v for k, v in result.items() if k != "forecast_id"})
    prediction.validate_forecast(result, job["snapshot"])


def test_caught_forecast_failure_retains_raw_record_audit_and_failed_receipt(monkeypatch):
    from src.runtime.starship_restored_integration import validate_execution_receipt
    job, policy = explicit_job()
    mock_propagation(monkeypatch, {})
    fake = obs.propagate
    def failed(*args, **kwargs):
        run = fake(*args, **kwargs)
        run.update(termination="numerical_or_control_failure",
            error={"type": "ValueError", "detail": "synthetic_non_depletion_failure"})
        return run
    monkeypatch.setattr(obs, "propagate", failed)
    mocked_integration_counters(monkeypatch, failed=True)
    result = prediction.forecast_job(job)
    assert result["record"]["error"]["detail"] == "synthetic_non_depletion_failure"
    assert "control_capability_audit" in result["record"] and "final_state" in result["record"]
    assert result["record"]["outcome"]["termination"] == "numerical_or_control_failure"
    assert result["integration_execution"]["status"] == "failed"
    validate_execution_receipt(result["integration_execution"], policy, require_completed=False)
    assert result["forecast_id"] == obs.digest({k: v for k, v in result.items() if k != "forecast_id"})
    with pytest.raises(ValueError):
        prediction.validate_forecast(result, job["snapshot"])


@pytest.mark.parametrize("mutation", ["job_policy", "missing_job_policy", "snapshot_policy", "snapshot_schema"])
def test_forecast_integration_mismatch_refuses_before_propagation(mutation):
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    job, _policy = explicit_job()
    if mutation == "job_policy":
        job["integration_policy"] = RestoredIntegrationPolicy(4, fuel_event_mode="outer_bracket_v2").record()
    elif mutation == "missing_job_policy":
        del job["integration_policy"]
    elif mutation == "snapshot_policy":
        job["snapshot"]["integration_policy"]["fuel_event_mode"] = "unregistered"
        reseal(job["snapshot"])
    else:
        job["snapshot"]["schema"] = prediction.SNAPSHOT_SCHEMA
        reseal(job["snapshot"])
    with pytest.raises(ValueError):
        prediction.forecast_job(job)


@pytest.mark.parametrize("mutation", ["policy", "receipt_policy", "receipt_failed", "schema_downgrade"])
def test_saved_forecast_policy_and_receipt_cannot_be_rebound_after_rehash(monkeypatch, mutation):
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    job, _policy = explicit_job()
    mock_propagation(monkeypatch, {})
    mocked_integration_counters(monkeypatch)
    result = prediction.forecast_job(job)
    if mutation == "policy":
        result["integration_policy"] = RestoredIntegrationPolicy(4, fuel_event_mode="outer_bracket_v2").record()
    elif mutation == "receipt_policy":
        result["integration_execution"]["policy"] = RestoredIntegrationPolicy(4, fuel_event_mode="outer_bracket_v2").record()
    elif mutation == "receipt_failed":
        result["integration_execution"]["status"] = "failed"
    else:
        result["schema"] = prediction.FORECAST_SCHEMA
    result["forecast_id"] = obs.digest({k: v for k, v in result.items() if k != "forecast_id"})
    with pytest.raises(ValueError):
        prediction.validate_forecast(result, job["snapshot"])
