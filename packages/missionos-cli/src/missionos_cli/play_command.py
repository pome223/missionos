"""Interactive ``missionos play`` deterministic what-if lab."""

from __future__ import annotations

from pathlib import Path

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


PLAY_STATUS_STYLE = {"ready": "green", "warning": "yellow", "blocked": "red"}

console = Console()


def _play_exposure_style(exposure: str) -> str:
    return {"low": "green", "medium": "yellow", "high": "red"}.get(
        exposure, "white"
    )


def _play_plan_table(scenario, plan) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(justify="right", style="bold")
    table.add_column()
    route = scenario.route(plan.route_name)
    table.add_row("Route", f"{plan.route_name} — {route.description}")
    table.add_row("Distance", f"{plan.route_distance_m:,.0f} m (round trip modelled)")
    table.add_row("Altitude", f"{plan.knobs.altitude_m:,.0f} m MSL")
    margin_style = "green" if plan.clearance_margin_m >= 0 else "red"
    table.add_row(
        "Terrain clearance",
        f"{plan.clearance_m:,.0f} m  "
        f"([{margin_style}]{plan.clearance_margin_m:+.0f} m vs "
        f"{plan.knobs.min_clearance_rule_m:.0f} m rule[/{margin_style}])",
    )
    exposure_style = _play_exposure_style(plan.wind_exposure)
    table.add_row(
        "Wind exposure",
        f"[{exposure_style}]{plan.wind_exposure}[/{exposure_style}] "
        f"({plan.effective_wind_mps:.1f} m/s effective)",
    )
    reserve_style = "green" if plan.return_feasible else "red"
    table.add_row(
        "Return reserve",
        f"[{reserve_style}]{plan.return_reserve_wh:,.0f} Wh "
        f"({plan.battery_reserve_fraction * 100:.0f}% of pack)[/{reserve_style}]",
    )
    table.add_row("Return feasible", "yes" if plan.return_feasible else "[red]no[/red]")
    if plan.risk_labels:
        table.add_row("Risk", ", ".join(plan.risk_labels))
    return table


def _render_play_plan(scenario, plan) -> None:
    style = PLAY_STATUS_STYLE.get(plan.status, "white")
    body = Group(
        _play_plan_table(scenario, plan),
        Text(""),
        Text.from_markup(
            f"[bold]MissionOS proposes[/bold] → "
            f"[{style}]{plan.recommendation.value}[/{style}]\n"
            f"{plan.recommendation_reason}"
        ),
    )
    console.print(
        Panel(
            body,
            title=f"[{style}]{scenario.title} — status: {plan.status}[/{style}]",
            border_style=style,
        )
    )


