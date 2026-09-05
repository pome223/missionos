"""Proposal display and command-language support for `missionos operate`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import shlex

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.panel import Panel

from .map_model import (
    _normalize_turtlebot_robot_profile,
    _turtlebot_robot_label_from_profile,
)


PROPOSAL_REDISPLAY_SECONDS = 30.0


_OPERATOR_RECOVERY_ACTIONS = {
    "return_to_launch": "RTL",
    "land": "LAND",
    "adjust_altitude": "ADJUST ALTITUDE",
    "adjust_speed": "ADJUST SPEED",
    "reroute": "REROUTE",
    "avoid_obstacle": "AVOID OBSTACLE",
    "calibrate_offboard": "CALIBRATE OFFBOARD",
}


def _proposal_signature(
    proposal: dict[str, Any] | None,
) -> tuple[str, tuple[str, ...]] | None:
    if not proposal:
        return None
    return (proposal.get("action", ""), tuple(sorted(proposal.get("risks", []))))


@dataclass
class ProposalGate:
    """Re-display gate for recovery proposals.

    A dismissed proposal is hidden until the cooldown elapses, then re-surfaces.
    A different (escalated) proposal signature bypasses the cooldown and shows
    immediately so the operator is not kept waiting on a worse situation.
    """

    cooldown_seconds: float = PROPOSAL_REDISPLAY_SECONDS
    dismissed_signature: tuple[str, tuple[str, ...]] | None = None
    dismissed_at: float = 0.0

    def should_show(self, proposal: dict[str, Any] | None, now: float) -> bool:
        if not proposal:
            return False
        signature = _proposal_signature(proposal)
        if (
            self.dismissed_signature is not None
            and signature == self.dismissed_signature
        ):
            return (now - self.dismissed_at) >= self.cooldown_seconds
        return True

    def dismiss(self, proposal: dict[str, Any] | None, now: float) -> None:
        self.dismissed_signature = _proposal_signature(proposal)
        self.dismissed_at = now


def _render_action_panel(proposal: dict[str, Any], *, confirming: str | None) -> Panel:
    risks = ", ".join(proposal.get("risks", [])) or "-"
    parameters = proposal.get("parameters")
    parameter_text = (
        ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))
        if isinstance(parameters, dict) and parameters
        else "-"
    )
    lines = [
        f"[bold]Agent Proposal:[/bold] {proposal.get('action', '-')}   "
        f"[dim](status={proposal.get('status', '-')}; dispatch_authority=False)[/dim]",
        f"[dim]risk = {risks}[/dim]",
        f"[dim]params = {parameter_text}[/dim]",
        "",
    ]
    if confirming:
        label = _OPERATOR_RECOVERY_ACTIONS.get(confirming, confirming)
        lines.append(
            f"[bold red]Send {label}. Press[/bold red] [bold]y[/bold]"
            "[bold red] to execute; any other key cancels.[/bold red]"
        )
        border = "red"
    else:
        lines.append(
            "[green]Default: do nothing (no dispatch)[/green]   "
            "[dim]proposal will reappear in 30s[/dim]"
        )
        if str(proposal.get("action") or "") in {"return_to_launch", "land"}:
            lines.append(
                "  [bold]r[/bold]=approve RTL (requires y)   "
                "[bold]l[/bold]=approve LAND (requires y)   "
                "[bold]d[/bold]/Esc=view status   [bold]q[/bold]=quit"
            )
        else:
            lines.append(
                "  type [bold]climb <m>[/bold] / [bold]speed <m/s>[/bold] / "
                "[bold]reroute <x> <y> (alt)[/bold] / [bold]avoid <x> <y> (alt)[/bold]   "
                "[bold]d[/bold]/Esc=view status   [bold]q[/bold]=quit"
            )
        border = "yellow"
    return Panel("\n".join(lines), title="Operator Action", border_style=border)


_RECOVERY_RISK_LABELS = {
    "battery_projected_insufficient_for_route": "battery insufficient to complete route",
    "battery_projected_insufficient_for_return_home": "battery insufficient to return home",
    "terrain_clearance_below_minimum": "terrain clearance below minimum",
    "route_deviation_above_limit": "route deviation above limit",
    "telemetry_stale": "telemetry is stale",
    "obstacle_or_building_risk": "obstacle or building risk",
}


def _humanize_risks(risks: list[str]) -> str:
    if not risks:
        return "none"
    return ", ".join(_RECOVERY_RISK_LABELS.get(r, r) for r in risks)


@dataclass
class OperateConsoleCommand:
    kind: str
    action: str = ""
    parameters: dict[str, Any] | None = None
    assume_yes: bool = False


_OPERATE_CONSOLE_COMMANDS = (
    "status",
    "refresh",
    "wait",
    "help",
    "approve",
    "defer",
    "rtl",
    "land",
    "climb",
    "speed",
    "reroute",
    "avoid",
    "avoid-obstacle",
    "calibrate",
    "calibrate-offboard",
    "quit",
)


_OPERATE_RECOVERY_ACTION_ALIASES = {
    "rtl": "return_to_launch",
    "return": "return_to_launch",
    "return-to-launch": "return_to_launch",
    "return_to_launch": "return_to_launch",
    "land": "land",
    "climb": "adjust_altitude",
    "altitude": "adjust_altitude",
    "adjust-altitude": "adjust_altitude",
    "adjust_altitude": "adjust_altitude",
    "speed": "adjust_speed",
    "adjust-speed": "adjust_speed",
    "adjust_speed": "adjust_speed",
    "reroute": "reroute",
    "route": "reroute",
    "avoid": "avoid_obstacle",
    "avoid-obstacle": "avoid_obstacle",
    "avoid_obstacle": "avoid_obstacle",
    "calibrate": "calibrate_offboard",
    "calibrate-offboard": "calibrate_offboard",
    "calibrate_offboard": "calibrate_offboard",
}


_OPERATE_PARAMETER_ALIASES = {
    "alt": "target_altitude_m",
    "altitude": "target_altitude_m",
    "altitude_m": "target_altitude_m",
    "target_altitude": "target_altitude_m",
    "target_altitude_m": "target_altitude_m",
    "speed": "target_speed_mps",
    "speed_mps": "target_speed_mps",
    "target_speed": "target_speed_mps",
    "target_speed_mps": "target_speed_mps",
    "x": "target_x_m",
    "x_m": "target_x_m",
    "target_x": "target_x_m",
    "target_x_m": "target_x_m",
    "y": "target_y_m",
    "y_m": "target_y_m",
    "target_y": "target_y_m",
    "target_y_m": "target_y_m",
}


def _operate_console_help_panel(task_id: str, *, robot: str = "px4") -> Panel:
    if robot == "vla":
        return Panel(
            "\n".join(
                [
                    "[bold]Governed VLA controls[/bold]",
                    "  status | refresh        show frozen authority and latest evidence",
                    "  approve                 approve one exact post-episode retry proposal",
                    "  defer                  leave a failure proposal for operator review",
                    "  quit                   exit operate",
                    "",
                    "[dim]The official LIBERO runner exposes no external mid-episode "
                    "stop or recovery callback. Post-episode retry still requires "
                    "a recorded failure, an exact proposal, and separate human approval.[/dim]",
                ]
            ),
            title=f"Operate Commands · task={task_id}",
            border_style="magenta",
        )
    robot_profile = _normalize_turtlebot_robot_profile(robot)
    if robot_profile == "turtlebot3":
        robot_label = _turtlebot_robot_label_from_profile(robot_profile)
        lines = [
            "[bold]Operator controls[/bold]",
            "  While running: status shows current Nav2 evidence",
            "  When Recovery stops the robot:",
            "    approve               approve the displayed recovery proposal",
            "    defer                 keep stopped; create no dispatch authority",
            "    or type a change: 左へ大きく迂回して / 障害物を避けて / 引き返して",
            f"  status                  show the latest {robot_label} sim state",
            "  quit                    exit operate",
            "",
            "[dim]Dispatches still go through recovery-dispatch and require human confirmation. "
            f"{robot_label} operate does not expose land/climb/speed/RTL flight controls.[/dim]",
        ]
    elif robot_profile in {"turtlebot4", "nova_carter"}:
        robot_label = _turtlebot_robot_label_from_profile(robot_profile)
        lines = [
            "[bold]Operator controls[/bold]",
            "  While running: status shows current Nav2 evidence",
            "  When Recovery stops the robot:",
            "    approve               approve the displayed recovery proposal",
            "    defer                 keep stopped; create no dispatch authority",
            f"  status                  show the latest {robot_label} sim state",
            "  quit                    exit operate",
            "",
            "[dim]Natural-language checkpoint revision is not verified for this "
            "robot profile. Dispatches still require human confirmation. "
            f"{robot_label} operate does not expose land/climb/speed/RTL flight "
            "controls.[/dim]",
        ]
    else:
        lines = [
            "[bold]Commands[/bold]",
            "  status | refresh        show the latest recovery/telemetry state",
            "  describe a change       ask Recovery Agent for a bounded proposal",
            "                            e.g. 大きく右へ迂回して障害物を避けて",
            "  rtl                     request return-to-launch",
            "  land                    request land",
            "  climb 45                request altitude adjustment to 45 m above home",
            "  speed 7                 request speed adjustment to 7 m/s",
            "  reroute 120 -20 (45)    request local NED x/y target, optional altitude",
            "  avoid 40 20 (45)        request obstacle-avoidance target",
            "  quit                    exit operate",
            "",
            "[dim]Natural language creates a proposal only. The proposed concrete "
            "command must be reviewed and separately confirmed before dispatch.[/dim]",
        ]
    return Panel(
        "\n".join(lines),
        title=f"Operate Commands · task={task_id}",
        border_style="cyan",
    )


def _build_operate_session(history_path: Path) -> PromptSession[str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.touch(exist_ok=True)
    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(list(_OPERATE_CONSOLE_COMMANDS), ignore_case=True),
        complete_while_typing=True,
        multiline=False,
        mouse_support=False,
    )


def _float_operate_argument(raw: Any, *, label: str) -> float:
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise click.ClickException(f"{label} must be a number: {raw}") from exc


def _normalize_operate_parameter_key(raw: str) -> str:
    key = raw.strip().lstrip("-").replace("-", "_")
    return _OPERATE_PARAMETER_ALIASES.get(key, key)


def _parse_operate_console_parameters(
    action: str,
    tokens: list[str],
) -> tuple[dict[str, Any], bool]:
    assume_yes = False
    values: dict[str, Any] = {}
    positional: list[str] = []
    for token in tokens:
        if token == "--yes":
            assume_yes = True
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            key = _normalize_operate_parameter_key(key)
            if not key:
                raise click.ClickException(f"parameter key is empty: {token}")
            values[key] = _float_operate_argument(value, label=key)
            continue
        positional.append(token)

    if action in {"return_to_launch", "land"}:
        if values or positional:
            raise click.ClickException(f"{action} does not accept parameters")
        return {}, assume_yes

    if action == "adjust_altitude":
        if positional:
            values["target_altitude_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_altitude_m",
            )
        if positional:
            raise click.ClickException("climb accepts one altitude value")
        if "target_altitude_m" not in values:
            raise click.ClickException("usage: climb <altitude_m>")
        return values, assume_yes

    if action == "adjust_speed":
        if positional:
            values["target_speed_mps"] = _float_operate_argument(
                positional.pop(0),
                label="target_speed_mps",
            )
        if positional:
            raise click.ClickException("speed accepts one speed value")
        if "target_speed_mps" not in values:
            raise click.ClickException("usage: speed <speed_mps>")
        return values, assume_yes

    if action in {"reroute", "avoid_obstacle", "calibrate_offboard"}:
        if positional:
            values["target_x_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_x_m",
            )
        if positional:
            values["target_y_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_y_m",
            )
        if positional:
            values["target_altitude_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_altitude_m",
            )
        if positional:
            raise click.ClickException(
                "reroute/avoid/calibrate accepts x y and altitude"
            )
        if "target_x_m" not in values or "target_y_m" not in values:
            verb = (
                "avoid"
                if action == "avoid_obstacle"
                else "calibrate"
                if action == "calibrate_offboard"
                else "reroute"
            )
            raise click.ClickException(f"usage: {verb} <target_x_m> <target_y_m> [altitude_m]")
        if action == "calibrate_offboard" and "target_altitude_m" not in values:
            raise click.ClickException(
                "usage: calibrate <target_x_m> <target_y_m> <altitude_m>"
            )
        return values, assume_yes

    raise click.ClickException(f"unsupported recovery action: {action}")


def _parse_operate_console_command(raw: str) -> OperateConsoleCommand:
    text = raw.strip()
    if not text:
        return OperateConsoleCommand(kind="refresh")
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise click.ClickException(f"could not parse operate command: {exc}") from exc
    if not tokens:
        return OperateConsoleCommand(kind="refresh")
    command = tokens[0].lower()
    if command in {"q", "quit", "exit"}:
        return OperateConsoleCommand(kind="quit")
    if command in {"?", "help"}:
        return OperateConsoleCommand(kind="help")
    if command in {"approve", "y", "yes"}:
        return OperateConsoleCommand(kind="approve_pending")
    if command in {"defer", "d", "hold"}:
        return OperateConsoleCommand(kind="defer_pending")
    if command in {"status", "refresh", "wait", "sleep", "back"}:
        return OperateConsoleCommand(kind="refresh")
    if command == "recover":
        if len(tokens) < 2:
            raise click.ClickException("usage: recover <action> [parameters]")
        command = tokens[1].lower()
        tokens = [tokens[0], *tokens[2:]]
    action = _OPERATE_RECOVERY_ACTION_ALIASES.get(command)
    if not action:
        raise click.ClickException(
            "unknown operate command; type `help` for available commands"
        )
    parameters, assume_yes = _parse_operate_console_parameters(action, tokens[1:])
    return OperateConsoleCommand(
        kind="dispatch",
        action=action,
        parameters=parameters,
        assume_yes=assume_yes,
    )
