"""Proposal-only chat parsing, prompts, and local navigation state."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re

import click
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .chat_state import _load_state, _save_state
from .job_status import _as_float


CHAT_HELP_LINES = (
    "Type a MissionOS instruction, or a slash command.",
    'You can also start here with: missionos chat "Plan a delivery from Tokyo Station to Kawasaki Station"',
    "  /status                      — show operator surfaces",
    "  /approve /reject /revision   — operator review intents",
    "  /run /repair                 — execution and repair intents",
    "  /start-sitl [task_id]        — SITL startup boundary",
    "  /execute-sitl [task_id]      — Execute Live SITL boundary",
    "    --preflight-calibration    — approve one bounded OFFBOARD calibration",
    "                                 before obstacle flight",
    "                                interactive chat opens operate/watch/map companion terminals",
    "  /job-status [task_id]        — show stored/running task status",
    "  /map [task_id]               — open the live source-backed route map",
    "  /land <task_id>              — operator-approved LAND dispatch",
    "  /rtl <task_id>               — operator-approved RTL dispatch",
    "  /review-recovery [task_id]   — review with y=approve, d=defer, c=change",
    "  /approve-recovery [task_id]  — expert explicit approval fallback",
    "  /climb 45                    — operator-approved altitude adjustment",
    "  /speed 7                     — operator-approved speed adjustment",
    "  /reroute 120 -20 (45)        — operator-approved local reroute",
    "  /avoid 40 20 (45)            — operator-approved obstacle avoidance",
    "  /calibrate 40 20 45           — operator-approved SITL OFFBOARD calibration",
    "  高度を45mに上げて             — ask Recovery Agent for a proposal",
    "  障害物を避けて迂回して        — ask Recovery Agent for an avoidance proposal",
    "  /back                        — return to the previous chat decision point",
    "  /help /clear /quit",
    "Flow: Enter opens the suggested step; recovery review requires y (default defer).",
    "Editing: ↑/↓ history, Ctrl+R search, Tab completes /commands,",
    "         Esc then Enter inserts a newline, Enter submits, Ctrl+D quits.",
)

_RECOVERY_NATURAL_LANGUAGE_TRANSLATION = str.maketrans(
    "０１２３４５６７８９．，、ｍＭ",
    "0123456789.,,mM",
)
_RECOVERY_METRIC_NUMBER_RE = re.compile(
    r"(?P<value>-?\d+(?:[.,]\d+)?)\s*(?:m|meter|meters|metre|metres|メートル)?",
    re.IGNORECASE,
)

console = Console()


def _chat_help_panel() -> Panel:
    return Panel(
        Text("\n".join(CHAT_HELP_LINES)),
        title="MissionOS CLI",
        border_style="cyan",
    )


def _normalize_recovery_natural_language(raw: str) -> str:
    return raw.translate(_RECOVERY_NATURAL_LANGUAGE_TRANSLATION).lower()


def _recovery_natural_language_number(raw: str) -> float | None:
    match = _RECOVERY_METRIC_NUMBER_RE.search(
        _normalize_recovery_natural_language(raw).replace(",", ".")
    )
    if not match:
        return None
    return _as_float(match.group("value"))


def _recovery_natural_language_xy(raw: str) -> tuple[float, float] | None:
    text = _normalize_recovery_natural_language(raw).replace(",", ".")
    x_match = re.search(r"(?:target_)?x(?:_m)?\s*[=:]?\s*(-?\d+(?:\.\d+)?)", text)
    y_match = re.search(r"(?:target_)?y(?:_m)?\s*[=:]?\s*(-?\d+(?:\.\d+)?)", text)
    if x_match and y_match:
        x_value = _as_float(x_match.group(1))
        y_value = _as_float(y_match.group(1))
        if x_value is not None and y_value is not None:
            return x_value, y_value
    return None


def _looks_like_mission_planning_request(raw: str) -> bool:
    text = _normalize_recovery_natural_language(raw)
    if any(marker in text for marker in ("->", "→", "⇒")):
        return True
    if re.search(r"\S+\s*から\s*\S+\s*まで", text):
        return True
    if re.search(r"\bfrom\s+.+\bto\s+.+", text):
        return True
    physical_ai_terms = (
        "vla",
        "gr00t",
        "groot",
        "libero",
        "panda",
        "physical ai",
        "フィジカルai",
        "vlaミッション",
    )
    physical_ai_mission_terms = (
        "ミッション",
        "依頼",
        "実行",
        "動か",
        "管制",
        "監視",
        "mission",
        "run",
        "control",
        "monitor",
    )
    if any(term in text for term in physical_ai_terms) and any(
        term in text for term in physical_ai_mission_terms
    ):
        return True
    home_robot_terms = (
        "turtlebot3",
        "turtlebot",
        "nova carter",
        "nova-carter",
        "nova_carter",
        "isaac sim",
        "nvidia",
        "亀",
        "かめ",
        "タートルボット",
        "屋内",
        "家の中",
        "部屋",
    )
    mission_terms = (
        "配送",
        "配達",
        "届け",
        "目的地",
        "ルート",
        "走って",
        "一周",
        "patrol",
        "delivery",
        "deliver",
        "route",
    )
    return any(term in text for term in home_robot_terms) and any(
        term in text for term in mission_terms
    )


def _natural_language_recovery_request(raw: str) -> dict[str, Any] | None:
    """Parse a proposal request; this does not grant approval or dispatch authority."""
    text = _normalize_recovery_natural_language(raw)
    altitude_terms = (
        "高度",
        "上げ",
        "あげ",
        "上昇",
        "climb",
        "altitude",
        "higher",
        "raise",
    )
    obstacle_terms = (
        "障害物",
        "ビル",
        "建物",
        "障害",
        "回避",
        "避け",
        "avoid",
        "obstacle",
        "building",
    )
    reroute_terms = (
        "迂回",
        "ルート変更",
        "経路変更",
        "route change",
        "change route",
        "reroute",
        "detour",
    )
    has_altitude = any(term in text for term in altitude_terms)
    has_obstacle = any(term in text for term in obstacle_terms)
    has_reroute = any(term in text for term in reroute_terms)
    if not has_altitude and not has_obstacle and not has_reroute:
        return None
    action = (
        "avoid_obstacle"
        if has_obstacle
        else "reroute"
        if has_reroute
        else "adjust_altitude"
    )
    parameters: dict[str, Any] = {}
    if has_altitude or action in {"avoid_obstacle", "reroute"}:
        altitude = _recovery_natural_language_number(raw)
        if altitude is not None and (has_altitude or "alt" in text):
            parameters["target_altitude_m"] = altitude
    xy = _recovery_natural_language_xy(raw)
    if xy is not None:
        parameters["target_x_m"], parameters["target_y_m"] = xy
    return {
        "requested_action": action,
        "requested_parameters": parameters,
    }


def _recovery_command_number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    number = _as_float(value)
    if number is None:
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _recovery_proposal_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _recovery_proposal_command(payload: dict[str, Any]) -> str | None:
    summary = _recovery_proposal_summary(payload)
    action = str(
        payload.get("selected_bounded_action")
        or summary.get("selected_bounded_action")
        or ""
    )
    params = payload.get("proposed_parameters")
    params = params if isinstance(params, dict) else {}
    if not params:
        params = summary.get("proposed_parameters")
        params = params if isinstance(params, dict) else {}
    if action == "adjust_altitude":
        altitude = params.get("target_altitude_m")
        if altitude is None:
            return None
        return f"/climb {_recovery_command_number(altitude)}"
    if action == "adjust_speed":
        speed = params.get("target_speed_mps")
        if speed is None:
            return None
        return f"/speed {_recovery_command_number(speed)}"
    if action in {"reroute", "avoid_obstacle"}:
        x_value = params.get("target_x_m")
        y_value = params.get("target_y_m")
        if x_value is None or y_value is None:
            return None
        command = "/avoid" if action == "avoid_obstacle" else "/reroute"
        parts = [
            command,
            _recovery_command_number(x_value),
            _recovery_command_number(y_value),
        ]
        if params.get("target_altitude_m") is not None:
            parts.append(_recovery_command_number(params["target_altitude_m"]))
        return " ".join(parts)
    return None


def _print_recovery_agent_request_proposal(payload: dict[str, Any]) -> None:
    summary = _recovery_proposal_summary(payload)
    action = str(
        payload.get("selected_bounded_action")
        or summary.get("selected_bounded_action")
        or "operator_review"
    )
    status = str(payload.get("proposal_status") or summary.get("proposal_status") or "-")
    params = payload.get("proposed_parameters")
    params = params if isinstance(params, dict) else {}
    if not params:
        params = summary.get("proposed_parameters")
        params = params if isinstance(params, dict) else {}
    param_text = (
        ", ".join(
            f"{key}={_recovery_command_number(value)}"
            for key, value in sorted(params.items())
        )
        if params
        else "-"
    )
    lines = [
        f"proposal_status={status}",
        f"selected_bounded_action={action}",
        f"proposed_parameters={param_text}",
        "dispatch_authority=False · operator_approval_required=True",
        "physical_execution_invoked=False · progress_counted=False",
    ]
    proposal_id = str(
        payload.get("proposal_id") or summary.get("proposal_id") or ""
    )
    checkpoint_status = str(
        payload.get("checkpoint_status")
        or summary.get("checkpoint_status")
        or ""
    )
    if proposal_id:
        lines.insert(
            1,
            f"proposal_id={proposal_id} · checkpoint_status="
            f"{checkpoint_status or 'awaiting_operator_approval'}",
        )
    if status != "computed":
        lines.append(
            "No bounded maneuver was available from the current telemetry/context."
        )
    console.print(
        Panel(
            Text("\n".join(lines)),
            title="Recovery Agent Proposal",
            border_style="yellow" if status == "computed" else "red",
        )
    )


def _set_chat_suggestion(ctx: click.Context, *, raw: str, label: str) -> None:
    ctx.obj["missionos_chat_suggestion"] = {"raw": raw, "label": label}


def _clear_chat_suggestion(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_suggestion", None)


def _chat_back_stack(ctx: click.Context) -> list[dict[str, Any]]:
    stack = ctx.obj.get("missionos_chat_back_stack")
    if not isinstance(stack, list):
        stack = []
        ctx.obj["missionos_chat_back_stack"] = stack
    return stack


def _chat_back_available(ctx: click.Context) -> bool:
    stack = ctx.obj.get("missionos_chat_back_stack")
    return isinstance(stack, list) and bool(stack)


def _chat_suggestion(ctx: click.Context) -> dict[str, str]:
    suggestion = ctx.obj.get("missionos_chat_suggestion")
    if not isinstance(suggestion, dict):
        return {}
    raw = str(suggestion.get("raw") or "").strip()
    label = str(suggestion.get("label") or "").strip()
    if not raw or not label:
        return {}
    return {"raw": raw, "label": label}


def _chat_state_snapshot(ctx: click.Context) -> dict[str, Any]:
    state_path = ctx.obj.get("missionos_state_path")
    state = _load_state(state_path) if isinstance(state_path, Path) else {}
    return {"state": state, "suggestion": _chat_suggestion(ctx)}


def _push_chat_back_state(ctx: click.Context) -> None:
    stack = _chat_back_stack(ctx)
    snapshot = _chat_state_snapshot(ctx)
    if stack and stack[-1] == snapshot:
        return
    stack.append(snapshot)
    del stack[:-20]


def _clear_chat_back_stack(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_back_stack", None)


def _restore_chat_back_state(ctx: click.Context) -> bool:
    stack = _chat_back_stack(ctx)
    if not stack:
        return False
    snapshot = stack.pop()
    state_path = ctx.obj.get("missionos_state_path")
    state = snapshot.get("state") if isinstance(snapshot, dict) else {}
    if isinstance(state_path, Path):
        if isinstance(state, dict) and state:
            _save_state(state_path, state)
        else:
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
    suggestion = snapshot.get("suggestion") if isinstance(snapshot, dict) else {}
    if (
        isinstance(suggestion, dict)
        and str(suggestion.get("raw") or "").strip()
        and str(suggestion.get("label") or "").strip()
    ):
        _set_chat_suggestion(
            ctx,
            raw=str(suggestion["raw"]).strip(),
            label=str(suggestion["label"]).strip(),
        )
    else:
        _clear_chat_suggestion(ctx)
    return True


def _is_chat_back_request(raw: str) -> bool:
    return raw.strip().lower() in {
        "/back",
        "back",
        "go back",
        "previous",
        "undo",
        "戻る",
        "戻って",
        "前に戻る",
        "一つ前",
        "ひとつ前",
    }


def _chat_prompt_fragment(ctx: click.Context) -> HTML:
    suggestion = _chat_suggestion(ctx)
    if suggestion:
        back_hint = ", /back" if _chat_back_available(ctx) else ""
        return HTML(
            "\n<ansigreen><b>MissionOS</b></ansigreen> "
            f"<ansiyellow>[Enter={suggestion['label']}{back_hint}]</ansiyellow>"
            "<ansigreen><b>&gt;</b></ansigreen> "
        )
    if _chat_back_available(ctx):
        return HTML(
            "\n<ansigreen><b>MissionOS</b></ansigreen> "
            "<ansiyellow>[/back]</ansiyellow>"
            "<ansigreen><b>&gt;</b></ansigreen> "
        )
    return HTML("\n<ansigreen><b>MissionOS&gt;</b></ansigreen> ")


def _print_chat_followup(message: str) -> None:
    console.print(
        Panel(
            f"[bold]MissionOS[/bold]: {message}",
            title="Next",
            border_style="cyan",
        )
    )
