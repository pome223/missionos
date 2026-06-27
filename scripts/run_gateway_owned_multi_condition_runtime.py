#!/usr/bin/env python3
"""Run the C5b Gateway-owned multi-condition MissionOS runtime chain.

This runner is the first-class E2E entrypoint for the C5b milestone. It runs or
consumes a live multi-condition supervisor runtime artifact, derives the Gateway
mission-session/lifecycle refs, invokes the loopback Gateway supervisor-process
probe route, and then materializes source-bound Gateway observation and recovery
process evidence. The final probe may claim `full_gateway_runtime_loop=true`
only when all of those boundaries agree.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from scripts.audit_mission_os_multi_condition_supervisor_runtime import (
    DEFAULT_DRIFT_THRESHOLD_M,
    DEFAULT_WIND_DIRECTION_DEG,
    DEFAULT_WIND_MPS,
    _prepare_source_backed_terrain_world,
)
from scripts import smoke_digital_twin_world_bound_sitl_e2e as terrain_world_sitl
from scripts.probe_gateway_supervisor_process_boundary import _run_probe
from scripts.run_gateway_live_runtime_probe import (
    PROBE_STATUS_OBSERVED,
    GatewayRuntimeProbeError,
    _read_json,
    _run_live_supervisor_runtime,
    _write_stage_artifact,
    build_gateway_live_runtime_probe_chain,
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _terrain_world_sitl_agent(
    *,
    output_dir: Path,
    run_live_sitl: bool,
) -> tuple[dict[str, Any], str | None]:
    """Load source-backed terrain in a real PX4/Gazebo world-bound agent.

    The Gateway-owned runtime uses this as a sibling agent to the horizontal
    Form 3 supervisor. It proves terrain world materialization and PX4/Gazebo
    startup/readback, not terrain-collision flight physics.
    """

    agent_dir = output_dir / "terrain_world_sitl_agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    if not run_live_sitl:
        artifact = {
            "schema_version": "gateway_terrain_world_sitl_agent.v1",
            "terrain_world_sitl_agent_observed": False,
            "terrain_agent_status": "not_invoked_existing_source_runtime_mode",
            "source_backed_terrain": False,
            "terrain_flight_physics_affected": False,
            "terrain_collision_mode": "",
            "runtime_invocation_evidence": {},
            "progress_counted": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "dispatch_authority_created": False,
        }
        _write_json(agent_dir / "gateway_terrain_world_sitl_agent.json", artifact)
        return artifact, None

    terrain = _prepare_source_backed_terrain_world(agent_dir)
    prepared_world_path = Path(terrain["prepared_world_path"])
    world_root = prepared_world_path.parent.parent
    started_at = datetime.now(timezone.utc).isoformat()
    stopped_at = started_at
    command: list[str] = []
    pid = 0
    logs = ""
    startup_ok = False
    exit_code = 0
    error: str | None = None
    try:
        command, pid, logs, startup_ok = terrain_world_sitl._start_world_bound_container(
            world_root,
            prepared_world_path,
        )
    except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
        error = f"terrain_world_sitl_agent_error:{exc}"
    finally:
        try:
            final_logs, exit_code = terrain_world_sitl._stop_world_bound_container()
            if final_logs:
                logs = final_logs
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            error = error or f"terrain_world_sitl_agent_stop_error:{exc}"
        stopped_at = datetime.now(timezone.utc).isoformat()

    stdout_path = agent_dir / "px4_gazebo_terrain_world.stdout.log"
    stdout_path.write_text(logs, encoding="utf-8")
    world_text = prepared_world_path.read_text(encoding="utf-8")
    heightmap_dir = world_root / "heightmaps"
    heightmap_files = sorted(heightmap_dir.glob("*")) if heightmap_dir.exists() else []
    terrain_model_present = "digital_twin_heightmap_terrain" in world_text
    terrain_visual_present = "terrain_visual" in world_text
    terrain_artifact_used = bool(
        terrain_model_present and terrain_visual_present and heightmap_files
    )
    px4_gazebo_startup_observed = (
        "Gazebo world is ready" in logs
        and "Startup script returned successfully" in logs
    )
    startup_ok = bool(startup_ok or px4_gazebo_startup_observed)
    observed = bool(
        startup_ok
        and terrain_artifact_used
        and terrain.get("source_backed_terrain") is True
        and terrain.get("terrain_sampling_mode") == "anchor_point_sampled"
    )
    artifact = {
        "schema_version": "gateway_terrain_world_sitl_agent.v1",
        "terrain_world_sitl_agent_observed": observed,
        "terrain_agent_status": (
            "source_backed_terrain_world_loaded_in_px4_gazebo"
            if observed
            else "blocked"
        ),
        "source_backed_terrain": bool(terrain.get("source_backed_terrain")),
        "terrain_provider_response_status": terrain.get(
            "terrain_provider_response_status", ""
        ),
        "terrain_sampling_mode": terrain.get("terrain_sampling_mode", ""),
        "terrain_vertical_reference": terrain.get("terrain_vertical_reference", ""),
        "terrain_collision_mode": terrain.get("terrain_collision_mode", ""),
        "terrain_flight_physics_affected": False,
        "terrain_physics_boundary_note": (
            "The source-backed terrain world was loaded by a Gateway runtime "
            "agent. The horizontal-route Form 3 supervisor still uses the "
            "established recovery smoke; this is not a terrain-collision "
            "flight-physics claim."
        ),
        "terrain_model_present": terrain_model_present,
        "terrain_visual_present": terrain_visual_present,
        "terrain_collision_present": "terrain_collision" in world_text,
        "heightmap_file_count": len(heightmap_files),
        "terrain_artifact_used": terrain_artifact_used,
        "prepared_world_path": str(prepared_world_path),
        "prepared_world_sha256": _sha256_file(prepared_world_path),
        "terrain_world_source_ref": terrain.get("terrain_world_source_ref", ""),
        "source_backed_target_latitude": terrain.get("source_backed_target_latitude"),
        "source_backed_target_longitude": terrain.get("source_backed_target_longitude"),
        "process_pid": pid,
        "process_exit_code": exit_code,
        "startup_ok": startup_ok,
        "px4_gazebo_startup_observed": px4_gazebo_startup_observed,
        "error": error,
        "runtime_invocation_evidence": {
            "schema_version": "runtime_invocation_evidence.v1",
            "command_argv": command,
            "process_pid": pid,
            "stdout_path": str(stdout_path),
            "stdout_sha256": _sha256_file(stdout_path),
            "exit_code": exit_code,
            "started_at": started_at,
            "stopped_at": stopped_at,
        },
        "progress_counted": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "dispatch_authority_created": False,
    }
    _write_json(agent_dir / "gateway_terrain_world_sitl_agent.json", artifact)
    return artifact, error


def _source_runtime_path(
    *,
    run_live_sitl: bool,
    source_runtime_artifact: Path | None,
    output_dir: Path,
    wind_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    timeout_seconds: int,
) -> tuple[Path, str | None]:
    if not run_live_sitl:
        if source_runtime_artifact is None:
            raise GatewayRuntimeProbeError("source_runtime_artifact_required")
        return source_runtime_artifact, None

    runtime_path, live_error = _run_live_supervisor_runtime(
        output_dir=output_dir / "source_runtime",
        wind_mps=wind_mps,
        wind_direction_deg=wind_direction_deg,
        drift_threshold_m=drift_threshold_m,
        timeout_seconds=timeout_seconds,
    )
    if runtime_path is None:
        raise GatewayRuntimeProbeError(live_error or "source runtime missing")
    return runtime_path, live_error


def run_gateway_owned_multi_condition_runtime(
    *,
    output_dir: Path,
    run_live_sitl: bool,
    source_runtime_artifact: Path | None = None,
    wind_mps: float = DEFAULT_WIND_MPS,
    wind_direction_deg: float = DEFAULT_WIND_DIRECTION_DEG,
    drift_threshold_m: float = DEFAULT_DRIFT_THRESHOLD_M,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Run the integrated C5b chain and return the final probe artifact."""

    output_dir.mkdir(parents=True, exist_ok=False)
    terrain_agent, terrain_error = _terrain_world_sitl_agent(
        output_dir=output_dir,
        run_live_sitl=run_live_sitl,
    )
    source_runtime_artifact_path, live_error = _source_runtime_path(
        run_live_sitl=run_live_sitl,
        source_runtime_artifact=source_runtime_artifact,
        output_dir=output_dir,
        wind_mps=wind_mps,
        wind_direction_deg=wind_direction_deg,
        drift_threshold_m=drift_threshold_m,
        timeout_seconds=timeout_seconds,
    )
    combined_live_error = live_error or terrain_error

    preflight_dir = output_dir / "preflight_gateway_chain"
    preflight_dir.mkdir()
    preflight = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact_path,
        probe_dir=preflight_dir,
        live_probe_invoked=run_live_sitl,
        live_runtime_error=combined_live_error,
    )
    preflight_path = _write_stage_artifact(
        output_dir,
        "preflight_gateway_live_runtime_probe.json",
        preflight,
    )
    preflight["gateway_live_runtime_probe_artifact_path"] = str(preflight_path)
    _write_json(preflight_path, preflight)

    gateway_session = _read_json(Path(preflight["gateway_mission_session_artifact_path"]))
    lifecycle = _read_json(Path(preflight["gateway_supervisor_lifecycle_artifact_path"]))
    boundary_dir = output_dir / "gateway_supervisor_process_boundary"
    boundary = asyncio.run(
        _run_probe(
            output_dir=boundary_dir,
            source_runtime_artifact=source_runtime_artifact_path,
            gateway_mission_session_ref=str(
                gateway_session.get("gateway_mission_session_ref") or ""
            ),
            supervisor_session_ref=str(gateway_session.get("supervisor_session_ref") or ""),
            gateway_supervisor_lifecycle_ref=str(
                lifecycle.get("gateway_supervisor_lifecycle_ref") or ""
            ),
        )
    )
    boundary_path = Path(
        boundary["gateway_supervisor_process_probe_boundary_artifact_path"]
    )

    # The lifecycle ref is intentionally source-bound to the Gateway session
    # artifact path. Rebuild the materialized chain in the same stage directory
    # used to derive the Gateway refs that were sent through the live route.
    final_probe = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact_path,
        probe_dir=preflight_dir,
        live_probe_invoked=run_live_sitl,
        materialize_live_gateway_processes=True,
        gateway_supervisor_process_probe_boundary_artifact_path=boundary_path,
        live_runtime_error=combined_live_error,
    )
    terrain_observed = terrain_agent.get("terrain_world_sitl_agent_observed") is True
    final_probe.setdefault("checks", {})[
        "gateway_terrain_world_sitl_agent_observed"
    ] = terrain_observed
    if not terrain_observed:
        final_probe["gateway_runtime_probe_status"] = "blocked"
        final_probe["causal_form"] = "Form 0b"
        final_probe["progress_counted"] = False
        final_probe["gateway_capability_progress_counted"] = False
        final_probe["full_gateway_runtime_loop"] = False
        final_probe["form3_claim_supported"] = False
        final_probe.setdefault("blocked_reasons", []).append(
            "gateway_terrain_world_sitl_agent_not_observed"
        )
    final_probe.update(
        {
            "gateway_owned_runtime_runner_schema": (
                "gateway_owned_multi_condition_runtime_runner.v1"
            ),
            "gateway_owned_runtime_runner_mode": (
                "live_sitl" if run_live_sitl else "existing_source_artifact"
            ),
            "preflight_gateway_live_runtime_probe_artifact_path": str(preflight_path),
            "gateway_supervisor_process_probe_boundary_artifact_path": str(
                boundary_path
            ),
            "gateway_terrain_world_sitl_agent_artifact_path": str(
                output_dir
                / "terrain_world_sitl_agent"
                / "gateway_terrain_world_sitl_agent.json"
            ),
            "source_backed_terrain_world_loaded_by_gateway_agent": terrain_observed,
            "terrain_flight_physics_affected": False,
            "terrain_collision_mode": terrain_agent.get("terrain_collision_mode", ""),
            "integrated_runner_observed": True,
        }
    )
    final_path = _write_stage_artifact(
        output_dir,
        "gateway_owned_multi_condition_runtime_probe.json",
        final_probe,
    )
    final_probe["gateway_owned_multi_condition_runtime_probe_artifact_path"] = str(
        final_path
    )
    _write_json(final_path, final_probe)
    return final_probe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run C5b Gateway-owned multi-condition MissionOS runtime."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-live-sitl", action="store_true")
    source.add_argument("--source-runtime-artifact", type=Path)
    parser.add_argument("--wind-mps", type=float, default=DEFAULT_WIND_MPS)
    parser.add_argument("--wind-direction-deg", type=float, default=DEFAULT_WIND_DIRECTION_DEG)
    parser.add_argument("--drift-threshold-m", type=float, default=DEFAULT_DRIFT_THRESHOLD_M)
    parser.add_argument("--live-timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(
            Path("output/mission_designer_behavior_delta_audits")
            / f"gateway_owned_multi_condition_runtime_{_utc_stamp()}"
        ),
    )
    args = parser.parse_args()

    probe = run_gateway_owned_multi_condition_runtime(
        output_dir=args.output_dir,
        run_live_sitl=args.run_live_sitl,
        source_runtime_artifact=args.source_runtime_artifact,
        wind_mps=args.wind_mps,
        wind_direction_deg=args.wind_direction_deg,
        drift_threshold_m=args.drift_threshold_m,
        timeout_seconds=args.live_timeout_seconds,
    )
    print(json.dumps(probe, indent=2, sort_keys=True))
    return 0 if probe.get("gateway_runtime_probe_status") == PROBE_STATUS_OBSERVED else 1


if __name__ == "__main__":
    raise SystemExit(main())
