"""Capability spec catalog grouped by provider."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.bridges.host_bridge_client import get_host_bridge_client
from src.bridges.host_bridge_schema import HostFileListRequest
from src.config.settings import get_settings
from src.security.policy import get_security_policy
from src.skills.runtime import ensure_skills_loaded
from src.skills.base import get_skill_registry
from src.tools.browser import (
    browser_click,
    browser_extract_text,
    browser_fill,
    browser_navigate,
    browser_press,
    browser_screenshot,
)
from src.tools.context import resolve_tool_context
from src.tools.control_ui_chat import control_ui_chat_send_message
from src.tools.current_tab import (
    current_tab_click,
    current_tab_extract_text,
    current_tab_fill,
    current_tab_info,
    current_tab_navigate,
)
from src.tools.desktop import (
    desktop_ax_find,
    desktop_ax_snapshot,
    desktop_control_click,
    desktop_control_drag,
    desktop_control_focus_window,
    desktop_control_hotkey,
    desktop_control_launch_app,
    desktop_control_scroll,
    desktop_control_type,
    desktop_runtime_clear_stop,
    desktop_runtime_status,
    desktop_runtime_stop,
    desktop_wait_element,
    desktop_wait_window,
    desktop_view_frontmost_app,
    desktop_view_screenshot,
    desktop_view_windows,
)
from src.tools.file_manager import read_file, write_file
from src.tools.shell import run_shell_guarded
from src.runtime.capability_models import RuntimeCapabilitySpec


def _runtime_context(tool_context: Optional[ToolContext]) -> dict[str, str]:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    return {
        "session_id": ctx.get("session_id") or "runtime-session",
        "user_id": ctx.get("user_id") or "runtime-user",
        "agent_name": ctx.get("agent_name") or "runtime_registry",
    }


async def _skill_list_capability() -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()
    items = []
    for meta in sorted(
        registry.list_skills(),
        key=lambda item: (0 if item.name.startswith("promoted/") else 1, item.name),
    ):
        items.append(
            {
                "name": meta.name,
                "description": meta.description,
                "version": meta.version,
                "author": meta.author,
                "tags": meta.tags,
            }
        )
    return {"count": len(items), "skills": items}


async def _skill_execute_capability(
    name: str,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()
    skill = registry.get_skill(name)
    if not skill:
        return {"ok": False, "message": f"Skill not found: {name}"}

    payload = params or {}
    if not isinstance(payload, dict):
        return {"ok": False, "message": "params must decode to object"}

    is_valid, reason = await skill.validate_input(**payload)
    if not is_valid:
        return {"ok": False, "message": reason or "Invalid input"}

    result = await skill.execute(**payload)
    return {"ok": True, "skill": name, "result": result}


async def _file_list_capability(
    path: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    settings = get_settings()
    if settings.host_bridge_enabled:
        client = get_host_bridge_client()
        if client is None:
            return {"success": False, "path": path, "error": "Host Bridge is not enabled."}

        ctx = _runtime_context(tool_context)
        request = HostFileListRequest(
            request_id=f"runtime-file-list-{uuid.uuid4().hex[:12]}",
            session_id=ctx["session_id"],
            user_id=ctx["user_id"],
            agent_name=ctx["agent_name"],
            path=path,
        )
        try:
            result = await client.list_files(request)
        except Exception as exc:  # pragma: no cover
            return {"success": False, "path": path, "error": str(exc)}

        return {
            "success": result.ok,
            "path": result.path or path,
            "entries": [entry.model_dump() for entry in result.entries],
            "count": len(result.entries),
            **({"error": result.error} if result.error else {}),
        }

    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(path, "read")
    if not allowed:
        return {"success": False, "path": path, "error": f"Access denied: {reason}"}

    try:
        dir_path = Path(path).expanduser().resolve()
        entries = []
        for entry in sorted(dir_path.iterdir(), key=lambda item: item.name.lower()):
            try:
                stat = entry.stat()
                size = stat.st_size if entry.is_file() else 0
            except OSError:
                size = 0
            entries.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "size": size,
                }
            )
        return {
            "success": True,
            "path": str(dir_path),
            "entries": entries,
            "count": len(entries),
        }
    except FileNotFoundError:
        return {"success": False, "path": path, "error": f"Directory not found: {path}"}
    except NotADirectoryError:
        return {"success": False, "path": path, "error": f"Not a directory: {path}"}
    except PermissionError:
        return {"success": False, "path": path, "error": f"Permission denied: {path}"}
    except Exception as exc:  # pragma: no cover
        return {"success": False, "path": path, "error": str(exc)}


def _skill_specs() -> list[RuntimeCapabilitySpec]:
    return [
        RuntimeCapabilitySpec(
            name="skill.list",
            provider="skills",
            description="List loaded repository skills.",
            risk="low",
            requires_approval=False,
            transport="runtime",
            bridge_capability=None,
            invoker=_skill_list_capability,
        ),
        RuntimeCapabilitySpec(
            name="skill.execute",
            provider="skills",
            description="Inspect or execute a loaded repository skill.",
            risk="low",
            requires_approval=False,
            transport="runtime",
            bridge_capability=None,
            invoker=_skill_execute_capability,
        ),
    ]


def _host_specs() -> list[RuntimeCapabilitySpec]:
    return [
        RuntimeCapabilitySpec(
            name="shell.run",
            provider="host",
            description="Run a guarded shell command through the configured host runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.shell.run",
            invoker=run_shell_guarded,
        ),
        RuntimeCapabilitySpec(
            name="file.read",
            provider="host",
            description="Read a guarded file through the configured host runtime.",
            risk="low",
            requires_approval=False,
            transport="host_bridge_or_local",
            bridge_capability="host.file.read",
            invoker=read_file,
        ),
        RuntimeCapabilitySpec(
            name="file.write",
            provider="host",
            description="Write a guarded file through the configured host runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.file.write",
            invoker=write_file,
        ),
        RuntimeCapabilitySpec(
            name="file.list",
            provider="host",
            description="List a guarded directory through the configured host runtime.",
            risk="low",
            requires_approval=False,
            transport="host_bridge_or_local",
            bridge_capability="host.file.list",
            invoker=_file_list_capability,
        ),
    ]


def _browser_specs() -> list[RuntimeCapabilitySpec]:
    return [
        RuntimeCapabilitySpec(
            name="browser.navigate",
            provider="browser",
            description="Navigate a browser page through the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.navigate",
            invoker=browser_navigate,
        ),
        RuntimeCapabilitySpec(
            name="browser.click",
            provider="browser",
            description="Click a selector in the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.click",
            invoker=browser_click,
        ),
        RuntimeCapabilitySpec(
            name="browser.fill",
            provider="browser",
            description="Fill a selector in the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.fill",
            invoker=browser_fill,
        ),
        RuntimeCapabilitySpec(
            name="browser.press",
            provider="browser",
            description="Press a key in the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.press",
            invoker=browser_press,
        ),
        RuntimeCapabilitySpec(
            name="browser.extract_text",
            provider="browser",
            description="Extract text from the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.extract_text",
            invoker=browser_extract_text,
        ),
        RuntimeCapabilitySpec(
            name="browser.screenshot",
            provider="browser",
            description="Capture a browser screenshot through the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.screenshot",
            invoker=browser_screenshot,
        ),
        RuntimeCapabilitySpec(
            name="control_ui_chat.send_message",
            provider="browser",
            description="Send a message through the boiled-claw Control UI chat runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.control_ui_chat.send_message",
            invoker=control_ui_chat_send_message,
        ),
    ]


def _current_tab_specs() -> list[RuntimeCapabilitySpec]:
    return [
        RuntimeCapabilitySpec(
            name="current_tab.info",
            provider="current_tab",
            description="Inspect the active Chrome tab through the current-tab relay.",
            risk="low",
            requires_approval=False,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.info",
            invoker=current_tab_info,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.navigate",
            provider="current_tab",
            description="Navigate the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.navigate",
            invoker=current_tab_navigate,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.click",
            provider="current_tab",
            description="Click a selector inside the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.click",
            invoker=current_tab_click,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.fill",
            provider="current_tab",
            description="Fill a selector inside the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.fill",
            invoker=current_tab_fill,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.extract_text",
            provider="current_tab",
            description="Extract text from the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.extract_text",
            invoker=current_tab_extract_text,
        ),
    ]


def _desktop_specs() -> list[RuntimeCapabilitySpec]:
    return [
        RuntimeCapabilitySpec(
            name="desktop.view.windows",
            provider="desktop",
            description="List visible windows from the desktop runtime.",
            risk="low",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.view.windows",
            invoker=desktop_view_windows,
        ),
        RuntimeCapabilitySpec(
            name="desktop.wait.window",
            provider="desktop",
            description="Wait for a matching window in the desktop runtime.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.wait.window",
            invoker=desktop_wait_window,
        ),
        RuntimeCapabilitySpec(
            name="desktop.view.frontmost_app",
            provider="desktop",
            description="Inspect the frontmost app in the desktop runtime.",
            risk="low",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.view.frontmost_app",
            invoker=desktop_view_frontmost_app,
        ),
        RuntimeCapabilitySpec(
            name="desktop.view.screenshot",
            provider="desktop",
            description="Capture a screenshot from the desktop runtime.",
            risk="medium",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.view.screenshot",
            invoker=desktop_view_screenshot,
        ),
        RuntimeCapabilitySpec(
            name="desktop.ax.find",
            provider="desktop",
            description="Resolve a matching accessibility element in the desktop runtime.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.ax.find",
            invoker=desktop_ax_find,
        ),
        RuntimeCapabilitySpec(
            name="desktop.wait.element",
            provider="desktop",
            description="Wait for a matching accessibility element in the desktop runtime.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.wait.element",
            invoker=desktop_wait_element,
        ),
        RuntimeCapabilitySpec(
            name="desktop.ax.snapshot",
            provider="desktop",
            description="Capture an accessibility snapshot from the desktop runtime.",
            risk="medium",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.ax.snapshot",
            invoker=desktop_ax_snapshot,
        ),
        RuntimeCapabilitySpec(
            name="desktop.runtime.status",
            provider="desktop",
            description="Inspect desktop emergency-stop state.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.runtime.status",
            invoker=desktop_runtime_status,
        ),
        RuntimeCapabilitySpec(
            name="desktop.runtime.stop",
            provider="desktop",
            description="Trigger desktop emergency stop.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.runtime.stop",
            invoker=desktop_runtime_stop,
        ),
        RuntimeCapabilitySpec(
            name="desktop.runtime.clear_stop",
            provider="desktop",
            description="Clear desktop emergency stop and re-enable control.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.runtime.clear_stop",
            invoker=desktop_runtime_clear_stop,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.click",
            provider="desktop",
            description="Click on the desktop or a matched accessibility element.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.click",
            invoker=desktop_control_click,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.type",
            provider="desktop",
            description="Type into the desktop or a matched accessibility element.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.type",
            invoker=desktop_control_type,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.launch_app",
            provider="desktop",
            description="Launch an app through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.launch_app",
            invoker=desktop_control_launch_app,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.focus_window",
            provider="desktop",
            description="Focus a window through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.focus_window",
            invoker=desktop_control_focus_window,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.hotkey",
            provider="desktop",
            description="Send a hotkey through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.hotkey",
            invoker=desktop_control_hotkey,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.scroll",
            provider="desktop",
            description="Scroll through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.scroll",
            invoker=desktop_control_scroll,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.drag",
            provider="desktop",
            description="Drag the pointer through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.drag",
            invoker=desktop_control_drag,
        ),
    ]


def build_capability_specs() -> dict[str, RuntimeCapabilitySpec]:
    specs = [
        *_skill_specs(),
        *_host_specs(),
        *_browser_specs(),
        *_current_tab_specs(),
        *_desktop_specs(),
    ]
    return {spec.name: spec for spec in specs}


_CAPABILITY_SPECS = build_capability_specs()

_HOST_BRIDGE_CAPABILITY_MAP = {
    "host.shell.run": "shell.run",
    "host.file.read": "file.read",
    "host.file.write": "file.write",
    "host.file.list": "file.list",
    "host.browser.navigate": "browser.navigate",
    "host.browser.click": "browser.click",
    "host.browser.fill": "browser.fill",
    "host.browser.press": "browser.press",
    "host.browser.extract_text": "browser.extract_text",
    "host.browser.screenshot": "browser.screenshot",
    "host.control_ui_chat.send_message": "control_ui_chat.send_message",
    "host.current_tab.info": "current_tab.info",
    "host.current_tab.navigate": "current_tab.navigate",
    "host.current_tab.click": "current_tab.click",
    "host.current_tab.fill": "current_tab.fill",
    "host.current_tab.extract_text": "current_tab.extract_text",
}


__all__ = ["_CAPABILITY_SPECS", "_HOST_BRIDGE_CAPABILITY_MAP", "build_capability_specs"]
