#!/usr/bin/env python3
"""Audit payload advisory with a scoped Mission OS supervisor Form 3 loop.

The strict payload supervisor slice consumes a source-bound Form 2b payload
feasibility advisory, dispatches a supervisor-approved bounded RTL action,
uses that RTL outcome as cycle 2 input, then dispatches a separate bounded LAND
action and observes the LAND outcome. Missing cycle 2 action/outcome evidence is
recorded as Form 0b instead of being counted as Form 3 progress.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from scripts.audit_mission_designer_payload_recovery_action import (
    DEFAULT_PAYLOAD_MASS_KG,
    PAYLOAD_ADVISORY_REF,
    PAYLOAD_RECOVERY_ACTION_REF,
    UNSAFE_AUTHORITY_KEYS as PAYLOAD_UNSAFE_AUTHORITY_KEYS,
    _advisory_source_bound,
    _load_advisory,
    _nested_true_keys,
    _payload_application_source_bound,
    _read_json,
    _summary_path,
    _write_json,
)

SCHEMA_VERSION = "mission_designer_payload_supervisor_form3_closed_loop_audit.v1"
PAYLOAD_SUPERVISOR_SCOPE = "payload_form3_sitl_only"
PAYLOAD_SUPERVISOR_POST_RECOVERY_ACTION_REF = (
    "payload_supervisor_post_recovery_action:mission_designer_payload_mass"
)
PAYLOAD_RTL_RESPONSE_REF = "mission_response_candidate:payload_advisory_bounded_rtl"
PAYLOAD_LAND_RESPONSE_REF = "mission_response_candidate:payload_rtl_state_bounded_land"
POLICY_GATE_REF = "policy_gate_result:payload_rtl_state_bounded_land"
OPERATOR_DECISION_REF = "operator_decision:payload_rtl_state_bounded_land"
AI_ASSESSMENT_REF = "ai_mission_situation_assessment:payload_rtl_state_bounded_land"
UNSAFE_AUTHORITY_KEYS = tuple(
    sorted(
        set(PAYLOAD_UNSAFE_AUTHORITY_KEYS)
        | {
            "dispatch_authority_created",
            "created_dispatch_authority",
            "ai_judgment_is_gate_verdict",
            "ai_judgment_created_dispatch_authority",
            "llm_gate_judge_used",
        }
    )
)


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _latest_partial_run_dir(artifact_root: Path) -> Path | None:
    if not artifact_root.exists():
        return None
    candidates = [
        path
        for path in artifact_root.rglob("pose_samples.jsonl")
        if path.parent.is_dir()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime).parent


def _read_pose_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _pose_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    phases = Counter(str(row.get("phase") or "unknown") for row in rows)
    z_values = [
        _as_float((row.get("sample") or {}).get("z"))
        for row in rows
        if isinstance(row, dict)
    ]
    z_values = [value for value in z_values if value is not None]
    return {
        "pose_sample_count": len(rows),
        "phase_counts": dict(sorted(phases.items())),
        "first_phase": rows[0].get("phase") if rows else None,
        "last_phase": rows[-1].get("phase") if rows else None,
        "min_pose_z_m": min(z_values) if z_values else None,
        "max_pose_z_m": max(z_values) if z_values else None,
    }


def _build_partial_run_artifact(
    run_dir: Path,
    *,
    audit_dir: Path,
    run_mode: str,
    smoke_error: str | None = None,
) -> dict[str, Any]:
    summary = (
        _read_json(run_dir / "summary.json")
        if (run_dir / "summary.json").exists()
        else None
    )
    pose_rows = _read_pose_rows(run_dir / "pose_samples.jsonl")
    missing = []
    if summary is None:
        missing.append("summary_json_missing")
    if not pose_rows:
        missing.append("pose_samples_missing")
    if not (summary or {}).get("payload_recovery_state_observed"):
        missing.append("cycle1_payload_rtl_state_observation_missing")
    if not (summary or {}).get("post_recovery_dispatch_ref"):
        missing.append("cycle2_land_dispatch_missing")
    if not (summary or {}).get("payload_supervisor_post_recovery_action_ref"):
        missing.append("cycle2_land_action_outcome_observation_missing")
    return {
        "schema_version": "mission_designer_payload_supervisor_form3_partial_run_diagnostic.v1",
        "diagnostic_id": (
            "mission_designer_payload_supervisor_form3_partial_run_diagnostic:"
            f"{run_dir.name}"
        ),
        "condition_kind": "source_bound_payload_supervisor_form3_live_blocker",
        "causal_form": "Form 0b",
        "diagnostic_status": "live_blocker_diagnosed",
        "form3_claim_supported": False,
        "progress_counted": False,
        "audit_dir": str(audit_dir),
        "artifact_dir": str(run_dir),
        "run_mode": run_mode,
        "summary_json_present": summary is not None,
        "pose_samples_present": bool(pose_rows),
        "pose_trace": _pose_summary(pose_rows),
        "ready_blocker": "live_px4_gazebo_payload_supervisor_form3_not_observed",
        "missing_evidence": missing,
        "smoke_error_digest": None if smoke_error is None else smoke_error[-2000:],
        "safety_boundary": {
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "diagnostic_created_dispatch_authority": False,
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _payload_rtl_recovery_observed(summary: dict[str, Any], advisory_ref: str) -> bool:
    action = summary.get("payload_recovery_action_artifact") or {}
    dispatch_ref = summary.get("payload_recovery_dispatch_ref")
    pre_recovery_distance = _as_float(
        summary.get("payload_pre_recovery_distance_to_pickup_m")
    )
    recovery_distance = _as_float(summary.get("payload_recovery_distance_to_pickup_m"))
    route_progress_observed = (
        summary.get("payload_route_progress_away_from_pickup_observed") is True
        and pre_recovery_distance is not None
        and pre_recovery_distance >= 2.5
    )
    rtl_distance_reduced = (
        pre_recovery_distance is not None
        and recovery_distance is not None
        and recovery_distance <= 2.0
        and recovery_distance < pre_recovery_distance
    )
    return (
        action.get("schema_version") == "payload_recovery_action.v1"
        and action.get("action_ref") == PAYLOAD_RECOVERY_ACTION_REF
        and action.get("payload_feasibility_advisory_ref") == advisory_ref
        and action.get("bounded_action_kind") == "rtl"
        and action.get("dispatch_status") in ("accepted", "timeout")
        and action.get("dispatch_ref") == dispatch_ref
        and action.get("bounded_action_ref") == dispatch_ref
        and str(action.get("dispatch_ref", "")).startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
        and action.get("recovery_state_observed") is True
        and action.get("recovery_completed") is True
        and action.get("recovery_state_label") == "return_to_launch_state_observed"
        and action.get("delivery_completion_claimed") is False
        and action.get("hardware_target_allowed") is False
        and action.get("physical_execution_invoked") is False
        and route_progress_observed
        and rtl_distance_reduced
        and summary.get("payload_recovery_state_observed") is True
        and summary.get("payload_recovery_completed") is True
        and summary.get("payload_recovery_state_label")
        == "return_to_launch_state_observed"
    )


def _payload_land_outcome_observed(summary: dict[str, Any], advisory_ref: str) -> bool:
    action = summary.get("payload_supervisor_post_recovery_action_artifact") or {}
    dispatch_ref = summary.get("post_recovery_dispatch_ref")
    pose_z = _as_float(summary.get("post_recovery_pose_z_m"))
    return (
        action.get("schema_version") == "payload_supervisor_post_recovery_action.v1"
        and action.get("action_ref") == PAYLOAD_SUPERVISOR_POST_RECOVERY_ACTION_REF
        and action.get("payload_feasibility_advisory_ref") == advisory_ref
        and action.get("decision_loop_driver") == "mission_os_supervisor"
        and action.get("supervisor_scope") == PAYLOAD_SUPERVISOR_SCOPE
        and action.get("full_gateway_runtime_loop") is False
        and action.get("bounded_action_kind") == "land"
        and action.get("dispatch_status") in ("accepted", "timeout")
        and action.get("dispatch_ref") == dispatch_ref
        and action.get("bounded_action_ref") == dispatch_ref
        and str(action.get("dispatch_ref", "")).startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
        and action.get("recovery_state_observed") is True
        and action.get("recovery_completed") is True
        and pose_z is not None
        and pose_z <= 0.15
        and action.get("delivery_completion_claimed") is False
        and action.get("hardware_target_allowed") is False
        and action.get("physical_execution_invoked") is False
    )


def _dispatch_chain_distinct(summary: dict[str, Any]) -> bool:
    cycle1 = summary.get("payload_recovery_dispatch_ref")
    cycle2 = summary.get("post_recovery_dispatch_ref")
    cycle1_action = summary.get("payload_recovery_action_artifact") or {}
    cycle2_action = (
        summary.get("payload_supervisor_post_recovery_action_artifact") or {}
    )
    return bool(
        cycle1
        and cycle2
        and cycle1 != cycle2
        and cycle1_action.get("dispatch_ref") == cycle1
        and cycle1_action.get("bounded_action_ref") == cycle1
        and cycle2_action.get("dispatch_ref") == cycle2
        and cycle2_action.get("bounded_action_ref") == cycle2
    )


def _cycle_ref_chain_supported(
    cycle: dict[str, Any],
    *,
    cycle_index: int,
    expected_source_observation_ref: str,
    expected_response_ref: str,
    expected_primary_trigger: str,
    expected_action: str,
    expected_dispatch_ref: str,
    expected_approval_ref: str,
    expected_outcome_ref: str,
) -> bool:
    if not isinstance(cycle, dict):
        return False
    decision = cycle.get("decision")
    request = cycle.get("action_request")
    receipt = cycle.get("action_receipt")
    outcome = cycle.get("outcome_observation")
    if not all(
        isinstance(artifact, dict) for artifact in (decision, request, receipt, outcome)
    ):
        return False
    decision_id = decision.get("decision_id")
    request_id = request.get("request_id")
    receipt_id = receipt.get("receipt_id")
    outcome_id = outcome.get("observation_id")
    return (
        cycle.get("cycle_index") == cycle_index
        and cycle.get("decision_ref") == decision_id
        and cycle.get("action_request_ref") == request_id
        and cycle.get("action_receipt_ref") == receipt_id
        and cycle.get("outcome_observation_ref") == outcome_id
        and decision.get("schema_version") == "mission_os_recovery_decision.v1"
        and decision.get("cycle_index") == cycle_index
        and decision.get("decision_loop_driver") == "mission_os_supervisor"
        and decision.get("supervisor_scope") == PAYLOAD_SUPERVISOR_SCOPE
        and decision.get("full_gateway_runtime_loop") is False
        and decision.get("source_observation_ref") == expected_source_observation_ref
        and decision.get("mission_response_candidate_ref") == expected_response_ref
        and decision.get("primary_trigger")
        == "payload_feasibility_advisory_operator_review_required"
        and decision.get("selected_bounded_action") == expected_action
        and decision.get("operator_approval_required") is True
        and decision.get("automatic_dispatch_allowed") is False
        and decision.get("operator_approved_dispatch_allowed") is True
        and decision.get("ai_judgment_is_gate_verdict") is False
        and decision.get("ai_judgment_created_dispatch_authority") is False
        and decision.get("llm_gate_judge_used") is False
        and decision.get("created_dispatch_authority") is False
        and decision.get("delivery_completion_claimed") is False
        and decision.get("hardware_target_allowed") is False
        and decision.get("physical_execution_invoked") is False
        and request.get("schema_version") == "mission_os_backend_action_request.v1"
        and request.get("cycle_index") == cycle_index
        and request.get("decision_ref") == decision_id
        and request.get("backend_target") == "px4_gazebo_sitl"
        and request.get("bounded_action") == expected_action
        and request.get("expected_dispatch_ref") == expected_dispatch_ref
        and request.get("approval_ref") == expected_approval_ref
        and request.get("allowlisted_action") is True
        and request.get("operator_approved") is True
        and request.get("automatic_dispatch_allowed") is False
        and request.get("dispatch_authority_created") is False
        and request.get("hardware_target_allowed") is False
        and request.get("physical_execution_invoked") is False
        and receipt.get("schema_version") == "mission_os_backend_action_receipt.v1"
        and receipt.get("cycle_index") == cycle_index
        and receipt.get("action_request_ref") == request_id
        and receipt.get("dispatch_ref") == expected_dispatch_ref
        and receipt.get("dispatch_observed") is True
        and receipt.get("backend_target") == "px4_gazebo_sitl"
        and receipt.get("hardware_target_allowed") is False
        and receipt.get("physical_execution_invoked") is False
        and outcome.get("schema_version")
        == "mission_os_recovery_outcome_observation.v1"
        and outcome.get("cycle_index") == cycle_index
        and outcome.get("action_receipt_ref") == receipt_id
        and outcome.get("outcome_observation_ref") == expected_outcome_ref
        and outcome.get("outcome_observed") is True
        and outcome.get("delivery_completion_claimed") is False
        and outcome.get("hardware_target_allowed") is False
        and outcome.get("physical_execution_invoked") is False
    )


def _supervisor_loop_observed(summary: dict[str, Any]) -> bool:
    loop = summary.get("mission_os_supervisor_recovery_loop")
    if not isinstance(loop, dict):
        return False
    cycles = loop.get("cycles") or []
    if len(cycles) != 2:
        return False
    cycle1_action = summary.get("payload_recovery_action_artifact") or {}
    cycle2_action = (
        summary.get("payload_supervisor_post_recovery_action_artifact") or {}
    )
    cycle1_dispatch = summary.get("payload_recovery_dispatch_ref")
    cycle2_dispatch = summary.get("post_recovery_dispatch_ref")
    cycle1_outcome = summary.get("payload_recovery_action_ref")
    cycle2_outcome = summary.get("payload_supervisor_post_recovery_action_ref")
    return (
        summary.get("decision_loop_driver") == "mission_os_supervisor"
        and summary.get("supervisor_scope") == PAYLOAD_SUPERVISOR_SCOPE
        and summary.get("full_gateway_runtime_loop") is False
        and loop.get("decision_loop_driver") == "mission_os_supervisor"
        and loop.get("supervisor_scope") == PAYLOAD_SUPERVISOR_SCOPE
        and loop.get("full_gateway_runtime_loop") is False
        and loop.get("supervisor_loop_claim_supported") is True
        and loop.get("conflicting_risks") == []
        and _cycle_ref_chain_supported(
            cycles[0],
            cycle_index=1,
            expected_source_observation_ref=PAYLOAD_ADVISORY_REF,
            expected_response_ref=PAYLOAD_RTL_RESPONSE_REF,
            expected_primary_trigger="payload_feasibility_advisory_operator_review_required",
            expected_action="rtl",
            expected_dispatch_ref=cycle1_dispatch,
            expected_approval_ref=cycle1_action.get("approval_ref"),
            expected_outcome_ref=cycle1_outcome,
        )
        and _cycle_ref_chain_supported(
            cycles[1],
            cycle_index=2,
            expected_source_observation_ref=cycle1_outcome,
            expected_response_ref=PAYLOAD_LAND_RESPONSE_REF,
            expected_primary_trigger="payload_feasibility_advisory_operator_review_required",
            expected_action="land",
            expected_dispatch_ref=cycle2_dispatch,
            expected_approval_ref=cycle2_action.get("approval_ref"),
            expected_outcome_ref=cycle2_outcome,
        )
    )


def _build_ai_assessment(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "ai_mission_situation_assessment.v1",
        "assessment_id": AI_ASSESSMENT_REF,
        "cycle_index": 2,
        "assessment_generated_by": "deterministic_rule",
        "ai_model_invoked": False,
        "assessment_authority_scope": "mission_evidence_only",
        "assessment_is_gate_input": True,
        "assessment_is_gate_verdict": False,
        "assessment_created_dispatch_authority": False,
        "source_observation_ref": summary.get("payload_recovery_action_ref"),
        "mission_state_interpretation": "payload_rtl_state_observed_after_advisory",
        "mission_response_candidate": "bounded_land",
        "candidate_confidence": "medium",
        "uncertainty_reasons": [
            "payload_feasibility_advisory_required_operator_review",
            "rtl_state_observed_not_delivery_completion",
            "dropoff_unverified",
        ],
        "operator_question": (
            "Payload advisory recovery reached RTL state. Approve a separate "
            "bounded LAND action and verify landing outcome?"
        ),
        "operator_review_required": True,
        "automatic_dispatch_suppressed": False,
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "llm_gate_judge_used": False,
        "created_dispatch_authority": False,
    }


def _build_policy_gate_result(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "policy_gate_result.v1",
        "gate_id": POLICY_GATE_REF,
        "cycle_index": 2,
        "input_response_candidate_ref": PAYLOAD_LAND_RESPONSE_REF,
        "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
        "ai_assessment_required": True,
        "gate_status": "pass",
        "gate_decision_basis": [
            "source_bound_payload_advisory_observed",
            "return_to_launch_state_observed",
            "operator_approval_required",
            "bounded_land_allowlisted",
        ],
        "operator_review_required": True,
        "automatic_dispatch_allowed": False,
        "operator_approved_dispatch_allowed": True,
        "bounded_action_dispatch_allowed": True,
        "allowed_actions": ["bounded_land"],
        "forbidden_actions": [
            "delivery_completion_claim",
            "approval_free_dispatch",
            "hardware_execution",
            "physical_execution",
        ],
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
    }


def _build_operator_decision_record(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "operator_decision_record.v1",
        "decision_id": OPERATOR_DECISION_REF,
        "cycle_index": 2,
        "advisory_ref": PAYLOAD_LAND_RESPONSE_REF,
        "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
        "operator_question": ("Payload RTL state was observed. Approve bounded LAND?"),
        "operator_question_options": ["approve_bounded_land", "hold", "abort"],
        "selected_option": "approve_bounded_land",
        "selection_status": "operator_approved",
        "selected_bounded_action": "land",
        "created_dispatch_authority": False,
        "bounded_action_ref": "bounded_action:payload_rtl_state_land",
        "dispatch_ref": summary.get("post_recovery_dispatch_ref"),
        "action_outcome_observation_ref": summary.get(
            "payload_supervisor_post_recovery_action_ref"
        ),
    }


def _summarize_payload_supervisor_form3(
    *,
    advisory: dict[str, Any],
    run_dir: Path,
    expected_payload_kg: float,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    advisory_ref = str(advisory.get("advisory_ref") or "")
    checks = {
        "advisory_source_bound": _advisory_source_bound(advisory),
        "horizontal_route_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        )
        is True,
        "payload_application_source_bound": _payload_application_source_bound(
            summary,
            expected_payload_kg=expected_payload_kg,
        ),
        "cycle1_payload_rtl_action_outcome_observed": (
            _payload_rtl_recovery_observed(summary, advisory_ref)
        ),
        "cycle2_payload_land_action_outcome_observed": (
            _payload_land_outcome_observed(summary, advisory_ref)
        ),
        "cycle2_dispatch_chain_distinct_from_cycle1": _dispatch_chain_distinct(summary),
        "mission_os_supervisor_loop_observed": _supervisor_loop_observed(summary),
        "dropoff_not_claimed": summary.get("dropoff_region_reached") is False
        and summary.get("dropoff_verified") is False
        and summary.get("delivery_completion_claimed") is False,
        "top_level_hardware_physical_false": summary.get("hardware_target_allowed")
        is False
        and summary.get("physical_execution_invoked") is False,
        "unsafe_authority_flags_absent": not unsafe_flags,
    }
    missing = [name for name, passed in checks.items() if not passed]
    form3_observed = not missing
    ai_assessment = _build_ai_assessment(summary)
    policy_gate = _build_policy_gate_result(summary)
    operator_decision = _build_operator_decision_record(summary)
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": (
            "mission_designer_payload_supervisor_form3_closed_loop_audit:"
            "mission_designer_payload_mass"
        ),
        "condition_kind": "source_bound_payload_supervisor_form3_closed_loop",
        "causal_form": "Form 3" if form3_observed else "Form 0b",
        "audit_status": "form3_observed" if form3_observed else "unsupported",
        "form3_claim_supported": form3_observed,
        "progress_counted": form3_observed,
        "source_bound": form3_observed,
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": PAYLOAD_SUPERVISOR_SCOPE,
        "full_gateway_runtime_loop": False,
        "cycle_count": 2 if form3_observed else 1,
        "artifact_dir": str(run_dir),
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed"
            for name in missing
            if name != "unsafe_authority_flags_absent"
        ]
        + (["source_run_forbidden_authority_flags_observed"] if unsafe_flags else []),
        "cycles": [
            {
                "cycle_index": 1,
                "observation_ref": advisory_ref,
                "response_ref": PAYLOAD_RTL_RESPONSE_REF,
                "bounded_action_ref": summary.get("payload_recovery_dispatch_ref"),
                "re_observation_ref": summary.get("payload_recovery_action_ref"),
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": checks[
                    "cycle1_payload_rtl_action_outcome_observed"
                ],
            },
            {
                "cycle_index": 2,
                "observation_ref": summary.get("payload_recovery_action_ref"),
                "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
                "policy_gate_ref": POLICY_GATE_REF,
                "operator_decision_ref": OPERATOR_DECISION_REF,
                "response_ref": PAYLOAD_LAND_RESPONSE_REF,
                "bounded_action_ref": summary.get("post_recovery_dispatch_ref"),
                "re_observation_ref": summary.get(
                    "payload_supervisor_post_recovery_action_ref"
                ),
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": checks[
                    "cycle2_payload_land_action_outcome_observed"
                ],
            },
        ],
        "cycle2_ai_situation_assessment": ai_assessment,
        "cycle2_policy_gate_result": policy_gate,
        "cycle2_operator_decision_record": operator_decision,
        "mission_os_supervisor_recovery_loop": summary.get(
            "mission_os_supervisor_recovery_loop"
        ),
        "observed": {
            "task_status": summary.get("task_status"),
            "final_status": summary.get("final_status"),
            "payload_recovery_action": summary.get("payload_recovery_action"),
            "payload_recovery_state_label": summary.get("payload_recovery_state_label"),
            "post_recovery_action_taken": summary.get("post_recovery_action_taken"),
            "post_recovery_pose_z_m": summary.get("post_recovery_pose_z_m"),
            "unsafe_authority_flags_observed": unsafe_flags,
        },
        "source_refs": {
            "payload_feasibility_advisory": advisory_ref,
            "cycle1_payload_recovery_action": PAYLOAD_RECOVERY_ACTION_REF,
            "cycle1_recovery_dispatch": summary.get("payload_recovery_dispatch_ref"),
            "cycle2_recovery_dispatch": summary.get("post_recovery_dispatch_ref"),
            "cycle2_recovery_outcome": summary.get(
                "payload_supervisor_post_recovery_action_ref"
            ),
        },
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_payload_supervisor_smoke(
    *,
    advisory_ref: str,
    payload_mass_kg: float,
    artifact_root: Path,
) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": str(payload_mass_kg),
        }
    )
    for key in (
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC",
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
        "MISSION_DESIGNER_REALISM_WIND_GUST_MPS",
        "MISSION_DESIGNER_REALISM_WIND_VARIANCE",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
            "--payload-advisory-recovery-action",
            "rtl",
            "--post-recovery-action",
            "land",
            "--mission-os-supervisor-payload-loop",
            "--payload-feasibility-advisory-ref",
            advisory_ref,
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=480,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route payload supervisor Form 3 smoke failed: "
            f"rc={result.returncode}\n"
            f"stdout_tail={result.stdout[-2000:]}\n"
            f"stderr_tail={result.stderr[-2000:]}"
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "horizontal route smoke did not emit JSON summary: "
            f"{result.stdout[-2000:]}"
        ) from exc
    run_dir = Path(summary["artifact_dir"])
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit scoped payload Mission OS supervisor Form 3 behavior."
    )
    parser.add_argument("--advisory-artifact", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--payload-mass-kg", type=float, default=DEFAULT_PAYLOAD_MASS_KG
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    advisory = _load_advisory(args.advisory_artifact)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"payload_supervisor_form3_closed_loop_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_mode = "existing_run" if args.run_dir else "executed_run"
    try:
        run_dir = args.run_dir or _run_payload_supervisor_smoke(
            advisory_ref=str(advisory.get("advisory_ref") or ""),
            payload_mass_kg=args.payload_mass_kg,
            artifact_root=audit_dir / "runs" / "payload_supervisor_form3",
        )
        artifact = _summarize_payload_supervisor_form3(
            advisory=advisory,
            run_dir=run_dir,
            expected_payload_kg=args.payload_mass_kg,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        partial_run_dir = args.run_dir or _latest_partial_run_dir(
            audit_dir / "runs" / "payload_supervisor_form3"
        )
        if partial_run_dir is None:
            raise
        artifact = _build_partial_run_artifact(
            partial_run_dir,
            audit_dir=audit_dir,
            run_mode=run_mode,
            smoke_error=str(exc),
        )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = run_mode
    output_path = (
        audit_dir / "mission_designer_payload_supervisor_form3_closed_loop.json"
    )
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["form3_claim_supported"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
