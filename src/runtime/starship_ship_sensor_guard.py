"""Credential-free admission for a deliberately narrow sensor-disambiguation study.

This process checks causal observations and registered counterfactual records. It
does not infer the physical fault label, call a model, advance physics, or turn a
saved successful point into a continuous capability certificate. The historical
v1 contract permits nominal continuation without a recovery certificate. Opt-in
v2 checks both physical choices against all compatible registered discrete points.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys

from . import starship_ship_capability_guard as prior
from . import starship_ship_capability_observation as obs
from . import starship_ship_sensor_observation as sensors
from .starship_return_feasibility import backend as backend
from .starship_sixdof_mission import vehicle

BUNDLE_SCHEMA = "missionos.ship_sensor_guard_bundle.v1"
AUTHORITY_SCHEMA = "missionos.ship_sensor_authority.v1"
AUTHORITY_V2_SCHEMA = "missionos.ship_sensor_authority.v2"
AUTHORITY_SCHEMA_V2 = AUTHORITY_V2_SCHEMA
REQUEST_V2_SCHEMA = "missionos.ship_sensor_observation.v2"
RECEIPT_SCHEMA = "missionos.ship_sensor_guard_receipt.v1"
SCOPE = "discrete_counterfactual_points_single_origin_not_qualification"
ACTIONS = ("continue_nominal_assumption", "adopt_accelerometer_estimate")
ROOT = Path(__file__).resolve().parents[2]
BUNDLE_KEYS = {"schema", "run_id", "authority_sha256", "request", "accelerometer_telemetry",
               "tracking_delivery", "nominal_profile", "selected_action", "application_time_s",
               "checked_at_s", "source_sha256", "backend", "evidence"}
EVIDENCE_KEYS = {"evidence_id", "action", "assumed_factor", "sampled_capability_factor",
                 "record_sha256", "application_time_s"}
REQUEST_KEYS = {"schema", "request_id", "issued_at_s", "deadline_s", "observation",
                "accelerometer_estimate", "tracking_observation", "previews", "evidence"}
AUTHORITY_REQUIRED = {"schema", "scope", "contact_limits", "source_sha256", "backend",
    "nominal_profile_sha256", "retained_count", "origin_state_sha256", "application_time_s",
    "max_band_half_width", "allowed_tracking_samples_sha256", "allowed_tracking_source_sha256",
    "counterfactual_evidence"}
REQUIRED_SOURCE = {"src/runtime/starship_ship_sensor_guard.py",
    "src/runtime/starship_ship_sensor_observation.py", "src/runtime/starship_ship_capability_guard.py",
    "src/runtime/starship_ship_capability_observation.py", "src/runtime/starship_sixdof.py"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _finite_tree(value):
    if type(value) is float and not math.isfinite(value):
        raise ValueError("nonfinite_json_value")
    if type(value) is dict:
        for item in value.values():
            _finite_tree(item)
    elif type(value) is list:
        for item in value:
            _finite_tree(item)


def _strict_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def consume_receipt(receipt, consumed, *, run_id, request_sha256, authority_sha256,
                    selected_action, application_time_s):
    """Validate and consume one receipt identifier; this performs no actuation."""
    if type(receipt) is not dict or type(consumed) is not set:
        return False
    identity = receipt.get("receipt_id")
    valid = (_hash(identity) and identity == obs.digest({k: v for k, v in receipt.items() if k != "receipt_id"})
        and receipt.get("schema") == RECEIPT_SCHEMA and receipt.get("passed") is True
        and receipt.get("run_id") == run_id and receipt.get("request_sha256") == request_sha256
        and receipt.get("authority_sha256") == authority_sha256
        and receipt.get("selected_action") == selected_action
        and receipt.get("application_time_s") == application_time_s and identity not in consumed)
    if valid:
        consumed.add(identity)
    return bool(valid)


def check_bundle(bundle, authority):
    """Return an auditable rejection rather than raising into a running flight."""
    checks, errors = {}, []
    receipt = {"schema": RECEIPT_SCHEMA, "passed": False, "run_id": None,
        "request_sha256": None, "authority_sha256": None, "selected_action": None,
        "factor_estimate": None, "application_time_s": None, "accelerometer_estimate_id": None,
        "tracking_estimate_id": None, "tracking_delivery_id": None, "evidence_ids": [],
        "checks": checks, "errors": errors, "scope": SCOPE}
    try:
        _finite_tree(bundle)
        _finite_tree(authority)
        if type(bundle) is not dict or set(bundle) != BUNDLE_KEYS or bundle["schema"] != BUNDLE_SCHEMA:
            raise ValueError("invalid_sensor_guard_bundle")
        _check(bundle, authority, receipt, checks, errors)
        receipt["passed"] = bool(checks) and all(checks.values())
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        checks["bundle_structure_valid"] = False
    receipt["receipt_id"] = obs.digest(receipt)
    return receipt


def _check(bundle, authority, receipt, checks, errors):
    v2 = type(authority) is dict and authority.get("schema") == AUTHORITY_V2_SCHEMA
    checks["authority_schema"] = (type(authority) is dict and AUTHORITY_REQUIRED <= set(authority)
        and authority["schema"] in (AUTHORITY_SCHEMA, AUTHORITY_V2_SCHEMA)
        and authority["scope"] == "experimental_6dof_only"
        and authority["contact_limits"] == prior.CONTACT_LIMITS
        and authority["max_band_half_width"] == .02
        and authority["retained_count"] == 0
        and type(authority["source_sha256"]) is dict and REQUIRED_SOURCE <= set(authority["source_sha256"])
        and _hash(authority["nominal_profile_sha256"]) and _hash(authority["origin_state_sha256"])
        and _number(authority["application_time_s"])
        and all(type(authority[name]) is list and bool(authority[name])
            and all(_hash(item) for item in authority[name])
            and len(set(authority[name])) == len(authority[name]) for name in
            ("allowed_tracking_samples_sha256", "allowed_tracking_source_sha256")))
    if not checks["authority_schema"]:
        raise ValueError("invalid_sensor_guard_authority")
    receipt.update(run_id=bundle["run_id"], authority_sha256=bundle["authority_sha256"],
        selected_action=bundle["selected_action"], application_time_s=bundle["application_time_s"])
    checks["run_identity"] = type(bundle["run_id"]) is str and 0 < len(bundle["run_id"]) <= 128
    checks["authority_bound"] = bundle["authority_sha256"] == obs.digest(authority)
    checks["source_backend_bound"] = (bundle["source_sha256"] == authority["source_sha256"]
        and bundle["backend"] == authority["backend"])
    checks.update(prior.current_source_checks(authority["source_sha256"], authority["backend"]))
    checks["nominal_profile_bound"] = obs.digest(bundle["nominal_profile"]) == authority["nominal_profile_sha256"]
    nominal = vehicle(bundle["nominal_profile"], payload_count=authority["retained_count"])
    request = bundle["request"]
    request_keys = REQUEST_KEYS | {"guard_feedback"} if v2 else REQUEST_KEYS
    request_schema = REQUEST_V2_SCHEMA if v2 else "missionos.ship_sensor_observation.v1"
    if type(request) is not dict or set(request) != request_keys or request["schema"] != request_schema:
        raise ValueError("invalid_sensor_guard_request")
    if v2 and request["guard_feedback"] is not None and type(request["guard_feedback"]) is not dict:
        raise ValueError("invalid_sensor_guard_feedback")
    _reject_hidden_truth(request)
    receipt["request_sha256"] = obs.digest(request)
    issued, checked, application = request["issued_at_s"], bundle["checked_at_s"], bundle["application_time_s"]
    checks["request_identity"] = type(request["request_id"]) is str and bool(request["request_id"])
    checks["decision_and_application_clock"] = (all(_number(x) for x in
        (issued, checked, application, request["deadline_s"]))
        and issued <= checked < application <= request["deadline_s"]
        and application == authority["application_time_s"])
    action = bundle["selected_action"]
    checks["selected_physical_action"] = action in ACTIONS
    telemetry = bundle["accelerometer_telemetry"]
    checks["accelerometer_schema_and_causality"] = (type(telemetry) is list and bool(telemetry)
        and all(type(row) is dict and set(row) == prior.TELEMETRY_KEYS
            and _number(row["time_s"]) and row["phase"] == "deorbit_burn"
            and row["time_s"]+obs.SENSOR["telemetry_latency_s"] <= issued+1e-9
            and type(row["engine_throttle_states"]) is list
            and len(row["engine_throttle_states"]) == len(nominal.engines)
            and type(row["engine_available"]) is list and len(row["engine_available"]) == len(nominal.engines)
            and all(type(x) is bool for x in row["engine_available"])
            and all(_number(x) and 0 <= x <= 1 for x in row["engine_throttle_states"])
            and _number(row["fuel_gauge_kg"]) and row["fuel_gauge_kg"] >= 0
            and type(row["measured_specific_force_body_mps2"]) is list
            and len(row["measured_specific_force_body_mps2"]) == 3
            and all(_number(x) for x in row["measured_specific_force_body_mps2"]) for row in telemetry)
        and all(a["time_s"] < b["time_s"] for a, b in zip(telemetry, telemetry[1:])))
    if not checks["accelerometer_schema_and_causality"]:
        raise ValueError("invalid_accelerometer_telemetry")
    accel = obs.estimate(obs.prepare_telemetry(telemetry, nominal), issued)
    checks["accelerometer_estimate_recomputed"] = request["accelerometer_estimate"] == accel
    receipt["accelerometer_estimate_id"] = accel["estimate_id"]
    delivery = bundle["tracking_delivery"]
    tracked = request["tracking_observation"]
    observation = request["observation"]
    checks["tracking_request_flag"] = (type(observation) is dict
        and observation.get("phase") == "complete_burn"
        and type(observation.get("tracking_requested")) is bool)
    tracking = None
    if delivery is None:
        checks["tracking_observation_absent_until_delivery"] = tracked is None
        checks["tracking_delivery_provenance"] = action != "adopt_accelerometer_estimate"
    else:
        sensors.validate_delivery(delivery, issued)
        checks["tracking_delivery_provenance"] = (
            observation.get("tracking_requested") is True
            and delivery["source_sha256"] in authority["allowed_tracking_source_sha256"]
            and obs.digest(delivery["samples"]) in authority["allowed_tracking_samples_sha256"]
            and delivery["requested_at_s"] <= delivery["received_at_s"] <= issued)
        tracking = sensors.estimate_tracking(delivery, nominal, issued)
        checks["tracking_estimate_recomputed"] = tracked == tracking
        receipt["tracking_estimate_id"] = tracking["estimate_id"]
        receipt["tracking_delivery_id"] = delivery["delivery_id"]
    if v2:
        checks["physical_tracking_required"] = (delivery is not None
            and observation.get("tracking_requested") is True)
        tracking_band = tracking["band"] if tracking is not None else None
        checks["physical_tracking_estimate_bounded"] = (tracking is not None
            and tracking["status"] == "estimated" and type(tracking_band) is list
            and len(tracking_band) == 2 and all(_number(x) for x in tracking_band)
            and _number(tracking["factor_estimate"])
            and obs.FACTOR_RANGE[0] <= tracking_band[0] <= tracking["factor_estimate"]
                <= tracking_band[1] <= obs.FACTOR_RANGE[1]
            and (tracking_band[1]-tracking_band[0])/2 <= authority["max_band_half_width"])
    expected_references = {"accel_factor": accel["factor_estimate"],
        "accel_band_low": accel["band"][0] if accel["band"] else None,
        "accel_band_high": accel["band"][1] if accel["band"] else None,
        "accel_samples": accel["accepted_samples"], "tracking_delay_s": sensors.TRACKING["request_latency_s"],
        "application_time_s": application}
    if tracking is not None:
        expected_references.update(tracking_factor=tracking["factor_estimate"],
            tracking_band_low=tracking["band"][0] if tracking["band"] else None,
            tracking_band_high=tracking["band"][1] if tracking["band"] else None,
            tracking_intervals=tracking["accepted_samples"])
    checks["numeric_reference_values_recomputed"] = (type(request["evidence"]) is dict
        and all(key in request["evidence"] and request["evidence"][key] == value
                for key, value in expected_references.items())
        and (tracking is not None or not any(key.startswith("tracking_") and key != "tracking_delay_s"
                                           for key in request["evidence"])))
    if action == "adopt_accelerometer_estimate":
        factor, band = accel["factor_estimate"], accel["band"]
        checks["accelerometer_bounded_anomaly"] = (accel["anomaly"] is True
            and type(band) is list and len(band) == 2
            and all(_number(x) for x in (factor, *band, accel["half_width"]))
            and obs.FACTOR_RANGE[0] <= band[0] <= factor <= band[1] < 1.
            and 0 <= accel["half_width"] <= authority["max_band_half_width"])
        tracking_band = tracking["band"] if tracking is not None else None
        checks["independent_tracking_supports_adoption"] = (tracking is not None
            and tracking["status"] == "estimated" and type(tracking_band) is list
            and len(tracking_band) == 2 and all(_number(x) for x in tracking_band)
            and obs.FACTOR_RANGE[0] <= tracking_band[0] <= tracking["factor_estimate"] <= tracking_band[1] < 1.
            and (tracking_band[1]-tracking_band[0])/2 <= authority["max_band_half_width"]
            and max(band[0], tracking_band[0]) <= min(band[1], tracking_band[1]))
        receipt["factor_estimate"] = factor
    else:
        # This admission is permission to leave the previously authorized
        # controller unchanged, not a prediction or guarantee of recovery.
        receipt["factor_estimate"] = 1.
        checks["nominal_choice_respected"] = action == "continue_nominal_assumption"
    evidence_check = _check_evidence_v2 if v2 else _check_evidence
    evidence_check(bundle["evidence"], authority, receipt, checks, errors,
                   nominal_profile=bundle["nominal_profile"], tracking=tracking)


def _check_evidence(evidence, authority, receipt, checks, errors, *, nominal_profile, tracking):
    registered = authority["counterfactual_evidence"]
    checks["registered_counterfactual_schema"] = (type(registered) is list and bool(registered)
        and all(type(row) is dict and set(row) == EVIDENCE_KEYS and type(row["evidence_id"]) is str
            and bool(row["evidence_id"]) and row["action"] in ACTIONS and _hash(row["record_sha256"])
            and all(_number(row[k]) for k in ("assumed_factor", "sampled_capability_factor", "application_time_s"))
            for row in registered)
        and len({row["evidence_id"] for row in registered}) == len(registered))
    if not checks["registered_counterfactual_schema"] or type(evidence) is not list:
        raise ValueError("invalid_registered_counterfactual_evidence")
    identities, supported = set(), False
    for index, item in enumerate(evidence):
        prefix = f"counterfactual_{index}"
        if type(item) is not dict or set(item) != {"evidence_id", "record"}:
            raise ValueError("invalid_counterfactual_record_wrapper")
        identity = item["evidence_id"]
        metadata = next((row for row in registered if row["evidence_id"] == identity), None)
        checks[prefix+"_registered_once"] = metadata is not None and identity not in identities
        identities.add(identity)
        if metadata is None:
            raise ValueError("counterfactual_not_registered")
        record = item["record"]
        checks[prefix+"_record_bound"] = (obs.digest(record) == metadata["record_sha256"]
            and obs.digest(record["initial_state"]) == authority["origin_state_sha256"]
            and record["plant_factor"] == metadata["sampled_capability_factor"])
        switch = record["switch"]
        checks[prefix+"_switch_bound"] = (switch is None and metadata["action"] == "continue_nominal_assumption"
            and metadata["assumed_factor"] == 1. or type(switch) is dict
            and metadata["action"] == "adopt_accelerometer_estimate"
            and switch["factor"] == metadata["assumed_factor"]
            and abs(switch["time_s"]-metadata["application_time_s"]) <= 1e-8)
        stronger = prior.stronger_record_checks(record, nominal_profile, authority["retained_count"])
        verified = obs.verify_record(record, authority["contact_limits"])
        checks[prefix+"_saved_record_valid"] = stronger["passed"] and verified["record_valid"]
        if not stronger["passed"]:
            errors.append(prefix+": "+",".join(k for k, v in stronger["checks"].items() if not v))
        # A failed continue or mismatched hypothesis remains valid negative
        # evidence; it does not invalidate the whole registered experiment.
        if (receipt["selected_action"] == "adopt_accelerometer_estimate"
                and metadata["action"] == "adopt_accelerometer_estimate"
                and metadata["assumed_factor"] == receipt["factor_estimate"]
                and abs(metadata["application_time_s"]-receipt["application_time_s"]) <= 1e-8
                and tracking is not None and tracking["band"] is not None
                and tracking["band"][0] <= metadata["sampled_capability_factor"] <= tracking["band"][1]):
            metrics = prior.contact_metrics(record["outcome"]["contact_receipt"], record["final_state"])
            limits_met = (metrics == verified["contact"] and verified["contact_limits_met"]
                and all(metrics[k] <= authority["contact_limits"][k] for k in
                    ("speed_mps", "tilt_deg", "body_rate_rad_s"))
                and metrics["reserve_kg"] >= authority["contact_limits"]["reserve_kg"])
            checks[prefix+"_adoption_contact_limits"] = limits_met
            supported |= limits_met
            receipt["evidence_ids"].append(identity)
    checks["registered_discrete_adoption_supported"] = (
        receipt["selected_action"] != "adopt_accelerometer_estimate" or supported)


def _check_evidence_v2(evidence, authority, receipt, checks, errors, *, nominal_profile, tracking):
    """Check every registered outcome, including negative and other-branch evidence.

    Compatibility comes only from the delivered tracking band and preregistered
    sampled factors. Case names or the runtime physical capability are never used
    to select a record. Passing covers discrete saved points, not the intervening
    capability range, a new physical rollout, or an operational safety guarantee.
    """
    registered = authority["counterfactual_evidence"]
    checks["registered_counterfactual_schema"] = (type(registered) is list and len(registered) == 4
        and all(type(row) is dict and set(row) == EVIDENCE_KEYS and type(row["evidence_id"]) is str
            and bool(row["evidence_id"]) and row["action"] in ACTIONS and _hash(row["record_sha256"])
            and all(_number(row[k]) for k in ("assumed_factor", "sampled_capability_factor", "application_time_s"))
            and obs.FACTOR_RANGE[0] <= row["assumed_factor"] <= obs.FACTOR_RANGE[1]
            and obs.FACTOR_RANGE[0] <= row["sampled_capability_factor"] <= obs.FACTOR_RANGE[1]
            for row in registered)
        and len({row["evidence_id"] for row in registered}) == len(registered))
    if not checks["registered_counterfactual_schema"] or type(evidence) is not list:
        raise ValueError("invalid_v2_registered_counterfactual_evidence")
    factors = {row["sampled_capability_factor"] for row in registered}
    checks["registered_counterfactual_action_pairs_complete"] = (len(factors) == 2
        and all(sorted(row["action"] for row in registered if row["sampled_capability_factor"] == factor)
                == sorted(ACTIONS) for factor in factors))
    for item in evidence:
        if type(item) is not dict or set(item) != {"evidence_id", "record"}:
            raise ValueError("invalid_v2_counterfactual_record_wrapper")
    identities = [item["evidence_id"] for item in evidence]
    checks["all_registered_counterfactual_records_present"] = (
        len(identities) == len(registered) and len(set(identities)) == len(identities)
        and set(identities) == {row["evidence_id"] for row in registered})
    evaluated, seen = {}, set()
    for index, item in enumerate(evidence):
        prefix = f"counterfactual_{index}"
        identity = item["evidence_id"]
        metadata = next((row for row in registered if row["evidence_id"] == identity), None)
        checks[prefix+"_registered_once"] = metadata is not None and identity not in seen
        seen.add(identity)
        if metadata is None:
            raise ValueError("v2_counterfactual_not_registered")
        record = item["record"]
        checks[prefix+"_record_bound"] = (obs.digest(record) == metadata["record_sha256"]
            and obs.digest(record["initial_state"]) == authority["origin_state_sha256"]
            and record["plant_factor"] == metadata["sampled_capability_factor"])
        switch = record["switch"]
        checks[prefix+"_switch_bound"] = (switch is None and metadata["action"] == "continue_nominal_assumption"
            and metadata["assumed_factor"] == 1. or type(switch) is dict
            and metadata["action"] == "adopt_accelerometer_estimate"
            and switch["factor"] == metadata["assumed_factor"]
            and abs(switch["time_s"]-metadata["application_time_s"]) <= 1e-8)
        cutoffs = [row["time_s"] for row in record["events"] if row["event"] == "deorbit_cutoff_command"]
        updates = [row["time_s"] for row in record["control_capability_audit"]["all_boundary_calls"]
                   if row["boundary"] == "update"]
        complete = min((t for t in updates if cutoffs and t >= cutoffs[0]+2.-1e-9), default=None)
        checks[prefix+"_application_schedule_reconstructed"] = (len(cutoffs) == 1
            and complete is not None and abs(metadata["application_time_s"]-complete-600.) <= 1e-8)
        stronger = prior.stronger_record_checks(record, nominal_profile, authority["retained_count"])
        verified = obs.verify_record(record, authority["contact_limits"])
        checks[prefix+"_saved_record_valid"] = stronger["passed"] and verified["record_valid"]
        if not stronger["passed"]:
            errors.append(prefix+": "+",".join(k for k, v in stronger["checks"].items() if not v))
        metrics = prior.contact_metrics(record["outcome"]["contact_receipt"], record["final_state"])
        checks[prefix+"_contact_arithmetic_matches"] = metrics == verified["contact"]
        limits_met = (verified["contact_limits_met"]
            and all(metrics[k] <= authority["contact_limits"][k] for k in
                    ("speed_mps", "tilt_deg", "body_rate_rad_s"))
            and metrics["reserve_kg"] >= authority["contact_limits"]["reserve_kg"])
        evaluated[identity] = {"contact_limits_met": bool(limits_met)}
    band = tracking["band"] if tracking is not None and tracking["status"] == "estimated" else None
    compatible = [row for row in registered if band is not None
        and band[0] <= row["sampled_capability_factor"] <= band[1]]
    selected = [row for row in compatible if row["action"] == receipt["selected_action"]]
    checks["compatible_discrete_capability_points_present"] = bool(compatible)
    checks["compatible_action_evidence_present"] = bool(selected)
    checks["compatible_action_evidence_complete"] = (bool(selected)
        and all(row["evidence_id"] in evaluated for row in selected))
    checks["compatible_action_assumption_and_application_match"] = (bool(selected)
        and all(row["assumed_factor"] == receipt["factor_estimate"]
            and abs(row["application_time_s"]-receipt["application_time_s"]) <= 1e-8 for row in selected))
    checks["compatible_action_contact_limits_met"] = (bool(selected)
        and all(evaluated.get(row["evidence_id"], {}).get("contact_limits_met") is True for row in selected))
    receipt["evidence_ids"] = [row["evidence_id"] for row in selected]


def _reject_hidden_truth(value):
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str or any(word in key.lower() for word in
                    ("actual", "truth", "true_", "seed", "fault_label", "fault_name", "physical_factor")):
                raise ValueError("truth_or_seed_in_sensor_request")
            _reject_hidden_truth(child)
    elif type(value) is list:
        for child in value:
            _reject_hidden_truth(child)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if any(any(part in key.upper() for part in ("API_KEY", "SECRET", "TOKEN", "CREDENTIAL"))
               for key in os.environ):
            raise ValueError("credential_forbidden_in_sensor_guard")
        text = args.input.read_text() if args.input else sys.stdin.read()
        value = json.loads(text, object_pairs_hook=_strict_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json_constant")))
        if type(value) is not dict or set(value) != {"bundle", "authority"}:
            raise ValueError("invalid_sensor_guard_payload")
        result = check_bundle(value["bundle"], value["authority"])
    except (ValueError, TypeError, OSError) as exc:
        result = {"schema": RECEIPT_SCHEMA, "passed": False, "checks": {"input_valid": False},
                  "errors": [str(exc)], "scope": SCOPE}
    rendered = json.dumps(result, sort_keys=True, allow_nan=False)+"\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        sys.stdout.write(rendered)
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
