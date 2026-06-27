"""Thin runtime substrate facade for resources and capabilities."""

from __future__ import annotations

import inspect
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.browser.current_tab_bridge import current_tab_bridge_enabled
from src.bridges.desktop_bridge_client import get_desktop_client
from src.bridges.host_bridge_client import get_host_bridge_client
from src.config.settings import get_settings
from src.skills.base import get_skill_registry
from src.skills.runtime import ensure_skills_loaded
from src.tools.browser import PLAYWRIGHT_AVAILABLE
from src.runtime.capability_catalog import _CAPABILITY_SPECS, _HOST_BRIDGE_CAPABILITY_MAP
from src.runtime.capability_models import RuntimeCapabilitySpec
from src.runtime.capability_resources import bridge_resources, skill_resource
from src.runtime.promoted_capabilities import load_promoted_capability_specs


async def _all_capability_specs() -> dict[str, RuntimeCapabilitySpec]:
    promoted = await load_promoted_capability_specs()
    merged = dict(_CAPABILITY_SPECS)
    merged.update(promoted)
    return merged


async def _implemented_overrides(refresh: bool) -> dict[str, bool]:
    settings = get_settings()
    implemented: dict[str, bool] = {
        "skill.list": True,
        "skill.execute": True,
        "shell.run": True,
        "file.read": True,
        "file.write": True,
        "file.list": True,
        "browser.navigate": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.click": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.fill": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.press": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.extract_text": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.screenshot": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "control_ui_chat.send_message": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "current_tab.info": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.navigate": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.click": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.fill": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.extract_text": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
    }
    for name in _CAPABILITY_SPECS:
        if name.startswith("desktop."):
            implemented[name] = True

    if settings.host_bridge_enabled and refresh:
        host_names = {
            name
            for name, spec in _CAPABILITY_SPECS.items()
            if spec.bridge_capability and spec.bridge_capability.startswith("host.")
        }
        try:
            client = get_host_bridge_client()
            if client is None:
                raise RuntimeError("Host Bridge is not enabled.")
            result = await client.list_capabilities()
            for descriptor in result.capabilities:
                canonical_name = _HOST_BRIDGE_CAPABILITY_MAP.get(descriptor.name)
                if canonical_name:
                    implemented[canonical_name] = descriptor.implemented
        except Exception:
            for name in host_names:
                implemented[name] = False

    if refresh or not settings.desktop_bridge_enabled:
        desktop_names = {name for name in _CAPABILITY_SPECS if name.startswith("desktop.")}
        try:
            result = await get_desktop_client().capabilities()
            for descriptor in result.capabilities:
                if descriptor.name in desktop_names:
                    implemented[descriptor.name] = descriptor.implemented
        except Exception:
            if refresh:
                for name in desktop_names:
                    implemented[name] = False

    return implemented


async def list_runtime_resources() -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()
    resources = bridge_resources()
    for meta in sorted(
        registry.list_skills(),
        key=lambda item: (0 if item.name.startswith("promoted/") else 1, item.name),
    ):
        skill = registry.get_skill(meta.name)
        if skill is not None:
            resources.append(skill_resource(skill))
    return {"count": len(resources), "resources": resources}


