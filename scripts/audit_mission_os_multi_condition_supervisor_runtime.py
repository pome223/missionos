#!/usr/bin/env python3
"""Audit a scoped multi-condition Mission OS supervisor runtime.

The runtime uses the proven wind RTL -> LAND SITL action path as the first
vertical slice, but the Mission OS supervisor decision must assess wind,
obstacle, payload, battery, telemetry, recovery state, and authority dimensions
in the same session before it can claim the multi-condition supervisor scope.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from scripts.audit_mission_designer_wind_drift_form3_closed_loop import (
    DEFAULT_DRIFT_THRESHOLD_M,
    DEFAULT_WIND_DIRECTION_DEG,
    DEFAULT_WIND_MPS,
    _latest_partial_run_dir,
    _post_recovery_land_observed,
    _rtl_recovery_observed,
)
from scripts.audit_mission_designer_wind_drift_recovery_closed_loop import (
    UNSAFE_AUTHORITY_KEYS,
    _nested_true_keys,
    _read_json,
    _source_refs_observed,
    _summary_path,
    _wind_application_source_bound,
    _wind_drift_observed,
    _write_json,
)
from scripts.diagnose_mission_designer_wind_form3_partial_run import (
    build_diagnostic,
)
from scripts import smoke_digital_twin_world_bound_sitl_e2e as terrain_world_smoke
from scripts.smoke_digital_twin_waypoint_reach_observed import (
    GSI_DEM_SAMPLE,
    SOURCE_BACKED_TARGET_LATITUDE,
    SOURCE_BACKED_TARGET_LONGITUDE,
)
from src.runtime.digital_twin_mission_environment import (
    DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
    build_digital_twin_stage1_environment,
)


SCHEMA_VERSION = "mission_os_multi_condition_supervisor_runtime_audit.v1"
TARGET_SUPERVISOR_SCOPE = "wind_obstacle_payload_form3_sitl"
LOOP_SCHEMA_VERSION = "mission_os_supervisor_recovery_loop.v1"
AUTHORITY_FALSE_KEYS = {
    "ai_judgment_is_gate_verdict",
    "ai_judgment_created_dispatch_authority",
    "llm_gate_judge_used",
    "dispatch_authority_created",
    "created_dispatch_authority",
    "automatic_dispatch_allowed",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "physical_form1_claimed",
    "full_gateway_runtime_loop",
}
REQUIRED_ASSESSMENT_DIMENSIONS = {
    "wind",
    "obstacle",
    "payload",
    "battery",
    "route",
    "telemetry",
    "recovery_state",
    "authority",
}
REQUIRED_SECONDARY_RISKS = {
    "route_blocking",
    "payload_feasibility",
    "battery_warning",
    "telemetry_continuity",
}
EXPECTED_SECONDARY_RISK_STATES = {
    "route_blocking": "not_active",
    "payload_feasibility": "not_active",
    "battery_warning": "nominal_or_unknown",
    "telemetry_continuity": "sufficient_for_recovery_audit",
}
REQUIRED_CONDITION_PRIORITY = {
    "authority_boundary",
    "route_blocking",
    "payload_feasibility",
    "battery_warning",
    "telemetry_continuity",
    "wind_drift",
}
VALID_BOUNDED_ACTIONS = {"rtl", "land"}
TERRAIN_PROMPT = "MissionOS multi-condition supervisor terrain-bound SITL route"
TERRAIN_PROMPT_REF = "px4_gazebo_mission_prompt_request:missionos_b1_supervisor_terrain"
TERRAIN_TARGET_LATITUDE_ENV = "MISSIONOS_SUPERVISOR_TERRAIN_TARGET_LATITUDE"
TERRAIN_TARGET_LONGITUDE_ENV = "MISSIONOS_SUPERVISOR_TERRAIN_TARGET_LONGITUDE"


def _float_env_or_default(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float") from exc


def _rebase_heightmap_to_takeoff_agl(
    world_path: Path,
    *,
    takeoff_terrain_elevation_m: float | None,
    elevation_min_m: float | None,
) -> dict[str, Any]:
    if takeoff_terrain_elevation_m is None or elevation_min_m is None:
        return {
            "terrain_vertical_reference": "source_dem_absolute_height_unmodified",
            "terrain_agl_rebase_applied": False,
            "terrain_agl_rebase_reason": "missing_takeoff_or_min_elevation",
        }
    local_takeoff_height_m = float(takeoff_terrain_elevation_m) - float(
        elevation_min_m
    )
    text = world_path.read_text(encoding="utf-8")
    updated = text.replace(
        "<pos>0 0 0</pos>",
        f"<pos>0 0 {-local_takeoff_height_m:.3f}</pos>",
    )
    if updated == text:
        return {
            "terrain_vertical_reference": "source_dem_absolute_height_unmodified",
            "terrain_agl_rebase_applied": False,
            "terrain_agl_rebase_reason": "heightmap_pos_not_found",
            "takeoff_terrain_elevation_m": float(takeoff_terrain_elevation_m),
            "terrain_elevation_min_m": float(elevation_min_m),
            "terrain_local_takeoff_height_m": local_takeoff_height_m,
        }
    world_path.write_text(updated, encoding="utf-8")
    return {
        "terrain_vertical_reference": "source_dem_rebased_to_takeoff_agl_zero",
        "terrain_agl_rebase_applied": True,
        "takeoff_terrain_elevation_m": float(takeoff_terrain_elevation_m),
        "terrain_elevation_min_m": float(elevation_min_m),
        "terrain_local_takeoff_height_m": local_takeoff_height_m,
        "terrain_heightmap_z_offset_m": -local_takeoff_height_m,
    }


def _convert_heightmap_collision_to_visual_only(world_path: Path) -> dict[str, Any]:
    text = world_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"\n        <collision name=\"terrain_collision\">.*?\n        </collision>",
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    if count:
        world_path.write_text(updated, encoding="utf-8")
    return {
        "terrain_collision_mode": "visual_only_supervisor_runtime",
        "terrain_collision_removed_for_horizontal_route_smoke": bool(count),
        "terrain_flight_physics_affected": False,
    }


def _valid_emergency_approval_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("px4_gazebo_emergency_command_approval:")
        and len(value.split(":", 1)[1]) > 0
    )


def _ref_startswith(value: Any, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value.split(":", 1)[1]) > 0
    )


def _gsi_dem_fetcher_from_env():
    mode = os.getenv("DIGITAL_TWIN_GSI_DEM_FETCH_MODE", "fixture").strip() or "fixture"
    if mode == "live":
        return None
    if mode == "fixture":
        return lambda _url: ("fixture_gsi_dem_sample", GSI_DEM_SAMPLE)
    raise ValueError("DIGITAL_TWIN_GSI_DEM_FETCH_MODE must be one of 'live', 'fixture'")


def _prepare_source_backed_terrain_world(artifact_root: Path) -> dict[str, Any]:
    terrain_dir = artifact_root / "source_backed_terrain_world"
    terrain_dir.mkdir(parents=True, exist_ok=True)
    source_backed_target_latitude = _float_env_or_default(
        TERRAIN_TARGET_LATITUDE_ENV,
        SOURCE_BACKED_TARGET_LATITUDE,
    )
    source_backed_target_longitude = _float_env_or_default(
        TERRAIN_TARGET_LONGITUDE_ENV,
        SOURCE_BACKED_TARGET_LONGITUDE,
    )
    digital_twin = build_digital_twin_stage1_environment(
        prompt=TERRAIN_PROMPT,
        prompt_request_ref=TERRAIN_PROMPT_REF,
        altitude_target_m=20,
        payload_weight_kg=1,
        weather_hazard_labels=(),
        source_backed_target_latitude=source_backed_target_latitude,
        source_backed_target_longitude=source_backed_target_longitude,
        source_backed_dem_fetcher=_gsi_dem_fetcher_from_env(),
        vehicle_profile_path=DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
        now=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )
    world_artifact = digital_twin["gazebo_world_artifact"]
    generated_world_path = Path(__file__).resolve().parents[1] / world_artifact[
        "world_file_path_or_artifact_uri"
    ]
    world_root = terrain_world_smoke._copy_px4_world_assets(terrain_dir)
    prepared_world_path = terrain_world_smoke._inject_digital_twin_terrain(
        world_root,
        generated_world_path,
    )
    dem = digital_twin.get("terrain_dem_source_snapshot") or {}
    mission_item = digital_twin["digital_twin_px4_mission_item_candidate"]
    vertical_reference = _rebase_heightmap_to_takeoff_agl(
        prepared_world_path,
        takeoff_terrain_elevation_m=mission_item.get("takeoff_terrain_elevation_m"),
        elevation_min_m=dem.get("elevation_min_m"),
    )
    collision_reference = _convert_heightmap_collision_to_visual_only(
        prepared_world_path,
    )
    return {
        "prepared_world_path": prepared_world_path,
        "prepared_world_sha256": hashlib.sha256(
            prepared_world_path.read_bytes()
        ).hexdigest(),
        "terrain_world_source_ref": (
            f"gazebo_world_artifact:{world_artifact.get('world_id', '')}"
        ),
        "terrain_provider_response_status": dem.get("provider_response_status", ""),
        "terrain_sampling_mode": mission_item.get("terrain_sampling_mode", ""),
        "source_backed_terrain": bool(dem.get("source_backed_terrain")),
        "generated_world_file_used": world_artifact.get(
            "world_file_path_or_artifact_uri",
            "",
        ),
        "source_backed_target_latitude": source_backed_target_latitude,
        "source_backed_target_longitude": source_backed_target_longitude,
        **vertical_reference,
        **collision_reference,
    }


def _authority_true_paths(payload: Any, *, path: str = "artifact") -> list[str]:
    paths: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            current_path = f"{path}.{key}"
            if key in AUTHORITY_FALSE_KEYS and value is not False:
                paths.append(current_path)
            if isinstance(value, (dict, list)):
                paths.extend(_authority_true_paths(value, path=current_path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            paths.extend(_authority_true_paths(item, path=f"{path}[{index}]"))
    return paths


def _run_multi_condition_smoke(
    *,
    wind_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    artifact_root: Path,
    source_backed_terrain_world: bool,
) -> Path:
    env = os.environ.copy()
    terrain_world = (
        _prepare_source_backed_terrain_world(artifact_root)
        if source_backed_terrain_world
        else None
    )
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS": str(wind_mps),
            "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG": str(wind_direction_deg),
        }
    )
    if terrain_world is not None:
        env.update(
            {
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SDF": str(
                    terrain_world["prepared_world_path"]
                ),
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SHA256": str(
                    terrain_world["prepared_world_sha256"]
                ),
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SOURCE_REF": str(
                    terrain_world["terrain_world_source_ref"]
                ),
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_PROVIDER_STATUS": str(
                    terrain_world["terrain_provider_response_status"]
                ),
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_SAMPLING_MODE": str(
                    terrain_world["terrain_sampling_mode"]
                ),
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_VERTICAL_REFERENCE": str(
                    terrain_world["terrain_vertical_reference"]
                ),
                "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_COLLISION_MODE": str(
                    terrain_world["terrain_collision_mode"]
                ),
            }
        )
    env.pop("MISSION_DESIGNER_REALISM_WIND_GUST_MPS", None)
    env.pop("MISSION_DESIGNER_REALISM_WIND_VARIANCE", None)
    command = [
        sys.executable,
        "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        "--on-deviation-action",
        "rtl",
        "--post-recovery-action",
        "land",
        "--max-pose-deviation-xy-m",
        str(drift_threshold_m),
        "--mission-os-supervisor-multi-condition-loop",
    ]
    result = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=420,
    )
    summary: dict[str, Any] | None = None
    if result.stdout.strip():
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError:
            summary = None
    if summary is not None:
        run_dir = Path(summary["artifact_dir"])
        if run_dir.exists() and (run_dir / "summary.json").exists():
            return run_dir
    if result.returncode != 0:
        raise RuntimeError(
            "multi-condition supervisor runtime smoke failed: "
            f"rc={result.returncode}\n"
            f"stdout_tail={result.stdout[-2000:]}\n"
            f"stderr_tail={result.stderr[-2000:]}"
        )
    if summary is None:
        raise RuntimeError(
            "multi-condition supervisor smoke did not emit JSON summary: "
            f"{result.stdout[-2000:]}"
        )
    run_dir = Path(summary["artifact_dir"])
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    return run_dir


def _assessment_dimensions_supported(decision: dict[str, Any]) -> bool:
    assessment = decision.get("assessment_inputs")
    if not isinstance(assessment, dict):
        return False
    if assessment.get("assessment_mode") != "compound_mission_state_assessment":
        return False
    if assessment.get("primary_trigger") != "wind_drift_exceeded_threshold":
        return False
    if assessment.get("supervisor_scope") != TARGET_SUPERVISOR_SCOPE:
        return False
    if not REQUIRED_ASSESSMENT_DIMENSIONS.issubset(assessment.keys()):
        return False
    secondary_risks = assessment.get("secondary_risks")
    if not isinstance(secondary_risks, list) or not secondary_risks:
        return False
    secondary_conditions = {
        risk.get("condition") for risk in secondary_risks if isinstance(risk, dict)
    }
    if not REQUIRED_SECONDARY_RISKS.issubset(secondary_conditions):
        return False
    condition_priority = assessment.get("condition_priority")
    if not isinstance(condition_priority, list):
        return False
    if not REQUIRED_CONDITION_PRIORITY.issubset(set(condition_priority)):
        return False
    if assessment.get("conflict_policy") != (
        "operator_review_required_or_form0b_readiness_when_conflict_active"
    ):
        return False
    if assessment.get("conflicting_risks") != []:
        return False
    secondary_risk_by_condition = {
        risk.get("condition"): risk for risk in secondary_risks if isinstance(risk, dict)
    }
    secondary_source_ref_by_condition: dict[str, Any] = {}
    for condition, expected_state in EXPECTED_SECONDARY_RISK_STATES.items():
        risk = secondary_risk_by_condition.get(condition)
        if not isinstance(risk, dict):
            return False
        if risk.get("risk_state") != expected_state:
            return False
        if risk.get("silent_continuation_allowed") is not True:
            return False
        source_prefix = {
            "route_blocking": "route_blocking_verification:",
            "payload_feasibility": "simulator_condition_application:",
            "battery_warning": "observed_vehicle_condition_evidence:",
            "telemetry_continuity": "telemetry_freshness_report:",
        }[condition]
        if not _ref_startswith(risk.get("source_ref"), source_prefix):
            return False
        secondary_source_ref_by_condition[condition] = risk.get("source_ref")
    wind = assessment.get("wind")
    if not isinstance(wind, dict):
        return False
    if (
        wind.get("drift_above_threshold") is not True
        or wind.get("primary_trigger") is not True
        or not isinstance(wind.get("wind_speed_mps"), (int, float))
        or not isinstance(wind.get("wind_direction_deg"), (int, float))
    ):
        return False
    obstacle = assessment.get("obstacle")
    if not isinstance(obstacle, dict):
        return False
    if (
        obstacle.get("condition_checked") is not True
        or obstacle.get("route_blocking_observed") is not False
        or not _ref_startswith(
            obstacle.get("route_blocking_verification_ref"),
            "route_blocking_verification:",
        )
    ):
        return False
    if (
        secondary_source_ref_by_condition.get("route_blocking")
        != obstacle.get("route_blocking_verification_ref")
    ):
        return False
    payload = assessment.get("payload")
    if not isinstance(payload, dict):
        return False
    if (
        payload.get("condition_checked") is not True
        or payload.get("payload_feasibility_advisory_active") is not False
        or not _ref_startswith(
            payload.get("payload_condition_application_ref"),
            "simulator_condition_application:",
        )
        or payload.get("payload_margin_risk") != "unknown_or_not_active"
    ):
        return False
    if (
        secondary_source_ref_by_condition.get("payload_feasibility")
        != payload.get("payload_condition_application_ref")
    ):
        return False
    battery = assessment.get("battery")
    if not isinstance(battery, dict):
        return False
    if (
        battery.get("condition_checked") is not True
        or battery.get("battery_warning_state") != "nominal_or_unknown"
        or not _ref_startswith(
            battery.get("battery_evidence_ref"),
            "observed_vehicle_condition_evidence:",
        )
        or battery.get("px4_battery_warning_state_affected") is not False
    ):
        return False
    if (
        secondary_source_ref_by_condition.get("battery_warning")
        != battery.get("battery_evidence_ref")
    ):
        return False
    route = assessment.get("route")
    if not isinstance(route, dict):
        return False
    if (
        route.get("route_blocked") is not False
        or route.get("dropoff_verified") is not False
        or route.get("delivery_completion_claimed") is not False
    ):
        return False
    telemetry = assessment.get("telemetry")
    if not isinstance(telemetry, dict):
        return False
    if (
        telemetry.get("telemetry_continuity") != "sufficient_for_recovery_audit"
        or not _ref_startswith(
            telemetry.get("telemetry_freshness_ref"),
            "telemetry_freshness_report:",
        )
        or telemetry.get("observer_dropout_active") is not False
    ):
        return False
    if (
        secondary_source_ref_by_condition.get("telemetry_continuity")
        != telemetry.get("telemetry_freshness_ref")
    ):
        return False
    recovery_state = assessment.get("recovery_state")
    if not isinstance(recovery_state, dict):
        return False
    if recovery_state.get("selected_bounded_action") not in VALID_BOUNDED_ACTIONS:
        return False
    authority = assessment.get("authority")
    if not isinstance(authority, dict):
        return False
    return (
        authority.get("operator_review_required") is True
        and authority.get("automatic_dispatch_allowed") is False
        and authority.get("bounded_action_dispatch_allowed") is True
        and authority.get("hardware_target_allowed") is False
        and authority.get("physical_execution_invoked") is False
    )


def _cycle_ref_chain_supported(
    cycle: dict[str, Any],
    *,
    cycle_index: int,
    expected_action: str,
    expected_dispatch_ref: str | None,
    expected_approval_ref: str | None,
    expected_outcome_ref: str | None,
    expected_source_observation_ref: str | None,
) -> bool:
    if not isinstance(cycle, dict):
        return False
    decision = cycle.get("decision")
    request = cycle.get("action_request")
    receipt = cycle.get("action_receipt")
    outcome = cycle.get("outcome_observation")
    if not all(
        isinstance(artifact, dict) for artifact in (decision, request, receipt, outcome)
    ):
        return False
    decision_id = decision.get("decision_id")
    request_id = request.get("request_id")
    receipt_id = receipt.get("receipt_id")
    outcome_id = outcome.get("observation_id")
    return (
        cycle.get("cycle_index") == cycle_index
        and decision.get("schema_version") == "mission_os_recovery_decision.v1"
        and request.get("schema_version") == "mission_os_backend_action_request.v1"
        and receipt.get("schema_version") == "mission_os_backend_action_receipt.v1"
        and outcome.get("schema_version")
        == "mission_os_recovery_outcome_observation.v1"
        and cycle.get("decision_ref") == decision_id
        and cycle.get("action_request_ref") == request_id
        and cycle.get("action_receipt_ref") == receipt_id
        and cycle.get("outcome_observation_ref") == outcome_id
        and decision.get("cycle_index") == cycle_index
        and request.get("cycle_index") == cycle_index
        and receipt.get("cycle_index") == cycle_index
        and outcome.get("cycle_index") == cycle_index
        and decision.get("decision_loop_driver") == "mission_os_supervisor"
        and decision.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        and decision.get("full_gateway_runtime_loop") is False
        and decision.get("source_observation_ref") == expected_source_observation_ref
        and decision.get("primary_trigger") == "wind_drift_exceeded_threshold"
        and _assessment_dimensions_supported(decision)
        and decision.get("selected_bounded_action") == expected_action
        and decision.get("operator_approval_required") is True
        and decision.get("automatic_dispatch_allowed") is False
        and decision.get("operator_approved_dispatch_allowed") is True
        and request.get("decision_ref") == decision_id
        and request.get("backend_target") == "px4_gazebo_sitl"
        and request.get("bounded_action") == expected_action
        and request.get("expected_dispatch_ref") == expected_dispatch_ref
        and request.get("approval_ref") == expected_approval_ref
        and _valid_emergency_approval_ref(request.get("approval_ref"))
        and request.get("allowlisted_action") is True
        and request.get("operator_approved") is True
        and request.get("automatic_dispatch_allowed") is False
        and request.get("dispatch_authority_created") is False
        and request.get("hardware_target_allowed") is False
        and request.get("physical_execution_invoked") is False
        and receipt.get("action_request_ref") == request_id
        and receipt.get("dispatch_ref") == expected_dispatch_ref
        and receipt.get("dispatch_observed") is True
        and receipt.get("backend_target") == "px4_gazebo_sitl"
        and receipt.get("hardware_target_allowed") is False
        and receipt.get("physical_execution_invoked") is False
        and outcome.get("action_receipt_ref") == receipt_id
        and outcome.get("outcome_observation_ref") == expected_outcome_ref
        and outcome.get("outcome_observed") is True
        and outcome.get("delivery_completion_claimed") is False
        and outcome.get("hardware_target_allowed") is False
        and outcome.get("physical_execution_invoked") is False
    )


def _source_artifacts_support_assessment(
    summary: dict[str, Any], decision: dict[str, Any]
) -> bool:
    assessment = decision.get("assessment_inputs")
    if not isinstance(assessment, dict):
        return False

    obstacle = assessment.get("obstacle")
    payload = assessment.get("payload")
    battery = assessment.get("battery")
    telemetry = assessment.get("telemetry")
    if not all(
        isinstance(value, dict) for value in (obstacle, payload, battery, telemetry)
    ):
        return False

    route_source = summary.get("route_blocking_verification")
    if not isinstance(route_source, dict):
        return False
    route_observed = route_source.get("observed") or {}
    if not isinstance(route_observed, dict):
        return False
    route_active = bool(
        route_source.get("verification_status")
        in {"verified", "route_blocking_verified", "blocked"}
        or route_observed.get("route_blocking_verified") is True
        or route_observed.get("route_blocked") is True
        or route_observed.get("route_blocking_observed") is True
    )
    if (
        route_source.get("verification_id")
        != obstacle.get("route_blocking_verification_ref")
        or route_active
    ):
        return False

    payload_source = summary.get("payload_simulator_condition_application")
    if not isinstance(payload_source, dict):
        return False
    payload_advisory = summary.get("payload_feasibility_advisory")
    payload_advisory_active = isinstance(payload_advisory, dict) and bool(
        payload_advisory
    )
    if (
        payload_source.get("application_id")
        != payload.get("payload_condition_application_ref")
        or payload_advisory_active
    ):
        return False

    battery_source = summary.get("observed_battery_condition_evidence")
    if not isinstance(battery_source, dict):
        return False
    battery_observed = battery_source.get("observed") or {}
    if not isinstance(battery_observed, dict):
        return False
    battery_warning = battery_observed.get("observed_warning")
    battery_warning_active = False
    if battery_warning is not None:
        try:
            battery_warning_active = int(battery_warning) > 0
        except (TypeError, ValueError):
            battery_warning_active = True
    if (
        battery_source.get("evidence_id") != battery.get("battery_evidence_ref")
        or battery_warning_active
    ):
        return False

    telemetry_source = summary.get("telemetry_freshness_report")
    if not isinstance(telemetry_source, dict):
        return False
    try:
        telemetry_gap_count = int(telemetry_source.get("gap_count") or 0)
    except (TypeError, ValueError):
        telemetry_gap_count = 1
    telemetry_dropout_active = (
        telemetry_source.get("freshness_status") == "gap_observed"
        and telemetry_gap_count > 0
    )
    return (
        telemetry_source.get("report_id") == telemetry.get("telemetry_freshness_ref")
        and not telemetry_dropout_active
    )


def summarize_multi_condition_supervisor_runtime(
    run_dir: Path,
    *,
    expected_wind_mps: float,
    expected_direction_deg: float,
    drift_threshold_m: float,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    loop = summary.get("mission_os_supervisor_recovery_loop")
    loop = loop if isinstance(loop, dict) else {}
    cycles = loop.get("cycles")
    cycles = cycles if isinstance(cycles, list) else []
    cycle1 = cycles[0] if len(cycles) > 0 and isinstance(cycles[0], dict) else {}
    cycle2 = cycles[1] if len(cycles) > 1 and isinstance(cycles[1], dict) else {}
    loop_conflicting_risks = loop.get("conflicting_risks")
    loop_conflicting_risks = (
        loop_conflicting_risks if isinstance(loop_conflicting_risks, list) else []
    )
    nested_authority_true_paths = _authority_true_paths(
        {"summary": summary, "loop": loop}
    )
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    checks = {
        "horizontal_route_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        )
        is True,
        "wind_application_source_bound": _wind_application_source_bound(
            summary,
            expected_wind_mps=expected_wind_mps,
            expected_direction_deg=expected_direction_deg,
        ),
        "wind_drift_observed": _wind_drift_observed(
            summary, drift_threshold_m=drift_threshold_m
        ),
        "cycle1_bounded_rtl_action_outcome_observed": _rtl_recovery_observed(
            summary
        ),
        "cycle2_bounded_land_action_outcome_observed": (
            _post_recovery_land_observed(summary)
        ),
        "decision_loop_driver_supervisor": (
            summary.get("decision_loop_driver") == "mission_os_supervisor"
            and loop.get("decision_loop_driver") == "mission_os_supervisor"
        ),
        "supervisor_scope_multi_condition": (
            summary.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
            and loop.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        ),
        "full_gateway_runtime_loop_false": (
            summary.get("full_gateway_runtime_loop") is False
            and loop.get("full_gateway_runtime_loop") is False
        ),
        "loop_schema_observed": loop.get("schema_version") == LOOP_SCHEMA_VERSION,
        "supervisor_loop_claim_supported": (
            loop.get("supervisor_loop_claim_supported") is True
        ),
        "same_session_cycle_count_observed": loop.get("cycle_count") == 2,
        "cycle_list_exactly_two": len(cycles) == 2,
        "compound_assessment_dimensions_observed": all(
            _assessment_dimensions_supported((cycle.get("decision") or {}))
            for cycle in (cycle1, cycle2)
        ),
        "secondary_source_artifacts_inactive": all(
            _source_artifacts_support_assessment(summary, (cycle.get("decision") or {}))
            for cycle in (cycle1, cycle2)
        ),
        "conflicting_risks_absent": not loop_conflicting_risks,
        "cycle1_ref_chain_consistent": _cycle_ref_chain_supported(
            cycle1,
            cycle_index=1,
            expected_action="rtl",
            expected_dispatch_ref=summary.get("recovery_dispatch_ref"),
            expected_approval_ref=summary.get("recovery_approval_ref"),
            expected_outcome_ref=summary.get("recovery_completion_ref"),
            expected_source_observation_ref="route_deviation_observation:wind_drift",
        ),
        "cycle2_ref_chain_consistent": _cycle_ref_chain_supported(
            cycle2,
            cycle_index=2,
            expected_action="land",
            expected_dispatch_ref=summary.get("post_recovery_dispatch_ref"),
            expected_approval_ref=summary.get("post_recovery_approval_ref"),
            expected_outcome_ref=summary.get("post_recovery_completion_ref"),
            expected_source_observation_ref=summary.get("recovery_completion_ref"),
        ),
        "cycle_dispatch_chains_distinct": bool(
            summary.get("recovery_dispatch_ref")
            and summary.get("post_recovery_dispatch_ref")
            and summary.get("recovery_dispatch_ref")
            != summary.get("post_recovery_dispatch_ref")
        ),
        "source_refs_observed": _source_refs_observed(summary),
        "dropoff_not_claimed": (
            summary.get("dropoff_region_reached") is False
            and summary.get("dropoff_verified") is False
            and summary.get("delivery_completion_claimed") is False
        ),
        "top_level_hardware_physical_false": (
            summary.get("hardware_target_allowed") is False
            and summary.get("physical_execution_invoked") is False
        ),
        "nested_authority_boundary_false": not nested_authority_true_paths,
        "unsafe_authority_flags_absent": not unsafe_flags,
    }
    missing = [name for name, passed in checks.items() if not passed]
    runtime_observed = not missing
    primary_decision = (cycle1.get("decision") or {}) if cycle1 else {}
    primary_assessment = primary_decision.get("assessment_inputs") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": "mission_os_multi_condition_supervisor_runtime:wind_primary",
        "condition_kind": "wind_primary_multi_condition_supervisor_runtime",
        "causal_form": "Form 3" if runtime_observed else "Form 0b",
        "audit_status": (
            "multi_condition_supervisor_runtime_observed"
            if runtime_observed
            else "unsupported"
        ),
        "form3_claim_supported": runtime_observed,
        "supervisor_runtime_claim_supported": runtime_observed,
        "progress_counted": runtime_observed,
        "source_bound": runtime_observed,
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "full_gateway_runtime_loop": False,
        "primary_trigger": "wind_drift_exceeded_threshold",
        "secondary_risks": primary_assessment.get("secondary_risks") or [],
        "condition_priority": primary_assessment.get("condition_priority") or [],
        "conflicting_risks": loop_conflicting_risks,
        "cycle_count": 2 if runtime_observed else 1,
        "artifact_dir": str(run_dir),
        "checks": checks,
        "unsupported_reasons": [f"{name}_not_observed" for name in missing]
        + (
            [
                "nested_authority_boundary_true_paths_observed:"
                + ",".join(nested_authority_true_paths)
            ]
            if nested_authority_true_paths
            else []
        ),
        "mission_os_supervisor_recovery_loop": loop,
        "authority_boundary": {
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "physical_form1_claimed": False,
        },
        "scope_boundary_notes": [
            "wind_primary_trigger_executes_rtl_then_land",
            "obstacle_payload_battery_telemetry_authority_are_assessed_as_condition_dimensions",
            "terrain_world_binding_is_a_gateway_owned_runtime_agent_requirement_not_a_horizontal_route_physics_claim",
            "full_gateway_runtime_loop_remains_false",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "terrain_world_readback": summary.get("terrain_world_readback") or {},
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_partial_run_artifact(
    run_dir: Path,
    *,
    audit_dir: Path,
    run_mode: str,
    smoke_error: str | None = None,
) -> dict[str, Any]:
    artifact = build_diagnostic(run_dir)
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = run_mode
    if smoke_error:
        artifact["smoke_error_digest"] = smoke_error[-2000:]
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit scoped multi-condition Mission OS supervisor runtime."
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--wind-mps", type=float, default=DEFAULT_WIND_MPS)
    parser.add_argument(
        "--wind-direction-deg", type=float, default=DEFAULT_WIND_DIRECTION_DEG
    )
    parser.add_argument(
        "--drift-threshold-m", type=float, default=DEFAULT_DRIFT_THRESHOLD_M
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    parser.add_argument(
        "--source-backed-terrain-world",
        action="store_true",
        help=(
            "Diagnostic mode: inject a source-backed terrain world directly "
            "into the horizontal-route supervisor smoke. The Gateway-owned "
            "runtime uses a separate terrain-world SITL agent by default "
            "because terrain collision/heightmap loading can destabilize the "
            "horizontal route recovery smoke."
        ),
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"multi_condition_supervisor_runtime_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_mode = "existing_run" if args.run_dir else "executed_run"
    try:
        run_dir = args.run_dir or _run_multi_condition_smoke(
            wind_mps=args.wind_mps,
            wind_direction_deg=args.wind_direction_deg,
            drift_threshold_m=args.drift_threshold_m,
            artifact_root=audit_dir / "runs" / "multi_condition_supervisor_runtime",
            source_backed_terrain_world=args.source_backed_terrain_world,
        )
        artifact = summarize_multi_condition_supervisor_runtime(
            run_dir,
            expected_wind_mps=args.wind_mps,
            expected_direction_deg=args.wind_direction_deg,
            drift_threshold_m=args.drift_threshold_m,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        partial_run_dir = args.run_dir or _latest_partial_run_dir(
            audit_dir / "runs" / "multi_condition_supervisor_runtime"
        )
        if partial_run_dir is None:
            raise
        artifact = _build_partial_run_artifact(
            partial_run_dir,
            audit_dir=audit_dir,
            run_mode=run_mode,
            smoke_error=str(exc),
        )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = run_mode
    output_path = audit_dir / "mission_os_multi_condition_supervisor_runtime.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("form3_claim_supported") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
