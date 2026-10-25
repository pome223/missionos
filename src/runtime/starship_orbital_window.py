"""Approved longer thinking window for prepared orbital comparisons only.

This selects a time budget, never an action or candidate. Actual selection and
dispatch still require fresh admission after the wait. Legacy requests retain
their 35-second actor / 30-second provider budget.
"""

import math


CONFIG = {
    "schema": "missionos.orbital_planning_window.v1",
    "scope": "prepared_uncertain_orbital_comparison",
    "provider_timeout_s": 120.0,
    "decision_timeout_s": 150.0,
    "booking_margin_s": 60.0,
    "coast_step_bound_s": 2.001,
}
DECISION_SCHEMA = "missionos.orbital_planning_window_decision.v1"
AUTHORITY_GAP_POLICY = {
    "schema": "missionos.authority_gap_reasoning.v1",
    "scope": "candidate_bound_scope_amendment",
    "operator_response_reserve_s": 20.0,
    "coast_guard_s": 4.002,
    "retry_policy": "once_per_prepared_generation",
}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def reserved_decision_s(envelope):
    """Reserve the maximum approved comparison window before starting forecasts."""
    if "authority_gap_reasoning" in envelope and (
        envelope["authority_gap_reasoning"] != AUTHORITY_GAP_POLICY
        or "orbital_planning_window" not in envelope
        or envelope.get("provider_budget_policy") != "independent_provider_reservations_v1"
    ):
        raise ValueError("invalid_authority_gap_reasoning_contract")
    if "orbital_planning_window" not in envelope:
        return envelope["decision_timeout_s"]
    config = envelope["orbital_planning_window"]
    if (type(config) is not dict or config != CONFIG
            or envelope["decision_timeout_s"] != 35.0
            or not envelope.get("comparison_preparation")
            or envelope.get("event_supervision", {}).get("scope")
            != "post_deployment_orbital_coast_only"):
        raise ValueError("invalid_orbital_planning_window_contract")
    return CONFIG["decision_timeout_s"]


def _authority_gap_protection(envelope, request):
    """Return the original deadline only for an actionable authority gap.

    This is reasoning admission, not candidate selection or approval. Host
    validation binds authority fields to its independently supplied contract;
    the executor still checks the original bounds before sending any request.
    """
    if "authority_gap_reasoning" not in envelope:
        return None
    from .starship_replanning import digest

    try:
        authority, context = request["active_authority"], request["comparison_context"]
        generation = context["generation_sha256"]
        if (type(generation) is not str or len(generation) != 64
                or any(c not in "0123456789abcdef" for c in generation)
                or context.get("missing_candidate_ids") != []
                or type(context.get("prepared_candidate_ids")) is not list
                or "next_orbit" not in context["prepared_candidate_ids"]
                or authority["schema"] != "missionos.inflight_active_authority.v2"
                or type(authority["scope_revision"]) is not int or authority["scope_revision"] != 0
                or authority["amendment_status"] != "idle"
                or authority["select_next_orbit_requires_candidate_bound_operator_approval"] is not True
                or authority["technical_candidates_are_not_authorization"] is not True
                or authority["scope_sha256"] != digest(authority["scope"])):
            return None
        commands, candidates = authority["scope"]["commands"], request["candidates"]
        if (type(commands) is not list or type(candidates) is not dict
                or any(type(c) is not dict for c in candidates.values())
                or "select_next_orbit" in commands or "select_next_orbit" not in request["allowed_actions"]
                or any(c.get("constraint_rejections") == [] and "select_"+name in commands
                       for name, c in candidates.items())):
            return None
        candidate = candidates["next_orbit"]
        material = candidate["comparison_material"]
        if (candidate["constraint_rejections"] != []
                or material["candidate_sha256"] != candidate["candidate_sha256"]
                or material["notice_sha256"] != digest(request["notice"])
                or material["return_time_s"] != candidate["return_time_s"]):
            return None
        window, now = authority["request_window"], request["time_s"]
        lead = CONFIG["decision_timeout_s"]+AUTHORITY_GAP_POLICY["operator_response_reserve_s"]+AUTHORITY_GAP_POLICY["coast_guard_s"]
        numeric = ("simulation_deadline_s", "remaining_simulation_s", "remaining_wall_s",
                   "model_response_reserve_s", "operator_response_reserve_s", "coast_guard_s", "latest_decision_start_s")
        if (window["schema"] != "missionos.inflight_request_window.v1"
                or any(not _finite(window[k]) for k in numeric)
                or window["request_available"] is not True or window["request_attempted"] is not False
                or window["trusted_telemetry_available"] is not True or window["reason"] != "request_window_open"
                or window["model_response_reserve_s"] != CONFIG["decision_timeout_s"]
                or window["operator_response_reserve_s"] != AUTHORITY_GAP_POLICY["operator_response_reserve_s"]
                or window["coast_guard_s"] != AUTHORITY_GAP_POLICY["coast_guard_s"]
                or window["remaining_simulation_s"] != window["simulation_deadline_s"]-now
                or window["latest_decision_start_s"] != window["simulation_deadline_s"]-lead
                or min(window["remaining_simulation_s"], window["remaining_wall_s"]) < lead):
            return None
        return window["simulation_deadline_s"]
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None


