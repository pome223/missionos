"""Analytical saved-record fixtures only; no trajectory or provider is run."""
from copy import deepcopy
from dataclasses import replace
import json
import math

import pytest

from src.runtime import starship_final_sensor_execution as execution
from src.runtime import starship_final_sensor_guard as guard
from src.runtime import starship_final_sensor_prediction as prediction
from src.runtime import starship_physics as env
from src.runtime import starship_ship_capability_observation as obs
from src.runtime import starship_ship_sensor_observation as sensors
from src.runtime import starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_return_feasibility import backend
from tests.contract.test_starship_final_sensor_prediction import fixture


@pytest.fixture(autouse=True)
def no_dynamics_or_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("saved checker attempted trajectory propagation")
    monkeypatch.setattr(dyn, "step", forbidden)
    monkeypatch.setattr(obs, "propagate", forbidden)
    monkeypatch.setattr(obs, "advance_plant", forbidden)


def forecast(snapshot, action, label, sampled, accel, tracking, source, speed):
    state, nominal, history = prediction.validate_snapshot(snapshot)
    plant, model = obs.model_vehicle(nominal, sampled), obs.model_vehicle(nominal, 1.)
    controller = obs.SwitchingController(nominal, model, snapshot["profile"],
        history.return_time_s, history.released_count, obs.SwitchAudit(nominal, model, plant))
    controller.controller = history
    application = snapshot["application_time_s"]
    later = replace(state, time_s=application, propellant_kg=99000.)
    for current in (state, later):
        if current.time_s == application and action == prediction.ADOPT:
            controller.apply_switch(application, accel["factor_estimate"], {
                "kind": "final_sensor_numeric_estimate", "estimate_id": accel["estimate_id"],
                "snapshot_id": snapshot["snapshot_id"]})
        profile = deepcopy(snapshot["profile"])
        profile["ship"]["engine_thrust_n"] = controller.model.engines[0].max_thrust_n
        for name in ("update", "capability_profile", "command", "capability_profile", "tvc_control", "plant"):
            controller.audit.inspect(name, current, plant if name == "plant" else controller.model,
                profile if name in ("capability_profile", "tvc_control") else None)
    q = (math.sqrt(.5), 0., math.sqrt(.5), 0.)
    point = [env.EARTH_EQUATORIAL_RADIUS_M, 0., 0.]
    velocity = [speed, env.EARTH_ROTATION_RAD_S*point[0], 0.]
    final = replace(later, time_s=3100., r_eci_m=tuple(point), v_eci_mps=tuple(velocity),
        q_body_to_eci=q, propellant_kg=58000.)
    contact = {"contact": True, "event_time_s": final.time_s, "point_eci_m": point,
        "point_velocity_eci_mps": velocity, "surface_normal_eci": [1., 0., 0.],
        "q_body_to_eci": list(q), "propellant_kg": final.propellant_kg,
        "surface_relative_speed_mps": speed}
    run = {"state": final, "samples": [mission._sample(s, plant, "ballistic_return") for s in (state, later, final)],
        "events": [], "termination": "low_speed_surface_contact" if speed <= 5. else "surface_impact",
        "contact": contact, "error": None, "steps": 2}
    switch = None if action == prediction.CONTINUE else {"factor": accel["factor_estimate"], "time_s": application}
    record = prediction.json_copy(obs.record("synthetic_saved_guard_fixture", snapshot["state"], plant,
        nominal, controller, run, plant_factor=sampled, switch=switch, wall_time_s=0., extra={
            "prediction_snapshot_id": snapshot["snapshot_id"], "prediction_controller_sha256": snapshot["controller_sha256"],
            "prediction_nominal_vehicle_sha256": snapshot["nominal_vehicle_sha256"],
            "prediction_inventory_sha256": snapshot["inventory_sha256"],
            "prediction_actuator_sha256": snapshot["actuator_sha256"],
            "prediction_retained_count": snapshot["retained_count"], "prediction_released_count": snapshot["released_count"],
            "prediction_is_execution": False, "online_prediction_scope": prediction.SCOPE}))
    result = {"schema": prediction.FORECAST_SCHEMA, "snapshot_id": snapshot["snapshot_id"], "action": action,
        "sample_label": label, "sampled_factor": sampled,
        "assumed_factor": accel["factor_estimate"] if action == prediction.ADOPT else 1.,
        "application_time_s": application, "accelerometer_estimate_id": accel["estimate_id"],
        "sampling_estimate_id": tracking["estimate_id"], "source_sha256": source, "backend": backend(),
        "record": record, "verification": obs.verify_record(record, prediction.CONTACT_LIMITS),
        "stronger_record_checks": prediction._record_checks(record, snapshot)}
    assert result["verification"]["record_valid"], result["verification"]
    result["forecast_id"] = obs.digest(result)
    return result


