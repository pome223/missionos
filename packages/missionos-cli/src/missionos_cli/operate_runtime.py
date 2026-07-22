"""Interactive refresh and input loop for ``missionos operate``."""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any
import sys
import threading
import time

import click
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console, Group
from rich.panel import Panel


def run_operate_console(
    client: Any,
    task_id: str,
    *,
    poll_interval: float,
    history_path: Path,
    console: Console,
    build_session: Callable[[Path], Any],
    help_panel: Callable[..., Panel],
    robot_for_task: Callable[[Any, str], str],
    status_group: Callable[[Any, str], tuple[Group, str, str]],
    parse_command: Callable[[str], Any],
    handle_command: Callable[[Any, str, Any], bool],
    handle_natural_language: Callable[[Any, str, str], bool],
    terminal_task_statuses: Collection[str],
) -> None:
    """Run the local console while injected callbacks retain authority semantics."""
    session = build_session(history_path) if sys.stdin.isatty() else None
    scripted_input = None if session is not None else iter(sys.stdin)
    console.print(help_panel(task_id, robot=robot_for_task(client, task_id)))

    render_lock = threading.Lock()
    stop_refresh = threading.Event()
    last_fingerprint = ""

    def print_status(*, force: bool = False) -> str:
        nonlocal last_fingerprint
        with render_lock:
            try:
                group, current_status, fingerprint = status_group(client, task_id)
                if force or fingerprint != last_fingerprint:
                    console.print(group)
                    last_fingerprint = fingerprint
                return current_status
            except click.ClickException as exc:
                console.print(
                    Panel(f"[red]{exc.message}[/red]", title="MissionOS Operate")
                )
                return "unavailable"

    def auto_refresh() -> None:
        while not stop_refresh.wait(max(5.0, poll_interval)):
            current_status = print_status()
            if current_status in terminal_task_statuses:
                stop_refresh.set()
                break

    refresh_thread: threading.Thread | None = None
    if session is not None:
        refresh_thread = threading.Thread(
            target=auto_refresh,
            name=f"missionos-operate-refresh-{task_id}",
            daemon=True,
        )
        refresh_thread.start()
    while True:
        status = print_status(force=not last_fingerprint)
        if status in terminal_task_statuses:
            break
        try:
            if scripted_input is not None:
                raw = next(scripted_input).strip()
                console.print(f"[bold cyan]operate>[/bold cyan] {raw}")
            else:
                with patch_stdout(raw=True):
                    raw = session.prompt(HTML("<ansicyan>operate></ansicyan> "))
        except StopIteration:
            break
        except KeyboardInterrupt:
            console.print("[yellow](Ctrl+C - type quit or Ctrl+D to exit)[/yellow]")
            continue
        except EOFError:
            break
        try:
            command = parse_command(raw)
        except click.ClickException as exc:
            if (
                robot_for_task(client, task_id) == "turtlebot3"
                and raw.strip()
                and handle_natural_language(client, task_id, raw)
            ):
                pass
            else:
                console.print(
                    f"[red]{exc.message}[/red]\n"
                    "[dim]You can also describe the change naturally, for example: "
                    "左へ大きく迂回して[/dim]"
                )
        else:
            try:
                if not handle_command(client, task_id, command):
                    break
            except click.ClickException as exc:
                console.print(
                    f"[red]{exc.message}[/red]\n"
                    "[yellow]The approved operation may still be running. This "
                    "console will rely on the durable task state; do not approve "
                    "the same checkpoint again.[/yellow]"
                )
        if raw.strip() in {"wait", "sleep"}:
            time.sleep(max(0.2, poll_interval))
    stop_refresh.set()
    if refresh_thread is not None:
        refresh_thread.join(timeout=1.0)
