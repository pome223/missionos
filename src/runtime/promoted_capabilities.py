"""Dynamic runtime capability registration for approved promoted artifacts."""

from __future__ import annotations

import json
from typing import Any

from src.runtime.capability_models import RuntimeCapabilitySpec
from src.tools.memory import get_memory_store
from src.tools.self_improvement_runtime.security import evaluate_promotion_deployability


def _promoted_capability_invoker(
    *,
    memory_id: int | None,
    promotion_artifact: dict[str, Any],
):
    async def _invoke(**params) -> dict[str, Any]:
        preview = str(promotion_artifact.get("content_preview") or "").strip()
        parsed_preview: dict[str, Any] | None = None
        if preview:
            try:
                parsed = json.loads(preview)
                if isinstance(parsed, dict):
                    parsed_preview = parsed
            except json.JSONDecodeError:
                parsed_preview = None
        return {
            "ok": True,
            "kind": "promoted_capability_patch",
            "memory_id": memory_id,
            "promotion_artifact": promotion_artifact,
            "preview": parsed_preview or preview,
            "params": params,
        }

    return _invoke


async def load_promoted_capability_specs(*, limit: int = 200) -> dict[str, RuntimeCapabilitySpec]:
    try:
        candidates = get_memory_store().search(
            query=None,
            kinds=["capability_patch"],
            limit=limit,
        )
    except Exception:
        return {}

    specs: dict[str, RuntimeCapabilitySpec] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        gate = evaluate_promotion_deployability(
            promotion_kind="capability_patch",
            metadata=metadata,
        )
        if not gate["deployable"]:
            continue
        artifact = gate["artifact"]
        capability_name = str(artifact.get("capability_name") or "").strip()
        if not capability_name or capability_name in specs:
            continue
        specs[capability_name] = RuntimeCapabilitySpec(
            name=capability_name,
            provider="promoted",
            description=(
                f"Approved promoted capability patch for {artifact.get('target') or 'unknown-target'} "
                f"on {artifact.get('surface') or 'unknown'}."
            ),
            risk="low",
            requires_approval=False,
            transport="runtime_promoted",
            bridge_capability=None,
            invoker=_promoted_capability_invoker(
                memory_id=item.get("id"),
                promotion_artifact=artifact,
            ),
        )
    return specs


__all__ = ["load_promoted_capability_specs"]
