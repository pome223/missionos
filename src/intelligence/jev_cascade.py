"""Explicit experimental routing inside Mission Assurance; no execution authority."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os

from src.intelligence.mission_assurance_agent import (
    ModelJudgment,
    MissionAssuranceJudgeUnavailable,
    MissionAssuranceJudgeError,
)
from src.intelligence.jev_assurance import JevAssuranceJudge

POLICY_VERSION = "jev_assurance_cascade.v2"
FAST_PATH_ENV = "MISSIONOS_JEV_CASCADE_FAST_PATH"
FAST_PATHS = {"disabled", "fixture_verified_detour_v1"}


def _escalation(reason, next_step):
    return {
        "proposed_response_kind": "operator_escalation",
        "parameters": {},
        "rationale": f"Cascade routing stopped: {reason}. This is an adapter template.",
        "expected_outcome": "No action is endorsed until the missing requirement is resolved.",
        "uncertainty": reason,
        "operator_question": next_step,
    }


def _fixture_fast_path(prompt, result, profile):
    """No physical/runtime applicability is inferred from the small fixture pilot."""
    if profile != "fixture_verified_detour_v1":
        return False
    situation = prompt["mission_situation"]
    observations = situation.get("observations", {})
    recovery = observations.get("runtime_recovery_agent_result", {}).get("assessment", {})
    feasibility = observations.get("source_action_feasibility", {})
    telemetry = observations.get("runtime_telemetry", {}).get("telemetry", {})
    return (
        situation.get("execution_scope") == "fixture"
        and recovery.get("selected_bounded_action") == "avoid_obstacle"
        and feasibility.get("feasibility_status") == "verified_feasible"
        and feasibility.get("action") == "avoid_obstacle"
        and telemetry.get("stale") is False
        and telemetry.get("dropout") is False
        and not situation.get("mission_contract", {}).get("response_mapping")
        and not situation.get("mission_contract", {})
        .get("mission_context", {})
        .get("response_mapping")
        and result.output.get("proposed_response_kind") in {"hold", "replan"}
        and result.invocation_evidence.get("review_signal") == "bounded"
    )


class JevCascadeJudge:
    """Route declared requirements first; consult Jev only for unresolved choices."""

    def __init__(self, reasoner, *, jev=None, fast_path=None):
        self.reasoner = reasoner
        self.jev = jev or JevAssuranceJudge(include_routing=True)
        self.fast_path = (
            fast_path if fast_path is not None else os.getenv(FAST_PATH_ENV, "disabled")
        )

    def judge(self, prompt):
        audit = {
            "policy_version": POLICY_VERSION,
            "fast_path_profile": self.fast_path,
            "confidence_used_for_routing": False,
            "reasoner_invoked": False,
            "jev_invoked": False,
            "dispatch_authority_created": False,
        }

        def finish(output, route, reason, selected=None):
            audit.update(route=route, reason=reason)
            return ModelJudgment(
                output=output,
                invocation_evidence={
                    **(dict(selected.invocation_evidence) if selected else {}),
                    "jev_cascade": audit,
                },
                model_inference_invoked=audit["jev_invoked"] or audit["reasoner_invoked"],
            )

        def stop(route, reason):
            question = (
                "Collect the missing observations, then request a fresh judgment."
                if route == "need_observation"
                else "Ask the operator to review the evidence and choose the next step."
            )
            return finish(_escalation(reason, question), route, reason)

        def provider_error(provider, exc, *, prior_invoked):
            unavailable = isinstance(exc, MissionAssuranceJudgeUnavailable)
            status = exc.status if isinstance(exc, MissionAssuranceJudgeError) else ("not_configured" if unavailable else "failed")
            invoked = exc.invoked if isinstance(exc, MissionAssuranceJudgeError) else not unavailable
            if isinstance(exc, MissionAssuranceJudgeError):
                audit[f"{provider}_failure"] = {
                    "reason": str(exc), "invocation_evidence": dict(exc.invocation_evidence),
                }
            reason = f"{provider}_{status}"
            audit.update(route="human_review", reason=reason)
            audit[f"{provider}_error_type"] = type(exc).__name__
            audit[f"{provider}_status"] = status
            audit[f"{provider}_invoked"] = invoked
            raise MissionAssuranceJudgeError(
                reason,
                status=status,
                invoked=prior_invoked or invoked,
                invocation_evidence={"jev_cascade": audit},
            ) from exc

        def reason(reason_code):
            audit["reasoner_invoked"] = True
            try:
                # Preserve original facts; Jev's answer is never an anchoring input.
                second = self.reasoner.judge(deepcopy(prompt))
                audit["reasoner"] = {
                    "output": dict(second.output),
                    "invocation_evidence": dict(second.invocation_evidence),
                }
                audit["reasoner_invoked"] = second.model_inference_invoked
            except Exception as exc:
                provider_error("reasoner", exc, prior_invoked=audit["jev_invoked"])
            return finish(second.output, "reasoner", reason_code, second)

        if self.fast_path not in FAST_PATHS:
            raise MissionAssuranceJudgeUnavailable("invalid_fast_path_profile")
        uncertainty = prompt["mission_situation"].get("uncertainty", {}).get("mission_context", {})
        if uncertainty.get("required_observations_missing"):
            return stop("need_observation", "declared_required_observations_missing")
        if uncertainty.get("operator_decision_required") is True:
            return stop("human_review", "declared_operator_decision_required")
        if uncertainty.get("requires_additional_reasoning") is True:
            return reason("declared_additional_reasoning")

        try:
            first = self.jev.judge(deepcopy(prompt))
            audit["jev_invoked"] = True
            audit["jev"] = {
                "output": dict(first.output),
                "invocation_evidence": dict(first.invocation_evidence),
            }
        except Exception as exc:
            provider_error("jev", exc, prior_invoked=False)

        route = first.invocation_evidence.get("assessment_route")
        if route in {"need_observation", "human_review"}:
            return stop(route, "jev_" + route)
        if route not in {"bounded", "deep_reasoning"}:
            return stop("human_review", "invalid_assessment_route")
        if first.output.get("proposed_response_kind") == "operator_escalation":
            return stop("human_review", "jev_response_requires_operator")
        if route == "bounded" and _fixture_fast_path(prompt, first, self.fast_path):
            return finish(first.output, "jev", "explicit_fixture_scope_matched", first)
        return reason(
            "jev_requested_reasoning" if route == "deep_reasoning" else "outside_fast_path"
        )


class JevCascadeShadowJudge:
    """Evaluate routing alongside the incumbent; the incumbent alone supplies output."""

    def __init__(self, primary, *, jev=None, fast_path=None):
        self.primary, self.jev, self.fast_path = primary, jev, fast_path

    def judge(self, prompt):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.primary.judge, deepcopy(prompt))

            class SharedReasoner:
                def judge(self, _prompt):
                    return future.result()

            try:
                candidate = JevCascadeJudge(
                    SharedReasoner(), jev=self.jev, fast_path=self.fast_path
                ).judge(deepcopy(prompt))
                candidate_output = dict(candidate.output)
                candidate_evidence = dict(candidate.invocation_evidence)
                candidate_status = {
                    "status": "observed",
                    "model_inference_invoked": candidate.model_inference_invoked,
                }
            except MissionAssuranceJudgeError as exc:
                candidate_output = {}
                candidate_evidence = dict(exc.invocation_evidence)
                candidate_status = {
                    "status": exc.status,
                    "model_inference_invoked": exc.invoked,
                    "blocking_reasons": [str(exc)],
                }
            primary_error = None
            try:
                primary = future.result()
            except Exception as exc:
                primary_error = exc
                primary = None
        shadow = {
            **candidate_status,
            "used_for_decision": False,
            "agrees_with_primary": primary is not None and candidate_output == primary.output,
            "response_agrees_with_primary": primary is not None
            and candidate_output.get("proposed_response_kind")
            == primary.output.get("proposed_response_kind"),
            "output": candidate_output,
            "invocation_evidence": candidate_evidence,
            "reasoner_reused_primary_call": candidate_evidence["jev_cascade"]["reasoner_invoked"],
        }
        if primary_error is not None:
            unavailable = isinstance(primary_error, MissionAssuranceJudgeUnavailable)
            typed = isinstance(primary_error, MissionAssuranceJudgeError)
            status = primary_error.status if typed else ("not_configured" if unavailable else "failed")
            invoked = primary_error.invoked if typed else not unavailable
            shadow["incumbent_error_type"] = type(primary_error).__name__
            if typed:
                shadow["incumbent_failure"] = {
                    "reason": str(primary_error),
                    "invocation_evidence": dict(primary_error.invocation_evidence),
                }
            raise MissionAssuranceJudgeError(
                f"incumbent_{status}",
                status=status,
                invoked=candidate_status.get("model_inference_invoked", True) or invoked,
                invocation_evidence={"jev_cascade_shadow": shadow},
            ) from primary_error
        return ModelJudgment(
            output=primary.output,
            invocation_evidence={**primary.invocation_evidence, "jev_cascade_shadow": shadow},
            model_inference_invoked=primary.model_inference_invoked
            or candidate_status["model_inference_invoked"],
        )
