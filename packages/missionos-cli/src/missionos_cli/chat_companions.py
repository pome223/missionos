"""Managed companion-terminal lifecycle for MissionOS chat sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .gateway_client import MissionOSGatewayClient
from .job_status import _task_artifacts
from .map_model import _turtlebot3_core_feasibility_from_artifacts


TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL = 1.0
TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "recovered", "blocked", "failed", "cancelled", "canceled"}
)
CHAT_COMPANION_TERMINAL_ROOT = Path("data/missionos_chat_companions")
CHAT_COMPANION_TERMINAL_SURFACES = ("operate", "watch", "map")

console = Console()


def _status_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _safe_chat_companion_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug[:80] or "missionos"


def _chat_companion_terminals_enabled(ctx: click.Context) -> bool:
    env_value = os.environ.get("MISSIONOS_CHAT_COMPANION_TERMINALS", "1").strip().lower()
    if env_value in {"0", "false", "no", "off"}:
        return False
    if not bool(ctx.obj.get("missionos_chat_companion_terminals_enabled")):
        return False
    return sys.stdin.isatty()


def _missionos_chat_companion_command_prefix(ctx: click.Context) -> str:
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else Path("missionos")
    if argv0.exists() and argv0.is_file() and os.access(argv0, os.X_OK):
        parts = [str(argv0.resolve())]
    elif argv0.name == "__main__.py" and argv0.parent.name == "missionos_cli":
        parts = [sys.executable, "-m", "missionos_cli"]
    else:
        parts = ["missionos"]
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or "").strip()
    if gateway_url:
        parts.extend(["--gateway-url", gateway_url])
    client = ctx.obj.get("missionos_client")
    if isinstance(client, MissionOSGatewayClient):
        parts.extend(["--timeout", str(client.timeout)])
    state_path = ctx.obj.get("missionos_state_path")
    if state_path:
        parts.extend(["--state-path", str(state_path)])
    return " ".join(shlex.quote(part) for part in parts)


def _chat_companion_terminal_script(
    *,
    title: str,
    command: str,
    stop_path: Path,
    gateway_api_key_path: Path | None,
    cwd: Path,
    hold_after_command: bool,
) -> str:
    hold = "1" if hold_after_command else "0"
    api_key_path = str(gateway_api_key_path or "")
    return f"""#!/bin/sh
set +e
cd {shlex.quote(str(cwd))}
STOP_PATH={shlex.quote(str(stop_path))}
GATEWAY_API_KEY_PATH={shlex.quote(api_key_path)}
TITLE={shlex.quote(title)}
HOLD_AFTER_COMMAND={hold}
if [ -n "$GATEWAY_API_KEY_PATH" ] && [ -f "$GATEWAY_API_KEY_PATH" ]; then
  IFS= read -r GATEWAY_API_KEY < "$GATEWAY_API_KEY_PATH"
  export GATEWAY_API_KEY
fi
printf '\\033]0;%s\\007' "$TITLE"
echo "$TITLE"
echo "This MissionOS companion terminal closes when missionos chat exits."
(
  while [ ! -f "$STOP_PATH" ]; do
    sleep 1
  done
  pkill -TERM -P $$ 2>/dev/null || true
  kill -TERM $$ 2>/dev/null || true
) &
WATCHER_PID=$!
trap 'kill "$WATCHER_PID" 2>/dev/null || true' EXIT INT TERM
{command}
COMMAND_STATUS=$?
if [ "$HOLD_AFTER_COMMAND" = "1" ]; then
  echo
  echo "Command finished. Waiting for missionos chat to close..."
  while [ ! -f "$STOP_PATH" ]; do
    sleep 1
  done
