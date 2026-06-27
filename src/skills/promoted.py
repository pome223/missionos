"""Runtime loading for approved promoted skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.skills.base import SkillMetadata, get_skill_registry
from src.skills.loader import MarkdownSkill
from src.tools.memory import get_memory_store
from src.tools.self_improvement_runtime.security import evaluate_promotion_deployability


class PromotedMarkdownSkill(MarkdownSkill):
    """Markdown-backed skill materialized from an approved promotion artifact."""

    def __init__(
        self,
        *,
        memory_id: int | None,
        promotion_artifact: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        self.memory_id = memory_id
        self.promotion_artifact = dict(promotion_artifact)
        super().__init__(
            skill_file=Path(promotion_artifact.get("proposed_path") or "skills/promoted/unknown/SKILL.md"),
            metadata=metadata,
            content=str(promotion_artifact.get("content_preview") or "").strip(),
        )

    def get_metadata(self) -> SkillMetadata:
        meta = super().get_metadata()
        tags = list(meta.tags or [])
        for tag in ("promoted", "approved_skill"):
            if tag not in tags:
                tags.append(tag)
        return SkillMetadata(
            name=meta.name,
            description=meta.description,
            version=meta.version,
            author=meta.author,
            tags=tags,
        )

    async def execute(self, **kwargs):
        payload = await super().execute(**kwargs)
        payload.update(
            {
                "promoted": True,
                "memory_id": self.memory_id,
                "promotion_artifact": self.promotion_artifact,
            }
        )
        return payload


def _clear_loaded_promoted_skills() -> None:
    registry = get_skill_registry()
    for name, skill in list(registry.skills.items()):
        if isinstance(skill, PromotedMarkdownSkill):
            del registry.skills[name]


async def ensure_promoted_skills_loaded(*, refresh: bool = False) -> dict[str, Any]:
    registry = get_skill_registry()
    if refresh:
        _clear_loaded_promoted_skills()
    elif any(isinstance(skill, PromotedMarkdownSkill) for skill in registry.skills.values()):
        names = sorted(
            meta.name
            for meta in registry.list_skills()
            if meta.name.startswith("promoted/")
        )
        return {"loaded": True, "count": len(names), "skills": names, "skipped": []}

    try:
        candidates = get_memory_store().search(
            query=None,
            kinds=["approved_skill"],
            limit=200,
        )
    except Exception as exc:
        return {"loaded": False, "count": 0, "skills": [], "skipped": [str(exc)]}

    loaded_names: list[str] = []
    skipped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        gate = evaluate_promotion_deployability(
            promotion_kind="approved_skill",
            metadata=metadata,
        )
        artifact = gate["artifact"]
        skill_name = str(artifact.get("skill_name") or "").strip()
        if not gate["deployable"] or not skill_name:
            skipped.append(
                {
                    "memory_id": item.get("id"),
                    "skill_name": skill_name,
                    "reasons": gate["reasons"],
                }
            )
            continue
        if skill_name in seen_names:
            continue
        seen_names.add(skill_name)
        skill = PromotedMarkdownSkill(
            memory_id=item.get("id"),
            promotion_artifact=artifact,
            metadata={
                "name": skill_name,
                "description": (
                    f"Approved promoted skill for {artifact.get('target') or 'unknown-target'} "
                    f"on {artifact.get('surface') or 'unknown'}."
                ),
                "version": "1.0.0",
                "author": "self_improvement_runtime",
                "tags": ["promoted", "approved_skill", str(artifact.get("surface") or "unknown")],
            },
        )
        registry.register(skill)
        loaded_names.append(skill_name)

    loaded_names.sort()
    return {
        "loaded": True,
        "count": len(loaded_names),
        "skills": loaded_names,
        "skipped": skipped,
    }


__all__ = ["PromotedMarkdownSkill", "ensure_promoted_skills_loaded"]
