"""Exact finite-point model evidence; never a flight certificate or permission.

The caller provides a frozen identity, a registered complete cell list, raw
records and trusted *code* callbacks for independent saved/current checks. The
callbacks must extract point preimages from those raw records; a producer's
``passed`` flag, point hashes, or callback name in JSON is not accepted.

This companion leaves the old 39-case V2 gate unchanged. A match establishes
only named, exact sampled model points. It proves no continuous uncertainty
envelope, authentication, human approval, dispatch or physical execution.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re

IDENTITY_SCHEMA = "missionos.starship_final_qualification_identity.v1"
CERTIFICATE_SCHEMA = "missionos.starship_restored_domain_qualification.v1"
CHECK_SCHEMA = "missionos.starship_final_qualification_checked_record.v1"
OBSERVATION_SCHEMA = "missionos.starship_final_qualification_current_record.v1"
POINT_SCHEMA = "missionos.starship_final_qualification_point.v1"
MATCH_SCHEMA = "missionos.starship_final_qualification_point_match.v1"
CONTACT_LIMITS = {"maximum_speed_mps": 5., "maximum_tilt_deg": 5.,
    "maximum_body_rate_rad_s": .02, "minimum_propellant_kg": 28000.}
MAXIMUM_RECORD_BYTES = 512 * 1024**2
MAXIMUM_POINT_BYTES = 32 * 1024**2
MAXIMUM_CELLS = 128
MAXIMUM_HISTORY_ROWS = 100000
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("final_qualification_finite_json_required") from error


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def _copy(value):
    return json.loads(canonical_bytes(value))


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _raw(value):
    _require(type(value) is bytes and 0 < len(value) <= MAXIMUM_RECORD_BYTES,
        "final_qualification_raw_record_required")
    return sha256(value).hexdigest()


def _record_json(raw):
    _raw(raw)
    def pairs(items):
        result = dict(items)
        _require(len(result) == len(items), "final_qualification_duplicate_raw_key")
        return result
    def invalid(_value):
        raise ValueError("final_qualification_nonfinite_raw_json")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("final_qualification_raw_json_required") from error
    canonical_bytes(value)
    return value


def _pointer(value, pointer):
    _require(type(pointer) is str and len(pointer) <= 1024
        and (pointer == "" or pointer.startswith("/")), "final_qualification_point_pointer")
    parts = pointer.split("/")[1:] if pointer else []
    _require(len(parts) <= 64, "final_qualification_point_pointer")
    for part in parts:
        _require(re.search(r"~(?![01])", part) is None, "final_qualification_point_pointer")
        part = part.replace("~1", "/").replace("~0", "~")
        if type(value) is dict:
            _require(part in value, "final_qualification_point_pointer_missing")
            value = value[part]
        elif type(value) is list:
            _require(re.fullmatch(r"0|[1-9][0-9]*", part) is not None and int(part) < len(value),
                "final_qualification_point_pointer_missing")
            value = value[int(part)]
        else:
            raise ValueError("final_qualification_point_pointer_missing")
    return value


def _bound_point(raw, report):
    pointers = report.get("point_pointers")
    _require(type(pointers) is dict and set(pointers) == {"state", "vehicle", "inventory", "actuator_history"},
        "final_qualification_point_pointers_required")
    record = _record_json(raw)
    extracted = {key: _pointer(record, path) for key, path in pointers.items()}
    history = extracted["actuator_history"]
    _require(type(history) is list and 1 <= len(history) <= MAXIMUM_HISTORY_ROWS,
        "final_qualification_actuator_history_required")
    normalized = []
    for row in history:
        _require(type(row) is dict, "final_qualification_actuator_preimages_required")
        state = row.get("state", row)
        _require(type(state) is dict and {"time_s", "engine_states", "flap_angles_rad"} <= state.keys(),
            "final_qualification_actuator_preimages_required")
        normalized.append({key: state[key] for key in ("time_s", "engine_states", "flap_angles_rad")})
    extracted["actuator_history"] = normalized
    _require(type(report.get("point_data")) is dict
        and canonical_bytes(report["point_data"]) == canonical_bytes(extracted),
        "final_qualification_point_preimage_not_from_raw_record")
    return make_point(extracted)


def make_identity(*, source_sha256, backend, profile_sha256, integration_policy_sha256):
    """Bind external source/backend facts without asserting files were read."""
    _require(type(source_sha256) is dict and 1 <= len(source_sha256) <= 4096,
        "final_qualification_source_inventory_required")
    for path, value in source_sha256.items():
        relative = PurePosixPath(path) if type(path) is str else None
        _require(relative is not None and path == relative.as_posix()
            and not relative.is_absolute() and ".." not in relative.parts
            and relative.parts and relative.parts[0] in ("src", "scripts", "packages", "examples", "docs", "tests")
            and _hash(value), "final_qualification_source_path_or_hash")
    fields = {"python", "platform", "numpy", "scipy", "executable_sha256"}
    _require(type(backend) is dict and set(backend) == fields
        and all(type(backend[key]) is str and 1 <= len(backend[key]) <= 512 for key in fields)
        and _hash(backend["executable_sha256"]) and _hash(profile_sha256)
        and _hash(integration_policy_sha256), "final_qualification_backend_profile_or_policy")
    return _copy({"schema": IDENTITY_SCHEMA, "source_sha256": source_sha256,
        "backend": backend, "profile_sha256": profile_sha256,
        "integration_policy_sha256": integration_policy_sha256})


def _identity(value):
    _require(type(value) is dict and set(value) == {"schema", "source_sha256", "backend",
        "profile_sha256", "integration_policy_sha256"} and value["schema"] == IDENTITY_SCHEMA,
        "final_qualification_identity_schema")
    expected = make_identity(**{key: value[key] for key in value if key != "schema"})
    _require(canonical_bytes(value) == canonical_bytes(expected), "final_qualification_identity_changed")
    return expected


def _domain(value):
    fields = {"ascent_condition", "retained_count", "released_count", "return_candidate_id",
        "fault_family", "plant_capability_factor", "sensor_gain", "controller_capability_factor"}
    _require(type(value) is dict and set(value) == fields
        and type(value["ascent_condition"]) is str and _NAME.fullmatch(value["ascent_condition"])
        and type(value["retained_count"]) is int and 0 <= value["retained_count"] <= 26
        and type(value["released_count"]) is int and value["released_count"]+value["retained_count"] == 26
        and value["return_candidate_id"] in ("nominal", "next_orbit")
        and value["fault_family"] in ("nominal", "return_propulsion_80", "return_sensor_gain_80")
        and all(_number(value[key]) and 0 < value[key] <= 1.1 for key in (
            "plant_capability_factor", "sensor_gain", "controller_capability_factor")),
        "final_qualification_finite_domain_required")
    factors = {"nominal": (1., 1.), "return_propulsion_80": (.8, 1.),
        "return_sensor_gain_80": (1., .8)}[value["fault_family"]]
    _require((value["plant_capability_factor"], value["sensor_gain"]) == factors,
        "final_qualification_fault_domain_inconsistent")
    return _copy(value)


def _cells(value):
    _require(type(value) is list and 1 <= len(value) <= MAXIMUM_CELLS,
        "final_qualification_registered_cells_required")
    ids, result = set(), []
    for row in value:
        _require(type(row) is dict and set(row) == {"cell_id", "domain"}
            and type(row["cell_id"]) is str and _NAME.fullmatch(row["cell_id"])
            and row["cell_id"] not in ids, "final_qualification_cell_identity")
        ids.add(row["cell_id"])
        result.append({"cell_id": row["cell_id"], "domain": _domain(row["domain"])})
    return result


def _vector(value, size):
    return type(value) is list and len(value) == size and all(_number(x) for x in value)


def _actuators(engines, flaps):
    _require(type(engines) is list and 1 <= len(engines) <= 128
        and type(flaps) is list and len(flaps) <= 128 and all(_number(x) for x in flaps),
        "final_qualification_actuator_preimages_required")
    for item in engines:
        _require(type(item) is dict and set(item) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
            and type(item["available"]) is bool and _number(item["throttle"]) and 0 <= item["throttle"] <= 1
            and all(_number(item[key]) for key in ("gimbal_x_rad", "gimbal_y_rad")),
            "final_qualification_actuator_preimages_required")


def make_point(value):
    """Hash validated state/vehicle/inventory/actuator *preimages*, not hashes."""
    _require(type(value) is dict and set(value) == {"state", "vehicle", "inventory", "actuator_history"}
        and len(canonical_bytes(value)) <= MAXIMUM_POINT_BYTES,
        "final_qualification_point_preimages_required")
    state, vehicle, inventory, history = (value[key] for key in ("state", "vehicle", "inventory", "actuator_history"))
    state_fields = {"time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
        "propellant_kg", "engine_states", "flap_angles_rad"}
    _require(type(state) is dict and set(state) == state_fields
        and _number(state["time_s"]) and 0 <= state["time_s"] <= 16000.
        and _number(state["propellant_kg"]) and state["propellant_kg"] >= 0
        and all(_vector(state[key], size) for key, size in (
            ("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)))
        and abs(sum(x*x for x in state["q_body_to_eci"])-1.) <= 1e-6,
        "final_qualification_rigid_state_required")
    _actuators(state["engine_states"], state["flap_angles_rad"])
    _require(type(vehicle) is dict and vehicle and type(vehicle.get("engines")) is list
        and len(vehicle["engines"]) == len(state["engine_states"])
        and type(vehicle.get("aero_panels")) is list and len(vehicle["aero_panels"]) == len(state["flap_angles_rad"]),
        "final_qualification_actual_vehicle_required")
    _require(type(inventory) is dict and type(inventory.get("total_count")) is int
        and inventory["total_count"] == 26
        and all(type(inventory.get(key)) is int and 0 <= inventory[key] <= 26
            for key in ("retained_count", "released_count"))
        and inventory["retained_count"]+inventory["released_count"] == 26
        and type(inventory.get("release_receipts")) is list
        and len(inventory["release_receipts"]) == inventory["released_count"],
        "final_qualification_actual_inventory_required")
    _require(type(history) is list and 1 <= len(history) <= MAXIMUM_HISTORY_ROWS,
        "final_qualification_actuator_history_required")
    previous = -1.
    for row in history:
        _require(type(row) is dict and set(row) == {"time_s", "engine_states", "flap_angles_rad"}
            and _number(row["time_s"]) and 0 <= row["time_s"] and previous < row["time_s"] <= state["time_s"],
            "final_qualification_actuator_history_clock")
        _actuators(row["engine_states"], row["flap_angles_rad"])
        _require(len(row["engine_states"]) == len(state["engine_states"])
            and len(row["flap_angles_rad"]) == len(state["flap_angles_rad"]),
            "final_qualification_actuator_history_dimensions")
        previous = row["time_s"]
    _require(history[-1] == {key: state[key] for key in ("time_s", "engine_states", "flap_angles_rad")},
        "final_qualification_actuator_history_endpoint")
    return {"schema": POINT_SCHEMA, **{key+"_sha256": digest(value[key]) for key in value},
        "time_s": state["time_s"], "retained_count": inventory["retained_count"],
        "released_count": inventory["released_count"]}


def _checked(raw, identity, cell, verify_record):
    _require(callable(verify_record), "final_qualification_independent_checker_callback_required")
    raw_sha = _raw(raw)
    result = verify_record(raw, expected_identity=_copy(identity), expected_cell=_copy(cell))
    fields = {"schema", "record_sha256", "identity_sha256", "cell_id", "domain", "point_data", "point_pointers",
        "record_checks_passed", "actual_state_continuity_verified", "controller_plant_separation_verified",
        "prediction_only", "contact", "new_flights_during_check", "new_model_calls_during_check"}
    _require(type(result) is dict and set(result) == fields and result["schema"] == CHECK_SCHEMA
        and result["record_sha256"] == raw_sha and result["identity_sha256"] == digest(identity)
        and result["cell_id"] == cell["cell_id"] and _domain(result["domain"]) == cell["domain"]
        and all(type(result[key]) is bool for key in ("record_checks_passed", "actual_state_continuity_verified",
            "controller_plant_separation_verified", "prediction_only"))
        and all(type(result[key]) is int and result[key] == 0 for key in (
            "new_flights_during_check", "new_model_calls_during_check")),
        "final_qualification_independent_record_binding")
    point = _bound_point(raw, result)
    _require(all(point[key] == cell["domain"][key] for key in ("retained_count", "released_count")),
        "final_qualification_point_inventory_domain")
    contact, contact_passed = result["contact"], False
    if contact is not None:
        _require(type(contact) is dict and set(contact) == {"speed_mps", "tilt_deg", "body_rate_rad_s", "propellant_kg"}
            and all(_number(x) and x >= 0 for x in contact.values()), "final_qualification_contact_shape")
        contact_passed = (contact["speed_mps"] <= CONTACT_LIMITS["maximum_speed_mps"]
            and contact["tilt_deg"] <= CONTACT_LIMITS["maximum_tilt_deg"]
            and contact["body_rate_rad_s"] <= CONTACT_LIMITS["maximum_body_rate_rad_s"]
            and contact["propellant_kg"] >= CONTACT_LIMITS["minimum_propellant_kg"])
    passed = (result["record_checks_passed"] and result["actual_state_continuity_verified"]
        and result["controller_plant_separation_verified"] and not result["prediction_only"] and contact_passed)
    return {"cell_id": cell["cell_id"], "domain": cell["domain"], "record_sha256": raw_sha,
        "point": point, "independent_check_sha256": digest(result), "contact": _copy(contact),
        "cell_passed": passed}


def build_certificate(*, expected_identity, required_cells, records_raw, verify_record):
    """Recheck all declared cells. Negative cells stay present and incomplete."""
    identity, cells = _identity(expected_identity), _cells(required_cells)
    _require(type(records_raw) is dict and set(records_raw) == {row["cell_id"] for row in cells},
        "final_qualification_complete_raw_cell_census_required")
    checked = [_checked(records_raw[cell["cell_id"]], identity, cell, verify_record) for cell in cells]
    return {"schema": CERTIFICATE_SCHEMA, "identity": identity, "registered_cells_sha256": digest(cells),
        "required_cell_ids": [cell["cell_id"] for cell in cells], "cells": checked,
        "qualification_scope": "ship_return_exact_model_points_only",
        "qualification_complete": all(row["cell_passed"] for row in checked),
        "contact_limits": _copy(CONTACT_LIMITS), "domain": "named_exact_finite_model_points_only",
        "continuous_uncertainty_envelope_verified": False, "old39_certificate_reused": False,
        "real_vehicle_validated": False, "physical_recovery_certified": False,
        "human_approval_inferred": False, "dispatch_authorized": False,
        "physical_execution_invoked": False, "runtime_execution_inferred_from_artifact": False}


def admit_point(certificate, *, expected_identity, required_cells, records_raw, verify_record,
        current_record_raw, observe_current):
    """Match a separately checked current preimage; never grant execution.

    ``observe_current`` is a trusted code callback that extracts its point from
    the supplied current raw record. Its availability/authentication/freshness
    belongs to the runtime owner; this pure comparison grants none of them.
    """
    rebuilt = build_certificate(expected_identity=expected_identity, required_cells=required_cells,
        records_raw=records_raw, verify_record=verify_record)
    _require(type(certificate) is dict and canonical_bytes(certificate) == canonical_bytes(rebuilt),
        "final_qualification_certificate_not_independently_rebuilt")
    _require(callable(observe_current), "final_qualification_current_checker_callback_required")
    raw_sha = _raw(current_record_raw)
    current = observe_current(current_record_raw, expected_identity=_copy(rebuilt["identity"]))
    _require(type(current) is dict and set(current) == {"schema", "record_sha256", "identity_sha256", "domain", "point_data", "point_pointers"}
        and current["schema"] == OBSERVATION_SCHEMA and current["record_sha256"] == raw_sha
        and current["identity_sha256"] == digest(rebuilt["identity"]),
        "final_qualification_current_record_binding")
    domain, point = _domain(current["domain"]), _bound_point(current_record_raw, current)
    matches = [row["cell_id"] for row in rebuilt["cells"] if row["cell_passed"]
        and row["domain"] == domain and row["point"] == point]
    matched = rebuilt["qualification_complete"] and bool(matches)
    return {"schema": MATCH_SCHEMA, "point_domain_evidence_matched": matched,
        "qualification_scope": "ship_return_exact_model_points_only",
        "matched_cell_ids": matches if matched else [], "current_record_sha256": raw_sha,
        "certificate_sha256": digest(rebuilt), "identity_sha256": digest(rebuilt["identity"]),
        "reason": None if matched else "incomplete_census_or_unsampled_point",
        "continuous_uncertainty_envelope_verified": False, "old39_certificate_admitted": False,
        "human_approval_inferred": False, "dispatch_authorized": False,
        "physical_execution_invoked": False, "runtime_execution_inferred_from_artifact": False}