def _render_play_compare(scenario, plan_a, plan_b, compare_plans) -> None:
    delta = compare_plans(plan_a, plan_b)
    table = Table(title="Compare: baseline → current", box=None)
    table.add_column("Metric", style="bold")
    table.add_column("Baseline", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Δ", justify="right")

    def signed(value: float, unit: str, good_when_positive: bool) -> str:
        good = value >= 0 if good_when_positive else value <= 0
        style = "green" if good else "red"
        return f"[{style}]{value:+,.0f} {unit}[/{style}]"

    table.add_row(
        "Clearance",
        f"{plan_a.clearance_m:,.0f} m",
        f"{plan_b.clearance_m:,.0f} m",
        signed(delta.clearance_m, "m", True),
    )
    table.add_row(
        "Return reserve",
        f"{plan_a.return_reserve_wh:,.0f} Wh",
        f"{plan_b.return_reserve_wh:,.0f} Wh",
        signed(delta.return_reserve_wh, "Wh", True),
    )
    table.add_row(
        "Effective wind",
        f"{plan_a.effective_wind_mps:.1f} m/s",
        f"{plan_b.effective_wind_mps:.1f} m/s",
        signed(delta.effective_wind_mps, "m/s", False),
    )
    table.add_row(
        "Distance",
        f"{plan_a.route_distance_m:,.0f} m",
        f"{plan_b.route_distance_m:,.0f} m",
        signed(delta.route_distance_m, "m", False),
    )
    console.print(table)


def _render_play_weather(scenario, forecast, knobs) -> None:
    """Show the real forecast and the SITL realism env it would forward."""
    from src.runtime.missionos_play_sitl_conditions import build_sitl_conditions

    agl = max(0.0, knobs.altitude_m - scenario.takeoff_elevation_m)
    conditions = build_sitl_conditions(
        forecast, flight_agl_m=agl, payload_kg=knobs.payload_kg
    )

    forecast_table = Table(title="Real weather (Open-Meteo)", box=None)
    forecast_table.add_column("Time (UTC)", style="bold")
    forecast_table.add_column("Surface wind", justify="right")
    forecast_table.add_column("Gust", justify="right")
    forecast_table.add_column("Dir", justify="right")
    cur = forecast.current
    forecast_table.add_row(
        f"{cur.valid_at} (now)",
        f"{cur.wind_speed_mps} m/s",
        f"{cur.wind_gust_mps} m/s",
        f"{cur.wind_direction_deg}°",
    )
    for sample in forecast.hourly[:6]:
        forecast_table.add_row(
            sample.valid_at,
            f"{sample.wind_speed_mps} m/s",
            f"{sample.wind_gust_mps} m/s",
            f"{sample.wind_direction_deg}°",
        )
    console.print(forecast_table)

    env_table = Table(
        title=f"Forwarded to SITL @ {agl:,.0f} m AGL (modelled altitude profile)",
        box=None,
    )
    env_table.add_column("Realism env", style="bold")
    env_table.add_column("Value", justify="right")
    for key, value in conditions.realism_env.items():
        env_table.add_row(key.replace("MISSION_DESIGNER_REALISM_", ""), value)
    console.print(env_table)

    matrix = conditions.capability_matrix
    notes = ", ".join(matrix.get("approximation_reasons", [])) or "none"
    console.print(
        f"[dim]real=forwarded surface wind/gust/direction · "
        f"modelled=altitude profile · approximations: {notes}\n"
        f"Final Gazebo/PX4 application is recorded by the runner's own "
        f"capability matrix at flight time.[/dim]"
    )


def _render_play_flight_result(result) -> None:
    style = "green" if result.status == "completed" else "red"
    table = Table(title="Live PX4/Gazebo SITL flight", box=None)
    table.add_column("Evidence", style="bold")
    table.add_column("Value")
    table.add_row("Status", f"[{style}]{result.status}[/{style}]")
    table.add_row(
        "Takeoff observed", "yes" if result.takeoff_observed else "[red]no[/red]"
    )
    table.add_row("Wind updates", str(len(result.wind_steps)))
    if result.wind_steps:
        latest = result.wind_steps[-1]
        table.add_row(
            "Latest wind",
            f"{latest.wind_mps:.2f} m/s from {latest.bearing_from_deg:.0f}° "
            f"@ {latest.altitude_agl_m:.1f} m AGL",
        )
        table.add_row(
            "Latest force",
            f"east={latest.force_east_n:.2f} N, north={latest.force_north_n:.2f} N",
        )
    recovery = result.recovery_agent_result or {}
    table.add_row("Recovery agent", str(recovery.get("runtime_status") or "not_run"))
    if recovery.get("blocking_reasons"):
        table.add_row("Recovery blocking", ", ".join(recovery["blocking_reasons"]))
    if result.blocking_reasons:
        table.add_row("Blocking", ", ".join(result.blocking_reasons))
    console.print(table)
    console.print(
        "[dim]This is a live simulator takeoff and wind-disturbance run. "
        "It does not claim delivery completion, physical execution, or progress.[/dim]"
    )


def _play_help_panel() -> Panel:
    return Panel(
        Text.from_markup(
            "[bold]MissionOS play — you are the controller.[/bold]\n"
            "Turn the knobs, read how the situation changes, take MissionOS's\n"
            "recommendation, and approve. Going higher is never a free win.\n\n"
            "[bold]Commands[/bold]\n"
            "  altitude <m>            set flight altitude (MSL)\n"
            "  route direct|east|west  pick a corridor\n"
            "  wind <m/s>              declare wind speed\n"
            "  payload <kg>            set payload weight\n"
            "  rule min-clearance <m>  set the safety clearance rule\n"
            "  weather                 show real weather + the SITL env it forwards\n"
            "  show                    re-render the current plan\n"
            "  compare                 compare baseline → current\n"
            "  approve                 accept current as the new baseline (human gate)\n"
            "  fly                     run live PX4/Gazebo takeoff + wind disturbance\n"
            "  help / quit"
        ),
        title="play",
        border_style="cyan",
    )


def _play_apply_command(knobs, raw: str, valid_routes):
    """Return ``(new_knobs, message)`` for one deterministic lab command."""
    parts = raw.split()
    if not parts:
        return knobs, ""
    verb = parts[0].lower()
    try:
        if verb in {"altitude", "alt"}:
            return knobs.with_(altitude_m=float(parts[1])), ""
        if verb == "route":
            choice = parts[1].lower()
            if choice not in valid_routes:
                return knobs, (
                    f"[red]Unknown route '{choice}'.[/red] "
                    f"Choices: {', '.join(valid_routes)}"
                )
            return knobs.with_(route=choice), ""
        if verb == "wind":
            return knobs.with_(declared_wind_mps=float(parts[1])), ""
        if verb == "payload":
            return knobs.with_(payload_kg=float(parts[1])), ""
        if verb == "rule":
            value = parts[-1]
            return knobs.with_(min_clearance_rule_m=float(value)), ""
    except (IndexError, ValueError):
        return knobs, f"[red]Could not parse:[/red] {raw}"
    return knobs, ""


def run_play_command(
    *,
    destination: tuple[str, ...],
    scenario_key: str | None,
    real_weather: bool,
    forecast_hours: int,
    flight_duration: float,
    wind_step: float,
    battery_coupling: bool,
    gps_denied: bool,
    history_path: Path,
) -> None:
    """Run the interactive lab without granting approval or flight implicitly."""
    from src.runtime.missionos_play_scenario import DEFAULT_SCENARIO_KEY, load_scenario
    from src.runtime.missionos_play_session import (
        PlayKnobs,
        compare_plans,
        evaluate_plan,
    )
    from src.runtime.missionos_play_sitl_conditions import wind_at_altitude
    from src.runtime.missionos_play_weather import (
        fetch_weather_forecast,
        profile_wind_at,
    )

    try:
        scenario = load_scenario(scenario_key or DEFAULT_SCENARIO_KEY)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    if destination:
        stated = " ".join(destination)
        console.print(
            f"[yellow]Custom destinations are not planned yet.[/yellow] "
            f"'{stated}' is noted but not routed — play runs the bundled "
            f"scenario [bold]'{scenario.title}'[/bold] (pick one with --scenario)."
        )

    forecast = None
    if real_weather:
        with console.status(
            "[cyan]Fetching real weather + altitude profile (Open-Meteo)...[/cyan]"
        ):
            forecast = fetch_weather_forecast(
                scenario.takeoff_lat,
                scenario.takeoff_lon,
                forecast_hours=forecast_hours,
                with_profile=True,
            )
        if forecast.source_unavailable:
            console.print(
                "[yellow]Real weather unavailable; falling back to bundled "
                f"ambient wind.[/yellow] ({forecast.provider_response_status})"
            )
            forecast = None
        else:
            console.print(
                f"[green]Real weather:[/green] surface wind "
                f"{forecast.current.wind_speed_mps} m/s, gust "
                f"{forecast.current.wind_gust_mps} m/s, dir "
                f"{forecast.current.wind_direction_deg}° "
                f"({len(forecast.hourly)} forecast hours)"
            )

    wind_pinned = {"value": False}

    def resolve_knobs(current: PlayKnobs) -> PlayKnobs:
        """Drive wind from real weather unless the operator pinned it."""
        if forecast is None or wind_pinned["value"]:
            return current
        agl = max(0.0, current.altitude_m - scenario.takeoff_elevation_m)
        real = profile_wind_at(forecast, agl)
        if real is None:
            surface = forecast.current.wind_speed_mps or scenario.ambient_wind_mps
            real = wind_at_altitude(surface, agl)
        return current.with_(declared_wind_mps=real)

    def eval_current(current: PlayKnobs):
        return evaluate_plan(scenario, resolve_knobs(current))

    knobs = PlayKnobs(altitude_m=3000.0, route="direct", payload_kg=1.0)
    baseline = eval_current(knobs)

    console.print(_play_help_panel())
    _render_play_plan(scenario, baseline)

    history_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(
            [
                "altitude",
                "route",
                "wind",
                "payload",
                "rule min-clearance",
                "weather",
                "show",
                "compare",
                "approve",
                "fly",
                "help",
                "quit",
            ],
            ignore_case=True,
        ),
    )

    while True:
        try:
            raw = session.prompt(HTML("<ansicyan>play></ansicyan> ")).strip()
        except KeyboardInterrupt:
            console.print("[yellow](Ctrl+C — type quit or Ctrl+D to exit)[/yellow]")
            continue
        except EOFError:
            break
        if not raw:
            continue
        verb = raw.split()[0].lower()
        if verb in {"quit", "exit"}:
            break
        if verb == "help":
            console.print(_play_help_panel())
            continue
        if verb == "weather":
            if forecast is None:
                console.print(
                    "[yellow]No real weather loaded.[/yellow] Start with "
                    "[bold]missionos play --real-weather[/bold] to pull live "
                    "Open-Meteo conditions for this scenario."
                )
                continue
            _render_play_weather(scenario, forecast, resolve_knobs(knobs))
            continue
        if verb in {"show", "status"}:
            _render_play_plan(scenario, eval_current(knobs))
            continue
        if verb == "compare":
            _render_play_compare(scenario, baseline, eval_current(knobs), compare_plans)
            continue
        if verb == "approve":
            plan = eval_current(knobs)
            if plan.status == "blocked":
                console.print(
                    "[red]Cannot approve a blocked plan.[/red] "
                    "Resolve the risks above first."
                )
                continue
            baseline = plan
            console.print(
                "[green]Approved.[/green] Recorded as the new baseline (human gate). "
                "Rules still constrain dispatch; approval is not flight."
            )
            continue
        if verb == "fly":
            if forecast is None:
                console.print(
                    "[yellow]Live play flight needs real weather for the wind "
                    "driver.[/yellow] Restart with "
                    "[bold]missionos play --real-weather[/bold]."
                )
                continue
            plan = eval_current(knobs)
            if plan.status == "blocked":
                console.print(
                    "[red]Cannot dispatch a blocked play plan.[/red] "
                    "Resolve the risks first."
                )
                continue
            from src.runtime.missionos_play_live_sitl import run_play_live_sitl

            console.print(
                "[cyan]Starting live PX4/Gazebo SITL, taking off, and injecting "
                "time/altitude-varying wind...[/cyan]"
            )
            with console.status("[cyan]Live SITL flight in progress...[/cyan]"):
                result = run_play_live_sitl(
                    scenario=scenario,
                    forecast=forecast,
                    duration_s=flight_duration,
                    step_s=wind_step,
                    battery_coupling=battery_coupling,
                    gps_denied=gps_denied,
                )
            _render_play_flight_result(result)
            continue
        if verb == "wind":
            wind_pinned["value"] = True
        knobs, message = _play_apply_command(knobs, raw, set(scenario.routes))
        if message:
            console.print(message)
            continue
        _render_play_plan(scenario, eval_current(knobs))
