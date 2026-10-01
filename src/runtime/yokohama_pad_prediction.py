"""WAM evidence -> mission judgment -> immutable candidate selection, without dispatch.

The native image probe cannot yet supply the calibrated semantic forecasts this
adapter requires. The CLI exercises this boundary with explicitly synthetic
forecasts and a fixture judge. Neither is an aircraft controller.
"""

from __future__ import annotations

from dataclasses import replace
import math

from missionos_core.prediction import prediction_digest
from src.intelligence.mission_assurance_agent import MissionAssuranceAgent
from src.intelligence.prediction_evidence import receive_prediction_evidence, snapshot


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _pad_contract(situation, envelope, now):
    """Owner-supplied capability/policy must be independent of model output.

    References bind an externally qualified profile; they do not perform or
    prove that qualification. There is currently no qualified native profile.
    """
    c = situation.constraints
    if situation.execution_scope not in {"fixture", "simulation"}:
        return "unsupported_execution_scope"
    if situation.progress != {"region": "city", "phase": "pad_wait"}:
        return "outside_city_pad_phase"
    profile = c["pad_forecast_profile"]
    if (
        profile["binding"] != envelope["context"]["binding"]
        or profile["clock"] != "sim"
        or not profile["semantic_validation_ref"]
        or not profile["time_calibration_ref"]
        or (situation.execution_scope != "fixture" and profile["source"] == "fixture")
    ):
        return "unqualified_pad_forecast_profile"
    options = envelope["request"]["options"]
    by_id = {o["option_id"]: o for o in options}
    if set(by_id) != {"hold", "vla"} or len(options) != 2:
        return "distinct_hold_and_vla_candidates_required"
    delta = c["vla_candidate"]["delta_body_frd"]
    if (
        not isinstance(delta, list)
        or len(delta) != 4
        or not all(finite(v) for v in delta)
        or not 1 <= delta[0] <= 3
        or delta[1:] != [0, 0, 0]
        or not c["vla_candidate"]["response_sha256"]
    ):
        return "invalid_native_step_candidate"
    if (
        by_id["hold"]["parameters"] != {"delta_body_frd": [0, 0, 0, 0]}
        or by_id["vla"]["parameters"] != c["vla_candidate"]
    ):
        return "candidate_changed_before_forecast"
    durations = c["candidate_duration_sim_s"]
    if set(durations) != {"hold", "vla"}:
        return "missing_candidate_duration"
    observed = envelope["request"]["observed_at"]
    for f in envelope["forecast"]["forecasts"]:
        duration = durations[f["option_id"]]
        if not finite(duration) or duration <= 0:
            return "invalid_candidate_duration"
        # An endpoint at an elapsed time is insufficient: the forecast must
        # describe conflict over the entire action interval, including latency.
        if now + duration > observed + f["horizon_seconds"]:
            return "forecast_expires_before_action_finishes"
        state = f["future_state"]
        if (
            set(state) != {"conflict_during_interval", "pad_state_at_horizon", "coverage"}
            or state["coverage"] != "observation_through_horizon"
            or type(state["conflict_during_interval"]) is not bool
            or state["pad_state_at_horizon"] not in {"occupied", "clear", "unknown"}
        ):
            return "missing_dynamic_pad_semantics"
    current = situation.observations
    if (
        not finite(current["observed_sim_s"])
        or not 0 <= now - current["observed_sim_s"] <= 1
        or current["bounded_hold_available"] is not True
        or current["wait_budget_available"] is not True
    ):
        return "current_hold_or_wait_budget_unavailable"
    return None


def evaluate(situation, envelope, judge, *, now, max_age_seconds=2):
    """Invoke the shared judge only with admitted, temporally useful evidence.

    The returned selection is for review, never authorization or execution. An
    eventual executor must independently revalidate approval and fresh Rules.
    """
    situation = replace(
        situation, allowed_response_kinds=("hold", "continue", "operator_escalation")
    )
    updated, admission = receive_prediction_evidence(
        situation, envelope, now=now, max_age_seconds=max_age_seconds
    )
    result = dict(
        schema="missionos.pad-prediction-selection.v1",
        execution_scope=situation.execution_scope,
        prediction_admission=admission,
        proposal=None,
        selected_candidate=None,
        selection_valid_for_review=False,
        judge_invoked=False,
        approval_recorded=False,
        dispatch_authority_created=False,
        dispatch_request_sent=False,
        physical_execution_invoked=False,
        completion_claimed=False,
    )
    if admission["status"] != "adopted":
        result["reason"] = admission["reason"]
        return result
    try:
        envelope = snapshot(envelope)
        reason = _pad_contract(situation, envelope, now)
    except (KeyError, TypeError, ValueError, AttributeError):
        reason = "malformed_pad_contract"
    if reason:
        result["reason"] = reason
        return result
    # Make exact source-bound candidates visible; never ask VLA to manufacture
    # zero motion to veto its own positive proposal.
    constraints = snapshot(dict(updated.constraints))
    constraints["pad_selection_contract"] = dict(
        hold_selects="hold",
        continue_selects="vla",
        forecast_is_not_current_occupancy=True,
        candidates=envelope["request"]["options"],
        required_parameter="candidate_id",
    )
    updated = replace(updated, constraints=constraints)
    updated = replace(updated, input_digest=prediction_digest(updated.to_dict()))
    proposal = MissionAssuranceAgent(judge).evaluate(updated).to_dict()
    result.update(judge_invoked=True, proposal=proposal, situation=updated.to_dict())
    if proposal["judgment_status"] != "proposal_guardrail_passed":
        result["reason"] = "mission_judgment_unavailable"
        return result
    kind = proposal["proposed_response_kind"]
    if kind == "operator_escalation":
        result["reason"] = "mission_judge_requested_reassessment"
        return result
    selected = "hold" if kind == "hold" else "vla"
    if proposal["parameters"] != {"candidate_id": selected}:
        result["reason"] = "judge_modified_or_mismatched_candidate"
        return result
    if selected == "vla" and not all(
        situation.observations.get(k) is True for k in ("pad_clear", "approach_clear")
    ):
        result["reason"] = "current_pad_or_approach_not_clear"
        return result
    forecast = next(f for f in envelope["forecast"]["forecasts"] if f["option_id"] == selected)
    if forecast["future_state"]["conflict_during_interval"]:
        result["reason"] = "selected_forecast_contains_conflict"
        return result
    if selected == "vla" and forecast["future_state"]["pad_state_at_horizon"] != "clear":
        result["reason"] = "selected_forecast_does_not_support_clear_pad"
        return result
    option = next(o for o in envelope["request"]["options"] if o["option_id"] == selected)
    result.update(
        selected_candidate=option,
        selection_valid_for_review=True,
        reason="source_bound_proposal_only_recheck_approval_and_rules_at_execution",
    )
    return result
