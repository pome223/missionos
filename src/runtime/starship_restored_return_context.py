"""Pure opt-in projections of existing return tools and actual reservations.

These numeric facts and conditional effects add no candidate, permission,
controller action, forecast or threshold. A contrary eligible choice remains a
valid judgment; its later admission is still independent.
"""
from copy import deepcopy
import math

VERSION_KEY = "return_context.version"
SOURCE = "src/runtime/starship_restored_return_context.py"
CANDIDATES = ("nominal", "next_orbit")
REASONS = frozenset({
    "current_source_inventory_vehicle_or_frozen_opportunity_mismatch",
    "notice_generation_or_content_changed_rebind_saved_prediction",
    "resource_generation_or_content_changed_rebind_saved_prediction",
    "notice_stale", "original_nominal_booking_guard", "dispatch_not_first_due_reserved_tick",
    "actual_state_outside_continuous_predicted_coast", "prediction_coast_sample_gap",
    "prediction_coast_event_straddle", "fresh_position_m", "fresh_velocity_mps",
    "fresh_body_rate_rad_s", "fresh_attitude_deg", "fresh_fuel_kg",
    "fresh_actual_engine_availability", "fresh_throttle", "fresh_gimbal_rad", "fresh_flap_rad",
    "numerical_contact_capability_unmet", "synthetic_area_or_contact_availability_window",
    "resource_observation_is_future", "synthetic_energy_budget", "synthetic_temperature_budget",
    "synthetic_reserve_after_modeled_loss_budget",
})
REQUIRED = frozenset({VERSION_KEY, "reservation.nominal", "reservation.next_orbit",
    "reservation.return_time_s", "notice.nominal_available", "notice.next_orbit_available",
    *(f"candidate.{candidate}.{name}" for candidate in CANDIDATES for name in (
        "numerical_contact_capability_met", "admission_reason_count"))})
FACT_KEYS = REQUIRED | frozenset(f"candidate.{candidate}.reason.{reason}"
    for candidate in CANDIDATES for reason in REASONS)
INSTRUCTIONS = (
    " The typed return_context identifies the actual current reservation and each candidate's "
    "independent tool checks. Compare conditional_return_effects with those checks and current "
    "operating availability and scope. Keeping or waiting preserves the named reservation; "
    "it cannot make a failed check acceptable. Candidate tool acceptance grants no authority, "
    "and any scope change still needs independent operator approval and fresh admission. "
    "The candidate.*.power_kwh values are predicted energy remaining at return, not energy consumed; "
    "resources.power_kwh is current modeled energy remaining at decision time. "
    "Choose an eligible action without treating these tools as a guarantee."
)


