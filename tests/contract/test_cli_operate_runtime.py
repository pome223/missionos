from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import click
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from missionos_cli import operate_runtime


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def _run_console(
    monkeypatch: Any,
    *,
    input_text: str,
    robot: str = "turtlebot3",
    status: str = "running",
    parse_command: Any = lambda raw: raw,
    handle_command: Any = lambda _client, _task_id, _command: False,
    handle_natural_language: Any = lambda _client, _task_id, _raw: False,
) -> str:
    console, output = _console_output()
    monkeypatch.setattr(operate_runtime.sys, "stdin", StringIO(input_text))

    operate_runtime.run_operate_console(
        object(),
        "task_operate_runtime",
        poll_interval=0.2,
        history_path=Path("unused-history"),
        console=console,
        build_session=lambda _path: (_ for _ in ()).throw(
            AssertionError("scripted input must not create an interactive session")
        ),
        help_panel=lambda task_id, *, robot: Panel(
            f"task={task_id} robot={robot}", title="Operate"
        ),
        robot_for_task=lambda _client, _task_id: robot,
        status_group=lambda _client, _task_id: (
            Group(Text(f"status={status}")),
            status,
            f"fingerprint:{status}",
        ),
        parse_command=parse_command,
        handle_command=handle_command,
        handle_natural_language=handle_natural_language,
        terminal_task_statuses={"completed", "blocked"},
    )
    return output.getvalue()


def test_scripted_operate_command_uses_injected_handler(monkeypatch: Any) -> None:
    handled: list[tuple[str, str]] = []

    output = _run_console(
        monkeypatch,
        input_text="quit\n",
        handle_command=lambda _client, task_id, command: (
            handled.append((task_id, command)) or False
        ),
    )

    assert handled == [("task_operate_runtime", "quit")]
    assert "operate> quit" in output
    assert "status=running" in output


def test_turtlebot_natural_language_fallback_keeps_authority_in_callback(
    monkeypatch: Any,
) -> None:
    revisions: list[str] = []

    def reject_command(_raw: str) -> None:
        raise click.ClickException("unknown command")

    output = _run_console(
        monkeypatch,
        input_text="左へ大きく迂回して\n",
        parse_command=reject_command,
        handle_command=lambda *_args: (_ for _ in ()).throw(
            AssertionError("invalid command must not reach the command handler")
        ),
        handle_natural_language=lambda _client, _task_id, raw: (
            revisions.append(raw) or True
        ),
    )

    assert revisions == ["左へ大きく迂回して"]
    assert "unknown command" not in output


def test_px4_natural_language_fallback_uses_same_authority_callback(
    monkeypatch: Any,
) -> None:
    proposals: list[str] = []

    def reject_command(_raw: str) -> None:
        raise click.ClickException("unknown command")

    output = _run_console(
        monkeypatch,
        input_text="大きく右へ迂回して\n",
        robot="px4",
        parse_command=reject_command,
        handle_natural_language=lambda _client, _task_id, raw: (
            proposals.append(raw) or True
        ),
    )

    assert proposals == ["大きく右へ迂回して"]
    assert "unknown command" not in output


def test_unsupported_natural_language_is_not_advertised(
    monkeypatch: Any,
) -> None:
    def reject_command(_raw: str) -> None:
        raise click.ClickException("unknown command")

    output = _run_console(
        monkeypatch,
        input_text="自然文\n",
        robot="nova_carter",
        parse_command=reject_command,
    )

    assert "unknown command" in output
    assert "You can also describe the change naturally" not in output


def test_terminal_task_exits_before_reading_a_command(monkeypatch: Any) -> None:
    output = _run_console(
        monkeypatch,
        input_text="approve\n",
        status="completed",
        handle_command=lambda *_args: (_ for _ in ()).throw(
            AssertionError("terminal tasks must not accept commands")
        ),
    )

    assert "status=completed" in output
    assert "operate> approve" not in output
