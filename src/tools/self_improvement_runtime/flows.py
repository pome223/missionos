"""High-level self-improvement demo/search flows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from google.adk.agents.context import Context as ToolContext

from src.tools.self_improvement_runtime.common import (
    candidate_diff_metrics,
    candidate_ranking_key,
    parse_candidate_specs,
    search_candidate_goal,
    search_candidate_summary,
)
from src.tools.self_improvement_runtime.reuse import (
    build_reuse_trace,
    build_repair_prompt,
    improvement_summary_with_reuse,
    trajectory_demo_goal,
    trajectory_failure_reason,
    trajectory_improvement_summary,
    trajectory_reuse_hints,
    trajectory_search_goal,
)


AsyncDictFn = Callable[..., Awaitable[dict[str, Any]]]
SyncDictFn = Callable[..., dict[str, Any]]
TrajectoryStoreGetter = Callable[[], Any]


@dataclass(frozen=True)
class FlowDeps:
    get_computer_trajectory_store: TrajectoryStoreGetter
    find_reuse_suggestions: AsyncDictFn
    record_trajectory_reuse: Callable[[int, dict[str, Any]], bool]
    create_task_record: SyncDictFn
    update_task_record: SyncDictFn
    prepare_canary: AsyncDictFn
    persist_state: SyncDictFn
    run_candidate_commands: AsyncDictFn
    cleanup_canary: AsyncDictFn
    package_candidate: AsyncDictFn


async def demo_from_trajectory(
    *,
    deps: FlowDeps,
    trajectory_id: int,
    candidate_commands: str,
    benchmark_commands: str,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root: Optional[str] = None,
    goal: Optional[str] = None,
    improvement_summary: Optional[str] = None,
    timeout_seconds: int = 0,
    record_as_approved: bool = False,
    promotion_kind: str = "approved_improvement_memory",
    approval_dependencies: list[str] | None = None,
    auto_cleanup: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    trajectory = deps.get_computer_trajectory_store().get(trajectory_id)
    if trajectory is None:
        return {"success": False, "error": f"Unknown computer trajectory: {trajectory_id}"}
    if str(trajectory.get("status") or "") != "failed":
        return {
            "success": False,
            "error": f"Trajectory {trajectory_id} must have status=failed for the demo flow",
            "trajectory": trajectory,
        }

    resolved_goal = goal or trajectory_demo_goal(trajectory)
    resolved_summary = improvement_summary or trajectory_improvement_summary(trajectory)
    reuse = await deps.find_reuse_suggestions(trajectory, tool_context=tool_context)
    reuse_trace = build_reuse_trace(
        trajectory,
        reuse,
        source="self_improvement_demo",
    )
    deps.record_trajectory_reuse(trajectory_id, reuse_trace)
    resolved_summary_with_reuse = improvement_summary_with_reuse(resolved_summary, reuse)
    repair_prompt = build_repair_prompt(
        goal=resolved_goal,
        improvement_summary=resolved_summary_with_reuse,
        trajectory=trajectory,
        reuse=reuse,
    )
    task_record = deps.create_task_record(
        kind="self_improvement_demo",
        title=resolved_goal,
        status="running",
        artifacts={
            "trajectory": trajectory,
            "goal": resolved_goal,
            "improvement_summary": resolved_summary,
            "improvement_summary_with_reuse": resolved_summary_with_reuse,
            "repair_prompt": repair_prompt,
            "reuse_query": reuse.get("query", ""),
            "reuse_suggestions": reuse.get("results", []),
            "reuse_memory_ids": reuse.get("memory_ids", []),
            "reuse_policy": reuse.get("policy", {}),
            "promotion_kind": promotion_kind,
        },
        metadata={
            "trajectory_id": trajectory_id,
            "record_as_approved": record_as_approved,
            "promotion_kind": promotion_kind,
            "auto_cleanup": auto_cleanup,
        },
        approval_dependencies=approval_dependencies,
        tool_context=tool_context,
    )
    task_id = str(task_record["task_id"])

    prepare = await deps.prepare_canary(
        goal=resolved_goal,
        repo_path=repo_path,
        base_ref=base_ref,
        worktree_root=worktree_root,
        tool_context=tool_context,
    )
    if not prepare.get("success"):
        deps.update_task_record(
            task_id,
            status="failed",
            artifacts={"prepare": prepare},
            error=prepare.get("error") or prepare.get("stderr") or "failed to prepare canary",
        )
        return {
            "success": False,
            "task_id": task_id,
            "trajectory": trajectory,
            "prepare": prepare,
            "error": prepare.get("error") or prepare.get("stderr") or "failed to prepare canary",
        }

    canary = Path(prepare["canary_path"]).resolve()
    deps.persist_state(
        canary,
        demo={
            "trajectory_id": trajectory_id,
            "trajectory_status": trajectory.get("status"),
            "failure_reason": trajectory_failure_reason(trajectory),
            "failure_type": trajectory.get("normalized_failure_type")
            or trajectory.get("failure_type")
            or trajectory.get("preliminary_failure_type")
            or "",
            "goal": resolved_goal,
            "improvement_summary": resolved_summary,
            "improvement_summary_with_reuse": resolved_summary_with_reuse,
            "repair_prompt": repair_prompt,
            "reuse_hints": trajectory_reuse_hints(trajectory),
            "reuse_query": reuse.get("query", ""),
            "reuse_suggestions": reuse.get("results", []),
            "reuse_memory_ids": reuse.get("memory_ids", []),
            "reuse_policy": reuse.get("policy", {}),
            "promotion_kind": promotion_kind,
            "approval_dependencies": list(approval_dependencies or []),
            "started_at": time.time(),
        },
    )

    candidate = await deps.run_candidate_commands(
        canary=canary,
        commands=candidate_commands,
        timeout_seconds=timeout_seconds,
        tool_context=tool_context,
    )
    if not candidate.get("success"):
        payload = {
            "success": False,
            "task_id": task_id,
            "trajectory": trajectory,
            "prepare": prepare,
            "candidate": candidate,
            "error": candidate.get("error") or "candidate command failed",
        }
        if auto_cleanup:
            payload["cleanup"] = await deps.cleanup_canary(
                canary_path=str(canary),
                tool_context=tool_context,
            )
        deps.update_task_record(
            task_id,
            status="failed",
            artifacts={key: value for key, value in payload.items() if key in {"prepare", "candidate", "cleanup"}},
            error=payload["error"],
        )
        return payload

    packaged = await deps.package_candidate(
        canary_path=str(canary),
        benchmark_commands=benchmark_commands,
        improvement_summary=resolved_summary_with_reuse,
        repo_path=repo_path,
        timeout_seconds=timeout_seconds,
        record_as_approved=record_as_approved,
        promotion_kind=promotion_kind,
        approval_dependencies=approval_dependencies,
        tool_context=tool_context,
    )
    payload = {
        "success": bool(packaged.get("success")),
        "task_id": task_id,
        "trajectory": trajectory,
        "prepare": prepare,
        "candidate": candidate,
        "package": packaged,
        "goal": resolved_goal,
        "improvement_summary": resolved_summary,
        "improvement_summary_with_reuse": resolved_summary_with_reuse,
        "repair_prompt": repair_prompt,
        "reuse_query": reuse.get("query", ""),
        "reuse_suggestions": reuse.get("results", []),
        "reuse_memory_ids": reuse.get("memory_ids", []),
        "reuse_policy": reuse.get("policy", {}),
    }
    if auto_cleanup:
        payload["cleanup"] = await deps.cleanup_canary(
            canary_path=str(canary),
            tool_context=tool_context,
        )
    deps.update_task_record(
        task_id,
        status="completed" if payload["success"] else "failed",
        artifacts={key: value for key, value in payload.items() if key in {"prepare", "candidate", "package", "cleanup"}},
        error=None if payload["success"] else packaged.get("error"),
    )
    return payload


async def search_from_trajectory(
    *,
    deps: FlowDeps,
    trajectory_id: int,
    candidate_specs_json: str,
    benchmark_commands: str,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root: Optional[str] = None,
    goal: Optional[str] = None,
    improvement_summary: Optional[str] = None,
    timeout_seconds: int = 0,
    record_winner_as_approved: bool = False,
    promotion_kind: str = "approved_improvement_memory",
    approval_dependencies: list[str] | None = None,
    cleanup_losers: bool = True,
    auto_cleanup: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    trajectory = deps.get_computer_trajectory_store().get(trajectory_id)
    if trajectory is None:
        return {"success": False, "error": f"Unknown computer trajectory: {trajectory_id}"}
    if str(trajectory.get("status") or "") != "failed":
        return {
            "success": False,
            "error": f"Trajectory {trajectory_id} must have status=failed for the search flow",
            "trajectory": trajectory,
        }

    try:
        candidate_specs = parse_candidate_specs(candidate_specs_json)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    resolved_goal = goal or trajectory_search_goal(trajectory)
    resolved_summary = improvement_summary or trajectory_improvement_summary(trajectory)
    reuse = await deps.find_reuse_suggestions(trajectory, tool_context=tool_context)
    reuse_trace = build_reuse_trace(
        trajectory,
        reuse,
        source="self_improvement_search",
    )
    deps.record_trajectory_reuse(trajectory_id, reuse_trace)
    resolved_summary_with_reuse = improvement_summary_with_reuse(resolved_summary, reuse)
    repair_prompt = build_repair_prompt(
        goal=resolved_goal,
        improvement_summary=resolved_summary_with_reuse,
        trajectory=trajectory,
        reuse=reuse,
    )
    parent_task = deps.create_task_record(
        kind="self_improvement_search",
        title=resolved_goal,
        status="running",
        artifacts={
            "trajectory": trajectory,
            "goal": resolved_goal,
            "improvement_summary": resolved_summary,
            "improvement_summary_with_reuse": resolved_summary_with_reuse,
            "repair_prompt": repair_prompt,
            "reuse_query": reuse.get("query", ""),
            "reuse_suggestions": reuse.get("results", []),
            "reuse_memory_ids": reuse.get("memory_ids", []),
            "reuse_policy": reuse.get("policy", {}),
            "promotion_kind": promotion_kind,
        },
        metadata={
            "trajectory_id": trajectory_id,
            "record_winner_as_approved": record_winner_as_approved,
            "promotion_kind": promotion_kind,
            "cleanup_losers": cleanup_losers,
            "auto_cleanup": auto_cleanup,
        },
        approval_dependencies=approval_dependencies,
        tool_context=tool_context,
    )
    parent_task_id = str(parent_task["task_id"])

    candidates: list[dict[str, Any]] = []
    winner_index: int | None = None
    winner_key: tuple[int, int, int, int, int, int] | None = None

    for index, spec in enumerate(candidate_specs):
        candidate_name = str(spec["name"])
        candidate_goal = search_candidate_goal(resolved_goal, candidate_name, spec.get("goal"))
        candidate_summary = search_candidate_summary(
            resolved_summary,
            candidate_name,
            spec.get("improvement_summary"),
        )
        candidate_summary_with_reuse = improvement_summary_with_reuse(candidate_summary, reuse)
        candidate_generation_prompt = build_repair_prompt(
            goal=candidate_goal,
            improvement_summary=candidate_summary_with_reuse,
            trajectory=trajectory,
            reuse=reuse,
        )

        prepare = await deps.prepare_canary(
            goal=candidate_goal,
            repo_path=repo_path,
            base_ref=base_ref,
            worktree_root=worktree_root,
            tool_context=tool_context,
        )
        candidate_result: dict[str, Any] = {
            "name": candidate_name,
            "index": index,
            "goal": candidate_goal,
            "improvement_summary": candidate_summary,
            "improvement_summary_with_reuse": candidate_summary_with_reuse,
            "candidate_generation_prompt": candidate_generation_prompt,
        }
        candidate_task = deps.create_task_record(
            kind="self_improvement_candidate",
            title=candidate_goal,
            status="running",
            parent_task_id=parent_task_id,
            artifacts={
                "trajectory_id": trajectory_id,
                "candidate_name": candidate_name,
                "goal": candidate_goal,
                "improvement_summary": candidate_summary,
                "improvement_summary_with_reuse": candidate_summary_with_reuse,
                "candidate_generation_prompt": candidate_generation_prompt,
                "promotion_kind": promotion_kind,
            },
            metadata={"candidate_index": index},
            approval_dependencies=approval_dependencies,
            tool_context=tool_context,
        )
        candidate_result["task_id"] = candidate_task["task_id"]
        candidate_result["prepare"] = prepare
        if not prepare.get("success"):
            candidate_result["success"] = False
            candidate_result["error"] = (
                prepare.get("error") or prepare.get("stderr") or "failed to prepare canary"
            )
            deps.update_task_record(
                str(candidate_task["task_id"]),
                status="failed",
                artifacts={"prepare": prepare},
                error=candidate_result["error"],
            )
            candidates.append(candidate_result)
            continue

        canary = Path(prepare["canary_path"]).resolve()
        deps.persist_state(
            canary,
            search={
                "trajectory_id": trajectory_id,
                "candidate_name": candidate_name,
                "candidate_index": index,
                "trajectory_status": trajectory.get("status"),
                "failure_reason": trajectory_failure_reason(trajectory),
                "failure_type": trajectory.get("normalized_failure_type")
                or trajectory.get("failure_type")
                or trajectory.get("preliminary_failure_type")
                or "",
                "goal": candidate_goal,
                "improvement_summary": candidate_summary,
                "improvement_summary_with_reuse": candidate_summary_with_reuse,
                "candidate_generation_prompt": candidate_generation_prompt,
                "reuse_hints": trajectory_reuse_hints(trajectory),
                "reuse_query": reuse.get("query", ""),
                "reuse_suggestions": reuse.get("results", []),
                "reuse_memory_ids": reuse.get("memory_ids", []),
                "reuse_policy": reuse.get("policy", {}),
                "promotion_kind": promotion_kind,
                "approval_dependencies": list(approval_dependencies or []),
                "started_at": time.time(),
            },
        )

        candidate = await deps.run_candidate_commands(
            canary=canary,
            commands="\n".join(spec["commands"]),
            timeout_seconds=timeout_seconds,
            tool_context=tool_context,
        )
        candidate_result["candidate"] = candidate
        if not candidate.get("success"):
            candidate_result["success"] = False
            candidate_result["error"] = candidate.get("error") or "candidate command failed"
            if cleanup_losers:
                candidate_result["cleanup"] = await deps.cleanup_canary(
                    canary_path=str(canary),
                    tool_context=tool_context,
                )
            deps.update_task_record(
                str(candidate_task["task_id"]),
                status="failed",
                artifacts={
                    key: value
                    for key, value in candidate_result.items()
                    if key in {"prepare", "candidate", "cleanup"}
                },
                error=candidate_result["error"],
            )
            candidates.append(candidate_result)
            continue

        packaged = await deps.package_candidate(
            canary_path=str(canary),
            benchmark_commands=benchmark_commands,
            improvement_summary=candidate_summary_with_reuse,
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            record_as_approved=False,
            promotion_kind=promotion_kind,
            approval_dependencies=approval_dependencies,
            tool_context=tool_context,
        )
        candidate_result["package"] = packaged
        candidate_result["diff_metrics"] = candidate_diff_metrics(canary)
        candidate_result["success"] = bool(packaged.get("success"))
        if not packaged.get("success"):
            candidate_result["error"] = packaged.get("error") or "failed to package candidate"
        deps.update_task_record(
            str(candidate_task["task_id"]),
            status="completed" if candidate_result["success"] else "failed",
            artifacts={
                key: value
                for key, value in candidate_result.items()
                if key in {"prepare", "candidate", "package", "diff_metrics", "ranking_key"}
            },
            error=candidate_result.get("error"),
        )
        ranking_key = candidate_ranking_key(candidate_result)
        candidate_result["ranking_key"] = list(ranking_key)
        if winner_key is None or ranking_key > winner_key:
            winner_key = ranking_key
            winner_index = index
        candidates.append(candidate_result)

    winner = candidates[winner_index] if winner_index is not None else None
    if winner is not None and winner.get("prepare", {}).get("success"):
        winner_canary = str(winner["prepare"]["canary_path"])
    else:
        winner_canary = None
    has_promotable_winner = bool(winner and winner.get("package", {}).get("promotable"))

    if has_promotable_winner and record_winner_as_approved:
        refreshed = await deps.package_candidate(
            canary_path=str(winner_canary),
            benchmark_commands=benchmark_commands,
            improvement_summary=str(
                winner.get("improvement_summary_with_reuse") or winner.get("improvement_summary") or ""
            ),
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            record_as_approved=True,
            promotion_kind=promotion_kind,
            approval_dependencies=approval_dependencies,
            tool_context=tool_context,
        )
        winner["package"] = refreshed
        winner["diff_metrics"] = candidate_diff_metrics(Path(winner_canary))

    for index, candidate in enumerate(candidates):
        canary_path = candidate.get("prepare", {}).get("canary_path")
        if not canary_path:
            continue
        should_cleanup = False
        if winner_index is None:
            should_cleanup = cleanup_losers
        elif not has_promotable_winner:
            should_cleanup = cleanup_losers
        elif index != winner_index:
            should_cleanup = cleanup_losers
        elif auto_cleanup:
            should_cleanup = True
        if should_cleanup:
            candidate["cleanup"] = await deps.cleanup_canary(
                canary_path=str(canary_path),
                tool_context=tool_context,
            )
            deps.update_task_record(
                str(candidate.get("task_id")),
                artifacts={"cleanup": candidate["cleanup"]},
            )

    payload = {
        "success": bool(winner and winner.get("package", {}).get("promotable")),
        "task_id": parent_task_id,
        "trajectory": trajectory,
        "goal": resolved_goal,
        "improvement_summary": resolved_summary,
        "improvement_summary_with_reuse": resolved_summary_with_reuse,
        "repair_prompt": repair_prompt,
        "reuse_query": reuse.get("query", ""),
        "reuse_suggestions": reuse.get("results", []),
        "reuse_memory_ids": reuse.get("memory_ids", []),
        "reuse_policy": reuse.get("policy", {}),
        "candidates": candidates,
    }
    if winner is not None:
        payload["winner"] = winner
        payload["winner_name"] = winner.get("name")
    if not payload["success"]:
        payload["error"] = "No promotable candidate found"
    candidate_task_ids = [str(candidate.get("task_id")) for candidate in candidates if candidate.get("task_id")]
    winner_task_id = str(winner.get("task_id")) if winner and winner.get("task_id") else None
    loser_task_ids = [task_id for task_id in candidate_task_ids if task_id != winner_task_id]
    deps.update_task_record(
        parent_task_id,
        status="completed" if payload["success"] else "failed",
        winner_task_id=winner_task_id,
        loser_task_ids=loser_task_ids,
        artifacts={
            "candidate_count": len(candidates),
            "candidate_task_ids": candidate_task_ids,
            "winner_name": payload.get("winner_name"),
            "winner_task_id": winner_task_id,
            "reuse_query": reuse.get("query", ""),
            "reuse_suggestions": reuse.get("results", []),
            "reuse_memory_ids": reuse.get("memory_ids", []),
            "reuse_policy": reuse.get("policy", {}),
        },
        error=payload.get("error"),
    )
    return payload


__all__ = ["FlowDeps", "demo_from_trajectory", "search_from_trajectory"]
