"""Saved M2 deployment decisions: authority, Rules, fallback and provider bounds."""

import math

from .starship_deployment_handoff_verifier import _require, _same
from .starship_mission_director import (
    check_action,
    eligible_actions,
    fallback_action,
    validate_observation,
)
from .starship_replanning import digest
from .starship_m2_attempts import authority_call_limits_valid, provider_timeout_limit
from .starship_call_limits import call_count_allowed


def verify_decisions(study):
    authority = study["authority"]
    _require(authority_call_limits_valid(authority), "m2_authority_call_limits")
    envelope = authority["deployment_authority"]
    records = study["deployment_run"]["mission_director"]["records"]
    _require(len(records) <= envelope["maximum_decisions"], "deployment_decision_budget")
    seen, points, reads, held = set(), set(), 0, 0.0
    counts = {"deployment_jev": 0, "deployment_llm": 0, "return_jev": 0, "return_llm": 0}
    for item in records:
        request, response, dispatch = item["request"], item["response"], item["dispatch"]
        _require(type(request) is dict and set(request) == {
            "schema", "request_id", "point", "observation", "allowed_actions",
            "envelope_sha256", "decision_deadline_s"}
            and request["schema"] == "missionos.starship_director_request.v5",
            "deployment_request_schema")
        token, point = request["request_id"], request["point"]
        _require(
            type(token) is str
            and token.startswith(study["run_id"] + "-deployment:")
            and token not in seen
            and point not in points
            and (point.startswith("deployment") or point == "booster_selection")
            and request["envelope_sha256"] == digest(envelope),
            "deployment_request_authority",
        )
        seen.add(token)
        points.add(point)
        before = validate_observation(request["observation"])
        _require(
            _same(
                request["allowed_actions"],
                eligible_actions(
                    envelope, point, before, observation_requests=reads, hold_used_s=held
                ),
            ),
            "deployment_request_choices",
        )
        _require(
            type(response) is dict and type(dispatch) is dict, "deployment_decision_incomplete"
        )
        row = validate_observation(dispatch["observation"])
        deadline = request["decision_deadline_s"]
        _require(
            type(deadline) in (float, int)
            and math.isfinite(deadline)
            and before["time_s"] < deadline <= before["time_s"] + envelope["decision_expiry_s"]
            and dispatch["time_s"] == row["time_s"],
            "deployment_deadline_binding",
        )
        reason = check_action(
            envelope,
            point,
            response.get("action"),
            row,
            elapsed_s=row["time_s"] - before["time_s"],
            observation_requests=reads,
            hold_used_s=held,
        )
        if row["time_s"] >= deadline:
            reason = "decision_deadline_reached"
        if response.get("action") not in request["allowed_actions"]:
            reason = reason or "outside_request_choices"
        bound = (
            response.get("request_id") == token
            and response.get("request_sha256") == digest(request)
            and response.get("mode") == envelope["mode"]
        )
        reason = reason or (None if bound else "response_binding_mismatch")
        fallback = fallback_action(
            envelope, point, row, observation_requests=reads, hold_used_s=held
        )
        fallback_reason = (
            check_action(
                envelope,
                point,
                fallback,
                row,
                elapsed_s=0.0,
                observation_requests=reads,
                hold_used_s=held,
            )
            if reason
            else None
        )
        _require(
            dispatch["rejection"] == reason
            and dispatch["rules_accepted"] is (reason is None)
            and dispatch["fallback_rejection"] == fallback_reason
            and dispatch["fallback_rules_accepted"]
            is (reason is not None and fallback_reason is None)
            and dispatch["action"] == (fallback if reason else response["action"]),
            "deployment_independent_action_check",
        )
        later = validate_observation(item["later_observation"])
        _require(later["time_s"] > dispatch["time_s"], "deployment_later_observation")
        reads += dispatch["action"] == "collect_status"
        held += envelope["maximum_hold_s"] if dispatch["action"] == "hold" else 0.0
        for field, key in (
            ("jev_invocation", "deployment_jev"),
            ("llm_invocation", "deployment_llm"),
        ):
            receipt = response.get(field)
            if receipt is not None:
                _require(type(receipt.get("call_attempted")) is bool, "provider_attempt_boolean")
                if receipt["call_attempted"]:
                    counts[key] += 1
                    _require(
                        field == "jev_invocation" and point.startswith("deployment"),
                        "m2_deployment_jev_only",
                    )
                    timeout = receipt.get("timeout_seconds")
                    _require(
                        type(timeout) in (int, float)
                        and math.isfinite(timeout)
                        and 3 <= timeout <= min(30.0, deadline - before["time_s"] - 2.0)
                        and receipt.get("application_margin_s") == 2.0
                        and receipt.get("retries") == 0,
                        "deployment_provider_deadline",
                    )
    for record in study["return_study"]["decisions"]:
        for field, key in (("jev_invocation", "return_jev"), ("llm_invocation", "return_llm")):
            receipt = (record.get("response") or {}).get(field)
            if receipt is not None:
                _require(type(receipt.get("call_attempted")) is bool, "provider_attempt_boolean")
                if receipt["call_attempted"]:
                    counts[key] += 1
                    timeout = receipt.get("timeout_seconds")
                    request = record["request"]
                    _require(
                        type(timeout) in (int, float)
                        and math.isfinite(timeout)
                        and 3
                        <= timeout
                        <= min(provider_timeout_limit(authority, "return",
                            "deepseek" if field == "llm_invocation" else "typesafe", request),
                            request["deadline_s"] - request["time_s"] - 2.0)
                        and receipt.get("application_margin_s") == 2.0
                        and receipt.get("retries") == 0,
                        "return_provider_deadline",
                    )
    _require(
        counts["deployment_llm"] == 0
        and counts["deployment_jev"] <= envelope["maximum_jev_calls"]
        and counts["return_jev"] <= authority["return_authority"]["maximum_jev_calls"]
        and call_count_allowed(counts["return_llm"], authority["return_authority"]["maximum_llm_calls"])
        and call_count_allowed(sum(counts.values()), authority["maximum_model_calls"])
        and (study["runtime"]["model_calls"] if authority["mode"] == "fixture"
             else study["runtime"].get("actor_record_model_calls")) == sum(counts.values()),
        "m2_phase_model_budget",
    )
    from .starship_m2_attempts import verify_attempts
    return {**counts, "host_accounting": verify_attempts(study, counts)}
