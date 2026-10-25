"""Pure uncalibrated review routing; no inference, flight, or outcome claims."""
from copy import deepcopy
import math

import pytest

from src.runtime import starship_restored_return_review as review


def request(phase="return", actions=None):
    return {"phase": phase, "allowed_actions": actions or
        ["keep_reserved_return", "select_next_orbit", "request_deep_reasoning"]}


def judgment(*, confidence=.8, probabilities=None, action="keep_reserved_return", status="valid"):
    return {"provider": "typesafe", "status": status, "action": action,
        "provider_receipt": {"status": "succeeded", "validation_error": None,
            "native_choice_confidence": confidence,
            "native_choice_probabilities": probabilities or
                {"keep_reserved_return": .7, "select_next_orbit": .2, "request_deep_reasoning": .1}}}


def route(value=None, req=None):
    return review.review_route(req or request(), value or judgment(), review.DEFAULT_POLICY)


def test_closed_policy_is_copied_and_has_stable_digest():
    value = review.validate_policy(review.DEFAULT_POLICY)
    assert value == review.DEFAULT_POLICY and value is not review.DEFAULT_POLICY
    assert len(review.policy_digest(value)) == 64
    value["confidence_floor"] = .9
    assert review.DEFAULT_POLICY["confidence_floor"] == .5


@pytest.mark.parametrize("change", [
    lambda p: p.update(schema="wrong"), lambda p: p.update(extra=True),
    lambda p: p.pop("confidence_floor"), lambda p: p.update(confidence_floor=True),
    lambda p: p.update(confidence_floor=math.nan), lambda p: p.update(confidence_floor=.49),
    lambda p: p.update(selected_action_margin_floor=.11),
    lambda p: p.update(selected_action_margin_floor="0.1"),
])
def test_policy_rejects_missing_extra_forged_nonfinite_or_changed_threshold(change):
    value = deepcopy(review.DEFAULT_POLICY)
    change(value)
    with pytest.raises(ValueError, match="policy_invalid"):
        review.validate_policy(value)


def test_high_metrics_preserve_final_jev_choice_without_preference_or_mutation():
    req, value = request(), judgment()
    before = deepcopy((req, value))
    proof = route(value, req)
    assert proof["route"] == "jev_final" and proof["review_required"] is False
    assert proof["confidence"] == .8 and proof["selected_action_margin"] == .5
    assert (req, value) == before
    assert proof["metrics_are_uncalibrated"] is True
    assert proof["policy_sha256"] == review.policy_digest(review.DEFAULT_POLICY)


def test_confidence_floor_is_strictly_less_and_not_less_equal():
    assert route(judgment(confidence=.5))["route"] == "jev_final"
    proof = route(judgment(confidence=.499999))
    assert proof["route"] == "rules_uncertainty_review" and proof["low_confidence"]
    assert proof["small_selected_margin"] is False


def test_selected_probability_margin_boundary_is_exact_decimal_point_one():
    boundary = {"keep_reserved_return": .5, "select_next_orbit": .4, "request_deep_reasoning": .1}
    proof = route(judgment(probabilities=boundary))
    assert proof["selected_action_margin"] == .1 and proof["route"] == "jev_final"
    near = {"keep_reserved_return": .49999, "select_next_orbit": .40001, "request_deep_reasoning": .1}
    assert route(judgment(probabilities=near))["small_selected_margin"] is True


def test_margin_uses_selected_action_not_top_two_sorted_actions():
    probabilities = {"keep_reserved_return": .2, "select_next_orbit": .7, "request_deep_reasoning": .1}
    proof = route(judgment(probabilities=probabilities))
    assert proof["selected_action_probability"] == .2
    assert proof["maximum_other_probability"] == .7
    assert proof["selected_action_margin"] == -.5
    assert proof["route"] == "rules_uncertainty_review"


@pytest.mark.parametrize("selected", ["keep_reserved_return", "select_next_orbit"])
def test_action_names_and_order_do_not_prefer_a_result(selected):
    other = "select_next_orbit" if selected == "keep_reserved_return" else "keep_reserved_return"
    probabilities = {selected: .7, other: .2, "request_deep_reasoning": .1}
    proof = route(judgment(action=selected, probabilities=probabilities),
        request(actions=list(reversed(probabilities))))
    assert proof["route"] == "jev_final" and proof["review_required"] is False


def test_explicit_jev_delegation_is_a_distinct_route_not_a_rules_metrics_trigger():
    value = judgment(action="request_deep_reasoning")
    value.pop("provider_receipt")
    proof = route(value)
    assert proof["route"] == "explicit_jev_delegation" and proof["review_required"]
    assert proof["confidence"] is None and proof["low_confidence"] is False


@pytest.mark.parametrize("change", [
    lambda j: j.pop("provider_receipt"),
    lambda j: j["provider_receipt"].pop("native_choice_confidence"),
    lambda j: j["provider_receipt"].update(native_choice_confidence=True),
    lambda j: j["provider_receipt"].update(native_choice_confidence=math.inf),
    lambda j: j["provider_receipt"].update(native_choice_confidence=math.nan),
    lambda j: j["provider_receipt"].update(status="invalid_response"),
    lambda j: j["provider_receipt"].update(validation_error="model_mismatch"),
    lambda j: j["provider_receipt"].update(native_choice_probabilities=None),
    lambda j: j["provider_receipt"]["native_choice_probabilities"].pop("select_next_orbit"),
    lambda j: j["provider_receipt"]["native_choice_probabilities"].update(extra=0.),
    lambda j: j["provider_receipt"]["native_choice_probabilities"].update(keep_reserved_return=True),
    lambda j: j["provider_receipt"]["native_choice_probabilities"].update(keep_reserved_return=math.nan),
    lambda j: j["provider_receipt"]["native_choice_probabilities"].update(keep_reserved_return=-.1),
    lambda j: j["provider_receipt"]["native_choice_probabilities"].update(keep_reserved_return=.9),
])
def test_missing_forged_nonfinite_or_invalid_metric_does_not_trigger_secondary_review(change):
    value = judgment()
    change(value)
    proof = route(value)
    assert proof["route"] == "invalid_jev_metrics" and proof["review_required"] is False
    assert proof["confidence"] is proof["selected_action_margin"] is None


def test_deployment_is_unchanged_even_without_metrics():
    proof = review.review_route(request("deployment"), {"status": "valid"}, review.DEFAULT_POLICY)
    assert proof["route"] == "not_applicable" and proof["review_required"] is False


def test_no_judgment_or_invalid_answer_is_never_promoted_to_review():
    assert review.review_route(request(), None, review.DEFAULT_POLICY)["route"] == "not_evaluated"
    proof = route(judgment(status="no_valid_judgment", action=None))
    assert proof["route"] == "no_valid_jev_judgment" and proof["review_required"] is False
