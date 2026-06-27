#!/usr/bin/env python3
"""Build an obstacle alternate-route Form 3 readiness audit.

This script does not add a new applicator, dispatcher, verifier, or gate. It
reads an existing obstacle -> alternate-route closed-loop run and records a
second response-candidate / boundary-reobservation shape:

1. obstacle blocks route -> alternate route response -> alternate waypoint observed
2. alternate waypoint observed -> operator-review advisory -> boundary re-observed

Because cycle 2 does not execute a new bounded action or observe an action
outcome, this artifact is a Form 3 candidate/readiness record rather than strict
Form 3 capability progress.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from scripts.audit_mission_designer_obstacle_alternate_route_closed_loop import (
    DEFAULT_PROGRESS_THRESHOLD_M,
    DEFAULT_WAYPOINT_THRESHOLD_M,
    _read_json,
    _summarize_closed_loop,
    _write_json,
)


SCHEMA_VERSION = "mission_designer_obstacle_alternate_route_form3_reobservation.v1"
RESPONSE_CANDIDATE_REF = (
    "mission_response_candidate:"
    "obstacle_alternate_route_post_waypoint_operator_review"
)
POLICY_GATE_REF = (
    "policy_gate_result:"
    "obstacle_alternate_route_post_waypoint_advisory_only"
)
OPERATOR_DECISION_REF = (
    "operator_review_required:"
    "obstacle_alternate_route_post_waypoint"
)
AI_SITUATION_ASSESSMENT_REF = (
    "ai_mission_situation_assessment:"
    "obstacle_alternate_route_post_waypoint_operator_review"
)


def _summary_path(run_dir: Path) -> Path:
    path = run_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found under {run_dir}")
    return path


def _bool_at(summary: dict[str, Any], path: tuple[str, ...]) -> bool:
    node: Any = summary
    for key in path:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return node is True


def _build_cycle2_advisory(summary: dict[str, Any]) -> dict[str, Any]:
    route_execution = summary.get("alternate_route_execution_evidence") or {}
    route_observed = route_execution.get("observed") or {}
    return {
        "schema_version": "mission_response_candidate.v1",
        "candidate_id": "obstacle_alternate_route_post_waypoint_operator_review",
        "causal_form": "Form 2b",
        "form2_subtype": "Form 2b",
        "mission_response_kind": "advisory",
        "trigger_level": "level_2_inferred",
        "candidate_generated_by": "deterministic_rule",
        "candidate_confidence": "medium",
        "ai_situation_assessment_ref": AI_SITUATION_ASSESSMENT_REF,
        "mission_response_candidate": "operator_escalation",
        "mission_response_advisory_reason": (
            "alternate_waypoint_reached_but_original_dropoff_unverified"
        ),
        "required_action": (
            "operator_review_original_objective_after_alternate_route_waypoint"
        ),
        "operator_question": (
            "Alternate waypoint was reached, but the original dropoff remains "
            "unverified. Should Mission OS abort, hold, or request a new "
            "operator-approved bounded route?"
        ),
        "uncertainty_reasons": [
            "original_dropoff_verified_false",
            "delivery_completion_claimed_false",
            "alternate_route_is_recovery_state_not_delivery_completion",
        ],
        "operator_review_required": True,
        "automatic_dispatch_suppressed": True,
        "eligible_for_direct_trigger": False,
        "eligible_for_advisory_only": True,
        "forbidden_action": "automatic_dispatch_to_recovery_without_operator_review",
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "source_observation_ref": (
            "alternate_route_execution_evidence:mission_designer_route_blocking"
        ),
        "observed": {
            "alternate_waypoint_reached_observed": route_execution.get(
                "alternate_waypoint_reached_observed"
            ),
            "completion_basis": route_observed.get("completion_basis"),
            "original_dropoff_verified": route_observed.get(
                "original_dropoff_verified"
            ),
            "dropoff_verified": route_observed.get("dropoff_verified"),
            "delivery_completion_claimed": route_observed.get(
                "delivery_completion_claimed"
            ),
        },
    }


def _build_ai_situation_assessment(advisory: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "ai_mission_situation_assessment.v1",
        "assessment_id": (
            "ai_mission_situation_assessment:"
            "obstacle_alternate_route_post_waypoint_operator_review"
        ),
        "cycle_index": 2,
        "assessment_generated_by": "deterministic_rule",
        "ai_model_invoked": False,
        "assessment_authority_scope": "mission_evidence_only",
        "assessment_is_gate_input": True,
        "assessment_is_gate_verdict": False,
        "assessment_created_dispatch_authority": False,
        "source_observation_ref": advisory["source_observation_ref"],
        "input_response_candidate_ref": RESPONSE_CANDIDATE_REF,
        "mission_state_interpretation": (
            "alternate_waypoint_reached_original_dropoff_unverified"
        ),
        "assessment_summary": (
            "The alternate waypoint was reached, but the original dropoff is "
            "still unverified and delivery completion remains false. The next "
            "step is an operator-review advisory, not automatic dispatch."
        ),
        "mission_response_candidate": advisory["mission_response_candidate"],
        "candidate_confidence": advisory["candidate_confidence"],
        "uncertainty_reasons": advisory["uncertainty_reasons"],
        "operator_question": advisory["operator_question"],
        "operator_review_required": True,
        "automatic_dispatch_suppressed": True,
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "llm_gate_judge_used": False,
        "created_dispatch_authority": False,
        "authority_flags": {
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "observed": advisory["observed"],
    }


def _build_policy_gate_result(
    advisory: dict[str, Any],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "policy_gate_result.v1",
        "gate_id": "policy_gate:obstacle_form3_cycle2",
        "cycle_index": 2,
        "input_response_candidate_ref": RESPONSE_CANDIDATE_REF,
        "gate_status": "escalate",
        "gate_decision_basis": [
            "original_dropoff_unverified",
            "delivery_completion_claimed_false",
            "automatic_dispatch_suppressed_required",
        ],
        "operator_review_required": True,
        "automatic_dispatch_allowed": False,
        "allowed_actions": [
            "operator_review",
            "hold",
            "mission_abort",
            "request_new_bounded_route",
        ],
        "forbidden_actions": [
            "automatic_dispatch_to_recovery_without_operator_review",
            "delivery_completion_claim",
        ],
        "ai_situation_assessment_ref": AI_SITUATION_ASSESSMENT_REF,
        "ai_assessment_required": True,
        "ai_proposal_confidence": assessment["candidate_confidence"],
        "ai_judgment_used_for_gate": False,
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "llm_gate_judge_used": False,
        "authority_flags": {
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "source_advisory_ref": advisory.get("source_observation_ref"),
        "forward_compat_note": (
            "ai_situation_assessment_ref is linked as mission evidence only; "
            "the gate decision remains deterministic and authority-boundary "
            "flags remain false"
        ),
    }


def _build_operator_decision_record(
    advisory: dict[str, Any],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "operator_decision_record.v1",
        "decision_id": "operator_decision:obstacle_form3_cycle2",
        "cycle_index": 2,
        "advisory_ref": RESPONSE_CANDIDATE_REF,
        "operator_question": advisory["operator_question"],
        "operator_question_options": [
            "hold",
            "mission_abort",
            "request_new_bounded_route",
            "reject_advisory",
        ],
        "selected_option": "operator_review_required",
        "selection_status": "pending_operator_review",
        "selection_reason": None,
        "ai_situation_assessment_ref": assessment["assessment_id"],
        "operator_review_required": True,
        "automatic_dispatch_suppressed": True,
        "created_dispatch_authority": False,
        "bounded_action_ref": None,
        "dispatch_ref": None,
        "source_policy_gate_ref": POLICY_GATE_REF,
    }


def _summarize_form3(
    run_dir: Path,
    *,
    progress_threshold_m: float,
    waypoint_threshold_m: float,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    closed_loop = _summarize_closed_loop(
        run_dir,
        progress_threshold_m=progress_threshold_m,
        waypoint_threshold_m=waypoint_threshold_m,
    )
    cycle2_advisory = _build_cycle2_advisory(summary)
    ai_situation_assessment = _build_ai_situation_assessment(cycle2_advisory)
    policy_gate_result = _build_policy_gate_result(
        cycle2_advisory, ai_situation_assessment
    )
    operator_decision_record = _build_operator_decision_record(
        cycle2_advisory, ai_situation_assessment
    )
    cycle1_observed = closed_loop["closed_loop_observed"] is True
    cycle2_observation = (
        cycle2_advisory["observed"]["alternate_waypoint_reached_observed"] is True
        and cycle2_advisory["observed"]["original_dropoff_verified"] is False
        and cycle2_advisory["observed"]["delivery_completion_claimed"] is False
    )
    cycle2_response = (
        cycle2_advisory["operator_review_required"] is True
        and cycle2_advisory["automatic_dispatch_suppressed"] is True
        and cycle2_advisory["ai_judgment_is_gate_verdict"] is False
        and cycle2_advisory["ai_judgment_created_dispatch_authority"] is False
    )
    cycle2_reobservation = (
        summary.get("task_status") == "blocked"
        and summary.get("final_status") == "blocked"
        and summary.get("delivery_completion_claimed") is False
        and summary.get("hardware_target_allowed") is False
        and summary.get("physical_execution_invoked") is False
        and not _bool_at(summary, ("alternate_route_execution_evidence", "observed", "auto_gate"))
        and not _bool_at(
            summary,
            ("alternate_route_execution_evidence", "observed", "task_status_mutated"),
        )
        and not _bool_at(
            summary,
            ("alternate_route_execution_evidence", "observed", "gate_status_mutated"),
        )
    )
    checks = {
        "cycle1_closed_loop_observed": cycle1_observed,
        "cycle2_reobservation_input_observed": cycle2_observation,
        "cycle2_advisory_response_selected": cycle2_response,
        "cycle2_post_response_boundary_reobserved": cycle2_reobservation,
        "cycle2_bounded_action_observed": False,
        "policy_gate_result_artifact_observed": True,
        "operator_decision_record_artifact_observed": True,
    }
    readiness_checks = {
        key: value
        for key, value in checks.items()
        if key
        not in {
            "cycle2_bounded_action_observed",
            "policy_gate_result_artifact_observed",
            "operator_decision_record_artifact_observed",
        }
    }
    readiness_missing = [
        name for name, passed in readiness_checks.items() if not passed
    ]
    form3_candidate = not readiness_missing
    missing_form3_requirements = [
        "cycle2_bounded_action_absent",
        "cycle2_action_outcome_observation_absent",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": (
            "mission_designer_obstacle_alternate_route_form3_reobservation:"
            "mission_designer_collision_obstacle"
        ),
        "condition_kind": "obstacle_alternate_route_form3_reobservation",
        "causal_form": "Form 0b",
        "audit_status": "form3_candidate" if form3_candidate else "unsupported",
        "form3_candidate": form3_candidate,
        "form3_claim_supported": False,
        "progress_counted": False,
        "cycle_count": 1,
        "candidate_cycle_count": 2 if form3_candidate else 1,
        "missing_form3_requirements": missing_form3_requirements,
        "artifact_dir": str(run_dir),
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed" for name in readiness_missing
        ],
        "cycles": [
            {
                "cycle_index": 1,
                "observation_ref": closed_loop["source_refs"][
                    "route_blocking_verification"
                ],
                "response_ref": closed_loop["source_refs"][
                    "alternate_mission_upload_receipt"
                ],
                "re_observation_ref": closed_loop["source_refs"][
                    "alternate_route_execution"
                ],
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": cycle1_observed,
                "observed": cycle1_observed,
            },
            {
                "cycle_index": 2,
                "observation_ref": closed_loop["source_refs"][
                    "alternate_route_execution"
                ],
                "mission_response_candidate_ref": RESPONSE_CANDIDATE_REF,
                "policy_gate_ref": POLICY_GATE_REF,
                "operator_decision_ref": OPERATOR_DECISION_REF,
                "response_ref": RESPONSE_CANDIDATE_REF,
                "re_observation_ref": (
                    "post_response_boundary_observation:"
                    "obstacle_alternate_route_advisory"
                ),
                "mission_response_kind": "advisory",
                "form2_subtype": "Form 2b",
                "observed": cycle2_observation
                and cycle2_response
                and cycle2_reobservation,
                "qualifies_as_form3_cycle": False,
                "form3_candidate_cycle": cycle2_observation
                and cycle2_response
                and cycle2_reobservation,
                "missing_form3_requirements": missing_form3_requirements,
            },
        ],
        "cycle2_mission_response_candidate": cycle2_advisory,
        "cycle2_ai_situation_assessment": ai_situation_assessment,
        "cycle2_policy_gate_result": policy_gate_result,
        "cycle2_operator_decision_record": operator_decision_record,
        "ai_situation_assessment_refs": [AI_SITUATION_ASSESSMENT_REF],
        "mission_response_candidate_refs": [RESPONSE_CANDIDATE_REF],
        "policy_gate_refs": [POLICY_GATE_REF],
        "operator_decision_refs": [OPERATOR_DECISION_REF],
        "re_observation_refs": [
            closed_loop["source_refs"]["alternate_route_execution"],
            "post_response_boundary_observation:obstacle_alternate_route_advisory",
        ],
        "delivery_completion_claimed": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit obstacle alternate-route Form 3 re-observation."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    parser.add_argument(
        "--progress-threshold-m",
        type=float,
        default=DEFAULT_PROGRESS_THRESHOLD_M,
    )
    parser.add_argument(
        "--waypoint-threshold-m",
        type=float,
        default=DEFAULT_WAYPOINT_THRESHOLD_M,
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"obstacle_form3_reobservation_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    artifact = _summarize_form3(
        args.run_dir,
        progress_threshold_m=args.progress_threshold_m,
        waypoint_threshold_m=args.waypoint_threshold_m,
    )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = "existing_run"
    output_path = audit_dir / "mission_designer_obstacle_form3_reobservation.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["form3_candidate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
