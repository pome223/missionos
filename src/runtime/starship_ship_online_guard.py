"""Credential-free Rules for observation-bound online return counterfactuals.

This checker never advances physics or invokes a model. The host supplies the
immutable authority from a separately captured runtime context. Hashes provide
experimental provenance; they do not authenticate hardware or resist a hostile
process with the same operating-system identity.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys

from . import starship_ship_capability_guard as prior
from . import starship_ship_capability_observation as obs
from . import starship_ship_online_prediction as prediction
from . import starship_ship_sensor_observation as sensors
from ..intelligence import starship_ship_sensor_agent as agent

AUTHORITY_SCHEMA = "missionos.ship_sensor_online_authority.v1"
BUNDLE_SCHEMA = "missionos.ship_sensor_online_guard_bundle.v1"
RECEIPT_SCHEMA = "missionos.ship_sensor_online_guard_receipt.v1"
SCOPE = prediction.SCOPE
CONTACT_LIMITS = dict(prior.CONTACT_LIMITS)
AUTHORITY_KEYS = {"schema", "scope", "run_id", "handoff_sha256", "retained_count",
    "nominal_profile_sha256", "snapshot_sha256", "request_snapshot_sha256", "source_sha256", "backend", "contact_limits",
    "max_band_half_width", "tracking_source_sha256", "tracking_samples_sha256"}
BUNDLE_KEYS = {"schema", "run_id", "authority_sha256", "request", "accelerometer_telemetry",
    "tracking_delivery", "snapshot", "request_snapshot", "forecasts", "selected_action", "checked_at_s", "source_sha256", "backend"}
REQUIRED_SOURCE = {"src/runtime/starship_ship_online_prediction.py", "src/runtime/starship_ship_online_guard.py",
    "src/runtime/starship_ship_capability_observation.py", "src/runtime/starship_ship_sensor_observation.py",
    "src/runtime/starship_ship_capability_guard.py", "src/runtime/starship_ship_capability_matrix.py",
    "src/runtime/starship_ship_fault_screen.py", "src/runtime/starship_ship_return.py",
    "src/runtime/starship_sixdof.py", "src/runtime/starship_sixdof_mission.py",
    "src/runtime/starship_physics.py", "src/runtime/starship_sixdof_contact.py",
    "src/intelligence/starship_ship_sensor_agent.py"}


def numeric_evidence(accel, tracking, application_time_s, forecasts=None):
    """The public citation dictionary; every value has an independently checked origin."""
    result = {"accel_factor": accel["factor_estimate"],
        "accel_band_low": accel["band"][0] if accel["band"] else None,
        "accel_band_high": accel["band"][1] if accel["band"] else None,
        "accel_samples": accel["accepted_samples"],
        "tracking_delay_s": sensors.TRACKING["request_latency_s"], "application_time_s": application_time_s}
    if tracking is not None:
        result.update(tracking_factor=tracking["factor_estimate"],
            tracking_band_low=tracking["band"][0] if tracking["band"] else None,
            tracking_band_high=tracking["band"][1] if tracking["band"] else None,
            tracking_intervals=tracking["accepted_samples"])
    if forecasts is not None:
        result.update(prediction.forecast_evidence(forecasts))
    return result


def _bounded(estimate, maximum_half_width, *, tracking=False):
    factor, band = estimate.get("factor_estimate"), estimate.get("band")
    return (type(band) is list and len(band) == 2
        and all(prediction.number(x) for x in (factor, *band))
        and obs.FACTOR_RANGE[0] <= band[0] < factor < band[1] <= obs.FACTOR_RANGE[1]
        and (band[1]-band[0])/2 <= maximum_half_width+1e-12
        and (estimate.get("status") == "estimated" if tracking else
             type(estimate.get("accepted_samples")) is int
             and estimate["accepted_samples"] >= obs.ESTIMATOR["minimum_samples"]))


def _telemetry_valid(rows, nominal, issued, cutoff):
    return (type(rows) is list and bool(rows)
        and all(type(row) is dict and set(row) == prior.TELEMETRY_KEYS
            and prediction.number(row["time_s"]) and row["phase"] == "deorbit_burn"
            and row["time_s"] <= cutoff+1e-8
            and row["time_s"]+obs.SENSOR["telemetry_latency_s"] <= issued+1e-8
            and type(row["engine_throttle_states"]) is list
            and len(row["engine_throttle_states"]) == len(nominal.engines)
            and all(prediction.number(x) and 0 <= x <= 1 for x in row["engine_throttle_states"])
            and type(row["engine_available"]) is list and len(row["engine_available"]) == len(nominal.engines)
            and all(type(x) is bool for x in row["engine_available"])
            and prediction.number(row["fuel_gauge_kg"]) and row["fuel_gauge_kg"] >= 0
            and type(row["measured_specific_force_body_mps2"]) is list
            and len(row["measured_specific_force_body_mps2"]) == 3
            and all(prediction.number(x) for x in row["measured_specific_force_body_mps2"])
            for row in rows)
        and all(a["time_s"] < b["time_s"] for a, b in zip(rows, rows[1:])))


def check_bundle(bundle, authority):
    """Recompute observation/record predicates and return a bound rejection on error."""
    checks, errors = {}, []
    receipt = {"schema": RECEIPT_SCHEMA, "scope": SCOPE, "passed": False,
        "run_id": None, "request_sha256": None, "authority_sha256": None,
        "snapshot_id": None, "selected_action": None, "factor_estimate": None,
        "application_time_s": None, "accelerometer_estimate_id": None,
        "tracking_estimate_id": None, "tracking_delivery_id": None,
        "evidence_ids": [], "checks": checks, "errors": errors}
    try:
        # Conversion rejects all nested nonfinite values, including unused fields.
        bundle, authority = prediction.json_copy(bundle), prediction.json_copy(authority)
        if type(bundle) is not dict or set(bundle) != BUNDLE_KEYS or bundle["schema"] != BUNDLE_SCHEMA:
            raise ValueError("invalid_online_guard_bundle")
        _check(bundle, authority, receipt)
        receipt["passed"] = bool(checks) and all(checks.values())
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        checks["bundle_structure_valid"] = False
        errors.append(f"{type(exc).__name__}: {exc}")
    receipt["receipt_id"] = obs.digest(receipt)
    return receipt


def _check(bundle, authority, receipt):
    checks, errors = receipt["checks"], receipt["errors"]
    checks["authority_schema"] = (type(authority) is dict and set(authority) == AUTHORITY_KEYS
        and authority["schema"] == AUTHORITY_SCHEMA and authority["scope"] == "experimental_6dof_only"
        and authority["contact_limits"] == CONTACT_LIMITS and authority["max_band_half_width"] == .02
        and type(authority["retained_count"]) is int and authority["retained_count"] == 0
        and type(authority["source_sha256"]) is dict and REQUIRED_SOURCE <= set(authority["source_sha256"])
        and all(prediction.is_hash(authority[k]) for k in ("handoff_sha256", "nominal_profile_sha256",
            "snapshot_sha256", "request_snapshot_sha256", "tracking_source_sha256", "tracking_samples_sha256")))
    if not checks["authority_schema"]:
        raise ValueError("invalid_online_guard_authority")
    snapshot = bundle["snapshot"]
    origin_state, nominal, history = prediction.validate_snapshot(snapshot)
    request_snapshot = bundle["request_snapshot"]
    state, request_nominal, request_history = prediction.validate_snapshot(request_snapshot)
    receipt.update(run_id=bundle["run_id"], authority_sha256=bundle["authority_sha256"],
        snapshot_id=snapshot["snapshot_id"], selected_action=bundle["selected_action"],
        application_time_s=snapshot["application_time_s"])
    checks["authority_bound"] = bundle["authority_sha256"] == obs.digest(authority)
    checks["trusted_capture_bound"] = (bundle["run_id"] == snapshot["run_id"] == authority["run_id"]
        and snapshot["snapshot_id"] == authority["snapshot_sha256"]
        and snapshot["handoff_sha256"] == authority["handoff_sha256"]
        and obs.digest(snapshot["profile"]) == authority["nominal_profile_sha256"]
        and snapshot["retained_count"] == authority["retained_count"])
    checks["fresh_request_capture_bound"] = (
        request_snapshot["snapshot_id"] == authority["request_snapshot_sha256"]
        and all(request_snapshot[key] == snapshot[key] for key in
            ("run_id", "handoff_sha256", "retained_count", "profile", "cutoff_time_s", "complete_time_s", "application_time_s"))
        and request_nominal == nominal and origin_state.time_s <= state.time_s
        and state.propellant_kg <= origin_state.propellant_kg+1e-7
        and request_history.return_time_s == history.return_time_s
        and request_history.released_count == history.released_count)
    checks["source_backend_bound"] = (bundle["source_sha256"] == authority["source_sha256"]
        and bundle["backend"] == authority["backend"])
    checks.update(prior.current_source_checks(authority["source_sha256"], authority["backend"]))
    request = agent.validate_request(bundle["request"])
    if request["schema"] != "missionos.ship_sensor_observation.v3":
        raise ValueError("online_guard_requires_v3_observation")
    receipt["request_sha256"] = agent.digest(request)
    issued, checked, application = request["issued_at_s"], bundle["checked_at_s"], snapshot["application_time_s"]
    checks["decision_and_application_clock"] = (all(prediction.number(x) for x in
        (issued, checked, application, request["deadline_s"]))
        and state.time_s == issued and issued <= checked < request["deadline_s"] < application
        and math.isclose(application-request["deadline_s"], 2., rel_tol=0., abs_tol=1e-8))
    expected_observation = obs.observation(state, nominal, "complete_burn")
    expected_observation["tracking_requested"] = True
    checks["snapshot_matches_observation"] = request["observation"] == expected_observation
    telemetry = bundle["accelerometer_telemetry"]
    checks["accelerometer_schema_and_causality"] = _telemetry_valid(
        telemetry, nominal, issued, snapshot["cutoff_time_s"])
    if not checks["accelerometer_schema_and_causality"]:
        raise ValueError("invalid_online_accelerometer_telemetry")
    accel = obs.estimate(obs.prepare_telemetry(telemetry, nominal), issued)
    forecast_accel = obs.estimate(obs.prepare_telemetry(telemetry, nominal), origin_state.time_s)
    checks["accelerometer_estimate_recomputed"] = request["accelerometer_estimate"] == accel
    checks["forecast_and_request_estimate_values_match"] = (
        forecast_accel["factor_estimate"] == accel["factor_estimate"] and forecast_accel["band"] == accel["band"])
    checks["accelerometer_estimate_bounded"] = _bounded(accel, authority["max_band_half_width"])
    receipt["accelerometer_estimate_id"] = accel["estimate_id"]
    delivery = bundle["tracking_delivery"]
    checks["physical_tracking_required"] = delivery is not None and request["tracking_observation"] is not None
    if not checks["physical_tracking_required"]:
        raise ValueError("online_physical_choice_requires_tracking")
    sensors.validate_delivery(delivery, origin_state.time_s)
    checks["tracking_capture_and_causality_bound"] = (
        delivery["source_sha256"] == authority["tracking_source_sha256"]
        and obs.digest(delivery["samples"]) == authority["tracking_samples_sha256"]
        and snapshot["complete_time_s"] <= delivery["requested_at_s"]
        and delivery["received_at_s"] <= issued
        and all(row["time_s"] <= snapshot["cutoff_time_s"]+1e-8 for row in delivery["samples"]))
    tracking = sensors.estimate_tracking(delivery, nominal, issued)
    checks["tracking_estimate_recomputed"] = request["tracking_observation"] == tracking
    checks["tracking_estimate_bounded"] = _bounded(tracking, authority["max_band_half_width"], tracking=True)
    receipt.update(tracking_estimate_id=tracking["estimate_id"], tracking_delivery_id=delivery["delivery_id"])
    if not checks["tracking_estimate_bounded"] or not checks["accelerometer_estimate_bounded"]:
        raise ValueError("online_estimate_not_bounded")
    action = bundle["selected_action"]
    checks["selected_physical_action"] = action in prediction.ACTIONS
    receipt["factor_estimate"] = accel["factor_estimate"] if action == prediction.ADOPT else 1.
    if action == prediction.ADOPT:
        checks["independent_tracking_supports_adoption"] = (
            accel["anomaly"] is True and accel["band"][1] < 1. and tracking["band"][1] < 1.
            and max(accel["band"][0], tracking["band"][0]) <= min(accel["band"][1], tracking["band"][1]))
    rows = prediction.ordered_forecasts(bundle["forecasts"])
    points = (tracking["band"][0], tracking["factor_estimate"], tracking["band"][1])
    selected_passes = []
    for index, row in enumerate(rows):
        prefix = f"forecast_{index}"
        if (set(row) != prediction.FORECAST_KEYS or row["schema"] != prediction.FORECAST_SCHEMA
                or not prediction.is_hash(row["forecast_id"])):
            raise ValueError("invalid_online_forecast_schema")
        expected_action = prediction.ACTIONS[index//3]
        assumed = accel["factor_estimate"] if expected_action == prediction.ADOPT else 1.
        record = row["record"]
        checks[prefix+"_capture_and_candidate_bound"] = (
            row["forecast_id"] == obs.digest({k: v for k, v in row.items() if k != "forecast_id"})
            and row["snapshot_id"] == snapshot["snapshot_id"]
            and row["action"] == expected_action and row["sampled_factor"] == points[index % 3]
            and row["assumed_factor"] == assumed and row["application_time_s"] == application
            and row["source_sha256"] == authority["source_sha256"] and row["backend"] == authority["backend"]
            and record["initial_state"] == snapshot["state"]
            and record["plant_factor"] == row["sampled_factor"]
            and record["prediction_snapshot_id"] == snapshot["snapshot_id"]
            and record["prediction_controller_sha256"] == obs.digest(snapshot["controller"])
            and record["prediction_is_execution"] is False and record["online_prediction_scope"] == SCOPE)
        switch = record["switch"]
        checks[prefix+"_switch_bound"] = (switch is None if expected_action == prediction.CONTINUE else
            type(switch) is dict and set(switch) == {"factor", "time_s"}
            and switch["factor"] == assumed and prediction.number(switch["time_s"])
            and math.isclose(switch["time_s"], application, rel_tol=0., abs_tol=1e-8))
        if expected_action == prediction.ADOPT:
            switches = record["control_capability_audit"]["switches"]
            checks[prefix+"_origin_estimate_basis_bound"] = (len(switches) == 1
                and switches[0]["basis"] == {"kind": "online_numeric_estimate",
                    "estimate_id": forecast_accel["estimate_id"], "snapshot_id": snapshot["snapshot_id"]})
        strong = prior.stronger_record_checks(record, snapshot["profile"], 0)
        verified = obs.verify_record(record, CONTACT_LIMITS)
        checks[prefix+"_record_independently_checked"] = strong["passed"] and verified["record_valid"]
        checks[prefix+"_verification_summary_recomputed"] = (
            row["verification"] == verified and row["stronger_record_checks"] == strong)
        metrics = prior.contact_metrics(record["outcome"]["contact_receipt"], record["final_state"])
        checks[prefix+"_contact_arithmetic_matches"] = metrics == verified["contact"]
        limits = (verified["contact_limits_met"]
            and all(metrics[k] <= CONTACT_LIMITS[k] for k in ("speed_mps", "tilt_deg", "body_rate_rad_s"))
            and metrics["reserve_kg"] >= CONTACT_LIMITS["reserve_kg"])
        if expected_action == action:
            selected_passes.append(bool(limits))
            receipt["evidence_ids"].append(row["forecast_id"])
        if not strong["passed"]:
            errors.append(prefix+": "+",".join(k for k, passed in strong["checks"].items() if not passed))
    checks["selected_action_all_sampled_points_meet_contact_limits"] = len(selected_passes) == 3 and all(selected_passes)
    checks["public_forecast_preview_recomputed"] = request["previews"] == prediction.forecast_preview(rows)
    checks["numeric_reference_values_recomputed"] = request["evidence"] == numeric_evidence(accel, tracking, application, rows)


def consume_receipt(receipt, consumed, *, run_id, request_sha256, authority_sha256,
                    selected_action, application_time_s, snapshot_id=None):
    """Consume a bound receipt once; the executor separately rechecks current time/state."""
    if type(receipt) is not dict or type(consumed) is not set:
        return False
    identity = receipt.get("receipt_id")
    try:
        valid = (prediction.is_hash(identity)
            and identity == obs.digest({k: v for k, v in receipt.items() if k != "receipt_id"})
            and receipt.get("schema") == RECEIPT_SCHEMA and receipt.get("scope") == SCOPE
            and receipt.get("passed") is True and receipt.get("run_id") == run_id
            and receipt.get("request_sha256") == request_sha256
            and receipt.get("authority_sha256") == authority_sha256
            and receipt.get("selected_action") == selected_action
            and receipt.get("application_time_s") == application_time_s
            and (snapshot_id is None or receipt.get("snapshot_id") == snapshot_id)
            and identity not in consumed)
    except (ValueError, TypeError):
        return False
    if valid:
        consumed.add(identity)
    return bool(valid)


def _strict_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _read_json(text):
    return json.loads(text, object_pairs_hook=_strict_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json_constant")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority-file", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if any(any(part in key.upper() for part in ("API_KEY", "SECRET", "TOKEN", "CREDENTIAL"))
               for key in os.environ):
            raise ValueError("credential_forbidden_in_online_guard")
        authority = _read_json(args.authority_file.read_text())
        bundle = _read_json(sys.stdin.read())
        result = check_bundle(bundle, authority)
    except (ValueError, TypeError, OSError) as exc:
        result = {"schema": RECEIPT_SCHEMA, "scope": SCOPE, "passed": False,
                  "checks": {"input_valid": False}, "errors": [str(exc)]}
    rendered = json.dumps(result, sort_keys=True, allow_nan=False)+"\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        sys.stdout.write(rendered)
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