def bundle_fixture(monkeypatch, *, actual_family="propulsion", action=prediction.ADOPT, retained=13):
    snapshot, state, nominal_actual, nominal, _ = fixture(retained)
    factor = .8 if actual_family == "propulsion" else 1.
    # Unit estimator outputs are explicit fixture values; their correctness is
    # not inferred from these handcrafted, non-propagated guard records.
    def accel_estimate(rows, when):
        row = {"factor_estimate": .8, "band": [.79, .81], "accepted_samples": 100,
            "anomaly": True, "observed_at_s": when}
        return {**row, "estimate_id": obs.digest(row)}
    def tracking_estimate(delivery, model, when):
        row = {"factor_estimate": factor, "band": [factor-.01, factor+.01],
            "accepted_samples": 100, "observed_at_s": when}
        return {**row, "estimate_id": obs.digest(row)}
    monkeypatch.setattr(obs, "estimate", accel_estimate)
    monkeypatch.setattr(sensors, "estimate_tracking", tracking_estimate)
    source = prediction.source_inventory(["src/runtime/starship_final_sensor_execution.py",
        "src/runtime/starship_final_sensor_guard.py"])
    accel, tracking = accel_estimate([], state.time_s), tracking_estimate(None, nominal, state.time_s)
    rows = [forecast(snapshot, candidate, label, point, accel, tracking, source,
        3.5 if candidate == (prediction.ADOPT if actual_family == "propulsion" else prediction.CONTINUE) else 9.2)
        for candidate in prediction.ACTIONS for label, point in zip(prediction.POINT_LABELS,
            [factor-.01, factor, factor+.01])]
    origin = replace(state, time_s=2390.)
    truth = [sensors.tracking_source_row(origin, "deorbit_burn")]
    tracking_samples = sensors.measure_tracking(truth, 0)
    delivery = sensors.request_tracking(tracking_samples, 2400., obs.digest(truth))
    telemetry = [{"time_s": 2390., "phase": "deorbit_burn", "measured_specific_force_body_mps2": [0., 0., 5.],
        "engine_throttle_states": [engine.throttle for engine in state.engine_states],
        "engine_available": [engine.available for engine in state.engine_states], "fuel_gauge_kg": 100000.}]
    facts = {"time_s": state.time_s, "altitude_m": dyn.observe(state, nominal)["altitude_m"],
        "fuel_gauge_kg": obs.observation(state, nominal, "complete_burn")["fuel_gauge_kg"],
        "accel_factor": .8, "accel_band_low": .79, "accel_band_high": .81, "accel_samples": 100,
        "accel_anomaly": True, "tracking_requested": True, "tracking_delay_s": 30., "application_time_s": 3000.,
        "tracking_factor": factor, "tracking_band_low": factor-.01, "tracking_band_high": factor+.01,
        "tracking_intervals": 100, **prediction.forecast_evidence(rows, snapshot)}
    request = execution.validate_public_request({"schema": execution.PUBLIC_SCHEMA,
        "run_id": "final-sensor-fixture", "request_id": "final-sensor-fixture-final-1", "stage": "final",
        "issued_at_s": state.time_s, "deadline_s": state.time_s+150., "scope_sha256": "d"*64,
        "facts": facts, "allowed_actions": list(prediction.ACTIONS),
        "effects": {candidate: execution.EFFECTS[candidate] for candidate in prediction.ACTIONS}})
    decision = {"schema": execution.JUDGMENT_SCHEMA, "request_sha256": obs.digest(request),
        "action": action, "mode": "fixture", "provenance": {"model_inference_invoked": False}}
    decision["decision_id"] = obs.digest(decision)
    authority = {"schema": "missionos.starship_final_sensor_guard_authority.v1", "run_id": request["run_id"],
        "source_sha256": source, "backend": backend(), "scope_sha256": "d"*64, "mode": "fixture",
        "handoff_sha256": snapshot["handoff_sha256"], "inventory_sha256": snapshot["inventory_sha256"],
        "snapshot_id": snapshot["snapshot_id"], "application_time_s": 3000., "contact_limits": prediction.CONTACT_LIMITS}
    bundle = {"schema": "missionos.starship_final_sensor_guard_bundle.v1", "run_id": request["run_id"],
        "authority_sha256": obs.digest(authority), "request": request, "decision": decision,
        "selected_action": action, "snapshot": snapshot, "request_snapshot": deepcopy(snapshot),
        "current_snapshot": deepcopy(snapshot), "accelerometer_telemetry": telemetry,
        "tracking_delivery": delivery, "forecasts": rows, "checked_at_s": state.time_s,
        "source_sha256": source, "backend": backend()}
    return bundle, authority