def decision_window(envelope, request):
    """Derive a bounded window from decision-time material, not claimed deadlines.

    request.planning_window and request.deadline_s deliberately play no part in
    this calculation. Invalid or incomplete comparison material never earns a
    longer wait. There is no rolling extension of a submitted request.
    """
    result = {
        "schema": DECISION_SCHEMA,
        "duration_s": envelope["decision_timeout_s"],
        "provider_timeout_s": 30.0,
        "admitted": False,
        "reason": "legacy",
        "protected_until_s": None,
    }
    if "orbital_planning_window" in envelope or "authority_gap_reasoning" in envelope:
        reserved_decision_s(envelope)
    if "orbital_planning_window" not in envelope:
        return result
    result["reason"] = "invalid_comparison_inputs"
    if type(request) is not dict:
        return result
    try:
        now = request["time_s"]
        notice = request["notice"]
        if (not _finite(now) or not _finite(notice["expires_at_s"])
                or not _finite(notice["issued_at_s"])
                or not notice["issued_at_s"] <= now < notice["expires_at_s"]):
            return result
        context = request.get("comparison_context")
        if type(context) is not dict or context.get("ready") is not True:
            result["reason"] = "comparison_not_ready"
            return result
        candidates = request["candidates"]
        allowed = request["allowed_actions"]
        if type(candidates) is not dict or type(allowed) is not list:
            return result
        prepared = [
            c for name, c in candidates.items()
            if type(name) is str and type(c) is dict
            and c.get("constraint_rejections") == []
            and type(c.get("comparison_material")) is dict
            and "select_" + name in allowed
        ]
        gap_deadline = _authority_gap_protection(envelope, request)
        if gap_deadline is not None:
            returns = [c["return_time_s"] for c in candidates.values()
                       if c.get("constraint_rejections") == []]
            if (not returns or not all(_finite(t) for t in returns)
                    or now+sum(CONFIG[k] for k in ("decision_timeout_s", "booking_margin_s", "coast_step_bound_s"))
                    > min(notice["expires_at_s"], *returns)):
                result["reason"] = "insufficient_booking_margin"
                return result
            result.update(duration_s=CONFIG["decision_timeout_s"], provider_timeout_s=CONFIG["provider_timeout_s"],
                admitted=True, reason="authority_gap", protected_until_s=gap_deadline)
            return result
        if len(prepared) < 2:
            result["reason"] = "insufficient_admissible_candidates"
            return result
        service = notice.get("recovery_service_forecast", {})
        if type(service) is not dict or not any(
            type(v) is list and len(v) == 2 and all(_finite(x) for x in v)
            and v[0] < v[1] for v in service.values()
        ):
            result["reason"] = "comparison_not_uncertain"
            return result
        # Protect all admissible opportunities, including an existing booking
        # which might not be one of the two prepared comparison alternatives.
        returns = [
            c["return_time_s"] for c in candidates.values()
            if type(c) is dict and c.get("constraint_rejections") == []
        ]
        if not returns or not all(_finite(when) for when in returns):
            return result
        protected = min([notice["expires_at_s"], *returns])
        result["protected_until_s"] = protected
        if now + sum(CONFIG[k] for k in (
            "decision_timeout_s", "booking_margin_s", "coast_step_bound_s"
        )) > protected:
            result["reason"] = "insufficient_booking_margin"
            return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return result
    result.update(
        duration_s=CONFIG["decision_timeout_s"],
        provider_timeout_s=CONFIG["provider_timeout_s"],
        admitted=True,
        reason="long_comparison_admitted",
    )
    return result


def prepared_reasoning_eligible(envelope, request, reasoned_generations=()):
    """Whether this evidence warrants the separately budgeted comparison route.

    Suitability is independent of either provider's remaining call count. It does
    not force a call, make a candidate feasible, or allocate a larger deadline.
    """
    comparison = envelope.get("comparison_preparation")
    context = request.get("comparison_context") or {}
    if not comparison or not comparison.get("deep_reasoning") or context.get("ready") is not True:
        return False
    generation = context.get("generation_sha256")
    if not generation or generation in reasoned_generations:
        return False
    if "authority_gap_reasoning" in envelope and decision_window(envelope, request)["reason"] == "authority_gap":
        return True
    prepared = [
        c for name, c in request.get("candidates", {}).items()
        if c.get("constraint_rejections") == [] and c.get("comparison_material")
        and ("orbital_planning_window" not in envelope
             or "select_" + name in request.get("allowed_actions", ()))
    ]
    service = request.get("notice", {}).get("recovery_service_forecast", {})
    uncertain = any(
        type(value) is list and len(value) == 2
        and all(_finite(x) for x in value) and value[0] < value[1]
        for value in service.values()
    )
    return bool(len(prepared) >= 2 and uncertain and (
        "orbital_planning_window" not in envelope or decision_window(envelope, request)["admitted"]
    ))
