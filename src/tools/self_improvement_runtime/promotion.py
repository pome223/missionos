"""Typed promotion artifact helpers for self-improvement flows.

Artifact classes are intentionally separated so runtime registration and
governance stay explicit:

- approved_improvement_memory: retrieval-only repair knowledge
- approved_skill: reusable bounded task recipe for planner/repair reuse
- capability_patch: typed runtime surface that must register into the capability layer
- policy_patch: execution or promotion constraint enforced by security gates
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.tools.self_improvement_runtime.common import compact_text, slugify


DEFAULT_PROMOTION_KIND = "approved_improvement_memory"
VALID_PROMOTION_KINDS = (
    "approved_improvement_memory",
    "approved_skill",
    "capability_patch",
    "policy_patch",
)
PROMOTION_KIND_TO_MEMORY_KIND = {
    "approved_improvement_memory": "approved_improvement",
    "approved_skill": "approved_skill",
    "capability_patch": "capability_patch",
    "policy_patch": "policy_patch",
}
REUSE_MEMORY_KINDS = tuple(PROMOTION_KIND_TO_MEMORY_KIND.values())


def normalize_promotion_kind(value: str | None) -> str:
    normalized = str(value or DEFAULT_PROMOTION_KIND).strip() or DEFAULT_PROMOTION_KIND
    if normalized not in VALID_PROMOTION_KINDS:
        supported = ", ".join(VALID_PROMOTION_KINDS)
        raise ValueError(f"Unsupported promotion kind: {normalized}. Supported values: {supported}")
    return normalized


def promotion_memory_kind(value: str | None) -> str:
    promotion_kind = normalize_promotion_kind(value)
    return PROMOTION_KIND_TO_MEMORY_KIND[promotion_kind]


def promotion_search_kind_filter() -> str:
    return ",".join(REUSE_MEMORY_KINDS)


def _approval_payload(approval_dependencies: list[str] | None, *, promotion_kind: str) -> tuple[bool, list[str], str]:
    dependencies = [str(item).strip() for item in approval_dependencies or [] if str(item).strip()]
    approval_required = promotion_kind != DEFAULT_PROMOTION_KIND
    if dependencies:
        status = "linked"
    elif approval_required:
        status = "pending"
    else:
        status = "not_required"
    return approval_required, dependencies, status


def _artifact_handle(reuse_hints: dict[str, str], improvement_summary: str) -> str:
    surface = slugify(reuse_hints.get("surface") or "generic")
    target = slugify(
        reuse_hints.get("target")
        or reuse_hints.get("selector")
        or reuse_hints.get("identifier")
        or reuse_hints.get("title")
        or improvement_summary
    )
    return slugify(f"{surface}-{target}")[:80] or "promotion-candidate"


def _skill_preview(
    *,
    handle: str,
    goal: str,
    summary: str,
    surface: str,
    target: str,
    benchmark_results: list[dict[str, Any]],
    trajectory_id: int | None,
    failure_reason: str,
) -> str:
    return "\n".join(
        [
            f"# promoted/{handle}",
            "",
            "## Goal",
            goal or summary,
            "",
            "## Inputs",
            f"- surface: {surface or 'unknown'}",
            f"- target: {target or 'unknown-target'}",
            "",
            "## Procedure",
            "- Reuse the benchmarked repair path from the canary candidate.",
            "- Capture destination-bound evidence before declaring success.",
            "- Fall back to verification-first repair if the target has drifted.",
            "",
            "## Provenance",
            f"- trajectory_id: {trajectory_id if trajectory_id is not None else '-'}",
            f"- failure_reason: {failure_reason or '-'}",
            f"- benchmark_steps: {len(benchmark_results)}",
        ]
    )


def _capability_preview(
    *,
    handle: str,
    surface: str,
    target: str,
    summary: str,
    benchmark_results: list[dict[str, Any]],
) -> str:
    payload = {
        "name": f"promoted.{slugify(surface or 'generic')}.{handle.replace('-', '_')}",
        "surface": surface or "unknown",
        "target": target or "unknown-target",
        "summary": summary,
        "evidence": {
            "benchmark_steps": len(benchmark_results),
            "requires_destination_bound_verification": True,
        },
        "fallback_chain": ["current_tab", "desktop_ax", "desktop_screenshot"],
    }
    return json.dumps(payload, ensure_ascii=True, indent=2)


def _policy_preview(
    *,
    handle: str,
    summary: str,
    benchmark_results: list[dict[str, Any]],
) -> str:
    return "\n".join(
        [
            f"id: policy-{handle}",
            "scope: self_improvement_promotion",
            f"summary: {compact_text(summary, limit=200)}",
            "rules:",
            "  - promotion requires explicit approval dependency",
            "  - benchmark regressions block promotion",
            f"  - benchmark_steps: {len(benchmark_results)}",
        ]
    )


def build_promotion_artifact(
    *,
    promotion_kind: str,
    canary_path: str,
    branch: str,
    improvement_summary: str,
    goal: str = "",
    failure_reason: str = "",
    failure_type: str = "",
    trajectory_id: int | None = None,
    benchmark_results: list[dict[str, Any]] | None = None,
    diff_stat: str = "",
    reuse_hints: dict[str, str] | None = None,
    approval_dependencies: list[str] | None = None,
) -> dict[str, Any]:
    resolved_kind = normalize_promotion_kind(promotion_kind)
    resolved_reuse_hints = {
        str(key): str(value or "").strip()
        for key, value in (reuse_hints or {}).items()
        if str(key).strip()
    }
    benchmark_payload = list(benchmark_results or [])
    handle = _artifact_handle(resolved_reuse_hints, improvement_summary)
    approval_required, approval_refs, approval_status = _approval_payload(
        approval_dependencies,
        promotion_kind=resolved_kind,
    )
    memory_kind = promotion_memory_kind(resolved_kind)
    surface = resolved_reuse_hints.get("surface") or "unknown"
    target = (
        resolved_reuse_hints.get("target")
        or resolved_reuse_hints.get("selector")
        or resolved_reuse_hints.get("identifier")
        or resolved_reuse_hints.get("title")
        or "unknown-target"
    )

    artifact = {
        "artifact_kind": resolved_kind,
        "memory_kind": memory_kind,
        "proposed_handle": handle,
        "canary_path": str(Path(canary_path)),
        "branch": branch,
        "goal": goal,
        "improvement_summary": improvement_summary,
        "failure_reason": failure_reason,
        "failure_type": str(failure_type or "").strip(),
        "trajectory_id": trajectory_id,
        "surface": surface,
        "target": target,
        "selector": resolved_reuse_hints.get("selector") or "",
        "trajectory_key": resolved_reuse_hints.get("trajectory_key") or "",
        "diff_stat": diff_stat,
        "approval_required": approval_required,
        "approval_status": approval_status,
        "approval_dependencies": approval_refs,
        "benchmark_step_count": len(benchmark_payload),
    }

    if resolved_kind == "approved_skill":
        artifact.update(
            {
                "proposed_path": f"skills/promoted/{handle}/SKILL.md",
                "skill_name": f"promoted/{handle}",
                "content_preview": _skill_preview(
                    handle=handle,
                    goal=goal,
                    summary=improvement_summary,
                    surface=surface,
                    target=target,
                    benchmark_results=benchmark_payload,
                    trajectory_id=trajectory_id,
                    failure_reason=failure_reason,
                ),
            }
        )
    elif resolved_kind == "capability_patch":
        artifact.update(
            {
                "proposed_path": f"src/runtime/promoted_capabilities/{handle}.json",
                "capability_name": f"promoted.{slugify(surface)}.{handle.replace('-', '_')}",
                "content_preview": _capability_preview(
                    handle=handle,
                    surface=surface,
                    target=target,
                    summary=improvement_summary,
                    benchmark_results=benchmark_payload,
                ),
            }
        )
    elif resolved_kind == "policy_patch":
        artifact.update(
            {
                "proposed_path": f"data/policy_patches/{handle}.yaml",
                "policy_id": f"policy-{handle}",
                "content_preview": _policy_preview(
                    handle=handle,
                    summary=improvement_summary,
                    benchmark_results=benchmark_payload,
                ),
            }
        )
    else:
        artifact.update(
            {
                "proposed_path": None,
                "content_preview": compact_text(improvement_summary, limit=400),
            }
        )

    return artifact


__all__ = [
    "DEFAULT_PROMOTION_KIND",
    "PROMOTION_KIND_TO_MEMORY_KIND",
    "REUSE_MEMORY_KINDS",
    "VALID_PROMOTION_KINDS",
    "build_promotion_artifact",
    "normalize_promotion_kind",
    "promotion_memory_kind",
    "promotion_search_kind_filter",
]