@pytest.mark.parametrize("retained", [0, 13, 26])
@pytest.mark.parametrize("family,action", [("propulsion", prediction.ADOPT), ("sensor", prediction.CONTINUE)])
def test_saved_checker_accepts_correct_choice_for_both_hypotheses_without_physics(monkeypatch, retained, family, action):
    bundle, authority = bundle_fixture(monkeypatch, actual_family=family, action=action, retained=retained)
    result = guard.check_bundle(bundle, authority)
    assert result["passed"] is True, result
    assert result["receipt_id"] == obs.digest({k: v for k, v in result.items() if k != "receipt_id"})
    assert result["selected_action"] == action


@pytest.mark.parametrize("family,action", [("propulsion", prediction.CONTINUE), ("sensor", prediction.ADOPT)])
def test_saved_checker_rejects_inverse_choice_without_replacing_it(monkeypatch, family, action):
    bundle, authority = bundle_fixture(monkeypatch, actual_family=family, action=action)
    result = guard.check_bundle(bundle, authority)
    assert result["passed"] is False
    assert result["selected_action"] == action
    assert result["checks"]["tracking_corroborates_choice"] is False


@pytest.mark.parametrize("mutation", ["source", "backend", "authority", "time", "inventory", "telemetry_future",
    "tracking_not_arrived", "public_number", "decision", "forecast_point", "forecast_contact"])
def test_modified_saved_material_is_rejected(monkeypatch, mutation):
    bundle, authority = bundle_fixture(monkeypatch)
    if mutation == "source":
        authority["source_sha256"]["src/runtime/starship_final_sensor_guard.py"] = "f"*64
    elif mutation == "backend":
        authority["backend"] = {"numpy": "wrong", "scipy": "wrong"}
    elif mutation == "authority":
        bundle["authority_sha256"] = "f"*64
    elif mutation == "time":
        bundle["checked_at_s"] += 1.
    elif mutation == "inventory":
        bundle["current_snapshot"]["inventory"]["retained_count"] = 0
    elif mutation == "telemetry_future":
        bundle["accelerometer_telemetry"][0]["time_s"] = 2400.
    elif mutation == "tracking_not_arrived":
        bundle["tracking_delivery"]["received_at_s"] += 1.
    elif mutation == "public_number":
        bundle["request"]["facts"]["accel_factor"] = .6
    elif mutation == "decision":
        bundle["decision"]["decision_id"] = "f"*64
    elif mutation == "forecast_point":
        bundle["forecasts"][0]["sampled_factor"] = .7
    else:
        bundle["forecasts"][0]["record"]["outcome"]["contact_receipt"]["surface_relative_speed_mps"] = 1.
    assert guard.check_bundle(bundle, authority)["passed"] is False


