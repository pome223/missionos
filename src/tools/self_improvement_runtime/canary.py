"""Canary, benchmark, and packaging helpers for self-improvement."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from google.adk.agents.context import Context as ToolContext

from src.config.settings import get_settings
from src.security.audit import AuditEventType
from src.tools.self_improvement_runtime.common import (
    cached_benchmark_result,
    persist_state,
    read_state,
    repo_path,
    run_git,
    slugify,
    split_commands,
    trim_output,
    worktree_root,
    write_state,
)
from src.tools.self_improvement_runtime.promotion import (
    DEFAULT_PROMOTION_KIND,
    build_promotion_artifact,
    normalize_promotion_kind,
    promotion_memory_kind,
)
from src.tools.self_improvement_runtime.security import evaluate_promotion_deployability
from src.tools.self_improvement_runtime.reuse import state_reuse_hints


RunShellFn = Callable[..., Awaitable[dict[str, Any]]]
ResolveContextFn = Callable[[Optional[ToolContext]], dict[str, str]]
AuditLoggerGetter = Callable[[], Any]
MemoryStoreFn = Callable[..., Awaitable[dict[str, Any]]]
RunBenchmarksFn = Callable[..., Awaitable[dict[str, Any]]]


async def run_candidate_commands(
    *,
    canary: Path,
    commands: str,
    timeout_seconds: int,
    tool_context: Optional[ToolContext],
    run_shell_guarded_fn: RunShellFn,
) -> dict[str, Any]:
    command_list = split_commands(commands)
    if not command_list:
        return {"success": False, "error": "At least one candidate command is required"}

    timeout = timeout_seconds or get_settings().self_improvement_benchmark_timeout_seconds
    results: list[dict[str, Any]] = []
    all_passed = True
    for command in command_list:
        completed = await run_shell_guarded_fn(
            command=command,
            timeout=timeout,
            cwd=str(canary),
            tool_context=tool_context,
        )
        entry = {
            "command": command,
            "return_code": completed.get("return_code"),
            "passed": completed.get("return_code") == 0,
            "stdout": trim_output(str(completed.get("stdout") or "").strip()),
            "stderr": trim_output(str(completed.get("stderr") or "").strip()),
        }
        if completed.get("error") and not entry["stderr"]:
            entry["stderr"] = str(completed["error"])
        results.append(entry)
        if not entry["passed"]:
            all_passed = False
            break

    payload = {
        "success": all_passed,
        "canary_path": str(canary),
        "results": results,
    }
    persist_state(
        canary,
        candidate_commands={
            "commands": command_list,
            "all_passed": all_passed,
            "results": results,
            "completed_at": time.time(),
        },
    )
    if not all_passed:
        payload["error"] = "candidate command failed"
    return payload


async def prepare_canary(
    goal: str,
    *,
    repo_path_value: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root_value: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
    resolve_tool_context_fn: ResolveContextFn,
    get_audit_logger_fn: AuditLoggerGetter,
) -> dict[str, Any]:
    ctx = resolve_tool_context_fn(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger_fn()

    repo = repo_path(repo_path_value)
    root = worktree_root(worktree_root_value)
    slug = slugify(goal)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch_name = f"canary/{slug}-{stamp}"
    target = root / f"{slug}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)

    rev_parse = run_git(repo, "rev-parse", "--show-toplevel")
    if rev_parse.returncode != 0:
        error = rev_parse.stderr.strip() or "not a git repository"
        return {"success": False, "error": error}

    result = run_git(repo, "worktree", "add", "-b", branch_name, str(target), base_ref)
    success = result.returncode == 0
    payload = {
        "success": success,
        "goal": goal,
        "repo_path": str(repo),
        "canary_path": str(target),
        "branch": branch_name,
        "base_ref": base_ref,
        "stdout": trim_output(result.stdout.strip()),
        "stderr": trim_output(result.stderr.strip()),
    }
    if success:
        write_state(
            target,
            {
                "goal": goal,
                "repo_path": str(repo),
                "canary_path": str(target),
                "branch": branch_name,
                "base_ref": base_ref,
                "created_at": time.time(),
            },
        )
    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="self_improvement_prepare_canary",
        resource=str(target),
        result="success" if success else f"error:{payload['stderr'] or payload['stdout']}",
        metadata={"repo_path": str(repo), "branch": branch_name, "base_ref": base_ref},
    )
    return payload


async def run_benchmarks(
    canary_path: str,
    commands: str,
    *,
    timeout_seconds: int = 0,
    fail_fast: bool = True,
    tool_context: Optional[ToolContext] = None,
    resolve_tool_context_fn: ResolveContextFn,
    get_audit_logger_fn: AuditLoggerGetter,
    run_shell_guarded_fn: RunShellFn,
) -> dict[str, Any]:
    ctx = resolve_tool_context_fn(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger_fn()

    canary = Path(canary_path).resolve()
    if not canary.exists():
        return {"success": False, "error": f"Canary path does not exist: {canary}"}

    command_list = split_commands(commands)
    if not command_list:
        return {"success": False, "error": "At least one benchmark command is required"}

    cached = cached_benchmark_result(canary, commands)
    if cached is not None:
        return cached

    timeout = timeout_seconds or get_settings().self_improvement_benchmark_timeout_seconds
    results: list[dict[str, Any]] = []
    all_passed = True
    for command in command_list:
        completed = await run_shell_guarded_fn(
            command=command,
            timeout=timeout,
            cwd=str(canary),
            tool_context=tool_context,
        )
        entry = {
            "command": command,
            "return_code": completed.get("return_code"),
            "passed": completed.get("return_code") == 0,
            "stdout": trim_output(str(completed.get("stdout") or "").strip()),
            "stderr": trim_output(str(completed.get("stderr") or "").strip()),
        }
        if completed.get("error") and not entry["stderr"]:
            entry["stderr"] = str(completed["error"])
        results.append(entry)
        if not entry["passed"]:
            all_passed = False
            if fail_fast:
                break

    payload = {
        "success": True,
        "all_passed": all_passed,
        "canary_path": str(canary),
        "results": results,
    }
    persist_state(
        canary,
        benchmark={
            "commands": command_list,
            "timeout_seconds": timeout,
            "fail_fast": fail_fast,
            "all_passed": all_passed,
            "results": results,
            "completed_at": time.time(),
        },
    )
    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="self_improvement_run_benchmarks",
        resource=str(canary),
        result="success" if all_passed else "benchmark_failed",
        metadata={"count": len(results), "all_passed": all_passed},
    )
    return payload


async def package_candidate(
    canary_path: str,
    benchmark_commands: str,
    improvement_summary: str,
    *,
    repo_path_value: Optional[str] = None,
    timeout_seconds: int = 0,
    record_as_approved: bool = False,
    promotion_kind: str = DEFAULT_PROMOTION_KIND,
    approval_dependencies: list[str] | None = None,
    tool_context: Optional[ToolContext] = None,
    run_benchmarks_fn: RunBenchmarksFn,
    memory_store_fn: MemoryStoreFn,
    resolve_tool_context_fn: ResolveContextFn,
) -> dict[str, Any]:
    benchmark_result = await run_benchmarks_fn(
        canary_path=canary_path,
        commands=benchmark_commands,
        timeout_seconds=timeout_seconds,
        fail_fast=False,
        tool_context=tool_context,
    )
    if not benchmark_result.get("success"):
        return benchmark_result

    canary = Path(canary_path).resolve()
    repo = repo_path(repo_path_value or canary_path)
    state = read_state(canary)
    diff_stat = run_git(canary, "diff", "--stat")
    diff_patch = run_git(canary, "diff", "--minimal", "--binary", "--no-ext-diff")
    branch = run_git(canary, "rev-parse", "--abbrev-ref", "HEAD")
    resolved_promotion_kind = normalize_promotion_kind(promotion_kind)
    flow_state = {}
    for key in ("demo", "search"):
        candidate = state.get(key)
        if isinstance(candidate, dict):
            flow_state = candidate
            break
    reuse_hints = state_reuse_hints(canary)

    payload = {
        "success": True,
        "promotable": bool(benchmark_result["all_passed"]),
        "canary_path": str(canary),
        "repo_path": str(repo),
        "branch": branch.stdout.strip(),
        "improvement_summary": improvement_summary,
        "benchmark": benchmark_result,
        "benchmark_reused": bool(benchmark_result.get("reused")),
        "diff_stat": diff_stat.stdout.strip(),
        "diff_excerpt": trim_output(diff_patch.stdout.strip(), limit=8000),
        "promotion_kind": resolved_promotion_kind,
        "approval_dependencies": [str(item).strip() for item in approval_dependencies or [] if str(item).strip()],
    }
    payload["promotion_artifact"] = build_promotion_artifact(
        promotion_kind=resolved_promotion_kind,
        canary_path=str(canary),
        branch=payload["branch"],
        improvement_summary=improvement_summary,
        goal=str(flow_state.get("goal") or ""),
        failure_reason=str(flow_state.get("failure_reason") or ""),
        failure_type=str(flow_state.get("failure_type") or reuse_hints.get("failure_type") or ""),
        trajectory_id=int(flow_state["trajectory_id"]) if flow_state.get("trajectory_id") is not None else None,
        benchmark_results=list(benchmark_result.get("results") or []),
        diff_stat=payload["diff_stat"],
        reuse_hints=reuse_hints,
        approval_dependencies=payload["approval_dependencies"],
    )
    payload["promotion_gate"] = evaluate_promotion_deployability(
        promotion_kind=resolved_promotion_kind,
        artifact=payload["promotion_artifact"],
    )

    if record_as_approved and benchmark_result["all_passed"]:
        memory_kind = promotion_memory_kind(resolved_promotion_kind)
        ctx = resolve_tool_context_fn(tool_context) if tool_context is not None else {}
        if resolved_promotion_kind != DEFAULT_PROMOTION_KIND and not payload["promotion_gate"]["deployable"]:
            payload["approved_record"] = {
                "success": False,
                "error": payload["promotion_gate"]["reasons"][0]
                if payload["promotion_gate"]["reasons"]
                else (
                    f"promotion kind {resolved_promotion_kind} cannot be recorded as approved"
                ),
            }
        else:
            memory_payload = await memory_store_fn(
                content=improvement_summary,
                tags=f"self-improvement,approved,{memory_kind}",
                metadata=json.dumps(
                    {
                        "canary_path": str(canary),
                        "branch": payload["branch"],
                        "benchmark_results": benchmark_result["results"],
                        "diff_stat": payload["diff_stat"],
                        **reuse_hints,
                        "failure_type": str(flow_state.get("failure_type") or reuse_hints.get("failure_type") or ""),
                        "approval_dependencies": payload["approval_dependencies"],
                        "artifact_kind": resolved_promotion_kind,
                        "promotion_artifact": payload["promotion_artifact"],
                        "approved_by_user_id": ctx.get("user_id") or "",
                        "approved_in_session_id": ctx.get("session_id") or "",
                    },
                    ensure_ascii=True,
                ),
                kind=memory_kind,
                tool_context=tool_context,
            )
            payload["approved_record"] = memory_payload
            if memory_kind == "approved_improvement":
                payload["approved_memory"] = memory_payload

    return payload


async def cleanup_canary(
    canary_path: str,
    *,
    remove_branch: bool = True,
    tool_context: Optional[ToolContext] = None,
    resolve_tool_context_fn: ResolveContextFn,
    get_audit_logger_fn: AuditLoggerGetter,
) -> dict[str, Any]:
    ctx = resolve_tool_context_fn(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger_fn()

    canary = Path(canary_path).resolve()
    if not canary.exists():
        return {"success": False, "error": f"Canary path does not exist: {canary}"}

    state = read_state(canary)
    repo = repo_path(state.get("repo_path") or str(canary))
    branch_name = str(state.get("branch") or "").strip()
    if not branch_name:
        branch = run_git(canary, "rev-parse", "--abbrev-ref", "HEAD")
        branch_name = branch.stdout.strip()

    remove_result = run_git(repo, "worktree", "remove", "--force", str(canary))
    success = remove_result.returncode == 0
    branch_deleted = False
    branch_error = ""
    if success and remove_branch and branch_name:
        branch_result = run_git(repo, "branch", "-D", branch_name)
        branch_deleted = branch_result.returncode == 0
        branch_error = branch_result.stderr.strip()

    payload = {
        "success": success,
        "canary_path": str(canary),
        "branch": branch_name,
        "worktree_removed": success,
        "branch_deleted": branch_deleted,
        "stdout": trim_output(remove_result.stdout.strip()),
        "stderr": trim_output(remove_result.stderr.strip()),
    }
    if branch_error:
        payload["branch_stderr"] = trim_output(branch_error)

    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="self_improvement_cleanup_canary",
        resource=str(canary),
        result="success" if success else f"error:{payload['stderr'] or payload['stdout']}",
        metadata={"branch": branch_name, "remove_branch": remove_branch},
    )
    return payload


__all__ = [
    "cleanup_canary",
    "package_candidate",
    "prepare_canary",
    "run_benchmarks",
    "run_candidate_commands",
]
