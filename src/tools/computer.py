"""Browser-first computer-use tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.computer_use.trajectory_store import get_computer_trajectory_store
from src.tools.browser import browser_click, browser_extract_text, browser_fill
from src.tools.current_tab import (
    current_tab_click,
    current_tab_extract_text,
    current_tab_fill,
    current_tab_info,
)
from src.tools.desktop import (
    desktop_ax_find,
    desktop_ax_snapshot,
    desktop_control_click,
    desktop_control_type,
    desktop_view_frontmost_app,
    desktop_view_screenshot,
    desktop_view_windows,
)


def _tool_error(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = str(payload.get("error") or "").strip()
    if error:
        return error
    if payload.get("success") is False or payload.get("ok") is False:
        return "tool reported failure"
    return None


def _is_success(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if _tool_error(payload):
        return False
    if "success" in payload:
        return bool(payload.get("success"))
    if "ok" in payload:
        return bool(payload.get("ok"))
    return True


def _has_desktop_target(
    *,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
) -> bool:
    return any((window_id, role, title, identifier, value_contains))


def _observation_summary(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "preferred_surface": observation.get("preferred_surface"),
        "available_surfaces": observation.get("available_surfaces", []),
        **({"errors": observation.get("errors", {})} if observation.get("errors") else {}),
    }


def _action_payload(
    *,
    action: str,
    surface: str | None,
    strategy: str,
    observation: dict[str, Any],
    result: dict[str, Any],
    attempts: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
    trajectory_id: int | None = None,
) -> dict[str, Any]:
    success = _is_success(result)
    if verification is not None:
        success = success and bool(verification.get("success"))
    payload = {
        "action": action,
        "surface": surface,
        "strategy": strategy,
        "observation": _observation_summary(observation),
        "result": result,
        "success": success,
    }
    if attempts is not None:
        payload["attempts"] = attempts
        payload["recovered"] = bool(payload["success"] and len(attempts) > 1)
    if verification is not None:
        payload["verification"] = verification
    if trajectory_id is not None:
        payload["trajectory_id"] = trajectory_id
    error = _tool_error(result)
    if error:
        payload["error"] = error
    elif verification is not None and not verification.get("success"):
        payload["error"] = f"verification {verification.get('status', 'failed')}"
    return payload


def _normalize_observation_input(
    observed_available_surfaces: list[str] | None,
    observed_preferred_surface: Optional[str],
) -> dict[str, Any] | None:
    if observed_available_surfaces is None and observed_preferred_surface is None:
        return None
    return {
        "available_surfaces": list(observed_available_surfaces or []),
        **(
            {"preferred_surface": observed_preferred_surface}
            if observed_preferred_surface is not None
            else {}
        ),
    }


def _expected_verification(
    *,
    text_contains: Optional[str] = None,
    text_not_contains: Optional[str] = None,
    url_contains: Optional[str] = None,
    frontmost_app: Optional[str] = None,
    window_title_contains: Optional[str] = None,
) -> dict[str, str]:
    expected = {
        "text_contains": text_contains or "",
        "text_not_contains": text_not_contains or "",
        "url_contains": url_contains or "",
        "frontmost_app": frontmost_app or "",
        "window_title_contains": window_title_contains or "",
    }
    return {key: value for key, value in expected.items() if value}


def _contains_casefold(value: Any, expected: str) -> bool:
    if not expected:
        return True
    return expected.casefold() in str(value or "").casefold()


def _candidate_strategies(
    *,
    selector: Optional[str],
    has_desktop_target: bool,
    allow_managed_browser: bool,
    available_surfaces: list[str],
) -> list[tuple[str, str]]:
    strategies: list[tuple[str, str]] = []
    if selector and "current_tab" in available_surfaces:
        strategies.append(("current_tab", "current_tab_selector"))
    if has_desktop_target and "desktop" in available_surfaces:
        strategies.append(("desktop", "desktop_selector"))
    if selector and allow_managed_browser:
        strategies.append(("browser", "managed_browser_selector"))
    return strategies


ActionExecutor = Callable[[str], Awaitable[dict[str, Any]]]


async def _verification_snapshot(
    *,
    surface: Optional[str],
    selector: Optional[str],
    needs_text: bool,
    needs_url: bool,
    needs_frontmost_app: bool,
    needs_window_title: bool,
    tool_context: Optional[ToolContext],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"surface": surface}

    if needs_url or surface == "current_tab":
        current_tab = await current_tab_info(tool_context=tool_context)
        snapshot["current_tab"] = current_tab

    if needs_text:
        if surface == "browser":
            browser_text = await browser_extract_text(
                selector=selector,
                tool_context=tool_context,
            )
            snapshot["browser_text"] = browser_text
        else:
            current_tab_text = await current_tab_extract_text(
                selector=selector,
                tool_context=tool_context,
            )
            snapshot["current_tab_text"] = current_tab_text

    if needs_frontmost_app:
        snapshot["frontmost_app"] = await desktop_view_frontmost_app(tool_context=tool_context)

    if needs_window_title:
        snapshot["windows"] = await desktop_view_windows(
            include_minimized=False,
            tool_context=tool_context,
        )

    return snapshot


def _verification_status(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "skipped"
    passes = sum(1 for check in checks if check["passed"])
    if passes == len(checks):
        return "pass"
    if passes > 0:
        return "partial_pass"
    return "fail"


async def _evaluate_expectations(
    *,
    surface: Optional[str],
    selector: Optional[str],
    expected: dict[str, str],
    tool_context: Optional[ToolContext],
) -> dict[str, Any]:
    if not expected:
        return {"status": "skipped", "checks": [], "success": True}

    snapshot = await _verification_snapshot(
        surface=surface,
        selector=selector,
        needs_text=bool(expected.get("text_contains") or expected.get("text_not_contains")),
        needs_url=bool(expected.get("url_contains")),
        needs_frontmost_app=bool(expected.get("frontmost_app")),
        needs_window_title=bool(expected.get("window_title_contains")),
        tool_context=tool_context,
    )

    checks: list[dict[str, Any]] = []
    if expected.get("text_contains"):
        text_payload = snapshot.get("browser_text") if surface == "browser" else snapshot.get("current_tab_text")
        text_value = text_payload.get("text") if isinstance(text_payload, dict) else ""
        checks.append(
            {
                "name": "text_contains",
                "expected": expected["text_contains"],
                "actual": text_value,
                "passed": _contains_casefold(text_value, expected["text_contains"]),
            }
        )

    if expected.get("text_not_contains"):
        text_payload = snapshot.get("browser_text") if surface == "browser" else snapshot.get("current_tab_text")
        text_value = text_payload.get("text") if isinstance(text_payload, dict) else ""
        checks.append(
            {
                "name": "text_not_contains",
                "expected": expected["text_not_contains"],
                "actual": text_value,
                "passed": not _contains_casefold(text_value, expected["text_not_contains"]),
            }
        )

    if expected.get("url_contains"):
        current_tab = snapshot.get("current_tab", {})
        url_value = current_tab.get("url") if isinstance(current_tab, dict) else ""
        checks.append(
            {
                "name": "url_contains",
                "expected": expected["url_contains"],
                "actual": url_value,
                "passed": _contains_casefold(url_value, expected["url_contains"]),
            }
        )

    if expected.get("frontmost_app"):
        frontmost = snapshot.get("frontmost_app", {})
        app_name = frontmost.get("app_name") if isinstance(frontmost, dict) else ""
        checks.append(
            {
                "name": "frontmost_app",
                "expected": expected["frontmost_app"],
                "actual": app_name,
                "passed": _contains_casefold(app_name, expected["frontmost_app"]),
            }
        )

    if expected.get("window_title_contains"):
        windows_payload = snapshot.get("windows", {})
        windows = windows_payload.get("windows", []) if isinstance(windows_payload, dict) else []
        titles = [window.get("title", "") for window in windows if isinstance(window, dict)]
        checks.append(
            {
                "name": "window_title_contains",
                "expected": expected["window_title_contains"],
                "actual": titles,
                "passed": any(_contains_casefold(title, expected["window_title_contains"]) for title in titles),
            }
        )

    status = _verification_status(checks)
    return {
        "status": status,
        "checks": checks,
        "success": status in {"pass", "skipped"},
        "snapshot": snapshot,
    }


def _record_trajectory(
    *,
    action: str,
    request: dict[str, Any],
    observation: dict[str, Any],
    attempts: list[dict[str, Any]],
    verification: dict[str, Any] | None,
    final_surface: Optional[str],
    success: bool,
) -> int:
    from src.evals.failure_taxonomy import normalize_trajectory_failure

    if success:
        status = "recovered" if len(attempts) > 1 else "success"
    else:
        status = "failed"
    classification = normalize_trajectory_failure(
        {
            "status": status,
            "attempts": attempts,
            "verification": verification,
            "request": request,
            "observation": _observation_summary(observation),
            "final_surface": final_surface,
        },
        classified_by="verifier",
    )
    store = get_computer_trajectory_store()
    return store.record(
        action=action,
        status=status,
        final_surface=final_surface,
        attempts=attempts,
        verification=verification,
        request=request,
        observation=_observation_summary(observation),
        preliminary_failure_type=classification["preliminary_failure_type"],
        normalized_failure_type=classification["normalized_failure_type"],
        classified_by=classification["classified_by"],
        operator_override=classification["operator_override"],
    )


async def _resolve_observation(
    *,
    observed_available_surfaces: list[str] | None,
    observed_preferred_surface: Optional[str],
    selector: Optional[str],
    app_name: Optional[str],
    window_id: Optional[str],
    role: Optional[str],
    title: Optional[str],
    identifier: Optional[str],
    value_contains: Optional[str],
    tool_context: Optional[ToolContext],
) -> dict[str, Any]:
    normalized_observation = _normalize_observation_input(
        observed_available_surfaces,
        observed_preferred_surface,
    )
    if normalized_observation is not None:
        return normalized_observation

    return await computer_observe(
        include_current_tab=selector is not None,
        include_frontmost_app=True,
        include_windows=True,
        ax_app_name=app_name,
        ax_window_id=window_id,
        ax_role=role,
        ax_title=title,
        ax_identifier=identifier,
        ax_value_contains=value_contains,
        tool_context=tool_context,
    )


async def _execute_with_recovery(
    *,
    action: str,
    request: dict[str, Any],
    selector: Optional[str],
    expected: dict[str, str],
    has_desktop_target: bool,
    allow_managed_browser: bool,
    observed_available_surfaces: list[str] | None,
    observed_preferred_surface: Optional[str],
    app_name: Optional[str],
    window_id: Optional[str],
    role: Optional[str],
    title: Optional[str],
    identifier: Optional[str],
    value_contains: Optional[str],
    tool_context: Optional[ToolContext],
    execute_surface: ActionExecutor,
) -> dict[str, Any]:
    fixed_observation = (
        observed_available_surfaces is not None or observed_preferred_surface is not None
    )
    observation = await _resolve_observation(
        observed_available_surfaces=observed_available_surfaces,
        observed_preferred_surface=observed_preferred_surface,
        selector=selector,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        tool_context=tool_context,
    )
    attempts: list[dict[str, Any]] = []
    attempted_strategies: set[tuple[str, str]] = set()
    final_result: dict[str, Any] = {
        "error": f"No browser or desktop surface could satisfy computer_{action}",
        "success": False,
    }
    final_surface: str | None = None
    final_strategy = "no_available_surface"
    final_verification: dict[str, Any] | None = None

    while True:
        candidates = [
            candidate
            for candidate in _candidate_strategies(
                selector=selector,
                has_desktop_target=has_desktop_target,
                allow_managed_browser=allow_managed_browser,
                available_surfaces=observation.get("available_surfaces", []),
            )
            if candidate not in attempted_strategies
        ]
        if not candidates:
            break

        surface, strategy = candidates[0]
        attempted_strategies.add((surface, strategy))
        result = await execute_surface(surface)
        attempt: dict[str, Any] = {
            "surface": surface,
            "strategy": strategy,
            "result": result,
            "success": _is_success(result),
        }
        if expected and _is_success(result):
            verification = await _evaluate_expectations(
                surface=surface,
                selector=selector,
                expected=expected,
                tool_context=tool_context,
            )
            attempt["verification"] = verification
            if verification["success"]:
                attempts.append(attempt)
                final_result = result
                final_surface = surface
                final_strategy = strategy
                final_verification = verification
                break
        elif _is_success(result):
            attempts.append(attempt)
            final_result = result
            final_surface = surface
            final_strategy = strategy
            break

        attempts.append(attempt)
        final_result = result
        final_surface = surface
        final_strategy = strategy
        if attempt.get("verification"):
            final_verification = attempt["verification"]

        if not fixed_observation:
            observation = await _resolve_observation(
                observed_available_surfaces=None,
                observed_preferred_surface=None,
                selector=selector,
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                tool_context=tool_context,
            )

    trajectory_id = _record_trajectory(
        action=action,
        request=request,
        observation=observation,
        attempts=attempts,
        verification=final_verification,
        final_surface=final_surface,
        success=_is_success(final_result) and (final_verification or {"success": True})["success"],
    )
    return _action_payload(
        action=action,
        surface=final_surface,
        strategy=final_strategy,
        observation=observation,
        result=final_result,
        attempts=attempts,
        verification=final_verification,
        trajectory_id=trajectory_id,
    )


async def computer_evaluate(
    selector: Optional[str] = None,
    expected_text_contains: Optional[str] = None,
    expected_text_not_contains: Optional[str] = None,
    expected_url_contains: Optional[str] = None,
    expected_frontmost_app: Optional[str] = None,
    expected_window_title_contains: Optional[str] = None,
    surface: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Evaluate browser / desktop state against explicit expectations."""

    expected = _expected_verification(
        text_contains=expected_text_contains,
        text_not_contains=expected_text_not_contains,
        url_contains=expected_url_contains,
        frontmost_app=expected_frontmost_app,
        window_title_contains=expected_window_title_contains,
    )
    evaluation = await _evaluate_expectations(
        surface=surface,
        selector=selector,
        expected=expected,
        tool_context=tool_context,
    )
    evaluation["surface"] = surface
    evaluation["expected"] = expected
    return evaluation


