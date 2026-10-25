"""Opt-in Rules review of Jev's uncalibrated return-decision metrics.

This policy routes a provisional judgment; it grants no permission and picks no
preferred recovery action. Thresholds are fixed trial assumptions, not measured
confidence calibration, reliability estimates, or physical safety criteria.
"""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
import math

POLICY_SCHEMA = "missionos.starship_restored_return_review_policy.v1"
ROUTE_SCHEMA = "missionos.starship_restored_return_review_route.v1"
_FLOORS = {"confidence_floor": .5, "selected_action_margin_floor": .1}
DEFAULT_POLICY = {"schema": POLICY_SCHEMA, **_FLOORS}


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _metric(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_policy(value):
    """Validate the closed, registered V1 policy; None is handled by callers."""
    if (type(value) is not dict or set(value) != {"schema", *_FLOORS}
            or value.get("schema") != POLICY_SCHEMA
            or any(not _metric(value.get(key)) or value[key] != _FLOORS[key]
                for key in ("confidence_floor", "selected_action_margin_floor"))):
        raise ValueError("restored_return_review_policy_invalid")
    return deepcopy(value)


def policy_digest(value):
    return sha256(_wire(validate_policy(value))).hexdigest()


def review_route(request, judgment, policy):
    """Return reproducible routing proof, without changing the Jev judgment.

    The adapter has already validated a native answer. For a nondelegating
    return answer, this extra Rules boundary independently requires its numeric
    receipt fields. A missing/malformed metric is never promoted to a review.
    """
    policy = validate_policy(policy)
    proof = {"schema": ROUTE_SCHEMA, "policy_sha256": policy_digest(policy),
        "route": "not_evaluated", "review_required": False,
        "metrics_are_uncalibrated": True, "confidence": None,
        "selected_action_probability": None, "maximum_other_probability": None,
        "selected_action_margin": None, "low_confidence": False,
        "small_selected_margin": False}
    if request.get("phase") != "return":
        proof["route"] = "not_applicable"
        return proof
    if judgment is None:
        return proof
    if (type(judgment) is not dict or judgment.get("status") != "valid"
            or judgment.get("provider") != "typesafe"
            or judgment.get("action") not in request.get("allowed_actions", [])):
        proof["route"] = "no_valid_jev_judgment"
        return proof
    if judgment["action"] == "request_deep_reasoning":
        proof.update(route="explicit_jev_delegation", review_required=True)
        return proof
    receipt = judgment.get("provider_receipt")
    probabilities = receipt.get("native_choice_probabilities") if type(receipt) is dict else None
    confidence = receipt.get("native_choice_confidence") if type(receipt) is dict else None
    if (type(receipt) is not dict or receipt.get("status") != "succeeded"
            or receipt.get("validation_error") is not None
            or type(probabilities) is not dict or set(probabilities) != set(request["allowed_actions"])
            or not all(_metric(value) for value in probabilities.values()) or not _metric(confidence)
            or not math.isclose(sum(probabilities.values()), 1., rel_tol=1e-9, abs_tol=.001)):
        proof["route"] = "invalid_jev_metrics"
        return proof
    selected = probabilities[judgment["action"]]
    other = max((value for action, value in probabilities.items() if action != judgment["action"]), default=0.)
    # Decimal strings make an exact displayed .10 margin meet the .10 boundary;
    # no numerical epsilon silently changes the registered trigger.
    margin = Decimal(str(selected)) - Decimal(str(other))
    low = Decimal(str(confidence)) < Decimal(str(policy["confidence_floor"]))
    small = margin < Decimal(str(policy["selected_action_margin_floor"]))
    proof.update(route="rules_uncertainty_review" if low or small else "jev_final",
        review_required=low or small, confidence=confidence,
        selected_action_probability=selected, maximum_other_probability=other,
        selected_action_margin=float(margin), low_confidence=low, small_selected_margin=small)
    return proof
