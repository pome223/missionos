from __future__ import annotations

import json
from typing import Any

from src.runtime.replay_schema import ReplayContext, StepTraceEntry
from src.runtime.state_keys import StateKeys


def _parse_json(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_replay_context(raw: Any) -> dict[str, Any] | None:
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return None
    return ReplayContext.model_validate(parsed).model_dump(mode="json")


def _extract_plan_id(state: dict[str, Any], approved_plan: dict[str, Any] | None = None) -> str | None:
    plan = approved_plan if approved_plan is not None else _parse_json(state.get(StateKeys.PLAN_APPROVED))
    if not isinstance(plan, dict):
        return None
    plan_id = str(plan.get("plan_id") or "").strip()
    return plan_id or None


def _build_executor_message(
    *,
    approved_plan: dict[str, Any] | None = None,
    replay_context: dict[str, Any] | None,
) -> str:
    if not replay_context:
        return "Execute the approved plan."
    from_step = str(replay_context.get("from_step") or "").strip()
    source_task_id = str(replay_context.get("source_task_id") or "").strip()
    if not from_step:
        return "Execute the approved plan."
    remaining_steps: list[dict[str, Any]] = []
    if isinstance(approved_plan, dict):
        steps = approved_plan.get("steps")
        steps = steps if isinstance(steps, list) else []
        replay_started = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("step_id") or "").strip()
            if step_id == from_step:
                replay_started = True
            if replay_started:
                remaining_steps.append(step)
    suffix_block = ""
    if remaining_steps:
        suffix_block = (
            "Replay suffix steps (execute only these unless recovery is required):\n"
            f"{json.dumps(remaining_steps, ensure_ascii=False, indent=2)}\n"
        )
    return (
        "Replay the approved plan from the specified step.\n\n"
        f"Replay source task: {source_task_id or '-'}\n"
        f"Replay from step: {from_step}\n"
        f"{suffix_block}"
        "Treat earlier approved steps as already satisfied unless redoing them is "
        "strictly necessary to regain focus, recover the target app/tab state, or "
        "gather fresh evidence for the remaining suffix."
    )


def _build_step_trace(
    *,
    plan: dict[str, Any],
    executor_outputs: dict[str, Any],
    report: dict[str, Any],
    replay_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    steps = plan.get("steps")
    steps = steps if isinstance(steps, list) else []
    executed_items = executor_outputs.get("steps_executed")
    executed_items = executed_items if isinstance(executed_items, list) else []
    executed_by_step: dict[str, dict[str, Any]] = {}
    for item in executed_items:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("step_id") or "").strip()
        if step_id:
            executed_by_step[step_id] = item

    criterion_results = report.get("criterion_results")
    criterion_results = criterion_results if isinstance(criterion_results, list) else []
    failed_criteria_by_step: dict[str, list[str]] = {}
    for criterion in criterion_results:
        if not isinstance(criterion, dict) or criterion.get("passed"):
            continue
        criterion_name = str(criterion.get("name") or "").strip()
        refs = criterion.get("evidence_refs")
        refs = refs if isinstance(refs, list) else []
        for ref in refs:
            ref_text = str(ref or "").strip()
            if not ref_text:
                continue
            failed_criteria_by_step.setdefault(ref_text, [])
            if criterion_name and criterion_name not in failed_criteria_by_step[ref_text]:
                failed_criteria_by_step[ref_text].append(criterion_name)

    repair_actions = report.get("repair_actions")
    repair_actions = repair_actions if isinstance(repair_actions, list) else []
    repair_actions_by_step: dict[str, list[dict[str, Any]]] = {}
    for action in repair_actions:
        if not isinstance(action, dict):
            continue
        target_step_ids = action.get("target_step_ids")
        target_step_ids = target_step_ids if isinstance(target_step_ids, list) else []
        for step_id in target_step_ids:
            normalized_step_id = str(step_id or "").strip()
            if normalized_step_id:
                repair_actions_by_step.setdefault(normalized_step_id, []).append(action)

    replay_from_step = str((replay_context or {}).get("from_step") or "").strip()
    replay_started = not replay_from_step
    trace: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or f"step_{index + 1}").strip()
        if replay_from_step and step_id == replay_from_step:
            replay_started = True
        executed = executed_by_step.get(step_id, {})
        status = str(executed.get("status") or "").strip()
        replay_scope = "replayed" if replay_started else "preserved"
        summary = str(executed.get("output_summary") or "").strip()
        if not summary:
            if status:
                summary = f"{step_id} {status}"
            elif replay_scope == "preserved":
                summary = "kept from earlier successful prefix"
            else:
                summary = "not executed in this attempt"
        trace.append(
            StepTraceEntry(
                step_id=step_id,
                title=str(step.get("title") or step_id),
                description=str(step.get("description") or ""),
                step_type="plan",
                status=status or ("preserved" if replay_scope == "preserved" else "pending"),
                tool=str(executed.get("tool") or ""),
                artifact_ref=str(executed.get("artifact_ref") or ""),
                output_summary=summary,
                replay_scope=replay_scope,
                failed_criteria=failed_criteria_by_step.get(step_id, []),
                repair_actions=repair_actions_by_step.get(step_id, []),
            ).model_dump(mode="json")
        )

    report_status = str(report.get("status") or "").strip()
    if report_status:
        trace.append(
            StepTraceEntry(
                step_id="__verification__",
                title="Verification",
                description="Verifier assessment for the current control-loop attempt.",
                step_type="verification",
                status=report_status,
                output_summary=str(report.get("summary") or ""),
                failure_type=str(report.get("failure_type") or "") or None,
                overall_score=float(report.get("overall_score") or 0.0),
                failed_criteria=[
                    str(item.get("name") or "")
                    for item in criterion_results
                    if isinstance(item, dict) and not item.get("passed")
                ],
            ).model_dump(mode="json")
        )
        if repair_actions:
            trace.append(
                StepTraceEntry(
                    step_id="__repair__",
                    title="Repair Tail",
                    description="Repair actions suggested by the verifier for the next attempt.",
                    step_type="repair",
                    status="triggered",
                    output_summary="; ".join(
                        str(action.get("description") or "").strip()
                        for action in repair_actions
                        if isinstance(action, dict) and str(action.get("description") or "").strip()
                    ) or "repair actions queued",
                    target_step_ids=[
                        str(step_id)
                        for action in repair_actions
                        if isinstance(action, dict)
                        for step_id in (
                            action.get("target_step_ids")
                            if isinstance(action.get("target_step_ids"), list)
                            else []
                        )
                        if str(step_id).strip()
                    ],
                ).model_dump(mode="json")
            )
    return trace


def _infer_tail_replay_from_step(
    *,
    step_trace: list[dict[str, Any]],
    report: dict[str, Any],
) -> str | None:
    repair_actions = report.get("repair_actions")
    repair_actions = repair_actions if isinstance(repair_actions, list) else []
    for action in repair_actions:
        if not isinstance(action, dict):
            continue
        target_step_ids = action.get("target_step_ids")
        target_step_ids = target_step_ids if isinstance(target_step_ids, list) else []
        for step_id in target_step_ids:
            normalized = str(step_id or "").strip()
            if normalized:
                return normalized
    for step in step_trace:
        if str(step.get("step_type") or "") != "plan":
            continue
        if step.get("failed_criteria"):
            return str(step.get("step_id") or "").strip() or None
        if str(step.get("status") or "") in {"failed", "pending", "skipped"}:
            return str(step.get("step_id") or "").strip() or None
    return None