async def read_runtime_resource(resource_id: str, refresh: bool = False) -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()

    if resource_id.startswith("skill:"):
        name = resource_id.split(":", 1)[1]
        skill = registry.get_skill(name)
        if skill is None:
            return {"ok": False, "message": f"Resource not found: {resource_id}"}
        payload = skill_resource(skill)
        payload["content"] = getattr(skill, "content", "")
        return {"ok": True, "resource": payload}

    capabilities = await list_runtime_capabilities(refresh=refresh)
    capability_items = capabilities["capabilities"]

    if resource_id == "bridge:host":
        settings = get_settings()
        return {
            "ok": True,
            "resource": {
                "id": resource_id,
                "kind": "bridge",
                "provider": "host",
                "title": "Host runtime",
                "description": "Guarded shell, file, browser, and Control UI surfaces backed by Host Bridge or local fallbacks.",
                "enabled": bool(settings.host_bridge_enabled),
                "transport": "host_bridge" if settings.host_bridge_enabled else "local_fallback",
                "capabilities": [
                    item
                    for item in capability_items
                    if item["provider"] in {"host", "browser"}
                ],
            },
        }

    if resource_id == "bridge:current_tab":
        return {
            "ok": True,
            "resource": {
                "id": resource_id,
                "kind": "bridge",
                "provider": "current_tab",
                "title": "Current Tab relay",
                "description": "Chrome extension relay for the currently visible tab.",
                "enabled": bool(get_settings().host_bridge_enabled and current_tab_bridge_enabled()),
                "transport": "current_tab_extension_relay",
                "capabilities": [
                    item for item in capability_items if item["provider"] == "current_tab"
                ],
            },
        }

    if resource_id == "bridge:desktop":
        return {
            "ok": True,
            "resource": {
                "id": resource_id,
                "kind": "bridge",
                "provider": "desktop",
                "title": "Desktop runtime",
                "description": "Desktop capability plane for view, accessibility, and control actions.",
                "enabled": True,
                "transport": "desktop_bridge" if get_settings().desktop_bridge_enabled else "local_runtime",
                "capabilities": [
                    item for item in capability_items if item["provider"] == "desktop"
                ],
            },
        }

    return {"ok": False, "message": f"Resource not found: {resource_id}"}


async def list_runtime_capabilities(refresh: bool = False) -> dict[str, Any]:
    await ensure_skills_loaded()
    implemented = await _implemented_overrides(refresh=refresh)
    specs = await _all_capability_specs()
    capabilities = []
    for name in sorted(
        specs,
        key=lambda item: (0 if item.startswith("promoted.") else 1, item),
    ):
        spec = specs[name]
        capabilities.append(
            {
                "name": spec.name,
                "provider": spec.provider,
                "description": spec.description,
                "risk": spec.risk,
                "requires_approval": spec.requires_approval,
                "transport": spec.transport,
                "bridge_capability": spec.bridge_capability,
                "implemented": implemented.get(spec.name, True),
            }
        )
    return {"count": len(capabilities), "refresh": refresh, "capabilities": capabilities}


async def invoke_runtime_capability(
    name: str,
    params: Optional[dict[str, Any]] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    specs = await _all_capability_specs()
    spec = specs.get(name)
    if spec is None:
        return {"success": False, "capability": name, "error": f"Unknown capability: {name}"}

    if spec.requires_approval and tool_context is None:
        return {
            "success": False,
            "capability": name,
            "error": (
                f"Capability {name} requires tool_context-backed approval flow and "
                "cannot be invoked without an ADK tool context."
            ),
        }

    kwargs = dict(params or {})
    try:
        if "tool_context" in inspect.signature(spec.invoker).parameters:
            kwargs["tool_context"] = tool_context
        result = await spec.invoker(**kwargs)
    except TypeError as exc:
        return {
            "success": False,
            "capability": name,
            "error": f"Invalid parameters for capability {name}: {exc}",
        }
    except Exception as exc:  # pragma: no cover
        return {"success": False, "capability": name, "error": str(exc)}

    success = True
    if isinstance(result, dict):
        if "success" in result:
            success = bool(result["success"])
        elif "ok" in result:
            success = bool(result["ok"])
        elif "error" in result:
            success = False

    return {
        "success": success,
        "capability": name,
        "provider": spec.provider,
        "transport": spec.transport,
        "result": result if isinstance(result, dict) else {"value": result},
    }


__all__ = [
    "RuntimeCapabilitySpec",
    "_CAPABILITY_SPECS",
    "get_desktop_client",
    "invoke_runtime_capability",
    "list_runtime_capabilities",
    "list_runtime_resources",
    "read_runtime_resource",
]
