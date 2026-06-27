"""Promotion deployability checks for typed self-improvement artifacts."""

from __future__ import annotations

from typing import Any

from src.tools.self_improvement_runtime.promotion import (
    DEFAULT_PROMOTION_KIND,
    normalize_promotion_kind,
)

_MEMORY_KIND_TO_PROMOTION_KIND = {
    "approved_improvement": DEFAULT_PROMOTION_KIND,
    "approved_skill": "approved_skill",
    "capability_patch": "capability_patch",
    "policy_patch": "policy_patch",
}


def promotion_artifact_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = metadata if isinstance(metadata, dict) else {}
    artifact = payload.get("promotion_artifact")
    return artifact if isinstance(artifact, dict) else {}


def evaluate_promotion_deployability(
    *,
    promotion_kind: str | None = None,
    artifact: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(artifact or promotion_artifact_from_metadata(metadata))
    normalized_kind = str(promotion_kind or payload.get("artifact_kind") or DEFAULT_PROMOTION_KIND).strip()
    normalized_kind = _MEMORY_KIND_TO_PROMOTION_KIND.get(normalized_kind, normalized_kind)
    resolved_kind = normalize_promotion_kind(
        normalized_kind
    )
    approval_dependencies = [
        str(item).strip()
        for item in (
            payload.get("approval_dependencies")
            if isinstance(payload.get("approval_dependencies"), list)
            else (metadata or {}).get("approval_dependencies") or []
        )
        if str(item).strip()
    ]
    approval_required = bool(payload.get("approval_required")) or resolved_kind != DEFAULT_PROMOTION_KIND
    approval_status = str(payload.get("approval_status") or "").strip() or (
        "linked" if approval_dependencies else "pending" if approval_required else "not_required"
    )
    benchmark_step_count = int(payload.get("benchmark_step_count") or 0)

    checks = {
        "artifact_present": bool(payload),
        "approval_dependency_present": bool(approval_dependencies) or not approval_required,
        "approval_status_linked": approval_status == "linked" or not approval_required,
        "benchmarked": benchmark_step_count > 0 or resolved_kind == DEFAULT_PROMOTION_KIND,
    }
    if resolved_kind == "approved_skill":
        checks["content_present"] = bool(payload.get("content_preview")) and bool(payload.get("skill_name"))
    elif resolved_kind == "capability_patch":
        checks["content_present"] = bool(payload.get("content_preview")) and bool(payload.get("capability_name"))
    elif resolved_kind == "policy_patch":
        checks["content_present"] = bool(payload.get("content_preview")) and bool(payload.get("policy_id"))
    else:
        checks["content_present"] = True

    reasons: list[str] = []
    if not checks["artifact_present"]:
        reasons.append("missing promotion artifact metadata")
    if approval_required and not checks["approval_dependency_present"]:
        reasons.append(
            f"promotion kind {resolved_kind} requires at least one approval dependency before it can be deployed"
        )
    if approval_required and not checks["approval_status_linked"]:
        reasons.append(
            f"promotion kind {resolved_kind} requires approval_status=linked before it can be deployed"
        )
    if not checks["benchmarked"]:
        reasons.append(f"promotion kind {resolved_kind} requires benchmark evidence before it can be deployed")
    if not checks["content_present"]:
        reasons.append(f"promotion kind {resolved_kind} is missing deployable content")

    return {
        "promotion_kind": resolved_kind,
        "approval_required": approval_required,
        "approval_status": approval_status,
        "approval_dependencies": approval_dependencies,
        "deployable": not reasons,
        "reasons": reasons,
        "checks": checks,
        "artifact": payload,
    }


__all__ = [
    "evaluate_promotion_deployability",
    "promotion_artifact_from_metadata",
]
