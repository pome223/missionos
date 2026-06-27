"""Cross-session task analytics for step failure ranking and replay improvement."""

from __future__ import annotations

from typing import Any, Optional

from src.runtime.task_store import TaskStore


def _extract_step(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    payload = payload if isinstance(payload, dict) else {}
    step = payload.get("step") or {}
    return step if isinstance(step, dict) else {}


def compute_task_overview(
    store: TaskStore,
    *,
    owner_user_id: Optional[str] = None,
) -> dict[str, Any]:
    by_status = store.aggregate_status_counts(
        kind="control_loop",
        owner_user_id=owner_user_id,
    )
    total_tasks = sum(by_status.values())
    replay_by_status = store.aggregate_replay_counts(
        kind="control_loop",
        owner_user_id=owner_user_id,
    )
    total_replays = sum(replay_by_status.values())
    replay_successes = replay_by_status.get("completed", 0)
    return {
        "total_tasks": total_tasks,
        "by_status": by_status,
        "total_replays": total_replays,
        "replay_success_rate": round(
            replay_successes / max(total_replays, 1), 3
        ) if total_replays else 0.0,
    }


def compute_step_failure_ranking(
    store: TaskStore,
    *,
    owner_user_id: Optional[str] = None,
    limit: int = 10000,
) -> dict[str, Any]:
    total_events = store.count_step_events(
        owner_user_id=owner_user_id,
        kind="control_loop",
    )
    events = store.query_step_events(
        owner_user_id=owner_user_id,
        kind="control_loop",
        limit=limit,
    )
    step_stats: dict[str, dict[str, Any]] = {}
    for event in events:
        step = _extract_step(event)
        step_id = str(step.get("step_id") or "").strip()
        if not step_id or step_id.startswith("__"):
            continue
        step_type = str(step.get("step_type") or "").strip()
        if step_type != "plan":
            continue
        if step_id not in step_stats:
            step_stats[step_id] = {
                "step_id": step_id,
                "title": str(step.get("title") or step_id),
                "total": 0,
                "succeeded": 0,
                "failed": 0,
                "preserved": 0,
                "other": 0,
                "failed_criteria_counts": {},
                "task_ids": set(),
            }
        stats = step_stats[step_id]
        stats["total"] += 1
        stats["task_ids"].add(event.get("task_id", ""))
        status = str(step.get("status") or "").strip()
        if status == "succeeded":
            stats["succeeded"] += 1
        elif status in ("failed", "pending", "skipped"):
            stats["failed"] += 1
        elif status == "preserved":
            stats["preserved"] += 1
        else:
            stats["other"] += 1
        for criterion in step.get("failed_criteria") or []:
            name = str(criterion or "").strip()
            if name:
                stats["failed_criteria_counts"][name] = (
                    stats["failed_criteria_counts"].get(name, 0) + 1
                )

    ranking: list[dict[str, Any]] = []
    for stats in step_stats.values():
        total = stats["total"]
        failed = stats["failed"]
        criteria_counts = stats["failed_criteria_counts"]
        top_criteria = sorted(
            criteria_counts.items(), key=lambda x: x[1], reverse=True
        )[:5]
        ranking.append({
            "step_id": stats["step_id"],
            "title": stats["title"],
            "total": total,
            "succeeded": stats["succeeded"],
            "failed": failed,
            "preserved": stats["preserved"],
            "other": stats["other"],
            "failure_rate": round(failed / max(total, 1), 3),
            "task_count": len(stats["task_ids"]),
            "top_failed_criteria": [
                {"name": name, "count": count}
                for name, count in top_criteria
            ],
        })
    ranking.sort(key=lambda s: s["failure_rate"], reverse=True)
    truncated = total_events > limit
    return {
        "steps": ranking,
        "total_events": total_events,
        "sampled_events": len(events),
        "truncated": truncated,
    }


def compute_replay_improvement(
    store: TaskStore,
    *,
    owner_user_id: Optional[str] = None,
) -> dict[str, Any]:
    replay_counts = store.aggregate_replay_counts(
        kind="control_loop",
        owner_user_id=owner_user_id,
    )
    total_replay_tasks = sum(replay_counts.values())
    if total_replay_tasks == 0:
        return {"steps": [], "total_replay_tasks": 0, "sampled_replay_tasks": 0, "truncated": False}

    replay_tasks = store.query_replay_tasks(
        kind="control_loop",
        owner_user_id=owner_user_id,
        limit=total_replay_tasks,
    )

    source_ids = set()
    for task in replay_tasks:
        metadata = task.get("metadata") or {}
        source_id = str(metadata.get("replay_of_task_id") or "").strip()
        if source_id:
            source_ids.add(source_id)

    source_tasks_by_id: dict[str, dict[str, Any]] = {}
    for source_id in source_ids:
        source = store.get(source_id)
        if source is not None:
            source_tasks_by_id[source_id] = source

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task in replay_tasks:
        metadata = task.get("metadata") or {}
        source_id = str(metadata.get("replay_of_task_id") or "").strip()
        source = source_tasks_by_id.get(source_id)
        if source is not None:
            pairs.append((source, task))

    step_improvement: dict[str, dict[str, Any]] = {}
    for source_task, replay_task in pairs:
        source_trace = _task_step_trace(source_task)
        replay_trace = _task_step_trace(replay_task)
        source_by_id = {s["step_id"]: s for s in source_trace}
        replay_by_id = {s["step_id"]: s for s in replay_trace}
        all_step_ids = list(dict.fromkeys(
            [s["step_id"] for s in source_trace]
            + [s["step_id"] for s in replay_trace]
        ))
        for step_id in all_step_ids:
            if step_id.startswith("__"):
                continue
            if step_id not in step_improvement:
                step_improvement[step_id] = {
                    "step_id": step_id,
                    "title": (source_by_id.get(step_id) or replay_by_id.get(step_id, {})).get("title", step_id),
                    "source_fail": 0,
                    "replay_pass": 0,
                    "replay_fail": 0,
                    "pair_count": 0,
                }
            stats = step_improvement[step_id]
            source_step = source_by_id.get(step_id, {})
            replay_step = replay_by_id.get(step_id, {})
            source_failed = str(source_step.get("status") or "") in ("failed", "pending", "skipped") or bool(source_step.get("failed_criteria"))
            replay_succeeded = str(replay_step.get("status") or "") == "succeeded"
            replay_failed = str(replay_step.get("status") or "") in ("failed", "pending", "skipped") or bool(replay_step.get("failed_criteria"))
            if source_failed:
                stats["source_fail"] += 1
                stats["pair_count"] += 1
                if replay_succeeded:
                    stats["replay_pass"] += 1
                elif replay_failed:
                    stats["replay_fail"] += 1

    steps = []
    for stats in step_improvement.values():
        if stats["source_fail"] == 0:
            continue
        stats["improvement_rate"] = round(
            stats["replay_pass"] / max(stats["source_fail"], 1), 3
        )
        steps.append(stats)
    steps.sort(key=lambda s: s["improvement_rate"], reverse=True)
    return {
        "steps": steps,
        "total_replay_tasks": total_replay_tasks,
        "sampled_replay_tasks": len(replay_tasks),
        "truncated": len(replay_tasks) < total_replay_tasks,
    }


def _task_step_trace(task: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = task.get("artifacts") or {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    result = artifacts.get("result") or {}
    result = result if isinstance(result, dict) else {}
    trace = result.get("step_trace") or []
    return [
        item for item in trace
        if isinstance(item, dict) and str(item.get("step_id") or "").strip()
    ]


def compute_analytics(
    store: TaskStore,
    *,
    owner_user_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "overview": compute_task_overview(store, owner_user_id=owner_user_id),
        "step_failure_ranking": compute_step_failure_ranking(
            store, owner_user_id=owner_user_id,
        ),
        "replay_improvement": compute_replay_improvement(
            store, owner_user_id=owner_user_id,
        ),
    }