def _require(value, reason):
    if not value:
        raise ValueError("restored_return_context_"+reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def projection_facts(reservation, candidates, *, observed_s):
    """Read only the actual binding and already-computed independent checks."""
    _require(type(reservation) is dict and reservation.get("candidate_id") in CANDIDATES
        and _number(reservation.get("return_time_s")) and reservation["return_time_s"] >= 0
        and type(candidates) is dict and set(candidates) == set(CANDIDATES)
        and _number(observed_s) and observed_s >= 0, "actual_binding_or_candidates")
    active = reservation["candidate_id"]
    facts = {VERSION_KEY: 1, "reservation.nominal": active == "nominal",
        "reservation.next_orbit": active == "next_orbit", "reservation.return_time_s": reservation["return_time_s"]}
    for candidate, pair in candidates.items():
        _require(type(pair) is tuple and len(pair) == 2 and all(type(value) is dict for value in pair),
            "candidate_tool_pair")
        row, check = pair
        proposal = row.get("proposal", {})
        reasons = check.get("reasons")
        matched = check.get("matched_synthetic_site_ids")
        _require(type(proposal) is dict and proposal.get("candidate_id") == candidate and _number(proposal.get("return_time_s"))
            and check.get("stage") == "selection" and check.get("time_s") == observed_s
            and type(reasons) is list and all(type(reason) is str and reason in REASONS for reason in reasons)
            and reasons == sorted(set(reasons)) and type(check.get("accepted")) is bool
            and check["accepted"] is (not reasons)
            and type(check.get("numerical_contact_capability_met")) is bool
            and type(matched) is list and all(type(value) is str and value for value in matched)
            and matched == sorted(set(matched)), "independent_check_shape_or_epoch")
        _require(check["numerical_contact_capability_met"] is ("numerical_contact_capability_unmet" not in reasons)
            and bool(matched) is ("synthetic_area_or_contact_availability_window" not in reasons),
            "independent_check_contradiction")
        if candidate == active:
            _require(proposal["return_time_s"] == reservation["return_time_s"], "actual_reservation_time")
        facts.update({f"notice.{candidate}_available": bool(matched),
            f"candidate.{candidate}.numerical_contact_capability_met": check["numerical_contact_capability_met"],
            f"candidate.{candidate}.admission_reason_count": len(reasons)})
        facts.update({f"candidate.{candidate}.reason.{reason}": True for reason in reasons})
    return facts


def validate_context(facts):
    """Validate only explicit opt-in facts; callers retain legacy validation."""
    _require(type(facts) is dict and type(facts.get(VERSION_KEY)) is int and facts[VERSION_KEY] == 1
        and REQUIRED <= facts.keys(), "version_or_required_facts")
    _require(type(facts["reservation.nominal"]) is bool and type(facts["reservation.next_orbit"]) is bool
        and facts["reservation.nominal"] is not facts["reservation.next_orbit"]
        and _number(facts["reservation.return_time_s"]) and facts["reservation.return_time_s"] >= 0,
        "reservation_flags_or_time")
    reserved = "nominal" if facts["reservation.nominal"] else "next_orbit"
    rows = {}
    for candidate in CANDIDATES:
        prefix = f"candidate.{candidate}."
        keys = [key for key in facts if key.startswith(prefix+"reason.")]
        _require(all(key in FACT_KEYS and facts[key] is True for key in keys), "reason_flag_unknown_or_false")
        reasons = sorted(key[len(prefix+"reason."):] for key in keys)
        count = facts[prefix+"admission_reason_count"]
        available, capability = facts[f"notice.{candidate}_available"], facts[prefix+"numerical_contact_capability_met"]
        accepted, when = facts.get(prefix+"feasibility_passed"), facts.get(prefix+"return_time_s")
        allowed = facts.get(f"authority.{candidate}_allowed")
        _require(type(count) is int and count == len(reasons) and type(available) is type(capability) is type(accepted) is type(allowed) is bool
            and _number(when) and when >= 0 and accepted is (not reasons)
            and capability is ("numerical_contact_capability_unmet" not in reasons)
            and available is ("synthetic_area_or_contact_availability_window" not in reasons),
            "candidate_facts_or_reason_consistency")
        rows[candidate] = {"candidate_id": candidate, "return_time_s": when,
            "independent_tools_accepted": accepted, "numerical_contact_capability_met": capability,
            "operationally_available_at_predicted_contact": available, "admission_reasons": reasons,
            "current_scope_allows_candidate": allowed}
    _require(facts["reservation.return_time_s"] == rows[reserved]["return_time_s"]
        and type(facts.get("authority.can_request_amendment")) is bool, "reservation_candidate_or_authority")
    return {"reserved_candidate_id": reserved, "candidates": rows}


def public_projection(facts, actions):
    """Expose conditional action semantics without removing any allowed choice."""
    parsed = validate_context(facts)
    reserved, candidates = parsed["reserved_candidate_id"], parsed["candidates"]
    effects = {}
    roles = {"select_nominal": "propose_candidate", "select_next_orbit": "propose_candidate",
        "keep_reserved_return": "preserve_current_reservation", "wait": "wait_with_current_reservation",
        "request_scope_amendment": "request_operator_review_for_candidate",
        "request_deep_reasoning": "delegate_judgment_without_reservation_change"}
    _require(type(actions) is list and actions and len(set(actions)) == len(actions)
        and all(action in roles for action in actions), "action_set")
    for action in actions:
        candidate = "nominal" if action == "select_nominal" else "next_orbit" if action in (
            "select_next_orbit", "request_scope_amendment") else reserved
        effects[action] = {"action_role": roles[action], **deepcopy(candidates[candidate]),
            "would_preserve_current_reservation": action in ("keep_reserved_return", "wait", "request_deep_reasoning"),
            "operator_scope_amendment_required": not candidates[candidate]["current_scope_allows_candidate"],
            "operator_amendment_request_currently_available": facts["authority.can_request_amendment"],
            "answer_alone_changes_reservation": False, "dispatch_permission_granted": False,
            "deadline_extended": False}
    return {"return_context": {"schema": "missionos.starship_restored_public_return_context.v1",
        "current_reservation": deepcopy(candidates[reserved]), "candidates": deepcopy(candidates),
        "metric_semantics": {"candidate.*.power_kwh": "predicted energy remaining at return in kWh, not consumption",
            "resources.power_kwh": "current modeled energy remaining at decision time in kWh",
            "feasibility_passed": "independent current tool admission; not scope authority or a success guarantee"},
        "operations_are_synthetic": True, "mission_authority_granted": False,
        "future_contact_is_not_an_observed_outcome": True}, "conditional_return_effects": effects}