fi
exit "$COMMAND_STATUS"
"""


def _launch_macos_terminal_script(script_path: Path, *, title: str) -> bool:
    if sys.platform != "darwin":
        return False
    command = f"sh {shlex.quote(str(script_path.resolve()))}"
    applescript = "\n".join(
        [
            'tell application "Terminal"',
            "activate",
            f"set newTab to do script {json.dumps(command)}",
            "delay 0.1",
            f"set custom title of newTab to {json.dumps(title)}",
            "end tell",
        ]
    )
    try:
        subprocess.run(
            ["osascript", "-e", applescript],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _close_macos_companion_terminal_titles(titles: list[str]) -> None:
    if sys.platform != "darwin" or not titles:
        return
    conditions = " or ".join(
        f"custom title of t contains {json.dumps(title)}" for title in titles
    )
    applescript = "\n".join(
        [
            'tell application "Terminal"',
            "repeat 10 times",
            "set closedOne to false",
            "repeat with w in windows",
            "repeat with t in tabs of w",
            "try",
            f"if {conditions} then",
            "close w saving no",
            "set closedOne to true",
            "exit repeat",
            "end if",
            "end try",
            "end repeat",
            "if closedOne then exit repeat",
            "end repeat",
            "if not closedOne then exit repeat",
            "delay 0.1",
            "end repeat",
            "end tell",
        ]
    )
    try:
        subprocess.run(
            ["osascript", "-e", applescript],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _stop_chat_companion_terminals_impl(
    ctx: click.Context,
    *,
    close_terminals: Callable[[list[str]], None],
) -> None:
    state = ctx.obj.pop("missionos_chat_companion_terminals", None)
    if not isinstance(state, dict):
        return
    stop_raw = str(state.get("stop_path") or "")
    if stop_raw:
        stop_path = Path(stop_raw)
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.touch()
    time.sleep(0.5)
    api_key_path_raw = str(state.get("gateway_api_key_path") or "")
    if api_key_path_raw:
        Path(api_key_path_raw).unlink(missing_ok=True)
    titles = [str(title) for title in state.get("titles") or [] if str(title)]
    close_terminals(titles)


def _ensure_chat_companion_terminals_impl(
    ctx: click.Context,
    task_id: str,
    *,
    terminal_root: Path,
    terminal_surfaces: tuple[str, ...],
    terminals_enabled: Callable[[click.Context], bool],
    stop_existing: Callable[[click.Context], None],
    launch_terminal: Callable[..., bool],
) -> None:
    if not task_id or not terminals_enabled(ctx):
        return
    existing = ctx.obj.get("missionos_chat_companion_terminals")
    if isinstance(existing, dict) and existing.get("task_id") == task_id:
        return
    if isinstance(existing, dict):
        stop_existing(ctx)

    session_slug = _safe_chat_companion_slug(
        str(ctx.obj.get("missionos_chat_session_id") or "chat")
    )
    task_slug = _safe_chat_companion_slug(task_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (
        Path.cwd() / terminal_root / f"{session_slug}_{task_slug}_{stamp}"
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stop_path = root / "stop"
    client = ctx.obj.get("missionos_client")
    gateway_api_key = (
        str(client.api_key or "") if isinstance(client, MissionOSGatewayClient) else ""
    )
    gateway_api_key_path: Path | None = None
    if gateway_api_key:
        gateway_api_key_path = root / "gateway_api_key"
        gateway_api_key_path.write_text(gateway_api_key, encoding="utf-8")
        gateway_api_key_path.chmod(0o600)
    command_prefix = _missionos_chat_companion_command_prefix(ctx)
    commands = {
        "operate": f"{command_prefix} operate --task-id {shlex.quote(task_id)}",
        "watch": f"{command_prefix} watch --task-id {shlex.quote(task_id)}",
        "map": f"{command_prefix} map --task-id {shlex.quote(task_id)} --serve-live",
    }
    titles: list[str] = []
    launched: list[str] = []
    for surface in terminal_surfaces:
        title = f"MissionOS {surface} {task_id}"
        script_path = root / f"{surface}.sh"
        script_path.write_text(
            _chat_companion_terminal_script(
                title=title,
                command=commands[surface],
                stop_path=stop_path,
                gateway_api_key_path=gateway_api_key_path,
                cwd=Path.cwd(),
                hold_after_command=surface == "map",
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        titles.append(title)
        if launch_terminal(script_path, title=title):
            launched.append(surface)

    if launched:
        ctx.obj["missionos_chat_companion_terminals"] = {
            "task_id": task_id,
            "root": str(root),
            "stop_path": str(stop_path),
            "gateway_api_key_path": str(gateway_api_key_path or ""),
            "titles": titles,
            "launched": launched,
        }
        console.print(
            "[blue]Opened companion terminals: "
            + ", ".join(launched)
            + ". They will close when chat exits.[/blue]"
        )
    else:
        console.print(
            "[yellow]Companion terminals are unavailable here. Run these manually if needed: "
            f"missionos operate --task-id {task_id}; missionos watch --task-id {task_id}; "
            f"missionos map --task-id {task_id}[/yellow]"
        )


def _maybe_open_home_robot_companion_terminals_impl(
    ctx: click.Context,
    payload: dict[str, Any],
    *,
    is_home_robot_execution_target: Callable[[Any], bool],
    payload_task_id: Callable[[dict[str, Any] | None], str],
    ensure_terminals: Callable[[click.Context, str], None],
) -> None:
    """Bind all companion surfaces to the task returned by one operation."""

    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    summary = operation.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    if not is_home_robot_execution_target(summary.get("execution_target")):
        return
    task_id = payload_task_id(operation) or payload_task_id(payload)
    if task_id:
        ensure_terminals(ctx, task_id)


def _listed_home_robot_task_ids(
    client: MissionOSGatewayClient,
    *,
    is_home_robot_task_artifacts: Callable[[dict[str, Any]], bool],
) -> set[str] | None:
    """Return source-backed home-robot task ids, or ``None`` if unavailable.

    An unavailable listing is not equivalent to an observed empty task set.
    Callers must not use it as a discovery baseline.
    """

    try:
        payload = client.get("/tasks?page=1&page_size=20")
    except click.ClickException:
        return None
    if "items" in payload:
        items = payload.get("items")
    elif "tasks" in payload:
        items = payload.get("tasks")
    else:
        return None
    if not isinstance(items, list):
        return None
    task_ids: set[str] = set()
    for task in items:
        if not isinstance(task, dict):
            continue
        artifacts = task.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        task_id = str(task.get("task_id") or "").strip()
        if task_id and is_home_robot_task_artifacts(artifacts):
            task_ids.add(task_id)
    return task_ids


def _run_home_robot_conversation_with_companion_monitor_impl(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    operation: Callable[[], dict[str, Any]],
    *,
    terminals_enabled: Callable[[click.Context], bool],
    is_home_robot_task_artifacts: Callable[[dict[str, Any]], bool],
    remember_task_id: Callable[[click.Context, str], None],
    ensure_terminals: Callable[[click.Context, str], None],
) -> dict[str, Any]:
    """Open surfaces as soon as one new home-robot task appears.

    The before/after task-id set is the correlation boundary. Existing tasks are
    never rebound to the new conversation merely because they are still live.
    """

    if not terminals_enabled(ctx):
        return operation()
    existing_task_ids = _listed_home_robot_task_ids(
        client,
        is_home_robot_task_artifacts=is_home_robot_task_artifacts,
    )
    if existing_task_ids is None:
        # Without a source-backed baseline, an existing task could look new on
        # the next successful poll. Wait for the operation's exact response.
        return operation()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(operation)
    bound_task_id = ""
    try:
        while True:
            try:
                return future.result(timeout=0.5)
            except FutureTimeout:
                if bound_task_id:
                    # One task is already correlated. Do not let later task-list
                    # window changes replace it while the operation completes.
                    continue
                current_task_ids = _listed_home_robot_task_ids(
                    client,
                    is_home_robot_task_artifacts=is_home_robot_task_artifacts,
                )
                if current_task_ids is None:
                    continue
                new_task_ids = current_task_ids - existing_task_ids
                # A set delta is sufficient only when it identifies one task.
                # Concurrent new runs are ambiguous; wait for the operation's
                # exact response instead of binding a random task by ID order.
                if len(new_task_ids) == 1:
                    task_id = next(iter(new_task_ids))
                    remember_task_id(ctx, task_id)
                    ensure_terminals(ctx, task_id)
                    bound_task_id = task_id
    finally:
        executor.shutdown(wait=False, cancel_futures=False)


def _maybe_start_home_robot_chat_task_status_monitor_impl(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    payload: dict[str, Any],
    *,
    payload_task_id: Callable[[dict[str, Any] | None], str],
    stored_task_id: Callable[[click.Context], str],
    start_monitor: Callable[..., None],
) -> None:
    """Follow the same operation task in chat until durable terminal truth."""

    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    summary = operation.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    status = str(
        summary.get("status")
        or operation.get("summary_status")
        or operation.get("response_status")
        or ""
    ).strip().lower()
    if status in TERMINAL_TASK_STATUSES:
        return
    task_id = (
        payload_task_id(operation)
        or payload_task_id(payload)
        or stored_task_id(ctx)
    )
    if task_id:
        start_monitor(ctx, client, task_id=task_id)


def _print_turtlebot3_chat_task_terminal_update(task_payload: dict[str, Any]) -> None:
    """Append durable terminal task truth to the main chat transcript."""
    task = task_payload.get("task")
    task = task if isinstance(task, dict) else {}
    artifacts = _task_artifacts(task_payload)
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    status = str(task.get("status") or summary.get("status") or "unknown")
    completed = summary.get("segment_completion_count")
    planned = summary.get("planned_segment_count")
    core_feasibility = _turtlebot3_core_feasibility_from_artifacts(artifacts)
    lines = [
        f"task_id={_status_text(task.get('task_id'))}",
        f"operation_status={_status_text(status)}",
        "recovery_goal="
        f"{_status_text(summary.get('recovery_goal_status'))}; "
        "verification="
        f"{_status_text(summary.get('recovery_verification_status'))}; "
        f"route={_status_text(summary.get('route_resume_status'))}",
        f"segments={_status_text(completed)}/{_status_text(planned)}; "
        f"completion_claimed={summary.get('completion_claimed') is True}",
        "core_feasibility="
        f"{_status_text(core_feasibility.get('candidate_status'))}; "
        "predispatch_revalidation="
        f"{_status_text(core_feasibility.get('predispatch_revalidation_status'))}",
        "mission_delivery_completion_claimed="
        f"{summary.get('mission_delivery_completion_claimed') is True}; "
        "physical_execution_invoked="
        f"{summary.get('physical_execution_invoked') is True}",
    ]
    blocking_reasons = [
        str(reason) for reason in summary.get("blocking_reasons") or [] if str(reason)
    ]
    if blocking_reasons:
        lines.append("blocking_reasons=" + ", ".join(blocking_reasons))
    console.print(
        Panel(
            Text("\n".join(lines)),
            title="MissionOS task final update",
            border_style="green" if status == "completed" else "yellow",
        )
    )


def _stop_turtlebot3_chat_task_status_monitor(ctx: click.Context) -> None:
    state = ctx.obj.pop("missionos_turtlebot3_chat_task_status_monitor", None)
    if not isinstance(state, dict):
        return
    stop_event = state.get("stop_event")
    if isinstance(stop_event, threading.Event):
        stop_event.set()
    thread = state.get("thread")
    if isinstance(thread, threading.Thread) and thread is not threading.current_thread():
        thread.join(timeout=1.0)


def _start_turtlebot3_chat_task_status_monitor_impl(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    task_id: str,
    print_terminal_update: Callable[[dict[str, Any]], None],
) -> None:
    """Notify main chat when a companion-driven TurtleBot3 task terminates."""
    if not task_id:
        return
    existing = ctx.obj.get("missionos_turtlebot3_chat_task_status_monitor")
    if isinstance(existing, dict) and existing.get("task_id") == task_id:
        thread = existing.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return
    _stop_turtlebot3_chat_task_status_monitor(ctx)
    stop_event = threading.Event()

    def monitor() -> None:
        encoded_task_id = quote(task_id, safe="")
        while not stop_event.is_set():
            try:
                task_payload = client.get(f"/tasks/{encoded_task_id}")
            except click.ClickException:
                if stop_event.wait(TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL):
                    return
                continue
            task = task_payload.get("task")
            task = task if isinstance(task, dict) else {}
            status = str(task.get("status") or "").strip().lower()
            if status in TERMINAL_TASK_STATUSES:
                print_terminal_update(task_payload)
                return
            if stop_event.wait(TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL):
                return

    thread = threading.Thread(
        target=monitor,
        name=f"missionos-chat-task-status-{task_id}",
        daemon=True,
    )
    ctx.obj["missionos_turtlebot3_chat_task_status_monitor"] = {
        "task_id": task_id,
        "stop_event": stop_event,
        "thread": thread,
    }
    thread.start()