async def computer_observe(
    include_current_tab: bool = True,
    include_current_tab_text: bool = False,
    current_tab_selector: Optional[str] = None,
    include_frontmost_app: bool = True,
    include_windows: bool = True,
    include_screenshot: bool = False,
    include_ax_snapshot: bool = False,
    ax_app_name: Optional[str] = None,
    ax_window_id: Optional[str] = None,
    ax_role: Optional[str] = None,
    ax_title: Optional[str] = None,
    ax_identifier: Optional[str] = None,
    ax_value_contains: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Collect browser/desktop observations in a single browser-first bundle."""

    errors: dict[str, str] = {}
    result: dict[str, Any] = {
        "mode": "browser_first",
        "surface_order": ["current_tab", "desktop"],
        "available_surfaces": [],
    }

    if include_current_tab:
        current_tab = await current_tab_info(tool_context=tool_context)
        result["current_tab"] = current_tab
        error = _tool_error(current_tab)
        if error:
            errors["current_tab"] = error
        elif _is_success(current_tab):
            result["available_surfaces"].append("current_tab")

    if include_current_tab_text:
        current_tab_text = await current_tab_extract_text(
            selector=current_tab_selector,
            tool_context=tool_context,
        )
        result["current_tab_text"] = current_tab_text
        error = _tool_error(current_tab_text)
        if error:
            errors["current_tab_text"] = error

    if include_frontmost_app:
        frontmost_app = await desktop_view_frontmost_app(tool_context=tool_context)
        result["frontmost_app"] = frontmost_app
        error = _tool_error(frontmost_app)
        if error:
            errors["frontmost_app"] = error
        elif _is_success(frontmost_app) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if include_windows:
        windows = await desktop_view_windows(
            include_minimized=False,
            tool_context=tool_context,
        )
        result["windows"] = windows
        error = _tool_error(windows)
        if error:
            errors["windows"] = error
        elif _is_success(windows) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if include_screenshot:
        screenshot = await desktop_view_screenshot(tool_context=tool_context)
        result["screenshot"] = screenshot
        error = _tool_error(screenshot)
        if error:
            errors["screenshot"] = error
        elif _is_success(screenshot) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if any((ax_role, ax_title, ax_identifier, ax_value_contains, ax_window_id)):
        ax_find = await desktop_ax_find(
            app_name=ax_app_name,
            window_id=ax_window_id,
            role=ax_role,
            title=ax_title,
            identifier=ax_identifier,
            value_contains=ax_value_contains,
            tool_context=tool_context,
        )
        result["ax_find"] = ax_find
        error = _tool_error(ax_find)
        if error:
            errors["ax_find"] = error
        elif _is_success(ax_find) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if include_ax_snapshot:
        ax_snapshot = await desktop_ax_snapshot(
            app_name=ax_app_name,
            window_id=ax_window_id,
            tool_context=tool_context,
        )
        result["ax_snapshot"] = ax_snapshot
        error = _tool_error(ax_snapshot)
        if error:
            errors["ax_snapshot"] = error
        elif _is_success(ax_snapshot) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if errors:
        result["errors"] = errors

    result["success"] = bool(result["available_surfaces"])
    result["preferred_surface"] = (
        "current_tab"
        if "current_tab" in result["available_surfaces"]
        else "desktop"
        if "desktop" in result["available_surfaces"]
        else None
    )
    return result


async def computer_click(
    selector: Optional[str] = None,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    allow_managed_browser: bool = True,
    verify_text_contains: Optional[str] = None,
    verify_text_not_contains: Optional[str] = None,
    verify_url_contains: Optional[str] = None,
    verify_frontmost_app: Optional[str] = None,
    verify_window_title_contains: Optional[str] = None,
    observed_available_surfaces: list[str] | None = None,
    observed_preferred_surface: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Click the best available browser/desktop surface using browser-first fallback."""

    has_desktop_target = _has_desktop_target(
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if not selector and not has_desktop_target:
        return {
            "action": "click",
            "success": False,
            "error": "computer_click requires a CSS selector or desktop target fields",
        }

    expected = _expected_verification(
        text_contains=verify_text_contains,
        text_not_contains=verify_text_not_contains,
        url_contains=verify_url_contains,
        frontmost_app=verify_frontmost_app,
        window_title_contains=verify_window_title_contains,
    )
    async def _execute_surface(surface: str) -> dict[str, Any]:
        if surface == "current_tab":
            return await current_tab_click(selector, tool_context=tool_context)
        if surface == "desktop":
            return await desktop_control_click(
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                index=index,
                tool_context=tool_context,
            )
        return await browser_click(selector, tool_context=tool_context)

    return await _execute_with_recovery(
        action="click",
        request={
            "selector": selector,
            "app_name": app_name,
            "window_id": window_id,
            "role": role,
            "title": title,
            "identifier": identifier,
            "value_contains": value_contains,
            "allow_managed_browser": allow_managed_browser,
            "verify": expected,
        },
        selector=selector,
        expected=expected,
        has_desktop_target=has_desktop_target,
        allow_managed_browser=allow_managed_browser,
        observed_available_surfaces=observed_available_surfaces,
        observed_preferred_surface=observed_preferred_surface,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        tool_context=tool_context,
        execute_surface=_execute_surface,
    )


async def computer_fill(
    selector: Optional[str] = None,
    text: str = "",
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    allow_managed_browser: bool = True,
    verify_text_contains: Optional[str] = None,
    verify_text_not_contains: Optional[str] = None,
    verify_url_contains: Optional[str] = None,
    verify_frontmost_app: Optional[str] = None,
    verify_window_title_contains: Optional[str] = None,
    observed_available_surfaces: list[str] | None = None,
    observed_preferred_surface: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Fill the best available browser/desktop surface using browser-first fallback."""

    has_desktop_target = _has_desktop_target(
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if not selector and not has_desktop_target:
        return {
            "action": "fill",
            "success": False,
            "error": "computer_fill requires a CSS selector or desktop target fields",
        }

    expected = _expected_verification(
        text_contains=verify_text_contains,
        text_not_contains=verify_text_not_contains,
        url_contains=verify_url_contains,
        frontmost_app=verify_frontmost_app,
        window_title_contains=verify_window_title_contains,
    )
    async def _execute_surface(surface: str) -> dict[str, Any]:
        if surface == "current_tab":
            return await current_tab_fill(selector, text, tool_context=tool_context)
        if surface == "desktop":
            return await desktop_control_type(
                text=text,
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                index=index,
                tool_context=tool_context,
            )
        return await browser_fill(selector, text, tool_context=tool_context)

    return await _execute_with_recovery(
        action="fill",
        request={
            "selector": selector,
            "text_length": len(text),
            "app_name": app_name,
            "window_id": window_id,
            "role": role,
            "title": title,
            "identifier": identifier,
            "value_contains": value_contains,
            "allow_managed_browser": allow_managed_browser,
            "verify": expected,
        },
        selector=selector,
        expected=expected,
        has_desktop_target=has_desktop_target,
        allow_managed_browser=allow_managed_browser,
        observed_available_surfaces=observed_available_surfaces,
        observed_preferred_surface=observed_preferred_surface,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        tool_context=tool_context,
        execute_surface=_execute_surface,
    )


async def computer_trajectory_recent(
    status: Optional[str] = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return recent computer-use trajectories for failure and repair analysis."""

    trajectories = get_computer_trajectory_store().recent(status=status, limit=limit)
    return {
        "success": True,
        "count": len(trajectories),
        "trajectories": trajectories,
    }
