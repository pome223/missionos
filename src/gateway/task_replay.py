from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException

from src.runtime.replay_schema import (
    ReplayContext,
    StepComparePayload,
    StepCompareRow,
    StepTraceEntry,
    TaskReplayRequest,
    TaskResultSnapshot,
)
from src.runtime.state_keys import StateKeys
from src.tools.tasks import append_task_event_record


def parse_task_replay_request(payload: dict[str, Any] | object) -> TaskReplayRequest:
    body = payload if isinstance(payload, dict) else {}
    return TaskReplayRequest.model_validate(body)


def _normalize_step_trace_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        return StepTraceEntry.model_validate(raw).model_dump(mode="json")
    except Exception:
        return None


def task_result_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    result = artifacts.get("result")
    result = result if isinstance(result, dict) else {}
    verification_report = result.get("verification_report")
    verification_report = verification_report if isinstance(verification_report, dict) else {}
    verification_inputs = result.get("verification_inputs")
    verification_inputs = verification_inputs if isinstance(verification_inputs, dict) else {}
    artifact_refs = result.get("artifact_refs")
    if not isinstance(artifact_refs, list):
        artifact_refs = verification_inputs.get("artifact_refs")
    artifact_refs = [str(ref) for ref in (artifact_refs or []) if str(ref).strip()]
    screenshot_refs = [ref for ref in artifact_refs if ref.lower().endswith(".png")]
    criteria = verification_report.get("criterion_results")
    criteria = criteria if isinstance(criteria, list) else []
    failed_criteria = [
        str(item.get("name") or "")
        for item in criteria
        if isinstance(item, dict) and not item.get("passed")
    ]
    passed_count = sum(
        1 for item in criteria if isinstance(item, dict) and item.get("passed")
    )
    step_trace_items = result.get("step_trace")
    step_trace_items = step_trace_items if isinstance(step_trace_items, list) else []
    normalized_step_trace = [
        normalized
        for item in step_trace_items
        if (normalized := _normalize_step_trace_entry(item)) is not None
    ]
    snapshot = TaskResultSnapshot(
        success=bool(result.get("success")),
        final_text=str(result.get("final_text") or ""),
        verification_report_id=result.get("verification_report_id"),
        verification_status=(
            result.get("verification_status") or verification_report.get("status") or ""
        ),
        overall_score=float(verification_report.get("overall_score") or 0.0),
        repair_count=int(result.get("repair_count") or 0),
        artifact_refs=artifact_refs,
        screenshot_refs=screenshot_refs,
        failed_criteria=failed_criteria,
        passed_criteria_count=passed_count,
        criteria_count=len(criteria),
        verification_report=verification_report,
        verification_inputs=verification_inputs,
        approved_plan=result.get("approved_plan") if isinstance(result.get("approved_plan"), dict) else {},
        step_trace=normalized_step_trace,
        tail_replay_from_step_id=str(result.get("tail_replay_from_step_id") or ""),
    )
    return snapshot.model_dump(mode="json")


