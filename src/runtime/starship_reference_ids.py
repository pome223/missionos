"""Decision-specific reference IDs for an offline protocol probe only.

A model selects IDs; the host resolves their exact values from the original
request. This does not establish that the model copied, asserted or reasoned
correctly about those numbers. Existing numeric_evidence v1 is unchanged.
"""

from copy import deepcopy
from hashlib import sha256
import json
import re

from .starship_decision_evidence import MAX_REFERENCES, build_reference_catalog

SCHEMA = "missionos.m1_reference_id_catalog.v1"
AUDIT_SCHEMA = "missionos.m1_reference_id_audit.v1"
_ID_PATTERN = re.compile(r"r_[0-9a-f]{12}_[0-9a-f]{20}\Z")
_CATALOG_ERRORS = {
    "invalid_reference_request",
    "reference_id_collision",
    "duplicate_catalog_reference",
}


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _identifier(request_sha256, reference):
    # Both hashes bind the full request, not just the visible prefix. The
    # prefix identifies stale decisions; the suffix also binds path/value/unit.
    fingerprint = _digest({"request_sha256": request_sha256, "reference": reference})
    return f"r_{request_sha256[:12]}_{fingerprint[:20]}"


def catalog_for(request):
    """Build a deterministic table; never accept an externally supplied table.

    IDs contain 128 hash bits across a decision prefix and reference suffix.
    Within-catalog collisions fail closed; cryptographic collision resistance,
    not a mathematical guarantee across all possible requests, binds the IDs.
    Paths and units inherit the finite numeric field allowlist. Non-finite
    values anywhere in the request invalidate its canonical identity.
    """
    if type(request) is not dict or type(request.get("candidates")) is not dict:
        raise ValueError("invalid_reference_request")
    try:
        request_sha256 = _digest(request)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid_reference_request") from exc
    entries, identifiers, source_fields = [], set(), set()
    references = sorted(
        build_reference_catalog(request),
        key=lambda ref: (ref["candidate_id"], ref["field_path"]),
    )
    for reference in references:
        field = reference["candidate_id"], reference["field_path"]
        if field in source_fields:
            raise ValueError("duplicate_catalog_reference")
        source_fields.add(field)
        reference_id = _identifier(request_sha256, reference)
        if reference_id in identifiers:
            raise ValueError("reference_id_collision")
        identifiers.add(reference_id)
        entries.append({"reference_id": reference_id, **deepcopy(reference)})
    return {"schema": SCHEMA, "request_sha256": request_sha256, "entries": entries}


def verify_reference_ids(request, reference_ids, compared_candidate_ids):
    """Resolve selected IDs using only the original decision-time request.

    The audit is post-hoc and cannot approve or veto a flight operation. It
    requires one valid reference for each compared, observed candidate. Only
    individually resolved entries may be displayed, and any error makes the
    overall audit invalid. The model never supplies a trusted value or unit.
    """
    audit = {
        "schema": AUDIT_SCHEMA,
        "status": "invalid",
        "request_sha256": None,
        "errors": [],
        "reference_errors": [],
        "resolved_references": [],
        "referenced_candidate_ids": [],
        "host_resolved_values": True,
        "model_numeric_values_verified": False,
        "free_prose_verified": False,
        "internal_reasoning_verified": False,
        "forecast_truth_verified": False,
        "affects_action_admission": False,
    }

    def fail(reason, index=None):
        if reason not in audit["errors"]:
            audit["errors"].append(reason)
        if index is not None:
            audit["reference_errors"].append({"index": index, "reason": reason})

    try:
        catalog = catalog_for(request)
    except ValueError as exc:
        reason = str(exc)
        fail(reason if reason in _CATALOG_ERRORS else "invalid_reference_request")
        return audit
    audit["request_sha256"] = catalog["request_sha256"]
    candidates = request["candidates"]
    if (
        type(compared_candidate_ids) is not list
        or not 1 <= len(compared_candidate_ids) <= MAX_REFERENCES
        or any(type(name) is not str or name not in candidates for name in compared_candidate_ids)
        or len(set(compared_candidate_ids)) != len(compared_candidate_ids)
    ):
        fail("compared_candidate_ids")
    if type(reference_ids) is not list or not 1 <= len(reference_ids) <= MAX_REFERENCES:
        fail("reference_count_or_type")
    if audit["errors"]:
        return audit
    by_id = {entry["reference_id"]: entry for entry in catalog["entries"]}
    prefix = "r_" + catalog["request_sha256"][:12] + "_"
    seen = set()
    for index, reference_id in enumerate(reference_ids):
        if type(reference_id) is not str or _ID_PATTERN.fullmatch(reference_id) is None:
            fail("reference_id_shape", index)
            continue
        if reference_id in seen:
            fail("duplicate_reference_id", index)
            continue
        seen.add(reference_id)
        if not reference_id.startswith(prefix):
            fail("stale_reference_id", index)
            continue
        reference = by_id.get(reference_id)
        if reference is None:
            fail("unknown_reference_id", index)
            continue
        if reference["candidate_id"] not in compared_candidate_ids:
            fail("candidate_not_compared", index)
            continue
        audit["resolved_references"].append(deepcopy(reference))
    audit["referenced_candidate_ids"] = sorted(
        {reference["candidate_id"] for reference in audit["resolved_references"]}
    )
    if set(compared_candidate_ids) - set(audit["referenced_candidate_ids"]):
        fail("missing_compared_candidate_reference")
    if not audit["errors"]:
        audit["status"] = "valid"
    return audit
