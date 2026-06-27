"""Reuse and prompt helpers for self-improvement flows."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from google.adk.agents.context import Context as ToolContext

from src.runtime.promoted_capabilities import load_promoted_capability_specs
from src.skills.base import get_skill_registry
from src.skills.promoted import ensure_promoted_skills_loaded
from src.tools.self_improvement_runtime.common import compact_text, read_state
from src.tools.self_improvement_runtime.promotion import (
    REUSE_MEMORY_KINDS,
    promotion_search_kind_filter,
)
from src.tools.self_improvement_runtime.security import evaluate_promotion_deployability


MemorySearchFn = Callable[..., Awaitable[dict[str, Any]]]
MemoryStoreGetter = Callable[[], Any]


_KIND_PRIORITY = {
    "approved_skill": 40,
    "capability_patch": 30,
    "approved_improvement": 20,
    "policy_patch": 10,
}

_REUSE_POLICY_ALLOW_KEYS = (
    "allow_self_improvement_reuse",
    "allow_approved_improvement_reuse",
)
_REUSE_POLICY_DISABLE_KEYS = (
    "disable_self_improvement_reuse",
    "disable_approved_improvement_reuse",
)


def trajectory_failure_reason(trajectory: dict[str, Any]) -> str:
    verification = trajectory.get("verification")
    if isinstance(verification, dict) and not verification.get("success"):
        return f"verification {verification.get('status', 'failed')}"
    for attempt in trajectory.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        result = attempt.get("result")
        if isinstance(result, dict):
            error = str(result.get("error") or "").strip()
            if error:
                return error
        verification = attempt.get("verification")
        if isinstance(verification, dict) and not verification.get("success"):
            return f"verification {verification.get('status', 'failed')}"
    return "unknown failure"


def trajectory_demo_goal(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    return f"Investigate failed {action} trajectory {trajectory.get('id')} for {target}"


def trajectory_search_goal(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    return f"Search repair candidates for failed {action} trajectory {trajectory.get('id')} for {target}"


def trajectory_improvement_summary(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or "unknown-target"
    )
    return (
        f"Demo candidate for failed computer trajectory {trajectory.get('id')}: "
        f"improve {action} handling around {target} after {trajectory_failure_reason(trajectory)}."
    )


def trajectory_reuse_query(trajectory: dict[str, Any]) -> str:
    hints = trajectory_reuse_hints(trajectory)
    parts = [
        hints.get("action"),
        hints.get("selector"),
        hints.get("title"),
        hints.get("identifier"),
        hints.get("surface"),
        hints.get("failure_type"),
        hints.get("failure_reason"),
    ]
    return " ".join(str(part).strip() for part in parts if part)


def normalize_reuse_value(value: Any) -> str:
    return str(value or "").strip().lower()


def trajectory_reuse_hints(trajectory: dict[str, Any]) -> dict[str, str]:
    request = trajectory.get("request") or {}
    action = normalize_reuse_value(trajectory.get("action"))
    selector = normalize_reuse_value(request.get("selector"))
    title = normalize_reuse_value(request.get("title"))
    identifier = normalize_reuse_value(request.get("identifier"))
    value_contains = normalize_reuse_value(request.get("value_contains"))
    surface = normalize_reuse_value(trajectory.get("final_surface"))
    failure_type = normalize_reuse_value(
        trajectory.get("normalized_failure_type")
        or trajectory.get("failure_type")
        or trajectory.get("preliminary_failure_type")
    )
    failure_reason = normalize_reuse_value(trajectory_failure_reason(trajectory))
    target = selector or title or identifier or value_contains or surface or "unknown-target"
    trajectory_key = "::".join(part for part in [action, surface, target] if part)
    return {
        "trajectory_key": trajectory_key,
        "action": action,
        "selector": selector,
        "title": title,
        "identifier": identifier,
        "value_contains": value_contains,
        "surface": surface,
        "failure_type": failure_type,
        "failure_reason": failure_reason,
        "target": target,
    }


def approved_reuse_policy(trajectory: dict[str, Any]) -> dict[str, Any]:
    request = trajectory.get("request")
    request = request if isinstance(request, dict) else {}
    observation = trajectory.get("observation")
    observation = observation if isinstance(observation, dict) else {}
    candidates = [
        ("request.policy", request.get("policy")),
        ("observation.policy", observation.get("policy")),
        ("trajectory.policy", trajectory.get("policy")),
    ]
    for source, candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in _REUSE_POLICY_ALLOW_KEYS:
            if key in candidate:
                enabled = bool(candidate.get(key))
                return {
                    "enabled": enabled,
                    "source": f"{source}.{key}",
                    "reason": "policy_disabled" if not enabled else "policy_enabled",
                }
        for key in _REUSE_POLICY_DISABLE_KEYS:
            if key in candidate:
                disabled = bool(candidate.get(key))
                return {
                    "enabled": not disabled,
                    "source": f"{source}.{key}",
                    "reason": "policy_disabled" if disabled else "policy_enabled",
                }
    return {
        "enabled": True,
        "source": "default",
        "reason": "policy_enabled",
    }


def state_reuse_hints(canary) -> dict[str, str]:
    state = read_state(canary)
    for key in ("search", "demo"):
        candidate = state.get(key)
        if not isinstance(candidate, dict):
            continue
        reuse_hints = candidate.get("reuse_hints")
        if isinstance(reuse_hints, dict):
            return {str(name): normalize_reuse_value(value) for name, value in reuse_hints.items()}
    return {}


def cheap_reuse_match_score(
    hints: dict[str, str],
    metadata: dict[str, Any],
) -> int:
    score = 0
    metadata_key = normalize_reuse_value(metadata.get("trajectory_key"))
    if metadata_key and metadata_key == hints.get("trajectory_key"):
        score += 10
    for field, weight in {
        "selector": 5,
        "identifier": 4,
        "title": 3,
        "value_contains": 2,
        "action": 3,
        "surface": 2,
    }.items():
        hint_value = hints.get(field)
        metadata_value = normalize_reuse_value(metadata.get(field))
        if hint_value and metadata_value and hint_value == metadata_value:
            score += weight
    if hints.get("failure_type") and normalize_reuse_value(metadata.get("failure_type")) == hints.get("failure_type"):
        score += 6
    if hints.get("failure_reason") and normalize_reuse_value(metadata.get("failure_reason")) == hints.get("failure_reason"):
        score += 1
    return score


def reuse_memory_ids(results: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        raw = item.get("memory_id")
        if raw is None:
            continue
        try:
            memory_id = int(raw)
        except (TypeError, ValueError):
            continue
        if memory_id in seen:
            continue
        seen.add(memory_id)
        ids.append(memory_id)
    return ids


def build_reuse_trace(
    trajectory: dict[str, Any],
    reuse: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    results = reuse.get("results") if isinstance(reuse, dict) else []
    results = results if isinstance(results, list) else []
    hints = trajectory_reuse_hints(trajectory)
    policy = reuse.get("policy") if isinstance(reuse, dict) else None
    policy = policy if isinstance(policy, dict) else approved_reuse_policy(trajectory)
    return {
        "source": source,
        "query": str(reuse.get("query") or trajectory_reuse_query(trajectory)),
        "policy": policy,
        "failure_type": hints.get("failure_type") or "",
        "memory_ids": reuse_memory_ids(results),
        "used_memory_ids": reuse_memory_ids(results),
        "match_types": [
            str(item.get("match_type") or "").strip()
            for item in results
            if isinstance(item, dict) and str(item.get("match_type") or "").strip()
        ],
    }


def prefilter_reuse_suggestions(
    trajectory: dict[str, Any],
    *,
    limit: int,
    get_memory_store_fn: MemoryStoreGetter,
) -> list[dict[str, Any]]:
    hints = trajectory_reuse_hints(trajectory)
    try:
        candidates = get_memory_store_fn().search(
            query=None,
            kinds=list(REUSE_MEMORY_KINDS),
            limit=max(limit * 10, 50),
        )
    except Exception:
        return []

    matches: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        score = cheap_reuse_match_score(hints, metadata)
        if score <= 0:
            continue
        matches.append(
            {
                "memory_id": item.get("id"),
                "content": item.get("content"),
                "kind": item.get("kind"),
                "score": float(score),
                "created_at": item.get("created_at"),
                "tags": item.get("tags") or [],
                "metadata": metadata,
                "match_type": "prefilter",
            }
        )

    matches.sort(
        key=lambda item: (float(item.get("score") or 0.0), float(item.get("created_at") or 0.0)),
        reverse=True,
    )
    return matches[:limit]


def prefilter_reuse_payload(
    trajectory: dict[str, Any],
    *,
    limit: int,
    get_memory_store_fn: MemoryStoreGetter,
) -> dict[str, Any]:
    policy = approved_reuse_policy(trajectory)
    query = trajectory_reuse_query(trajectory)
    if not policy.get("enabled"):
        return {
            "query": query,
            "results": [],
            "memory_ids": [],
            "policy": policy,
        }
    results = prefilter_reuse_suggestions(
        trajectory,
        limit=limit,
        get_memory_store_fn=get_memory_store_fn,
    )
    return {
        "query": query,
        "results": results,
        "memory_ids": reuse_memory_ids(results),
        "policy": policy,
    }


async def find_reuse_suggestions(
    trajectory: dict[str, Any],
    *,
    get_memory_store_fn: MemoryStoreGetter,
    memory_search_fn: MemorySearchFn,
    limit: int = 3,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    base = prefilter_reuse_payload(
        trajectory,
        limit=max(1, min(limit, 10)),
        get_memory_store_fn=get_memory_store_fn,
    )
    query = str(base.get("query") or "")
    if not query:
        return {"query": "", "results": [], "memory_ids": [], "policy": base.get("policy", {})}

    if not base.get("policy", {}).get("enabled", True):
        return base

    bounded_limit = max(1, min(limit, 10))
    prefiltered = list(base.get("results") or [])
    if len(prefiltered) >= bounded_limit:
        enriched = await _enrich_reuse_results(prefiltered)
        return {
            "query": query,
            "results": enriched,
            "memory_ids": reuse_memory_ids(enriched),
            "policy": base.get("policy", {}),
        }

    results_by_id: dict[Any, dict[str, Any]] = {
        item.get("memory_id"): item for item in prefiltered if item.get("memory_id") is not None
    }
    search_error: str | None = None
    try:
        search = await memory_search_fn(
            query=query,
            kind=promotion_search_kind_filter(),
            limit=bounded_limit,
            tool_context=tool_context,
        )
    except Exception as exc:
        search = {"success": False, "error": str(exc)}

    if not search.get("success"):
        search_error = search.get("error") or "failed to search approved improvements"
    else:
        for item in search.get("results") or []:
            if not isinstance(item, dict):
                continue
            payload = {
                "memory_id": item.get("id"),
                "content": item.get("content"),
                "kind": item.get("kind"),
                "score": item.get("score"),
                "created_at": item.get("created_at"),
                "tags": item.get("tags") or [],
                "metadata": item.get("metadata") or {},
                "match_type": "semantic",
            }
            memory_id = payload.get("memory_id")
            if memory_id in results_by_id:
                continue
            results_by_id[memory_id] = payload

    if not results_by_id:
        payload = {
            "query": query,
            "results": [],
            "memory_ids": [],
            "policy": base.get("policy", {}),
        }
        if search_error:
            payload["error"] = search_error
        return payload

    results = await _enrich_reuse_results(list(results_by_id.values()))
    final_results = results[:bounded_limit]
    payload = {
        "query": query,
        "results": final_results,
        "memory_ids": reuse_memory_ids(final_results),
        "policy": base.get("policy", {}),
    }
    if search_error:
        payload["error"] = search_error
    return payload


def _kind_priority(kind: str) -> int:
    return _KIND_PRIORITY.get(str(kind or "").strip(), 0)


async def _enrich_reuse_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    await ensure_promoted_skills_loaded(refresh=True)
    registry = get_skill_registry()
    promoted_capability_specs = await load_promoted_capability_specs()
    enriched: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        payload = dict(item)
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        gate = evaluate_promotion_deployability(
            promotion_kind=payload.get("kind"),
            metadata=metadata,
        )
        artifact = gate["artifact"]
        runtime_registration: dict[str, Any] | None = None
        if payload.get("kind") == "approved_skill":
            skill_name = str(artifact.get("skill_name") or "").strip()
            runtime_registration = {
                "kind": "skill",
                "name": skill_name,
                "registered": bool(skill_name and registry.get_skill(skill_name)),
                "deployable": gate["deployable"],
            }
        elif payload.get("kind") == "capability_patch":
            capability_name = str(artifact.get("capability_name") or "").strip()
            runtime_registration = {
                "kind": "capability",
                "name": capability_name,
                "registered": capability_name in promoted_capability_specs,
                "deployable": gate["deployable"],
            }
        payload["promotion_artifact"] = artifact
        payload["deployment_gate"] = gate
        if runtime_registration is not None:
            payload["runtime_registration"] = runtime_registration
        enriched.append(payload)

    enriched.sort(
        key=lambda item: (
            1
            if (
                isinstance(item.get("runtime_registration"), dict)
                and item["runtime_registration"].get("registered")
                and isinstance(item.get("deployment_gate"), dict)
                and item["deployment_gate"].get("deployable")
            )
            else 0,
            _kind_priority(str(item.get("kind") or "")),
            float(item.get("score") or 0.0),
            float(item.get("created_at") or 0.0),
        ),
        reverse=True,
    )
    return enriched


def preferred_runtime_reuse(reuse: dict[str, Any]) -> dict[str, Any] | None:
    results = reuse.get("results") if isinstance(reuse, dict) else None
    if not isinstance(results, list):
        return None
    for item in results:
        if not isinstance(item, dict):
            continue
        runtime_registration = item.get("runtime_registration")
        gate = item.get("deployment_gate")
        if (
            isinstance(runtime_registration, dict)
            and runtime_registration.get("registered")
            and isinstance(gate, dict)
            and gate.get("deployable")
        ):
            return item
    return None


def reuse_guidance_lines(reuse: dict[str, Any], *, limit: int = 3) -> list[str]:
    results = reuse.get("results") if isinstance(reuse, dict) else None
    if not isinstance(results, list):
        return []

    guidance: list[str] = []
    for item in results[: max(1, min(limit, 5))]:
        if not isinstance(item, dict):
            continue
        runtime_registration = item.get("runtime_registration")
        if (
            isinstance(runtime_registration, dict)
            and runtime_registration.get("registered")
            and isinstance(item.get("deployment_gate"), dict)
            and item["deployment_gate"].get("deployable")
        ):
            name = runtime_registration.get("name") or "unknown"
            if runtime_registration.get("kind") == "skill":
                guidance.append(f"- Reuse registered approved skill {name} before inventing a new fix.")
                continue
            if runtime_registration.get("kind") == "capability":
                guidance.append(f"- Reuse registered promoted capability {name} before inventing a new fix.")
                continue
        content = compact_text(str(item.get("content") or ""), limit=160)
        if not content:
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        qualifiers = []
        if item.get("kind"):
            qualifiers.append(f"kind={item['kind']}")
        if metadata.get("trajectory_key"):
            qualifiers.append(str(metadata["trajectory_key"]))
        elif metadata.get("selector"):
            qualifiers.append(f"selector={metadata['selector']}")
        if metadata.get("surface"):
            qualifiers.append(f"surface={metadata['surface']}")
        if item.get("memory_id") is not None:
            qualifiers.append(f"memory={item['memory_id']}")
        qualifier_text = f" ({'; '.join(qualifiers)})" if qualifiers else ""
        guidance.append(f"- {content}{qualifier_text}")
    return guidance


def reuse_guidance_text(reuse: dict[str, Any], *, limit: int = 3) -> str:
    lines = reuse_guidance_lines(reuse, limit=limit)
    if not lines:
        return ""
    return "\n".join(
        [
            "Approved improvement reuse hints:",
            *lines,
            "Prefer adapting these approved improvements before inventing a new fix.",
        ]
    )


def improvement_summary_with_reuse(base_summary: str, reuse: dict[str, Any]) -> str:
    guidance = reuse_guidance_text(reuse)
    if not guidance:
        return base_summary
    return f"{base_summary}\n\n{guidance}"


def build_repair_prompt(
    *,
    goal: str,
    improvement_summary: str,
    trajectory: dict[str, Any],
    reuse: dict[str, Any],
) -> str:
    request = trajectory.get("request") or {}
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    surface = str(trajectory.get("final_surface") or "unknown")
    lines = [
        goal,
        "",
        f"Failure reason: {trajectory_failure_reason(trajectory)}",
        f"Target: {target}",
        f"Surface: {surface}",
        "",
        f"Improvement summary: {improvement_summary}",
    ]
    guidance = reuse_guidance_text(reuse)
    if guidance:
        lines.extend(["", guidance])
    preferred = preferred_runtime_reuse(reuse)
    if isinstance(preferred, dict):
        runtime_registration = preferred.get("runtime_registration")
        artifact = preferred.get("promotion_artifact")
        if isinstance(runtime_registration, dict) and isinstance(artifact, dict):
            content_preview = str(artifact.get("content_preview") or "").strip()
            if content_preview:
                lines.extend(
                    [
                        "",
                        "Registered approved reuse content:",
                        f"- mode: {runtime_registration.get('kind')}",
                        f"- name: {runtime_registration.get('name')}",
                        "",
                        content_preview,
                    ]
                )
    return "\n".join(lines)


__all__ = [
    "approved_reuse_policy",
    "build_reuse_trace",
    "build_repair_prompt",
    "find_reuse_suggestions",
    "improvement_summary_with_reuse",
    "prefilter_reuse_payload",
    "prefilter_reuse_suggestions",
    "preferred_runtime_reuse",
    "reuse_memory_ids",
    "state_reuse_hints",
    "trajectory_demo_goal",
    "trajectory_failure_reason",
    "trajectory_improvement_summary",
    "trajectory_reuse_hints",
    "trajectory_reuse_query",
    "trajectory_search_goal",
]