def timeline_kind_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        kind = str(entry.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def step_compare_rows(
    left_steps: list[dict[str, Any]],
    right_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    order: list[str] = []
    left_by_id: dict[str, dict[str, Any]] = {}
    right_by_id: dict[str, dict[str, Any]] = {}
    for target_index, items in ((left_by_id, left_steps), (right_by_id, right_steps)):
        for raw_item in items:
            item = _normalize_step_trace_entry(raw_item)
            if item is None:
                continue
            step_id = str(item.get("step_id") or "").strip()
            if not step_id:
                continue
            if step_id not in order:
                order.append(step_id)
            target_index[step_id] = item

    rows: list[dict[str, Any]] = []
    for step_id in order:
        left_item = left_by_id.get(step_id)
        right_item = right_by_id.get(step_id)
        title = str(
            (left_item or {}).get("title")
            or (right_item or {}).get("title")
            or step_id
        )
        left_status = str((left_item or {}).get("status") or "-")
        right_status = str((right_item or {}).get("status") or "-")
        changed = any(
            (
                left_status != right_status,
                str((left_item or {}).get("output_summary") or "")
                != str((right_item or {}).get("output_summary") or ""),
                list((left_item or {}).get("failed_criteria") or [])
                != list((right_item or {}).get("failed_criteria") or []),
            )
        )
        rows.append(
            StepCompareRow(
                step_id=step_id,
                title=title,
                left=left_item,
                right=right_item,
                changed=changed,
            ).model_dump(mode="json")
        )
    return rows


def suggest_tail_replay_from_task(task: dict[str, Any]) -> str:
    result = task_result_snapshot(task)
    explicit = str(result.get("tail_replay_from_step_id") or "").strip()
    if explicit:
        return explicit
    for item in result.get("step_trace") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("step_type") or "") != "plan":
            continue
        if item.get("failed_criteria"):
            return str(item.get("step_id") or "").strip()
    return ""


def build_partial_replay_seed(
    source_task: dict[str, Any],
    *,
    from_step: str,
) -> dict[str, Any]:
    result = task_result_snapshot(source_task)
    approved_plan = result.get("approved_plan")
    approved_plan = approved_plan if isinstance(approved_plan, dict) else {}
    if not approved_plan:
        raise HTTPException(status_code=400, detail="task is missing approved plan snapshot")
    normalized_from_step = str(from_step or "").strip()
    allowed_step_ids = {
        str(step.get("step_id") or "").strip()
        for step in approved_plan.get("steps", [])
        if isinstance(step, dict)
    }
    if normalized_from_step and normalized_from_step not in allowed_step_ids:
        raise HTTPException(
            status_code=400,
            detail=f"unknown replay step: {normalized_from_step}",
        )
    replay_context = ReplayContext(
        source_task_id=str(source_task.get("task_id") or ""),
        from_step=normalized_from_step,
        mode="tail",
        previous_verification_status=str(result.get("verification_status") or "") or None,
        previous_failed_criteria=result.get("failed_criteria") or [],
        step_trace=result.get("step_trace") or [],
    )
    return {
        StateKeys.PLAN_APPROVED: approved_plan,
        StateKeys.PLAN_RISK_LEVEL: approved_plan.get("risk_level"),
        StateKeys.APPROVAL_STATUS: "human_approved",
        StateKeys.APPROVAL_REQUEST: None,
        StateKeys.REPLAY_SOURCE_TASK_ID: source_task.get("task_id"),
        StateKeys.REPLAY_FROM_STEP: normalized_from_step,
        StateKeys.REPLAY_CONTEXT: replay_context.model_dump(mode="json"),
    }


def persist_control_loop_step_events(
    *,
    task_id: str,
    result: Any,
) -> None:
    step_trace = getattr(result, "metadata", {}).get("step_trace")
    if not isinstance(step_trace, list) or not step_trace:
        return
    for raw_item in step_trace:
        item = _normalize_step_trace_entry(raw_item)
        if item is None:
            continue
        append_task_event_record(
            task_id,
            event_type=f"step_{str(item.get('step_type') or 'plan')}",
            title=str(item.get("title") or item.get("step_id") or "step"),
            status=str(item.get("status") or ""),
            payload={
                "summary": str(item.get("output_summary") or ""),
                "step": item,
            },
        )


def build_task_compare_payload(
    left_task: dict[str, Any],
    right_task: dict[str, Any],
    *,
    build_task_timeline_payload: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    left_timeline = build_task_timeline_payload(left_task, page=1, page_size=40)
    right_timeline = build_task_timeline_payload(right_task, page=1, page_size=40)
    left_result = task_result_snapshot(left_task)
    right_result = task_result_snapshot(right_task)
    step_rows = step_compare_rows(
        left_result.get("step_trace") or [],
        right_result.get("step_trace") or [],
    )
    summary: list[str] = []
    if left_task.get("status") != right_task.get("status"):
        summary.append(
            f"task status changed: {left_task.get('status') or '-'} -> {right_task.get('status') or '-'}"
        )
    if left_result["verification_status"] != right_result["verification_status"]:
        summary.append(
            f"verification changed: {left_result['verification_status'] or '-'} -> {right_result['verification_status'] or '-'}"
        )
    if left_result["overall_score"] != right_result["overall_score"]:
        summary.append(
            f"overall score changed: {left_result['overall_score']:.2f} -> {right_result['overall_score']:.2f}"
        )
    if left_result["repair_count"] != right_result["repair_count"]:
        summary.append(
            f"repair count changed: {left_result['repair_count']} -> {right_result['repair_count']}"
        )
    if len(left_result["screenshot_refs"]) != len(right_result["screenshot_refs"]):
        summary.append(
            f"screenshot refs changed: {len(left_result['screenshot_refs'])} -> {len(right_result['screenshot_refs'])}"
        )
    if left_result["failed_criteria"] != right_result["failed_criteria"]:
        summary.append(
            "failed criteria changed: "
            f"{', '.join(left_result['failed_criteria']) or '-'} -> "
            f"{', '.join(right_result['failed_criteria']) or '-'}"
        )
    changed_step_count = sum(1 for row in step_rows if row.get("changed"))
    if changed_step_count:
        summary.append(f"step-level diff detected in {changed_step_count} step(s)")
    if not summary:
        summary.append("No high-level diff detected beyond timeline ordering.")

    step_compare = StepComparePayload(
        total=len(step_rows),
        changed=changed_step_count,
        rows=step_rows,
    )
    return {
        "left_task": left_task,
        "right_task": right_task,
        "left": {
            "status": left_task.get("status"),
            "timeline": {
                "total": left_timeline["pagination"]["total"],
                "kind_counts": timeline_kind_counts(left_timeline["entries"]),
                "entries": left_timeline["entries"][:8],
            },
            "result": left_result,
        },
        "right": {
            "status": right_task.get("status"),
            "timeline": {
                "total": right_timeline["pagination"]["total"],
                "kind_counts": timeline_kind_counts(right_timeline["entries"]),
                "entries": right_timeline["entries"][:8],
            },
            "result": right_result,
        },
        "summary": summary,
        "step_compare": step_compare.model_dump(mode="json"),
    }
