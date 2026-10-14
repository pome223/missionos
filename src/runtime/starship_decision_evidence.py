"""Post-hoc numeric citation checks against the original decision-time request.

This module never chooses or constrains an action, imports the comparison
material producer, or reads later state. Matching citations establish data
consistency, not the truth of forecasts or free prose/internal reasoning.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math

SCHEMA = "missionos.m1_decision_numeric_evidence.v1"
AUDIT_SCHEMA = "missionos.m1_decision_numeric_audit.v1"
MAX_REFERENCES = 16
_REFERENCE_FIELDS = {"candidate_id", "field_path", "value", "unit"}
_SCALARS = {
    "return_time_s": "s",
    "waiting_at_burn/elapsed_wait_s": "s",
    "waiting_at_burn/energy_remaining_kwh": "kWh",
    "waiting_at_burn/temperature_upper_k": "K",
    "waiting_at_burn/propellant_loss_upper_kg": "kg",
    "waiting_at_burn/valid_until_s": "s",
}
_RANGES = {
    "sampled_contact_ranges/time_s": "s",
    "sampled_contact_ranges/speed_mps": "m/s",
    "sampled_contact_ranges/tilt_deg": "deg",
    "sampled_contact_ranges/body_rate_rad_s": "rad/s",
    "sampled_contact_ranges/reserve_kg": "kg",
    "reserve_after_loss_kg": "kg",
}
_SERVICE_FIELDS = ("service_delay_after_contact_s", "estimated_completion_time_s")


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _lookup(material, path):
    value = material
    for component in path.split("/"):
        if type(value) is not dict:
            return None
        value = value.get(component)
    return value


def build_reference_catalog(request):
    """Return exact, finite source values on a finite field/unit allowlist.

    Paths are JSON pointers relative to a candidate's comparison_material.
    Area names are taken only from that request and escaped as pointer tokens;
    no expression/path evaluation or future scenario data is involved.
    """
    if type(request) is not dict or type(request.get("candidates")) is not dict:
        return []
    catalog = []

    def add(candidate_id, path, value, unit):
        if _number(value):
            catalog.append(
                {"candidate_id": candidate_id, "field_path": path, "value": value, "unit": unit}
            )

    def interval(candidate_id, path, values, unit):
        if type(values) is list and len(values) == 2:
            for index, value in enumerate(values):
                add(candidate_id, f"/{path}/{index}", value, unit)

    for candidate_id, candidate in request["candidates"].items():
        if type(candidate_id) is not str or not candidate_id or len(candidate_id) > 64:
            continue
        if type(candidate) is not dict:
            continue
        material = candidate.get("comparison_material")
        if type(material) is not dict:
            continue
        for path, unit in _SCALARS.items():
            add(candidate_id, "/" + path, _lookup(material, path), unit)
        for path, unit in _RANGES.items():
            interval(candidate_id, path, _lookup(material, path), unit)
        areas = material.get("area_service_estimates")
        if type(areas) is not dict:
            continue
        for area_id, service in areas.items():
            if type(area_id) is not str or not area_id or len(area_id) > 64:
                continue
            if type(service) is not dict:
                continue
            escaped = area_id.replace("~", "~0").replace("/", "~1")
            for field in _SERVICE_FIELDS:
                interval(
                    candidate_id,
                    f"area_service_estimates/{escaped}/{field}",
                    service.get(field),
                    "s",
                )
    return deepcopy(catalog)


def verify_numeric_evidence(request, evidence, compared_ids=None):
    """Audit exact numbers, request binding, units and comparison coverage.

    Numeric equality has no tolerance: 22 and 22.0 denote the same value, but
    a rounded 12.9 cannot cite 12.915. Display formatting may round *after*
    verification. Legacy absent evidence remains not_present, not fabricated.
    Only individually valid citations are returned for display; any error
    prevents an overall valid status. This is never a dispatch permission.
    """
    result = {
        "schema": AUDIT_SCHEMA,
        "status": "not_present" if evidence is None else "invalid",
        "request_sha256": None,
        "errors": [],
        "reference_errors": [],
        "verified_references": [],
        "referenced_candidate_ids": [],
        "free_prose_verified": False,
        "internal_reasoning_verified": False,
        "forecast_truth_verified": False,
        "affects_action_admission": False,
    }
    if evidence is None:
        return result

    def fail(reason, index=None):
        if reason not in result["errors"]:
            result["errors"].append(reason)
        if index is not None:
            result["reference_errors"].append({"index": index, "reason": reason})

    try:
        if type(request) is not dict:
            raise ValueError("request_type")
        checksum = sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError):
        fail("invalid_request")
        return result
    result["request_sha256"] = checksum
    if type(evidence) is not dict or set(evidence) != {"schema", "request_sha256", "references"}:
        fail("evidence_shape")
        return result
    if evidence["schema"] != SCHEMA:
        fail("evidence_schema")
    if type(evidence["request_sha256"]) is not str or evidence["request_sha256"] != checksum:
        fail("request_hash_mismatch")
    refs = evidence["references"]
    if type(refs) is not list or not 1 <= len(refs) <= MAX_REFERENCES:
        fail("reference_count_or_type")
    if result["errors"]:
        return result
    catalog = {
        (ref["candidate_id"], ref["field_path"]): ref for ref in build_reference_catalog(request)
    }
    candidates = request.get("candidates", {})
    if type(candidates) is not dict:
        fail("invalid_request")
        return result
    if compared_ids is not None and (
        type(compared_ids) is not list
        or not 1 <= len(compared_ids) <= MAX_REFERENCES
        or any(type(name) is not str or name not in candidates for name in compared_ids)
        or len(set(compared_ids)) != len(compared_ids)
    ):
        fail("compared_candidate_ids")
        return result
    seen = set()
    for index, ref in enumerate(refs):
        if type(ref) is not dict or set(ref) != _REFERENCE_FIELDS:
            fail("reference_shape", index)
            continue
        name, path = ref["candidate_id"], ref["field_path"]
        if type(name) is not str or name not in candidates:
            fail("unknown_candidate", index)
            continue
        if compared_ids is not None and name not in compared_ids:
            fail("candidate_not_compared", index)
            continue
        if type(path) is not str or (name, path) not in catalog:
            fail("unknown_field_or_unavailable_value", index)
            continue
        if (name, path) in seen:
            fail("duplicate_reference", index)
            continue
        seen.add((name, path))
        source = catalog[name, path]
        if type(ref["unit"]) is not str or ref["unit"] != source["unit"]:
            fail("unit_mismatch", index)
            continue
        if not _number(ref["value"]) or ref["value"] != source["value"]:
            fail("value_mismatch", index)
            continue
        result["verified_references"].append(deepcopy(source))
    result["referenced_candidate_ids"] = sorted(
        {ref["candidate_id"] for ref in result["verified_references"]}
    )
    if compared_ids is not None and set(compared_ids) - set(result["referenced_candidate_ids"]):
        fail("missing_compared_candidate_reference")
    if not result["errors"]:
        result["status"] = "valid"
    return result
