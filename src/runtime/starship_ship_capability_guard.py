"""Credential-free admission predicates for one experimental capability switch.

A sampled-band rollout is a shared-model prediction, not an independent physical
qualification. This checker independently binds telemetry, authority, snapshot,
source and saved prediction records. It never advances physics or calls a model.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_ship_capability_observation as obs
from .starship_mission_director import fuel_sensor
from .starship_return_feasibility import backend
from .starship_sixdof_mission import vehicle

BUNDLE_SCHEMA = "missionos.ship_capability_guard_bundle.v1"
RECEIPT_SCHEMA = "missionos.ship_capability_guard_receipt.v1"
AUTHORITY_SCHEMA = "missionos.ship_capability_step3_authority.v1"
SCOPE = "sampled_estimate_band_points_not_continuous_qualification"
BUNDLE_KEYS = {"schema", "run_id", "authority_sha256", "request", "telemetry", "nominal_profile",
               "retained_count", "snapshot", "predictions", "source_sha256", "backend", "checked_at_s",
               "application_time_s"}
SNAPSHOT_KEYS = {"state", "phase", "return_status", "switch", "controller_history_sha256"}
PREDICTION_KEYS = {"label", "factor", "record", "record_sha256", "snapshot_sha256", "application_time_s"}
TELEMETRY_KEYS = {"time_s", "phase", "measured_specific_force_body_mps2", "engine_throttle_states",
                  "engine_available", "fuel_gauge_kg"}
CONTACT_LIMITS = {"speed_mps": 5., "tilt_deg": 5., "body_rate_rad_s": .02, "reserve_kg": 28000.}
ROOT = Path(__file__).resolve().parents[2]


def current_source_checks(source_sha256, expected_backend):
    """Read actual repository bytes and installed backend in this checker process."""
    checks = {"source_paths_safe": False, "checker_source_hashes_match": False,
              "checker_installed_backend_matches": backend() == expected_backend}
    try:
        if type(source_sha256) is not dict or not source_sha256:
            return checks
        actual = {}
        for name, identity in source_sha256.items():
            if type(name) is not str or not name or not _hash(identity):
                return checks
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                return checks
            path = (ROOT/relative).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():
                return checks
            actual[name] = sha256(path.read_bytes()).hexdigest()
        checks["source_paths_safe"] = True
        checks["checker_source_hashes_match"] = actual == source_sha256
    except (OSError, ValueError):
        pass
    return checks


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return type(value) is str and len(value) == 64 and all(x in "0123456789abcdef" for x in value)


def _finite_tree(value):
    if type(value) is float and not math.isfinite(value):
        raise ValueError("nonfinite_json_value")
    if type(value) is dict:
        for item in value.values():
            _finite_tree(item)
    elif type(value) is list:
        for item in value:
            _finite_tree(item)


def contact_metrics(contact, final):
    """Separate arithmetic reconstruction of contact speed, tilt and reserve."""
    point = contact["point_eci_m"]
    normal = contact["surface_normal_eci"]
    quaternion = contact["q_body_to_eci"]
    if (len(point) != 3 or len(normal) != 3 or len(quaternion) != 4
            or len(contact["point_velocity_eci_mps"]) != 3 or len(final["omega_body_rad_s"]) != 3
            or not all(_number(x) for vector in (point, normal, quaternion,
                contact["point_velocity_eci_mps"], final["omega_body_rad_s"]) for x in vector)
            or abs(sum(x*x for x in normal)-1.) > 1e-8
            or abs(sum(x*x for x in quaternion)-1.) > 1e-8):
        raise ValueError("invalid_contact_pose_or_normal")
    surface = [-env.EARTH_ROTATION_RAD_S*point[1], env.EARTH_ROTATION_RAD_S*point[0], 0.]
    relative = [a-b for a, b in zip(contact["point_velocity_eci_mps"], surface)]
    w, x, y, z = contact["q_body_to_eci"]
    longitudinal = [2*(x*z+w*y), 2*(y*z-w*x), 1-2*(x*x+y*y)]
    cosine = sum(a*b for a, b in zip(longitudinal, contact["surface_normal_eci"]))
    return {"time_s": contact["event_time_s"], "speed_mps": math.sqrt(sum(x*x for x in relative)),
            "tilt_deg": math.degrees(math.acos(max(-1., min(1., cosine)))),
            "body_rate_rad_s": math.sqrt(sum(x*x for x in final["omega_body_rad_s"])),
            "reserve_kg": contact["propellant_kg"]}


def stronger_record_checks(record, profile, retained_count):
    """Rebuild limits and restore stage-1.5 ordered-route/witness checks.

    Actual capability in a prediction is its explicitly estimated band point.
    A flight's true capability may enter this post-run audit, never a model input.
    """
    checks = {}
    try:
        _finite_tree(record)
        craft = vehicle(profile, payload_count=retained_count)
        nominal = [e.max_thrust_n for e in craft.engines]
        # The sample producer filters engine_loads by name: RCS has complete
        # actuator state/audit entries, but is absent from main_engine_thrust_n.
        main_indices = [i for i, engine in enumerate(craft.engines) if "main" in engine.name]
        checks["rollout_schema_valid"] = record["schema"] == "missionos.ship_fault_step2_rollout.v1"
        checks["main_engine_sample_layout"] = len(main_indices) == 6 and set(obs.MAIN) <= set(main_indices)
        factor = record["plant_factor"]
        switch = record["switch"]
        audit = record["control_capability_audit"]
        rows = audit["all_boundary_calls"]
        actual = [x*factor if i in obs.MAIN else x for i, x in enumerate(nominal)]
        checks["nominal_limits_reconstructed"] = record["nominal_engine_limits_n"] == nominal
        checks["plant_limits_reconstructed"] = (record["plant_engine_limits_n"] == actual
            and audit["actual_engine_limits_n"] == actual)
        checks["initial_control_model_reconstructed"] = audit["initial_assumed_engine_limits_n"] == nominal
        switches = audit["switches"]
        if switch is None:
            checks["switches_bound"] = switches == []
            split, adopted = len(rows), nominal
        else:
            adopted = [x*switch["factor"] if i in obs.MAIN else x for i, x in enumerate(nominal)]
            checks["switches_bound"] = (len(switches) == 1 and switches[0]["engine_limits_n"] == adopted
                and switches[0]["time_s"] == switch["time_s"] and type(switches[0]["row_index"]) is int
                and 0 <= switches[0]["row_index"] < len(rows))
            split = switches[0]["row_index"] if switches else -1
        checks["audit_digest_reconstructed"] = audit["trace_sha256"] == obs.digest(rows)
        checks["audit_time_ordered"] = (bool(rows) and rows[0]["time_s"] == record["initial_state"]["time_s"]
            and all(a["time_s"] <= b["time_s"] for a, b in zip(rows, rows[1:]))
            and rows[-1]["time_s"] < record["final_state"]["time_s"])
        checks["switch_precedes_contact"] = switch is None or switch["time_s"] < record["final_state"]["time_s"]
        position, steps, ordered, limits_ok, split_valid = 0, 0, True, True, split in (0, len(rows))
        while position < len(rows):
            start = position
            block = rows[position:position+2]
            if [r["boundary"] for r in block] != ["update", "capability_profile"]:
                ordered = False
                break
            position += 2
            if position < len(rows) and rows[position]["boundary"] == "command":
                block += rows[position:position+4]
                if [r["boundary"] for r in block[2:]] != ["command", "capability_profile", "tvc_control", "plant"]:
                    ordered = False
                    break
                position += 4
                steps += 1
            if any(r["time_s"] != block[0]["time_s"] for r in block):
                ordered = False
            if split == start:
                split_valid = True
                if switch is not None:
                    split_valid &= block[0]["time_s"] == switch["time_s"]
            if start < split < position:
                split_valid = False
            for index, row in enumerate(block, start):
                expected = actual if row["boundary"] == "plant" else nominal if index < split else adopted
                limits_ok &= (row["engine_limits_n"] == expected
                    and row["is_actual_plant_object"] is (row["boundary"] == "plant"))
                if row["boundary"] in ("capability_profile", "tvc_control"):
                    limits_ok &= row["profile_main_limit_n"] == expected[0]
        checks["ordered_model_to_plant_route"] = ordered and steps == record["outcome"]["integration_steps"]
        checks["switch_at_control_step_boundary"] = split_valid
        checks["all_boundary_capabilities_reconstructed"] = limits_ok
        update_times = {r["time_s"] for r in rows if r["boundary"] == "update"}
        provenance, thrust, sample_shape = True, True, True
        for sample in record["samples"]:
            states = sample["engine_states"]
            main_thrust = sample["main_engine_thrust_n"]
            shape = (type(states) is list and len(states) == len(nominal)
                and type(main_thrust) is list and len(main_thrust) == len(main_indices)
                and all(_number(value) and value >= 0 for value in main_thrust)
                and all(type(engine) is dict and set(engine) == {f.name for f in fields(dyn.EngineState)}
                        for engine in states))
            sample_shape &= shape
            if not shape:
                continue
            for engine in states:
                dyn.EngineState(**engine)  # Validate every main and RCS actuator state.
            retained, trim = sample.get("retained_return"), sample.get("entry_trim")
            witness = retained is not None or trim is not None
            provenance &= (retained in (None, "activation", "previous", "trigger")
                and trim in (None, "preparation") and not (retained is not None and trim is not None))
            if witness:
                phase = "orbital_coast" if retained == "activation" else "ballistic_return"
                provenance &= sample["time_s"] in update_times and sample["phase"] == phase
            modeled = adopted if switch is not None and sample["time_s"] >= switch["time_s"] else nominal
            limits = modeled if witness else actual
            for slot, i in enumerate(main_indices):
                engine = states[i]
                expected = limits[i]*engine["throttle"] if engine["available"] and sample["propellant_kg"] > 0 else 0.
                thrust &= math.isclose(main_thrust[slot], expected, rel_tol=1e-9, abs_tol=1e-6)
        checks["all_actuator_states_and_main_thrust_shape"] = sample_shape
        checks["controller_witness_provenance"] = provenance
        checks["sample_thrust_source_reconstructed"] = thrust
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        checks["record_structure_valid"] = False
        return {"passed": False, "checks": checks, "errors": [f"{type(exc).__name__}: {exc}"]}
    return {"passed": bool(checks) and all(checks.values()), "checks": checks, "errors": []}


def check_bundle(bundle, authority):
    """Bind an experimental switch to causal evidence and saved predictions.

    Failure returns a rejection receipt; it never raises into a flight loop.
    The caller authenticates the authority/IPC and consumes a receipt once.
    """
    checks, errors = {}, []
    receipt = {"schema": RECEIPT_SCHEMA, "passed": False, "checks": checks, "errors": errors,
               "run_id": None, "authority_sha256": None, "request_sha256": None, "estimate_id": None,
               "snapshot_sha256": None, "application_time_s": None, "factor_estimate": None,
               "prediction_record_sha256": [], "scope": SCOPE}
    try:
        _finite_tree(bundle)
        _finite_tree(authority)
        if type(bundle) is not dict or set(bundle) != BUNDLE_KEYS or bundle["schema"] != BUNDLE_SCHEMA:
            raise ValueError("invalid_bundle_schema")
        required = {"schema", "contact_limits", "application_delay_s", "decision_deadline_after_issue_s",
                    "max_band_half_width", "source_sha256", "backend", "scope", "fallback",
                    "nominal_profile_sha256", "retained_count"}
        checks["authority_schema"] = (type(authority) is dict and required <= set(authority)
            and authority["schema"] == AUTHORITY_SCHEMA and authority["contact_limits"] == CONTACT_LIMITS
            and authority["application_delay_s"] == 600. and authority["decision_deadline_after_issue_s"] == 900.
            and authority["max_band_half_width"] == .02 and authority["scope"] == "experimental_6dof_only"
            and authority["fallback"] == "adopt_checked_estimate"
            and _hash(authority["nominal_profile_sha256"]) and type(authority["retained_count"]) is int)
        receipt["run_id"] = bundle["run_id"]
        receipt["authority_sha256"] = bundle["authority_sha256"]
        checks["run_identity"] = type(bundle["run_id"]) is str and 0 < len(bundle["run_id"]) <= 128
        checks["authority_bound"] = bundle["authority_sha256"] == obs.digest(authority)
        checks["source_backend_bound"] = (bundle["source_sha256"] == authority["source_sha256"]
            and bundle["backend"] == authority["backend"] and type(bundle["source_sha256"]) is dict
            and bool(bundle["source_sha256"]) and all(_hash(x) for x in bundle["source_sha256"].values()))
        checks.update(current_source_checks(authority["source_sha256"], authority["backend"]))
        request = obs.validate_request(bundle["request"])
        receipt["request_sha256"] = obs.digest(request)
        current = request["capability_estimate"]
        receipt["estimate_id"] = current["estimate_id"]
        receipt["factor_estimate"] = current["factor_estimate"]
        issued, application, checked = request["issued_at_s"], bundle["application_time_s"], bundle["checked_at_s"]
        receipt["application_time_s"] = application
        checks["complete_post_burn_request"] = (request["burn_data_complete"] is True
            and request["trigger"] == "burn_data_complete"
            and request["observation"]["phase"] == "ballistic_return"
            and request["allowed_actions"] == ["continue_nominal_assumption", "adopt_estimated_capability"]
            and request["effects"] == {a: obs.EFFECTS[a] for a in request["allowed_actions"]})
        checks["decision_and_application_clock"] = (all(_number(x) for x in (issued, application, checked))
            and request["deadline_s"] == issued+authority["decision_deadline_after_issue_s"]
            and abs(application-issued-authority["application_delay_s"]) <= 1e-8
            and issued <= checked < application <= request["deadline_s"])
        factor, band, half = current["factor_estimate"], current["band"], current["half_width"]
        checks["bounded_capability_estimate"] = (type(band) is list and len(band) == 2
            and all(_number(x) for x in [factor, *band, half]) and current["anomaly"] is True
            and current["accepted_samples"] >= obs.ESTIMATOR["minimum_samples"]
            and obs.FACTOR_RANGE[0] <= band[0] <= factor <= band[1] <= obs.FACTOR_RANGE[1]
            and 0 <= half <= authority["max_band_half_width"])
        retained = bundle["retained_count"]
        nominal = vehicle(bundle["nominal_profile"], payload_count=retained)
        checks["retained_count_bound"] = (type(retained) is int and 0 <= retained <= bundle["nominal_profile"]["payload"]["count"])
        checks["nominal_profile_authority_bound"] = authority["nominal_profile_sha256"] == obs.digest(bundle["nominal_profile"])
        checks["retained_count_authority_bound"] = authority["retained_count"] == retained
        telemetry = bundle["telemetry"]
        checks["telemetry_schema_and_causality"] = (type(telemetry) is list and bool(telemetry)
            and all(type(r) is dict and set(r) == TELEMETRY_KEYS and _number(r["time_s"])
                and r["phase"] in ("deorbit_burn", "ballistic_return")
                and r["time_s"]+obs.SENSOR["telemetry_latency_s"] <= issued+1e-9
                and len(r["engine_throttle_states"]) == len(nominal.engines)
                and len(r["engine_available"]) == len(nominal.engines)
                and all(type(x) is bool for x in r["engine_available"])
                and all(_number(x) and 0 <= x <= 1 for x in r["engine_throttle_states"])
                and _number(r["fuel_gauge_kg"]) and r["fuel_gauge_kg"] >= 0
                and len(r["measured_specific_force_body_mps2"]) == 3
                and all(_number(x) for x in r["measured_specific_force_body_mps2"]) for r in telemetry)
            and all(a["time_s"] < b["time_s"] for a, b in zip(telemetry, telemetry[1:])))
        recalculated = obs.estimate(obs.prepare_telemetry(telemetry, nominal), issued)
        checks["estimate_recomputed_from_telemetry"] = recalculated == current
        checks["estimate_latency_bound"] = (current["data_through_s"] is not None
            and current["data_through_s"]+current["telemetry_latency_s"] <= issued+1e-9)
        snapshot = bundle["snapshot"]
        receipt["snapshot_sha256"] = obs.digest(snapshot)
        checks["snapshot_schema"] = type(snapshot) is dict and set(snapshot) == SNAPSHOT_KEYS
        state_value = snapshot["state"]
        checks["snapshot_state_schema"] = (type(state_value) is dict
            and set(state_value) == {f.name for f in fields(dyn.State6DOF)})
        state = dyn.state_from_dict(state_value)
        _, _, altitude = env.ecef_to_geodetic(env.eci_to_ecef(state.r_eci_m, state.time_s))
        row = request["observation"]
        checks["snapshot_matches_observation"] = (state.time_s == row["time_s"] == issued
            and snapshot["phase"] == row["phase"] == "ballistic_return"
            and snapshot["return_status"] == "active" and snapshot["switch"] is None
            and _hash(snapshot["controller_history_sha256"])
            and len(state.engine_states) == len(nominal.engines)
            and len(state.flap_angles_rad) == len(nominal.aero_panels)
            and fuel_sensor(state.propellant_kg, state.time_s, "ship") == row["fuel_gauge_kg"]
            and math.isclose(altitude, row["altitude_m"], abs_tol=1e-7, rel_tol=1e-12)
            and env.norm(state.v_eci_mps) == row["speed_mps"]
            and env.norm(state.omega_body_rad_s) == row["body_rate_rad_s"]
            and [state.engine_states[i].available for i in obs.MAIN] == row["landing_engines_available"])
        predictions = bundle["predictions"]
        if type(predictions) is not list or len(predictions) != 3:
            raise ValueError("exactly_three_prediction_points_required")
        expected = [("band_low", band[0]), ("point_estimate", factor), ("band_high", band[1])]
        for index, (prediction, (label, point_factor)) in enumerate(zip(predictions, expected)):
            prefix = f"prediction_{index}"
            checks[prefix+"_schema"] = type(prediction) is dict and set(prediction) == PREDICTION_KEYS
            record = prediction["record"]
            receipt["prediction_record_sha256"].append(prediction["record_sha256"])
            checks[prefix+"_bound"] = (prediction["label"] == label and prediction["factor"] == point_factor
                and prediction["record_sha256"] == obs.digest(record)
                and prediction["snapshot_sha256"] == receipt["snapshot_sha256"]
                and prediction["application_time_s"] == application
                and record["initial_state"] == state_value and record["plant_factor"] == point_factor
                and type(record["switch"]) is dict and set(record["switch"]) == {"factor", "time_s"}
                and record["switch"]["factor"] == factor
                and _number(record["switch"]["time_s"])
                and abs(record["switch"]["time_s"]-application) <= 1e-8)
            strong = stronger_record_checks(record, bundle["nominal_profile"], retained)
            checks[prefix+"_strong_record_valid"] = strong["passed"]
            verification = obs.verify_record(record, authority["contact_limits"])
            checks[prefix+"_saved_record_valid"] = verification["record_valid"]
            metrics = contact_metrics(record["outcome"]["contact_receipt"], record["final_state"])
            checks[prefix+"_contact_arithmetic_matches"] = metrics == verification["contact"]
            checks[prefix+"_contact_limits_met"] = (all(metrics[k] <= authority["contact_limits"][k]
                for k in ("speed_mps", "tilt_deg", "body_rate_rad_s"))
                and metrics["reserve_kg"] >= authority["contact_limits"]["reserve_kg"])
            if not strong["passed"]:
                errors.append(prefix+": "+','.join(k for k, v in strong["checks"].items() if not v))
        receipt["passed"] = bool(checks) and all(checks.values())
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        checks["bundle_structure_valid"] = False
    receipt["receipt_id"] = obs.digest(receipt)
    return receipt


def _strict_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.input.read_text() if args.input else sys.stdin.read()
        payload = json.loads(text, object_pairs_hook=_strict_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json_constant")))
        if type(payload) is not dict or set(payload) != {"bundle", "authority"}:
            raise ValueError("invalid_checker_payload")
        result = check_bundle(payload["bundle"], payload["authority"])
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
