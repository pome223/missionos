"""Truthful operator projections for governed VLA simulator tasks."""

from __future__ import annotations

from typing import Any
import html
import json

from rich.panel import Panel
from rich.table import Table

from .job_status import _task_artifacts, _task_record, _task_status


def _is_vla_operator_task(task_payload: dict[str, Any]) -> bool:
    task = _task_record(task_payload)
    if task.get("kind") == "vla_mission_execution":
        return True
    artifacts = _task_artifacts(task_payload)
    return any(
        key in artifacts
        for key in (
            "missionos_vla_mission_run_record",
            "missionos_vla_recovery_state",
        )
    )


def _value(value: Any, default: str = "unknown") -> Any:
    return default if value is None or value == "" else value


def _vla_operator_snapshot(task_payload: dict[str, Any]) -> dict[str, Any]:
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    proposal = artifacts.get("physical_ai_mission_proposal")
    proposal = proposal if isinstance(proposal, dict) else {}
    approval = artifacts.get("physical_ai_mission_approval")
    approval = approval if isinstance(approval, dict) else {}
    record = artifacts.get("missionos_vla_mission_run_record")
    record = record if isinstance(record, dict) else {}
    predicate = record.get("predicate_evaluation")
    predicate = predicate if isinstance(predicate, dict) else {}
    recovery = artifacts.get("missionos_vla_recovery_state")
    recovery = recovery if isinstance(recovery, dict) else {}
    failure = artifacts.get("physical_ai_execution_failure")
    failure = failure if isinstance(failure, dict) else {}
    selection = record.get("vla_task_selection") or proposal.get("vla_task_selection")
    selection = selection if isinstance(selection, dict) else {}

    return {
        "task_id": str(task.get("task_id") or task_payload.get("task_id") or "unknown"),
        "task_status": _task_status(task_payload) or "unknown",
        "map_kind": "vla_evidence_timeline",
        "spatial_map_claimed": False,
        "point_count": 0,
        "catalog_entry_id": _value(selection.get("catalog_entry_id")),
        "display_name": _value(selection.get("display_name")),
        "environment": _value(selection.get("environment") or selection.get("env_name")),
        "run_identity": _value(record.get("run_identity") or proposal.get("parent_run_identity")),
        "episode_identity": _value(record.get("episode_identity") or proposal.get("episode_identity")),
        "proposal_sha256": _value(proposal.get("proposal_sha256")),
        "approval_sha256": _value(approval.get("approval_sha256")),
        "contract_sha256": _value(record.get("contract_sha256")),
        "execution_mode": _value(record.get("execution_mode") or task.get("metadata", {}).get("execution_mode") if isinstance(task.get("metadata"), dict) else record.get("execution_mode")),
        "evidence_readiness": _value(predicate.get("evidence_readiness")),
        "predicate_status": _value(predicate.get("status")),
        "verification_basis": _value(predicate.get("actual_verification_basis"), "unverified"),
        "outcome_claim_scope": _value(predicate.get("outcome_claim_scope")),
        "observation_content_sha256": _value(predicate.get("observation_content_sha256")),
        "bounded_outcome_claimed": _value(record.get("bounded_outcome_claimed")),
        "controller_ack_observed": _value(record.get("controller_ack_observed")),
        "mission_completion_claimed": _value(record.get("mission_completion_claimed")),
        "physical_execution_invoked": _value(record.get("physical_execution_invoked")),
        "policy_instruction_delivery_observed": _value(record.get("policy_instruction_delivery_observed")),
        "progress_observation": "unavailable_official_runner_no_callback",
        "recovery_status": _value(recovery.get("recovery_status")),
        "recovery_proposal_status": _value(recovery.get("proposal_status")),
        "recovery_action": _value(recovery.get("repair_action")),
        "recovery_proposal_id": _value(recovery.get("repair_proposal_id")),
        "retry_task_id": _value(recovery.get("retry_task_id")),
        "automatic_retry_allowed": _value(recovery.get("automatic_retry_allowed")),
        "retry_requires_new_human_approval": _value(recovery.get("retry_requires_new_human_approval")),
        "recovery_dispatch_authority_created": _value(recovery.get("dispatch_authority_created")),
        "in_episode_intervention_available": _value(
            recovery.get("in_episode_intervention_available")
        ),
        "post_episode_repair_implemented": _value(
            recovery.get("post_episode_repair_implemented")
        ),
        "failure_type": _value(failure.get("failure_type")),
        "claim_boundary": record.get("claim_boundary")
        or "This is a non-spatial evidence projection. It does not establish controller ACK, parent completion, physical execution, or a live recovery control path.",
    }


