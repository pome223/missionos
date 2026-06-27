"""Helpers for runtime capability resources."""

from __future__ import annotations

from typing import Any

from src.browser.current_tab_bridge import current_tab_bridge_enabled
from src.config.settings import get_settings
from src.skills.base import BaseSkill


def skill_resource(skill: BaseSkill) -> dict[str, Any]:
    meta = skill.get_metadata()
    skill_file = getattr(skill, "skill_file", None)
    return {
        "id": f"skill:{meta.name}",
        "kind": "skill",
        "provider": "skills",
        "title": meta.name,
        "description": meta.description,
        "version": meta.version,
        "author": meta.author,
        "tags": meta.tags,
        **({"path": str(skill_file)} if skill_file else {}),
    }


def bridge_resources() -> list[dict[str, Any]]:
    settings = get_settings()
    return [
        {
            "id": "bridge:host",
            "kind": "bridge",
            "provider": "host",
            "title": "Host runtime",
            "description": "Guarded shell, file, browser, and Control UI surfaces backed by Host Bridge or local fallbacks.",
            "enabled": bool(settings.host_bridge_enabled),
        },
        {
            "id": "bridge:current_tab",
            "kind": "bridge",
            "provider": "current_tab",
            "title": "Current Tab relay",
            "description": "Chrome extension relay for the currently visible tab.",
            "enabled": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        },
        {
            "id": "bridge:desktop",
            "kind": "bridge",
            "provider": "desktop",
            "title": "Desktop runtime",
            "description": "Desktop capability plane for view, accessibility, and control actions.",
            "enabled": True,
        },
    ]


__all__ = ["bridge_resources", "skill_resource"]
