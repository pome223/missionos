"""Offline canary and benchmark-gated self-improvement tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.computer_use.trajectory_store import get_computer_trajectory_store
from src.security.audit import get_audit_logger
from src.tools.context import resolve_tool_context
from src.tools.memory import get_memory_store, memory_search, memory_store
from src.tools.shell import run_shell_guarded
from src.tools.tasks import create_task_record, update_task_record
from src.tools.self_improvement_runtime.canary import (
    cleanup_canary as _cleanup_canary_impl,
    package_candidate as _package_candidate_impl,
    prepare_canary as _prepare_canary_impl,
    run_benchmarks as _run_benchmarks_impl,
    run_candidate_commands as _run_candidate_commands_impl,
)
from src.tools.self_improvement_runtime.common import persist_state as _persist_state_impl
from src.tools.self_improvement_runtime.flows import (
    FlowDeps,
    demo_from_trajectory as _demo_from_trajectory_impl,
    search_from_trajectory as _search_from_trajectory_impl,
)
from src.tools.self_improvement_runtime.reuse import (
    find_reuse_suggestions as _find_reuse_suggestions_impl,
)


def _persist_state(canary: Path, **updates: Any) -> dict[str, Any]:
    return _persist_state_impl(canary, **updates)


async def _find_reuse_suggestions(
    trajectory: dict[str, Any],
    *,
    limit: int = 3,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    return await _find_reuse_suggestions_impl(
        trajectory,
        get_memory_store_fn=get_memory_store,
        memory_search_fn=memory_search,
        limit=limit,
        tool_context=tool_context,
    )


async def self_improvement_prepare_canary(
    goal: str,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    return await _prepare_canary_impl(
        goal,
        repo_path_value=repo_path,
        base_ref=base_ref,
        worktree_root_value=worktree_root,
        tool_context=tool_context,
        resolve_tool_context_fn=resolve_tool_context,
        get_audit_logger_fn=get_audit_logger,
    )


async def self_improvement_run_benchmarks(
    canary_path: str,
    commands: str,
    timeout_seconds: int = 0,
    fail_fast: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    return await _run_benchmarks_impl(
        canary_path,
        commands,
        timeout_seconds=timeout_seconds,
        fail_fast=fail_fast,
        tool_context=tool_context,
        resolve_tool_context_fn=resolve_tool_context,
        get_audit_logger_fn=get_audit_logger,
        run_shell_guarded_fn=run_shell_guarded,
    )


async def self_improvement_package_candidate(
    canary_path: str,
    benchmark_commands: str,
    improvement_summary: str,
    repo_path: Optional[str] = None,
    timeout_seconds: int = 0,
    record_as_approved: bool = False,
    promotion_kind: str = "approved_improvement_memory",
    approval_dependencies: list[str] | None = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    return await _package_candidate_impl(
        canary_path,
        benchmark_commands,
        improvement_summary,
        repo_path_value=repo_path,
        timeout_seconds=timeout_seconds,
        record_as_approved=record_as_approved,
        promotion_kind=promotion_kind,
        approval_dependencies=approval_dependencies,
        tool_context=tool_context,
        run_benchmarks_fn=self_improvement_run_benchmarks,
        memory_store_fn=memory_store,
        resolve_tool_context_fn=resolve_tool_context,
    )


async def self_improvement_cleanup_canary(
    canary_path: str,
    remove_branch: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    return await _cleanup_canary_impl(
        canary_path,
        remove_branch=remove_branch,
        tool_context=tool_context,
        resolve_tool_context_fn=resolve_tool_context,
        get_audit_logger_fn=get_audit_logger,
    )


async def _run_candidate_commands(
    *,
    canary: Path,
    commands: str,
    timeout_seconds: int,
    tool_context: Optional[ToolContext],
) -> dict[str, Any]:
    return await _run_candidate_commands_impl(
        canary=canary,
        commands=commands,
        timeout_seconds=timeout_seconds,
        tool_context=tool_context,
        run_shell_guarded_fn=run_shell_guarded,
    )


def _flow_deps() -> FlowDeps:
    def _record_trajectory_reuse(trajectory_id: int, reuse_trace: dict[str, Any]) -> bool:
        return get_computer_trajectory_store().update_reuse_trace(
            int(trajectory_id),
            reuse_trace=reuse_trace,
        )

    return FlowDeps(
        get_computer_trajectory_store=get_computer_trajectory_store,
        find_reuse_suggestions=_find_reuse_suggestions,
        record_trajectory_reuse=_record_trajectory_reuse,
        create_task_record=create_task_record,
        update_task_record=update_task_record,
        prepare_canary=self_improvement_prepare_canary,
        persist_state=_persist_state,
        run_candidate_commands=_run_candidate_commands,
        cleanup_canary=self_improvement_cleanup_canary,
        package_candidate=self_improvement_package_candidate,
    )


async def self_improvement_demo_from_trajectory(
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
    return await _demo_from_trajectory_impl(
        deps=_flow_deps(),
        trajectory_id=trajectory_id,
        candidate_commands=candidate_commands,
        benchmark_commands=benchmark_commands,
        repo_path=repo_path,
        base_ref=base_ref,
        worktree_root=worktree_root,
        goal=goal,
        improvement_summary=improvement_summary,
        timeout_seconds=timeout_seconds,
        record_as_approved=record_as_approved,
        promotion_kind=promotion_kind,
        approval_dependencies=approval_dependencies,
        auto_cleanup=auto_cleanup,
        tool_context=tool_context,
    )


async def self_improvement_search_from_trajectory(
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
    return await _search_from_trajectory_impl(
        deps=_flow_deps(),
        trajectory_id=trajectory_id,
        candidate_specs_json=candidate_specs_json,
        benchmark_commands=benchmark_commands,
        repo_path=repo_path,
        base_ref=base_ref,
        worktree_root=worktree_root,
        goal=goal,
        improvement_summary=improvement_summary,
        timeout_seconds=timeout_seconds,
        record_winner_as_approved=record_winner_as_approved,
        promotion_kind=promotion_kind,
        approval_dependencies=approval_dependencies,
        cleanup_losers=cleanup_losers,
        auto_cleanup=auto_cleanup,
        tool_context=tool_context,
    )


__all__ = [
    "_find_reuse_suggestions",
    "_persist_state",
    "self_improvement_cleanup_canary",
    "self_improvement_demo_from_trajectory",
    "self_improvement_package_candidate",
    "self_improvement_prepare_canary",
    "self_improvement_run_benchmarks",
    "self_improvement_search_from_trajectory",
]
