"""Read-only MissionOS causal timeline projection for the Control UI.

The timeline view indexes already persisted Form 3 supervisor artifacts and
projects their cycle records into a human-readable sequence. It must not start
SITL, probe Gateway routes, create dispatch authority, or mutate mission state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from src.gateway.missionos_milestone import (
    ARTIFACT_ROOT,
    AUTHORITY_FALSE_KEYS,
    _authority_false_summary,
    _positive_evidence_path,
    _relative,
)


SCHEMA_VERSION = "missionos_causal_timeline_gui_summary.v1"
TIMELINE_CLASSIFICATION = "Form 0b / GUI causal visualization"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_json(root: Path, filename: str) -> tuple[str, dict[str, Any]]:
    if not root.exists():
        return "", {}
    candidates = sorted(
        root.rglob(filename),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    for path in candidates:
        if not _positive_evidence_path(path):
            continue
        payload = _read_json(path)
        if payload is not None:
            return _relative(path), payload
    return "", {}


def _plain_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _status_from(value: Any) -> str:
    return "observed" if value is True else "missing"


def _field_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != "" and value != []
    }


def _timeline_step(
    *,
    step_id: str,
    title: str,
    status: str,
    summary: str,
    artifact_ref: str = "",
    fields: Mapping[str, Any] | None = None,
    boundary_note: str = "",
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "title": title,
        "status": status,
        "summary": summary,
        "artifact_ref": artifact_ref,
        "fields": _field_values(fields or {}),
        "boundary_note": boundary_note,
    }


def _authority_boundary_for_timeline(
    payloads: list[Mapping[str, Any]],
) -> dict[str, Any]:
    boundary = _authority_false_summary(payloads)
    boundary["timeline_surface_mutates_runtime"] = False
    boundary["timeline_progress_counted"] = False
    return boundary


def _cycle_status(
    steps: list[Mapping[str, Any]],
    *,
    ref_chain_consistent: bool,
) -> str:
    return (
        "observed"
        if ref_chain_consistent
        and all(step.get("status") == "observed" for step in steps)
        else "partial"
    )


def _same_artifact_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def _build_cycle(cycle: Mapping[str, Any]) -> dict[str, Any]:
    decision = _plain_mapping(cycle.get("decision"))
    assessment = _plain_mapping(decision.get("assessment_inputs"))
    authority = _plain_mapping(assessment.get("authority"))
    action_request = _plain_mapping(cycle.get("action_request"))
    action_receipt = _plain_mapping(cycle.get("action_receipt"))
    outcome = _plain_mapping(cycle.get("outcome_observation"))

    wind = _plain_mapping(assessment.get("wind"))
    route = _plain_mapping(assessment.get("route"))
    payload = _plain_mapping(assessment.get("payload"))
    battery = _plain_mapping(assessment.get("battery"))
    telemetry = _plain_mapping(assessment.get("telemetry"))
    obstacle = _plain_mapping(assessment.get("obstacle"))

    cycle_index = int(cycle.get("cycle_index") or 0)
    selected_action = action_request.get("bounded_action") or decision.get("selected_bounded_action")
    source_observation_ref = str(decision.get("source_observation_ref") or "")
    decision_ref = str(cycle.get("decision_ref") or decision.get("decision_id") or "")
    decision_id = str(decision.get("decision_id") or "")
    action_request_ref = str(cycle.get("action_request_ref") or "")
    action_request_id = str(action_request.get("request_id") or "")
    action_receipt_ref = str(cycle.get("action_receipt_ref") or "")
    action_receipt_id = str(action_receipt.get("receipt_id") or "")
    outcome_observation_ref = str(cycle.get("outcome_observation_ref") or "")
    outcome_observation_id = str(outcome.get("observation_id") or "")
    ref_checks = {
        "decision_ref_matches_decision_id": bool(
            decision_ref and decision_id and decision_ref == decision_id
        ),
        "action_request_ref_matches_request_id": bool(
            action_request_ref
            and action_request_id
            and action_request_ref == action_request_id
        ),
        "action_request_points_to_decision": bool(
            action_request.get("decision_ref") == decision_ref
        ),
        "action_receipt_ref_matches_receipt_id": bool(
            action_receipt_ref
            and action_receipt_id
            and action_receipt_ref == action_receipt_id
        ),
        "action_receipt_points_to_request": bool(
            action_receipt.get("action_request_ref") == action_request_ref
        ),
        "outcome_ref_matches_observation_id": bool(
            outcome_observation_ref
            and outcome_observation_id
            and outcome_observation_ref == outcome_observation_id
        ),
        "outcome_points_to_receipt": bool(
            outcome.get("action_receipt_ref") == action_receipt_ref
        ),
    }
    ref_chain_consistent = all(ref_checks.values())
    ref_chain_errors = [
        key for key, value in ref_checks.items() if value is not True
    ]

    steps = [
        _timeline_step(
            step_id="observation",
            title="Observation",
            status="observed" if source_observation_ref else "missing",
            summary=(
                source_observation_ref
                or "No source observation ref recorded for this cycle."
            ),
            artifact_ref=source_observation_ref,
            fields={
                "primary_trigger": decision.get("primary_trigger"),
                "wind_drift_deviation_xy_m": wind.get("wind_drift_deviation_xy_m"),
                "wind_speed_mps": wind.get("wind_speed_mps"),
                "route_blocked": route.get("route_blocked"),
                "payload_margin_risk": payload.get("payload_margin_risk"),
                "battery_warning_state": battery.get("battery_warning_state"),
                "telemetry_continuity": telemetry.get("telemetry_continuity"),
            },
            boundary_note="Observed evidence only; no delivery completion claim.",
        ),
        _timeline_step(
            step_id="assessment",
            title="Assessment",
            status="observed" if decision.get("mission_state_interpretation") else "missing",
            summary=str(
                decision.get("mission_state_interpretation")
                or "No mission-state interpretation recorded."
            ),
            artifact_ref=decision_ref,
            fields={
                "assessment_mode": assessment.get("assessment_mode"),
                "supervisor_scope": decision.get("supervisor_scope"),
                "conflicting_risks": assessment.get("conflicting_risks"),
                "obstacle_status": obstacle.get("verification_status"),
                "secondary_risks": [
                    item.get("condition")
                    for item in _list(assessment.get("secondary_risks"))
                    if isinstance(item, Mapping)
                ],
            },
            boundary_note="AI/supervisor assessment is evidence, not authority.",
        ),
        _timeline_step(
            step_id="policy_gate",
            title="Policy Gate",
            status="observed"
            if decision.get("ai_judgment_is_gate_verdict") is False
            and decision.get("llm_gate_judge_used") is False
            else "missing",
            summary="bounded action requires operator approval; LLM gate judge is false",
            artifact_ref=decision_ref,
            fields={
                "llm_gate_judge_used": decision.get("llm_gate_judge_used"),
                "ai_judgment_is_gate_verdict": decision.get(
                    "ai_judgment_is_gate_verdict"
                ),
                "automatic_dispatch_allowed": decision.get(
                    "automatic_dispatch_allowed"
                ),
                "bounded_action_dispatch_allowed": authority.get(
                    "bounded_action_dispatch_allowed"
                ),
                "hardware_target_allowed": decision.get("hardware_target_allowed"),
                "physical_execution_invoked": decision.get(
                    "physical_execution_invoked"
                ),
            },
            boundary_note="Policy boundary remains deterministic; AI does not grant authority.",
        ),
        _timeline_step(
            step_id="operator_decision",
            title="Operator Decision",
            status="observed"
            if action_request.get("operator_approved") is True
            or decision.get("operator_approved_dispatch_allowed") is True
            else "missing",
            summary="operator-approved bounded SITL action"
            if action_request.get("operator_approved") is True
            else "operator approval not recorded",
            artifact_ref=str(action_request.get("approval_ref") or ""),
            fields={
                "operator_approval_required": decision.get(
                    "operator_approval_required"
                ),
                "operator_approved": action_request.get("operator_approved"),
                "approval_ref": action_request.get("approval_ref"),
                "automatic_dispatch_allowed": action_request.get(
                    "automatic_dispatch_allowed"
                ),
            },
            boundary_note="Approval is bounded to the recorded SITL action request.",
        ),
        _timeline_step(
            step_id="bounded_action",
            title="Bounded Action",
            status="observed"
            if action_request.get("allowlisted_action") is True and selected_action
            else "missing",
            summary=str(selected_action or "No bounded action recorded."),
            artifact_ref=action_request_ref,
            fields={
                "bounded_action": selected_action,
                "allowlisted_action": action_request.get("allowlisted_action"),
                "backend_target": action_request.get("backend_target"),
                "dispatch_authority_created": action_request.get(
                    "dispatch_authority_created"
                ),
                "expected_dispatch_ref": action_request.get("expected_dispatch_ref"),
            },
            boundary_note="Executor may only use this bounded action record.",
        ),
        _timeline_step(
            step_id="outcome",
            title="Outcome",
            status=_status_from(outcome.get("outcome_observed")),
            summary=str(
                outcome.get("state_label")
                or action_receipt.get("dispatch_status")
                or "Outcome not observed."
            ),
            artifact_ref=outcome_observation_ref,
            fields={
                "outcome_observed": outcome.get("outcome_observed"),
                "state_label": outcome.get("state_label"),
                "dispatch_observed": action_receipt.get("dispatch_observed"),
                "dispatch_status": action_receipt.get("dispatch_status"),
                "pose_z_m": outcome.get("pose_z_m"),
                "delivery_completion_claimed": outcome.get(
                    "delivery_completion_claimed"
                ),
            },
            boundary_note="Outcome observation does not claim delivery completion.",
        ),
    ]

    return {
        "cycle_index": cycle_index,
        "cycle_label": f"cycle {cycle_index}" if cycle_index else "cycle",
        "status": _cycle_status(
            steps,
            ref_chain_consistent=ref_chain_consistent,
        ),
        "primary_trigger": decision.get("primary_trigger"),
        "selected_bounded_action": selected_action,
        "decision_ref": decision_ref,
        "action_request_ref": action_request_ref,
        "action_receipt_ref": action_receipt_ref,
        "outcome_observation_ref": outcome_observation_ref,
        "ref_chain_consistent": ref_chain_consistent,
        "ref_chain_errors": ref_chain_errors,
        "steps": steps,
    }


def _build_replay_overlay(
    runtime_path: str,
    runtime: Mapping[str, Any],
    cycles: list[Mapping[str, Any]],
) -> dict[str, Any]:
    markers = []
    for cycle in cycles:
        decision = _plain_mapping(cycle.get("decision"))
        outcome = _plain_mapping(cycle.get("outcome_observation"))
        action_request = _plain_mapping(cycle.get("action_request"))
        cycle_index = cycle.get("cycle_index")
        markers.append(
            {
                "cycle_index": cycle_index,
                "marker_kind": "trigger",
                "label": decision.get("primary_trigger") or "observation",
                "artifact_ref": decision.get("source_observation_ref") or "",
                "status": "observed"
                if decision.get("source_observation_ref")
                else "missing",
            }
        )
        markers.append(
            {
                "cycle_index": cycle_index,
                "marker_kind": "bounded_action",
                "label": action_request.get("bounded_action") or "action",
                "artifact_ref": cycle.get("action_request_ref") or "",
                "status": "observed"
                if action_request.get("allowlisted_action") is True
                else "missing",
            }
        )
        markers.append(
            {
                "cycle_index": cycle_index,
                "marker_kind": "outcome",
                "label": outcome.get("state_label") or "outcome",
                "artifact_ref": cycle.get("outcome_observation_ref") or "",
                "status": _status_from(outcome.get("outcome_observed")),
            }
        )
    return {
        "overlay_status": "markers_projected" if markers else "missing",
        "mode": "artifact_replay_only",
        "planned_route": "not_projected_by_this_surface",
        "observed_trajectory": "not_loaded_without_pose_log",
        "source_artifact_path": runtime_path,
        "markers": markers,
        "boundary_note": (
            "These markers are artifact projections. They are not live telemetry, "
            "not dispatch controls, and not delivery-completion claims."
        ),
        "source_runtime_artifact_dir": runtime.get("artifact_dir", ""),
    }


def build_missionos_causal_timeline_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Return a read-only Form 3 causal timeline summary."""

    root = Path(artifact_root)
    runtime_path, runtime = _latest_json(
        root,
        "mission_os_multi_condition_supervisor_runtime.json",
    )
    c5b_path, c5b = _latest_json(root, "gateway_live_runtime_probe.json")
    loop = _plain_mapping(runtime.get("mission_os_supervisor_recovery_loop"))
    raw_cycles = [
        item for item in _list(loop.get("cycles")) if isinstance(item, Mapping)
    ]
    cycles = [_build_cycle(cycle) for cycle in raw_cycles]
    cycles_observed = bool(cycles) and all(
        cycle.get("status") == "observed" for cycle in cycles
    )
    selected_payloads = [payload for payload in [runtime, c5b] if payload]
    authority = _authority_boundary_for_timeline(selected_payloads)

    source_runtime_ref = str(runtime.get("audit_id") or "")
    c5b_source_runtime_path = str(c5b.get("source_runtime_artifact_path") or "")
    c5b_source_runtime_ref = str(c5b.get("source_runtime_artifact_ref") or "")
    c5b_source_runtime_path_consistent = _same_artifact_path(
        c5b_source_runtime_path,
        runtime_path,
    )
    c5b_source_runtime_ref_consistent = bool(
        source_runtime_ref and c5b_source_runtime_ref == source_runtime_ref
    )
    source_form3_observed = (
        runtime.get("audit_status") == "multi_condition_supervisor_runtime_observed"
        and runtime.get("causal_form") == "Form 3"
        and runtime.get("progress_counted") is True
        and runtime.get("supervisor_runtime_claim_supported") is True
        and len(cycles) == int(runtime.get("cycle_count") or len(cycles))
        and cycles_observed
    )
    c5b_observed = (
        c5b.get("gateway_runtime_probe_status") == "full_gateway_runtime_loop_observed"
        and c5b_source_runtime_path_consistent
        and c5b_source_runtime_ref_consistent
    )
    timeline_status = (
        "observed"
        if source_form3_observed
        and c5b_observed
        and authority["authority_boundary_supported"]
        else "partial"
        if runtime
        else "missing"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": _relative(root),
        "timeline_label": "Past recovery cycles - read-only replay",
        "timeline_status": timeline_status,
        "classification": {
            "causal_form": "Form 0b",
            "surface": "GUI causal visualization",
            "progress_counted": False,
            "runtime_capability_added": False,
        },
        "source_summary": {
            "source_runtime_artifact_path": runtime_path,
            "source_runtime_observed": source_form3_observed,
            "source_runtime_causal_form": runtime.get("causal_form"),
            "source_runtime_progress_counted": runtime.get("progress_counted"),
            "source_runtime_cycle_count": runtime.get("cycle_count"),
            "gateway_runtime_artifact_path": c5b_path,
            "gateway_runtime_observed": c5b_observed,
            "full_gateway_runtime_loop": c5b.get("full_gateway_runtime_loop"),
            "source_runtime_ref": source_runtime_ref,
            "gateway_source_runtime_ref": c5b_source_runtime_ref,
            "gateway_source_runtime_path": c5b_source_runtime_path,
            "gateway_source_runtime_ref_consistent": c5b_source_runtime_ref_consistent,
            "gateway_source_runtime_path_consistent": c5b_source_runtime_path_consistent,
            "supervisor_scope": runtime.get("supervisor_scope")
            or c5b.get("supervisor_scope"),
            "primary_trigger": runtime.get("primary_trigger"),
        },
        "authority_boundary": authority,
        "cycles": cycles,
        "replay_overlay": _build_replay_overlay(runtime_path, runtime, raw_cycles),
        "not_claimed": [
            "live_sitl_started_by_timeline",
            "gateway_probe_started_by_timeline",
            "physical_execution",
            "physical_form1",
            "hardware_target_authority",
            "dispatch_authority_creation",
            "delivery_completion",
            "public_sync",
        ],
        "operator_note": (
            "This view synthesizes persisted artifacts for readability. It is not "
            "an AI gate verdict, verifier, dispatch control, live telemetry stream, "
            "or delivery completion claim."
        ),
        "authority_false_keys": sorted(AUTHORITY_FALSE_KEYS),
        "timeline_classification": TIMELINE_CLASSIFICATION,
    }