def test_checker_cli_is_saved_only_exclusive_and_never_echoes_secret_values(tmp_path, monkeypatch, capsys):
    from src.runtime import starship_restored_environment as environment
    monkeypatch.setattr(environment, "worker_environment_view", lambda: {})
    input_file, output_file = tmp_path/"input.json", tmp_path/"receipt.json"
    input_file.write_text(json.dumps({"bundle": {}, "authority": {}}))
    assert guard.main(["--input", str(input_file), "--output", str(output_file)]) == 0
    assert json.loads(output_file.read_text())["passed"] is False
    assert json.loads(capsys.readouterr().out)["plant_steps"] == 0
    with pytest.raises(FileExistsError):
        guard.main(["--input", str(input_file), "--output", str(output_file)])
    monkeypatch.setattr(environment, "worker_environment_view", lambda: {"FAKE_API_KEY": "never-echo-value"})
    with pytest.raises(ValueError, match="credentials_forbidden"):
        guard.main(["--input", str(input_file), "--output", str(tmp_path/"forbidden.json")])
    assert "never-echo-value" not in capsys.readouterr().out



def explicit_integration_bundle(monkeypatch):
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy, restored_integration
    bundle, authority = bundle_fixture(monkeypatch)
    policy = RestoredIntegrationPolicy(2, fuel_event_mode="outer_bracket_v2")
    for name in ("snapshot", "request_snapshot", "current_snapshot"):
        snapshot = bundle[name]
        snapshot.update(schema=prediction.SNAPSHOT_SCHEMA_V2, integration_policy=policy.record())
        snapshot["snapshot_id"] = obs.digest({k: value for k, value in snapshot.items() if k != "snapshot_id"})
    snapshot = bundle["snapshot"]
    # These are analytical saved-record fixtures, not numerical RK4 executions.
    with restored_integration(policy) as scope:
        pass
    receipt = scope.record()
    receipt["trace"].update(rk4_calls=2, rk4_substeps=4)
    for forecast in bundle["forecasts"]:
        forecast.update(schema=prediction.FORECAST_SCHEMA_V2, integration_policy=policy.record(),
            integration_execution=deepcopy(receipt), snapshot_id=snapshot["snapshot_id"])
        record = forecast["record"]
        record["prediction_snapshot_id"] = snapshot["snapshot_id"]
        if forecast["action"] == prediction.ADOPT:
            record["control_capability_audit"]["switches"][0]["basis"]["snapshot_id"] = snapshot["snapshot_id"]
        forecast["verification"] = obs.verify_record(record, prediction.CONTACT_LIMITS)
        forecast["stronger_record_checks"] = prediction._record_checks(record, snapshot)
        forecast["forecast_id"] = obs.digest({k: value for k, value in forecast.items() if k != "forecast_id"})
    authority.update(schema="missionos.starship_final_sensor_guard_authority.v2",
        integration_policy=policy.record(), snapshot_id=snapshot["snapshot_id"])
    bundle.update(schema="missionos.starship_final_sensor_guard_bundle.v2",
        integration_policy=policy.record(), authority_sha256=obs.digest(authority))
    return bundle, authority, policy


def test_guard_binds_explicit_policy_across_captures_forecasts_and_authority(monkeypatch):
    bundle, authority, _policy = explicit_integration_bundle(monkeypatch)
    result = guard.check_bundle(bundle, authority)
    assert result["schema"] == execution.GUARD_SCHEMA_V2
    assert result["passed"] is True, result
    assert result["checks"]["integration_policy_bound"] is True


@pytest.mark.parametrize("mutation", ["authority", "request_snapshot", "forecast", "bundle_downgrade"])
def test_guard_rejects_integration_policy_rebinding_with_rehashed_container(monkeypatch, mutation):
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    bundle, authority, _policy = explicit_integration_bundle(monkeypatch)
    other = RestoredIntegrationPolicy(4, fuel_event_mode="outer_bracket_v2").record()
    if mutation == "authority":
        authority["integration_policy"] = other
        bundle["authority_sha256"] = obs.digest(authority)
    elif mutation == "request_snapshot":
        snapshot = bundle["request_snapshot"]
        snapshot["integration_policy"] = other
        snapshot["snapshot_id"] = obs.digest({k: v for k, v in snapshot.items() if k != "snapshot_id"})
    elif mutation == "forecast":
        row = bundle["forecasts"][0]
        row["integration_policy"] = other
        row["forecast_id"] = obs.digest({k: v for k, v in row.items() if k != "forecast_id"})
    else:
        bundle["schema"] = "missionos.starship_final_sensor_guard_bundle.v1"
    assert guard.check_bundle(bundle, authority)["passed"] is False
