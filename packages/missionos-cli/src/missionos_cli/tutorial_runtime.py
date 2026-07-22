"""Presentation and coordination for the guided MissionOS CLI tutorial."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel


TutorialOutcome = str | None
TutorialAction = Callable[..., TutorialOutcome]
TutorialReader = Callable[[str], str]

console = Console()


@dataclass
class TutorialStep:
    """One teaching step and the production boundary it crosses."""

    key: str
    title: str
    explanation: str
    command: str
    boundary: str
    action: TutorialAction
    live: bool = False


def build_tutorial_steps(
    *,
    status_action: TutorialAction,
    plan_action: TutorialAction,
    approve_action: TutorialAction,
    run_action: TutorialAction,
    start_sitl_action: TutorialAction,
    execute_sitl_action: TutorialAction,
) -> list[TutorialStep]:
    """Build the ordered Fuji-delivery walkthrough from injected actions."""
    return [
        TutorialStep(
            key="status",
            title="Read Current State",
            explanation=(
                "Read the MissionOS operator surfaces (Gateway / Plan / Review / "
                "Execution / Repair). This does not start anything."
            ),
            command="missionos status",
            boundary="Read-only. No PX4/Gazebo process and no dispatch authority.",
            action=status_action,
        ),
        TutorialStep(
            key="plan",
            title="Plan (say)",
            explanation=(
                "Ask for the plan in natural language. The CLI passes the bundled "
                "Mt. Fuji route coordinates (the same values as route.yaml). The "
                "Gateway creates a source-bound Mission Designer context, and the "
                "CLI stores that reference in state."
            ),
            command=(
                "missionos say --route-hint mission_designer_plan "
                "--coordinate-route-file docs/mission_os/fuji_delivery_route.yaml "
                '"Plan the Mt. Fuji delivery"'
            ),
            boundary="Planning only. No approval and no execution.",
            action=plan_action,
        ),
        TutorialStep(
            key="approve",
            title="Approve (approve)",
            explanation=(
                "Approve the plan as the operator. This uses the same conversation "
                "route as MissionOS chat approval, with Gateway policy gates still active."
            ),
            command="missionos approve",
            boundary="Sends only the approval intent. It does not bypass gates.",
            action=approve_action,
        ),
        TutorialStep(
            key="run",
            title="Prepare Bounded Action (run)",
            explanation=(
                "Prepare the approved bounded action through the execution gate. "
                "When a SITL execution task is returned, the CLI stores the task_id "
                "in state so later commands can reuse it."
            ),
            command="missionos run",
            boundary="Passes the execution gate, but the simulator is not started yet.",
            action=run_action,
        ),
        TutorialStep(
            key="start-sitl",
            title="Start SITL (start-sitl)",
            explanation=(
                "Use the PX4/Gazebo SITL startup boundary. This is where simulator "
                "readiness is brought up (task_id is read from state)."
            ),
            command="missionos start-sitl",
            boundary="Real PX4/Gazebo processes begin here.",
            action=start_sitl_action,
        ),
        TutorialStep(
            key="execute-sitl",
            title="Execute Live SITL (execute-sitl)",
            explanation=(
                "Use the Execute Live SITL boundary. The CLI sends explicit execution "
                "approval and live_flight_mode=true. This is a real execution gate, "
                "so it requires explicit confirmation."
            ),
            command="missionos execute-sitl --live-flight",
            boundary=(
                "Live execution. delivery_completion_claimed / "
                "physical_delivery_verified remain false; the CLI has no path that "
                "turns them true."
            ),
            action=execute_sitl_action,
            live=True,
        ),
    ]


def _print_tutorial_step(index: int, total: int, step: TutorialStep) -> None:
    body = (
        f"{step.explanation}\n\n"
        f"[dim]Manual command:[/dim]\n  [green]{step.command}[/green]\n\n"
        f"[dim]Boundary:[/dim] {step.boundary}"
    )
    border = "red" if step.live else "cyan"
    console.print(
        Panel(body, title=f"Step {index}/{total} — {step.title}", border_style=border)
    )


def run_tutorial_steps(
    ctx: Any,
    client: Any,
    *,
    session_id: str,
    steps: list[TutorialStep],
    interactive: bool,
    allow_live: bool,
    terminal_task_statuses: Collection[str],
    progress_status_text: Callable[[dict[str, Any]], str],
    reader: TutorialReader | None = None,
) -> None:
    """Run tutorial steps without weakening the explicit live-action gate."""
    ask: TutorialReader = reader or (lambda prompt: console.input(prompt))
    console.print(
        Panel(
            "Walk through the Mt. Fuji delivery while learning the CLI one command "
            "at a time.\n"
            "Each step shows the manual command and the production boundary it crosses.\n"
            "[dim]Enter=run / s=skip / q=quit. Live SITL execution requires 'yes'.[/dim]",
            title="MissionOS CLI Tutorial (Mt. Fuji Delivery)",
            border_style="magenta",
        )
    )
    for index, step in enumerate(steps, 1):
        _print_tutorial_step(index, len(steps), step)
        if step.live:
            if interactive:
                answer = ask(
                    "[bold red]Live execution will start. Type 'yes' to run > [/bold red]"
                )
                if answer.strip().lower() != "yes":
                    console.print(
                        "[yellow]Skipped live execution. Run the command above "
                        "manually when you are ready.[/yellow]"
                    )
                    break
            elif not allow_live:
                console.print(
                    "[yellow]Skipped live execution because --yes was not set. "
                    "Use `missionos tutorial --auto --yes` for a full auto run.[/yellow]"
                )
                break
        elif interactive:
            decision = ask("[cyan]Enter=run / s=skip / q=quit > [/cyan]").strip().lower()
            if decision in {"q", "quit"}:
                console.print("[yellow]Tutorial stopped.[/yellow]")
                return
            if decision in {"s", "skip"}:
                console.print("[dim](skipped this step)[/dim]")
                continue
        try:
            if step.live:
                console.print(
                    "[bold red]Live execution started.[/bold red]"
                    "PX4/Gazebo AUTO missions can take several to many minutes. "
                    "Wait for the completion or failure panel."
                )
                with console.status(
                    "[red]Execute Live SITL is running... waiting for Gateway response[/red]",
                    spinner="dots",
                ) as status:
                    outcome = step.action(
                        ctx,
                        client,
                        session_id,
                        progress_callback=lambda latest: status.update(
                            f"[red]{progress_status_text(latest)}[/red]"
                        ),
                    )
            else:
                outcome = step.action(ctx, client, session_id)
            if step.live and outcome and outcome not in terminal_task_statuses:
                console.print(
                    Panel(
                        "The AUTO mission is still running.\n"
                        "Run `missionos job-status` again to track position, distance, and battery.\n"
                        "delivery_completion_claimed remains false until the task becomes completed or blocked.",
                        title="Live Execution Still Running",
                        border_style="yellow",
                    )
                )
                return
            if step.live and outcome in {"blocked", "failed", "cancelled", "canceled"}:
                console.print(
                    Panel(
                        "Execute Live SITL stopped before completion.\n"
                        "Run `missionos job-status` to inspect the latest state and artifact_root.",
                        title="Live Execution Stopped",
                        border_style="red",
                    )
                )
                return
        except click.ClickException as exc:
            console.print(f"[red]{exc.message}[/red]")
            console.print(
                "[yellow]Stopped at this step. Fix the condition and resume.[/yellow]"
            )
            return
    console.print(
        Panel(
            "Done. Each manual command shown above is the real operational CLI.\n"
            "You can run each command directly as `missionos <sub>` "
            "(for example, `missionos status`).\n"
            "Before starting a different mission, clear state with `missionos clear-state`.",
            title="Tutorial Complete",
            border_style="green",
        )
    )
