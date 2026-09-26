"""Local stationary-ship delivery fixture and opt-in SITL commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click


def _load_scenario(path: Path | None) -> Any:
    from src.runtime.ship_delivery import ShipDeliveryScenario

    try:
        material = json.loads(path.read_text(encoding="utf-8")) if path else {}
        if not isinstance(material, dict):
            raise ValueError("scenario must be a JSON object")
        return ShipDeliveryScenario.model_validate(material)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Invalid ship delivery scenario: {exc}") from exc


def _emit_report(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"Cannot write ship delivery report: {exc}") from exc
    click.echo(rendered)


@click.group("ship-delivery")
def ship_delivery_command() -> None:
    """Plan and run stationary-ship delivery fixtures or opt-in PX4/Gazebo SITL."""


@ship_delivery_command.command("plan")
@click.option(
    "--scenario",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Scenario JSON; omitted fields use the fixture defaults.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also save the JSON plan to this file.",
)
def plan_command(scenario: Path | None, output: Path | None) -> None:
    """Inspect the scenario and exact parent contract before a fixture run."""
    from src.runtime.ship_delivery import build_ship_delivery_contract

    parsed = _load_scenario(scenario)
    contract = build_ship_delivery_contract(parsed)
    _emit_report(
        {
            "schema_version": "missionos_ship_delivery_plan.v1",
            "execution_mode": "fixture_only",
            "scenario": parsed.model_dump(mode="json"),
            "parent_contract": contract.to_material(),
            "parent_contract_sha256": contract.parent_mission_sha256,
            "operator_approved": False,
            "physical_execution_invoked": False,
            "px4_runtime_invoked": False,
            "vla_invoked": False,
            "wam_invoked": False,
        },
        output,
    )


@ship_delivery_command.command("run")
@click.option(
    "--scenario",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Scenario JSON; omitted fields use the fixture defaults.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also save the JSON execution report to this file.",
)
@click.option(
    "--approve-fixture",
    is_flag=True,
    default=False,
    help="Approve this scenario for the local fixture only; grants no flight authority.",
)
def run_command(scenario: Path | None, output: Path | None, approve_fixture: bool) -> None:
    """Execute and verify one fixture run; blocked runs exit with status 1."""
    from src.runtime.ship_delivery import run_ship_delivery_fixture

    report = run_ship_delivery_fixture(_load_scenario(scenario), operator_approved=approve_fixture)
    _emit_report(report, output)
    if report.get("status") != "completed":
        raise click.exceptions.Exit(1)


@ship_delivery_command.command("px4-plan")
@click.option(
    "--scenario",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Scenario JSON; only offshore/urban distances and cruise altitude map to the PX4 plan.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also save the JSON plan export to this file.",
)
def px4_plan_command(scenario: Path | None, output: Path | None) -> None:
    """Export a PX4 mission tape whose live execution remains blocked."""
    from src.runtime.ship_delivery_px4 import build_stationary_ship_px4_plan

    parsed = _load_scenario(scenario)
    try:
        report = build_stationary_ship_px4_plan(
            offshore_distance_m=parsed.offshore_distance_m,
            urban_distance_m=parsed.urban_distance_m,
            cruise_altitude_m=parsed.cruise_altitude_m,
        )
    except ValueError as exc:
        raise click.ClickException(f"Cannot export ship delivery PX4 plan: {exc}") from exc
    _emit_report(report, output)


@ship_delivery_command.command("run-sitl")
@click.option("--scenario", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="New directory for world, raw telemetry, events and result.json.",
)
@click.option("--approve-sitl", is_flag=True, help="Approve this bounded local simulator mission.")
@click.option("--timeout-seconds", type=click.FloatRange(120, 1800), default=900, show_default=True)
@click.option(
    "--urban-case",
    type=click.Choice(
        ["short_clear", "long_block", "brake_stop", "static_center", "static_near", "static_clear"]
    ),
    default=None,
)
@click.option(
    "--urban-policy",
    type=click.Choice(
        [
            "always_wait",
            "always_detour",
            "constant_velocity",
            "onboard_wait",
            "onboard_detour",
            "onboard_velocity",
            "onboard_stopping",
            "onboard_uncertainty",
            "onboard_anwm_static",
            "onboard_vlm_action",
            "onboard_vlm_forecast",
        ]
    ),
    default="constant_velocity",
    show_default=True,
)
@click.option(
    "--capture-anwm",
    is_flag=True,
    help="Record aligned RGB/depth history in urban hold; no model dispatch.",
)
@click.option(
    "--vla-guard-smoke",
    is_flag=True,
    help="Check explicit VLA test proposals against fresh PX4/RGB; no VLA dispatch.",
)
@click.option(
    "--vla-executor-smoke",
    is_flag=True,
    help="Approve one bounded fixture VLA-format candidate through PX4; no model inference.",
)
@click.option(
    "--aerovla-url",
    default=None,
    help="Opt in to one fresh native AeroVLA proposal through an existing loopback HTTP tunnel.",
)
@click.option(
    "--anwm-url",
    default=None,
    help="Opt in to native ANWM stationary candidate views through an existing loopback tunnel.",
)
def run_sitl_command(
    scenario,
    output_dir,
    approve_sitl,
    timeout_seconds,
    urban_case,
    urban_policy,
    capture_anwm,
    vla_guard_smoke,
    vla_executor_smoke,
    aerovla_url,
    anwm_url,
):
    """Run one real PX4/Gazebo ship round trip; requires local Docker."""
    from src.runtime.ship_delivery_sitl import run_ship_delivery_sitl

    try:
        report = run_ship_delivery_sitl(
            _load_scenario(scenario),
            output_dir=output_dir,
            operator_approved=approve_sitl,
            timeout_s=timeout_seconds,
            urban_case=urban_case,
            urban_policy=urban_policy,
            capture_anwm=capture_anwm,
            vla_guard_smoke=vla_guard_smoke,
            vla_executor_smoke=vla_executor_smoke,
            aerovla_url=aerovla_url,
            anwm_url=anwm_url,
        )
    except (PermissionError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_report(report, None)
    if report["status"] != "completed":
        raise click.exceptions.Exit(1)


@ship_delivery_command.command("urban-screen")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def urban_screen_command(output):
    """Inspect analytic wait/detour headroom without Docker or models."""
    from src.runtime.ship_urban_decision import screen_urban_decisions

    _emit_report(screen_urban_decisions(), output)


@ship_delivery_command.command("wam-headroom")
@click.option(
    "--freeze",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Create a protocol receipt without scoring; refuses to overwrite.",
)
@click.option(
    "--protocol",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Previously frozen protocol receipt required for scoring.",
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def wam_headroom_command(freeze, protocol, output):
    """Screen analytic WAM headroom on CPU; no simulator, dispatch, or model calls."""
    from src.runtime.ship_wam_headroom import freeze_protocol, run_screen

    if freeze is not None and (protocol is not None or output is not None):
        raise click.UsageError("Use --freeze alone, then --protocol with optional --output")
    if freeze is None and protocol is None:
        raise click.UsageError("Freeze a protocol first, then pass --protocol")
    if output is not None and output.resolve() == protocol.resolve():
        raise click.UsageError("Output must not overwrite the frozen protocol")
    try:
        report = freeze_protocol(freeze) if freeze is not None else run_screen(protocol)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_report(report, output)


@ship_delivery_command.command("dynamic-routes-screen")
@click.option("--freeze", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--panel",
    type=click.Choice(["sparse", "windows"]),
    default=None,
    help="Choose a panel only when freezing; scoring uses the saved protocol.",
)
@click.option("--protocol", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def dynamic_routes_screen_command(freeze, panel, protocol, output):
    """CPU-only two-route prediction comparison with a prior protocol freeze."""
    from src.runtime.ship_dynamic_routes import freeze_protocol, run_screen

    if freeze is not None and (protocol is not None or output is not None):
        raise click.UsageError("Use --freeze alone, then --protocol with optional --output")
    if freeze is None and protocol is None:
        raise click.UsageError("Freeze a protocol first, then pass --protocol")
    if panel is not None and freeze is None:
        raise click.UsageError("--panel only applies to --freeze; scoring uses its saved protocol")
    if output is not None and output.resolve() == protocol.resolve():
        raise click.UsageError("Output must not overwrite the frozen protocol")
    try:
        report = (
            freeze_protocol(freeze, panel or "sparse")
            if freeze is not None
            else run_screen(protocol)
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_report(report, output)


@ship_delivery_command.command("uncertainty-screen")
@click.option("--freeze", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--protocol", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="New .json.gz file for full evidence; stdout is a compact summary.",
)
def uncertainty_screen_command(freeze, protocol, output):
    """CPU-only uncertainty gate and re-observation comparison; no model calls."""
    from src.runtime.ship_uncertainty_gate import freeze_protocol, run_screen

    if freeze is not None and (protocol is not None or output is not None):
        raise click.UsageError("Use --freeze alone, then --protocol with --output")
    if freeze is None and (protocol is None or output is None):
        raise click.UsageError("Scoring requires a frozen --protocol and a new --output .json.gz")
    try:
        report = freeze_protocol(freeze) if freeze is not None else run_screen(protocol, output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_report(report, None)


@ship_delivery_command.command("vla-audit")
@click.option(
    "--run-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--model-input", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--model-output", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def vla_audit_command(run_dir, model_input, model_output, output):
    """Reopen a saved native proposal; retrospective constraints only, no dispatch."""
    from src.runtime.ship_vla_audit import audit_native_proposal

    try:
        report = audit_native_proposal(run_dir, model_input, model_output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_report(report, output)


@ship_delivery_command.command("urban-compare")
@click.option(
    "--run-dir",
    multiple=True,
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def urban_compare_command(run_dir, output):
    """Reverify four saved SITL runs and compare paired urban action outcomes."""
    from src.runtime.ship_urban_comparison import compare_urban_runs

    try:
        result = compare_urban_runs(run_dir)
    except (ValueError, KeyError, OSError) as exc:
        raise click.ClickException(f"Urban comparison rejected: {exc}") from exc
    _emit_report(result, output)


@ship_delivery_command.command("onboard-compare")
@click.option(
    "--run-dir",
    multiple=True,
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def onboard_compare_command(run_dir, output):
    """Reverify six saved onboard flights; compare rules and a local VLM."""
    from src.runtime.ship_onboard_comparison import compare_onboard_runs

    try:
        report = compare_onboard_runs(run_dir)
    except (ValueError, KeyError, OSError) as exc:
        raise click.ClickException(f"Onboard comparison rejected: {exc}") from exc
    _emit_report(report, output)


@ship_delivery_command.command("urban-camera-screen")
@click.option("--output-dir", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--approve-gazebo",
    is_flag=True,
    help="Approve local RGB sensor capture; no aircraft or models.",
)
@click.option("--timeout-seconds", type=click.FloatRange(30, 600), default=180, show_default=True)
def urban_camera_screen_command(output_dir, approve_gazebo, timeout_seconds):
    """Capture matched RGB histories and screen simple wait/detour policies."""
    from src.runtime.ship_urban_camera_screen import run_camera_screen

    try:
        report = run_camera_screen(
            output_dir=output_dir, operator_approved=approve_gazebo, timeout_s=timeout_seconds
        )
    except (PermissionError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_report(report, None)
    if report["status"] != "screened":
        raise click.exceptions.Exit(1)


@ship_delivery_command.command("urban-camera-verify")
@click.option(
    "--run-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
def urban_camera_verify_command(run_dir, output):
    """Recompute image tracks and proposals without starting Gazebo."""
    from src.runtime.ship_urban_camera_screen import verify_camera_screen

    try:
        result = verify_camera_screen(run_dir)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise click.ClickException(f"Camera screen rejected: {exc}") from exc
    _emit_report(result, output)
