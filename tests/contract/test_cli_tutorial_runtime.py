from io import StringIO

from rich.console import Console

import missionos_cli.tutorial_runtime as tutorial


def _action(log: list[str], name: str, outcome: str | None = None):
    def invoke(*_args, **_kwargs):
        log.append(name)
        return outcome

    return invoke


def test_tutorial_step_contract_marks_only_execute_as_live() -> None:
    calls: list[str] = []
    steps = tutorial.build_tutorial_steps(
        status_action=_action(calls, "status"),
        plan_action=_action(calls, "plan"),
        approve_action=_action(calls, "approve"),
        run_action=_action(calls, "run"),
        start_sitl_action=_action(calls, "start-sitl"),
        execute_sitl_action=_action(calls, "execute-sitl"),
    )

    assert [step.key for step in steps] == [
        "status",
        "plan",
        "approve",
        "run",
        "start-sitl",
        "execute-sitl",
    ]
    assert [step.key for step in steps if step.live] == ["execute-sitl"]
    assert calls == []


def test_live_tutorial_step_requires_explicit_yes(monkeypatch) -> None:
    calls: list[str] = []
    output = StringIO()
    monkeypatch.setattr(
        tutorial,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )
    step = tutorial.TutorialStep(
        key="execute-sitl",
        title="Execute Live SITL",
        explanation="Live execution boundary.",
        command="missionos execute-sitl --live-flight",
        boundary="Explicit operator confirmation required.",
        action=_action(calls, "execute-sitl", "completed"),
        live=True,
    )

    tutorial.run_tutorial_steps(
        object(),
        object(),
        session_id="tutorial-test",
        steps=[step],
        interactive=True,
        allow_live=False,
        terminal_task_statuses={"completed", "blocked"},
        progress_status_text=lambda _payload: "running",
        reader=lambda _prompt: "no",
    )

    assert calls == []
    assert "Skipped live execution" in output.getvalue()


def test_auto_tutorial_without_yes_does_not_execute_live_step(monkeypatch) -> None:
    calls: list[str] = []
    output = StringIO()
    monkeypatch.setattr(
        tutorial,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )
    non_live = tutorial.TutorialStep(
        key="plan",
        title="Plan",
        explanation="Planning only.",
        command="missionos say plan",
        boundary="No approval and no execution.",
        action=_action(calls, "plan"),
    )
    live = tutorial.TutorialStep(
        key="execute-sitl",
        title="Execute Live SITL",
        explanation="Live execution boundary.",
        command="missionos execute-sitl --live-flight",
        boundary="Explicit operator confirmation required.",
        action=_action(calls, "execute-sitl", "completed"),
        live=True,
    )

    tutorial.run_tutorial_steps(
        object(),
        object(),
        session_id="tutorial-test",
        steps=[non_live, live],
        interactive=False,
        allow_live=False,
        terminal_task_statuses={"completed", "blocked"},
        progress_status_text=lambda _payload: "running",
    )

    assert calls == ["plan"]
    assert "--yes was not set" in output.getvalue()
