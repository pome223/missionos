"""Saved-only sensor admission checks for a separate credential-free process.

Same-model sampled forecasts are not a physical qualification. This checker
recomputes captured observations and records; it never propagates a plant or
calls a provider. Its caller must bind actual process evidence independently.
"""
import argparse
import json
import os

from . import starship_final_sensor_prediction as prediction
from . import starship_ship_capability_observation as obs
from . import starship_ship_sensor_observation as sensors
from . import starship_sixdof as dyn
from .starship_final_sensor_execution import (GUARD_SCHEMA, GUARD_SCHEMA_V2, ADOPT,
    require, validate_public_request, validate_judgment)

def check_bundle(bundle, authority):
    """Pure saved-input numerical checker; run in a credential-free process.

    Source/observation bindings and finite band points are checked here. This is
    not an independent physical model, continuous-band proof or process receipt.
    """
    checks = {}
    explicit = type(bundle) is dict and bundle.get("schema") == "missionos.starship_final_sensor_guard_bundle.v2"
    receipt = {"schema": GUARD_SCHEMA_V2 if explicit else GUARD_SCHEMA, "bundle_sha256": obs.digest(bundle),
        "authority_sha256": obs.digest(authority), "snapshot_id": None, "selected_action": None,
        "application_time_s": None, "factor_estimate": None, "passed": False, "checks": checks}
    try:
        bundle, authority = prediction.json_copy(bundle), prediction.json_copy(authority)
        integration_keys = {"integration_policy"} if explicit else set()
        require(type(bundle) is dict and set(bundle) == integration_keys | {"schema", "run_id", "authority_sha256", "request",
            "decision", "selected_action", "snapshot", "request_snapshot", "current_snapshot",
            "accelerometer_telemetry", "tracking_delivery", "forecasts", "checked_at_s", "source_sha256", "backend"}
            and bundle["schema"] == ("missionos.starship_final_sensor_guard_bundle.v2" if explicit
                else "missionos.starship_final_sensor_guard_bundle.v1")
            and type(authority) is dict and set(authority) == integration_keys | {"schema", "run_id", "source_sha256", "backend",
                "scope_sha256", "mode", "handoff_sha256", "inventory_sha256", "snapshot_id", "application_time_s", "contact_limits"}
            and authority["schema"] == ("missionos.starship_final_sensor_guard_authority.v2" if explicit
                else "missionos.starship_final_sensor_guard_authority.v1")
            and authority["mode"] in ("fixture", "live"), "final_sensor_guard_structure")
        origin, nominal, _ = prediction.validate_snapshot(bundle["snapshot"])
        requested, request_nominal, _ = prediction.validate_snapshot(bundle["request_snapshot"])
        current, current_nominal, _ = prediction.validate_snapshot(bundle["current_snapshot"])
        integration = prediction.snapshot_integration_fields(bundle["snapshot"])
        require(explicit is bool(integration), "final_sensor_guard_snapshot_integration_version")
        if explicit:
            checks["integration_policy_bound"] = (bundle["integration_policy"] == authority["integration_policy"]
                == integration["integration_policy"]
                and all(prediction.snapshot_integration_fields(bundle[name]) == integration
                    for name in ("request_snapshot", "current_snapshot")))
        else:
            require(all(not prediction.snapshot_integration_fields(bundle[name])
                for name in ("request_snapshot", "current_snapshot")), "final_sensor_guard_mixed_integration_versions")
        request = validate_public_request(bundle["request"])
        decision = validate_judgment(bundle["decision"], request, authority["mode"])
        action = bundle["selected_action"]
        receipt.update(snapshot_id=bundle["snapshot"]["snapshot_id"], selected_action=action,
            application_time_s=authority["application_time_s"],
            factor_estimate=request["facts"]["accel_factor"] if action == ADOPT else 1.)
        checks["authority_bound"] = bundle["authority_sha256"] == obs.digest(authority)
        checks["context_bound"] = (request["stage"] == "final" and action in prediction.ACTIONS
            and action == decision["action"] and bundle["run_id"] == authority["run_id"] == request["run_id"]
            and authority["scope_sha256"] == request["scope_sha256"]
            and nominal == request_nominal == current_nominal
            and authority["snapshot_id"] == bundle["snapshot"]["snapshot_id"]
            and authority["contact_limits"] == prediction.CONTACT_LIMITS
            and authority["handoff_sha256"] == bundle["snapshot"]["handoff_sha256"]
            and authority["inventory_sha256"] == bundle["snapshot"]["inventory_sha256"])
        keys = ("run_id", "handoff_sha256", "retained_count", "released_count", "profile",
            "inventory", "cutoff_time_s", "complete_time_s", "application_time_s", "nominal_vehicle_sha256")
        checks["actual_capture_continuity"] = (all(bundle["snapshot"][key] == bundle[name][key]
            for key in keys for name in ("request_snapshot", "current_snapshot"))
            and origin.time_s <= requested.time_s <= current.time_s == bundle["checked_at_s"]
            and current.propellant_kg <= requested.propellant_kg <= origin.propellant_kg
            and requested.time_s == request["issued_at_s"]
            and requested.time_s <= current.time_s < request["deadline_s"]
            and request["deadline_s"] <= authority["application_time_s"]-2.
            and authority["application_time_s"] == bundle["snapshot"]["application_time_s"])
        checks["source_and_backend_current"] = (bundle["source_sha256"] == authority["source_sha256"]
            and bundle["backend"] == authority["backend"]
            and {"src/runtime/starship_final_sensor_execution.py", "src/runtime/starship_final_sensor_guard.py"}
                <= authority["source_sha256"].keys()
            and all(prediction.current_source_checks(authority["source_sha256"], authority["backend"]).values()))
        telemetry = bundle["accelerometer_telemetry"]
        previous = -1.
        for row in telemetry:
            require(type(row) is dict and set(row) == {
                "time_s", "phase", "measured_specific_force_body_mps2", "engine_throttle_states", "engine_available", "fuel_gauge_kg"}
                and prediction.number(row["time_s"]) and previous < row["time_s"] <= bundle["snapshot"]["cutoff_time_s"]
                and row["phase"] == "deorbit_burn"
                and len(row["engine_throttle_states"]) == len(nominal.engines)
                and len(row["engine_available"]) == len(nominal.engines)
                and all(type(x) is bool for x in row["engine_available"])
                and all(prediction.number(x) and 0 <= x <= 1 for x in row["engine_throttle_states"])
                and len(row["measured_specific_force_body_mps2"]) == 3
                and all(prediction.number(x) for x in row["measured_specific_force_body_mps2"])
                and prediction.number(row["fuel_gauge_kg"]) and row["fuel_gauge_kg"] >= 0,
                "final_sensor_guard_telemetry")
            previous = row["time_s"]
        accel = obs.estimate(obs.prepare_telemetry(telemetry, nominal), requested.time_s)
        forecast_accel = obs.estimate(obs.prepare_telemetry(telemetry, nominal), origin.time_s)
        delivery = sensors.validate_delivery(bundle["tracking_delivery"], origin.time_s)
        tracking = sensors.estimate_tracking(delivery, nominal, requested.time_s)
        forecast_tracking = sensors.estimate_tracking(delivery, nominal, origin.time_s)
        prediction._estimate(accel, requested.time_s)
        points = prediction._estimate(forecast_tracking, origin.time_s)
        checks["observations_bounded"] = (accel["band"][1]-accel["band"][0] <= .04
            and tracking["band"][1]-tracking["band"][0] <= .04)
        expected = {"accel_factor": accel["factor_estimate"], "accel_band_low": accel["band"][0],
            "accel_band_high": accel["band"][1], "accel_samples": accel["accepted_samples"],
            "accel_anomaly": accel["anomaly"], "tracking_requested": True,
            "tracking_factor": tracking["factor_estimate"], "tracking_band_low": tracking["band"][0],
            "tracking_band_high": tracking["band"][1], "tracking_intervals": tracking["accepted_samples"],
            "tracking_delay_s": sensors.TRACKING["request_latency_s"], "application_time_s": authority["application_time_s"],
            "time_s": requested.time_s, "altitude_m": dyn.observe(requested, nominal)["altitude_m"],
            "fuel_gauge_kg": obs.observation(requested, nominal, "complete_burn")["fuel_gauge_kg"]}
        expected.update(prediction.forecast_evidence(bundle["forecasts"], bundle["snapshot"]))
        checks["public_numbers_recomputed"] = all(request["facts"].get(key) == value for key, value in expected.items())
        rows = prediction.ordered_forecasts(bundle["forecasts"])
        if explicit:
            checks["integration_policy_bound"] &= all(row.get("integration_policy") == integration["integration_policy"]
                for row in rows)
        checks["forecast_observations_bound"] = all(row["sampled_factor"] == points[row["sample_label"]]
            and row["sampling_estimate_id"] == forecast_tracking["estimate_id"]
            and row["accelerometer_estimate_id"] == forecast_accel["estimate_id"] for row in rows)
        checks["tracking_corroborates_choice"] = (
            max(accel["band"][0], tracking["band"][0]) <= min(accel["band"][1], tracking["band"][1])
            and accel["band"][1] < 1. and tracking["band"][1] < 1. if action == ADOPT
            else tracking["band"][0] <= 1. <= tracking["band"][1])
        selected = [row for row in rows if row["action"] == action]
        checks["selected_sampled_points_meet_limits"] = len(selected) == 3 and all(
            prediction.validate_forecast(row, bundle["snapshot"])[0]["contact_limits_met"] for row in selected)
        receipt["passed"] = bool(checks) and all(checks.values())
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        checks["bundle_valid"] = False
    # A checker failure is a negative receipt, not a replacement judgment.
    receipt["receipt_id"] = obs.digest(receipt)
    return receipt


def main(argv=None):
    from .starship_final_shared import read, save
    from .starship_restored_environment import worker_environment_view
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    environment = worker_environment_view()
    require(not any(any(part in name.upper() for part in ("SECRET", "TOKEN", "API_KEY", "CREDENTIAL"))
        for name in environment), "final_sensor_checker_credentials_forbidden")
    value = read(args.input, maximum_bytes=256*2**20)
    require(type(value) is dict and set(value) == {"bundle", "authority"}, "final_sensor_checker_input")
    receipt = check_bundle(value["bundle"], value["authority"])
    save(args.output, receipt)
    print(json.dumps({"schema": "missionos.starship_final_sensor_guard_process.v1",
        "checker_pid": os.getpid(), "exit_code": 0, "credential_free": True,
        "fixture_only": False, "receipt_id": receipt["receipt_id"], "passed": receipt["passed"],
        "model_calls": 0, "plant_steps": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