def _render_vla_operator_panel(task_payload: dict[str, Any], *, title: str) -> Panel:
    snapshot = _vla_operator_snapshot(task_payload)
    table = Table.grid(padding=(0, 1))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    rows = (
        ("Task", f"{snapshot['task_id']} · {snapshot['task_status']} · {snapshot['execution_mode']}"),
        ("Frozen", f"catalog={snapshot['catalog_entry_id']} · approval={snapshot['approval_sha256']} · contract={snapshot['contract_sha256']}"),
        ("Observed", f"readiness={snapshot['evidence_readiness']} · content={snapshot['observation_content_sha256']} · progress={snapshot['progress_observation']}"),
        ("Predicate", f"status={snapshot['predicate_status']} · basis={snapshot['verification_basis']} · scope={snapshot['outcome_claim_scope']}"),
        ("Bounded outcome", f"claimed={snapshot['bounded_outcome_claimed']} · parent_completion={snapshot['mission_completion_claimed']}"),
        ("Recovery", f"status={snapshot['recovery_status']} · action={snapshot['recovery_action']} · proposal={snapshot['recovery_proposal_status']} · retry_task={snapshot['retry_task_id']}"),
        ("Recovery boundary", f"in_episode={snapshot['in_episode_intervention_available']} · post_episode={snapshot['post_episode_repair_implemented']} · auto_retry={snapshot['automatic_retry_allowed']} · dispatch_authority={snapshot['recovery_dispatch_authority_created']}"),
        ("Unconfirmed", f"controller_ack={snapshot['controller_ack_observed']} · instruction_delivery={snapshot['policy_instruction_delivery_observed']} · physical_execution={snapshot['physical_execution_invoked']}"),
    )
    for label, value in rows:
        table.add_row(label, str(value))
    return Panel(table, title=title, border_style="magenta")


def _vla_evidence_model(task_payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = _vla_operator_snapshot(task_payload)
    return {
        **snapshot,
        "provider": {"id": "none", "label": "non-spatial evidence timeline"},
        "points": [],
        "planned_points": [],
        "observed_points": [],
        "obstacles": [],
        "live": {"enabled": False},
    }


def _vla_evidence_html(model: dict[str, Any]) -> str:
    rows = []
    for label, key in (
        ("Frozen catalog", "catalog_entry_id"),
        ("Approval digest", "approval_sha256"),
        ("Contract digest", "contract_sha256"),
        ("Observed evidence", "evidence_readiness"),
        ("Predicate", "predicate_status"),
        ("Verification basis", "verification_basis"),
        ("Bounded outcome", "bounded_outcome_claimed"),
        ("Recovery", "recovery_status"),
        ("Controller ACK", "controller_ack_observed"),
        ("Physical execution", "physical_execution_invoked"),
    ):
        rows.append(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(model.get(key, 'unknown')))}</td></tr>"
        )
    payload = html.escape(json.dumps(model, ensure_ascii=False, sort_keys=True))
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>MissionOS VLA Evidence · {html.escape(str(model['task_id']))}</title><style>body{{font-family:system-ui;background:#07101d;color:#e5eefb;margin:0;padding:24px}}main{{max-width:980px;margin:auto}}table{{width:100%;border-collapse:collapse;background:#0f172a}}th,td{{padding:12px;border:1px solid #334155;text-align:left}}th{{width:220px;color:#67e8f9}}.warning{{color:#facc15}}pre{{white-space:pre-wrap;color:#94a3b8}}</style></head><body><main><h1>MissionOS VLA Evidence Timeline</h1><p>task={html.escape(str(model['task_id']))} · status={html.escape(str(model['task_status']))}</p><p class=\"warning\">Non-spatial view: no world map, geometry, live controller ACK, or physical-execution claim is created.</p><table>{''.join(rows)}</table><h2>Claim boundary</h2><p>{html.escape(str(model['claim_boundary']))}</p><details><summary>Machine-readable projection</summary><pre>{payload}</pre></details></main></body></html>"""


__all__ = [
    "_is_vla_operator_task",
    "_render_vla_operator_panel",
    "_vla_evidence_html",
    "_vla_evidence_model",
    "_vla_operator_snapshot",
]
