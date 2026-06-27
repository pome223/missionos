#!/usr/bin/env python3
"""Opt-in guarded AUTO.MISSION runtime smoke for MissionOS.

This smoke runs beyond the Phase 3B ACK boundary: it uploads an AUTO mission,
arms PX4, enters AUTO.MISSION, keeps a telemetry monitor loop alive, applies
basic guards, then sends a bounded LAND abort and records fail-closed evidence.

It still does not verify payload release, dropoff, waypoint envelopes, or
delivery completion. Those remain Phase 4 through Phase 6 gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import textwrap
import time
from typing import Any, Mapping

from scripts import smoke_px4_gazebo_sitl_mission_upload as upload_smoke
from src.runtime.missionos_auto_mission_runner import (
    AUTO_RUNTIME_ABORT_REASON,
    AUTO_RUNTIME_PROBE_STOP_REASON_MONITOR_WINDOW_COMPLETE,
    DEFAULT_AUTO_RUNTIME_ALTITUDE_GRACE_SECONDS,
    DEFAULT_AUTO_RUNTIME_BATTERY_MIN_REMAINING_PERCENT,
    DEFAULT_AUTO_RUNTIME_MIN_PROGRESS_M,
    DEFAULT_AUTO_RUNTIME_MIN_ROUTE_ALTITUDE_M,
    DEFAULT_AUTO_RUNTIME_MONITOR_SECONDS,
    DEFAULT_AUTO_RUNTIME_NO_PROGRESS_GRACE_SECONDS,
    DEFAULT_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS,
    DEFAULT_AUTO_RUNTIME_SIM_BATTERY_MIN_PCT,
    DEFAULT_DROPOFF_RELEASE_ALTITUDE_TOLERANCE_M,
    DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID,
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_DO_GRIPPER,
    MAV_CMD_DO_SET_MODE,
    MAV_GRIPPER_ACTION_RELEASE,
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    MAV_RESULT_ACCEPTED,
    MAV_RESULT_IN_PROGRESS,
    MISSIONOS_AUTO_MISSION_RUNTIME_MONITOR_SUMMARY_SCHEMA_VERSION,
    PX4_ARMING_STATE_ARMED,
    PX4_CUSTOM_MAIN_MODE_AUTO,
    PX4_CUSTOM_SUB_MODE_AUTO_MISSION,
    PX4_NAVIGATION_STATE_AUTO_MISSION,
    MissionOSAutoMissionTelemetrySample,
    build_auto_mission_dropoff_gate_summary,
    build_auto_mission_payload_release_sim_gate_summary,
    build_auto_mission_runtime_monitor_summary,
    build_auto_mission_sitl_delivery_gate_summary,
    build_auto_mission_waypoint_gate_summary_from_runtime,
    compile_operator_coordinate_route_auto_mission,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MAV_MISSION_ACCEPTED,
)

MAV_CMD_DO_CHANGE_SPEED = 178


OPT_IN_ENV = "RUN_MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE"
STRICT_ASSERTS_ENV = "MISSIONOS_AUTO_RUNTIME_STRICT_ASSERTS"
L1_CARGO_ENV = "MISSIONOS_AUTO_RUNTIME_L1_GAZEBO_CARGO"
OPERATOR_ROUTE_JSON_ENV = "MISSIONOS_AUTO_RUNTIME_OPERATOR_ROUTE_JSON"
ARTIFACT_ROOT_ENV = "MISSIONOS_AUTO_RUNTIME_ARTIFACT_ROOT"
OPERATOR_RECOVERY_REQUEST_PATH_ENV = (
    "MISSIONOS_AUTO_RUNTIME_OPERATOR_RECOVERY_REQUEST_PATH"
)
GAZEBO_OBSTACLE_MANIFEST_SCHEMA_VERSION = "missionos_gazebo_obstacle_manifest.v1"
GAZEBO_OBSTACLE_APPLICATION_SCHEMA_VERSION = (
    "missionos_gazebo_obstacle_application.v1"
)
GAZEBO_OBSTACLE_MODEL_PREFIX = "missionos_obstacle"
ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT_DIR / "output/missionos_auto_mission_runner/full_runtime_probe"


def _rel_to_root(path: Path) -> str:
    """Path relative to the repo root, or the absolute path if outside it.

    ARTIFACT_ROOT may point outside the repo (e.g. /tmp), in which case
    Path.relative_to(ROOT_DIR) raises ValueError. Fall back to the absolute
    path rather than crash the summary write after a completed flight.
    """
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)
DEFAULT_POST_ABORT_WAIT_SECONDS = 120.0
DEFAULT_LAND_POST_ABORT_WAIT_SECONDS = 300.0
DEFAULT_RTL_RECOVERY_MIN_PROGRESS_M = 900.0
DEFAULT_RTL_RECOVERY_WAIT_SAFETY_FACTOR = 1.25
DEFAULT_RTL_RECOVERY_LANDING_ALLOWANCE_SECONDS = 120.0
PX4_CUSTOM_MAIN_MODE_OFFBOARD = 6
PX4_NAVIGATION_STATE_OFFBOARD = 14
OPERATOR_RECOVERY_ASSIST_TRIGGER_SECONDS = 10.0
OPERATOR_RECOVERY_ASSIST_LAND_FINALIZE_SECONDS = 20.0
OPERATOR_RECOVERY_ASSIST_PRESTREAM_FRAMES = 20
# Non-terminal maneuvers need enough time to move a stale-but-still-bounded
# operator-approved setpoint, then hand control back to AUTO.MISSION.
OPERATOR_RECOVERY_ASSIST_MAX_SECONDS = 36.0
OPERATOR_RECOVERY_ASSIST_LAND_MAX_SECONDS = 60.0
OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS = 0.05
OPERATOR_RECOVERY_ASSIST_RTL_HOME_RADIUS_M = 3.0
OPERATOR_RECOVERY_ASSIST_LAND_ALTITUDE_M = 0.75
OPERATOR_RECOVERY_ASSIST_LAND_TARGET_Z_M = 0.5
OPERATOR_RECOVERY_ASSIST_DISARM_MAX_ATTEMPTS = 3
OPERATOR_RECOVERY_ASSIST_DISARM_RETRY_SECONDS = 1.5
OPERATOR_RECOVERY_ASSIST_FORCE_DISARM_MAGIC = 21196.0
PAYLOAD_MODEL_CONTAINER_PATH = "/tmp/boiled-claw-auto-l1-cargo-models"
PAYLOAD_DETACH_TOPIC = "/model/x500_0/delivery_payload/detach"
PAYLOAD_RELEASE_MIN_Z_DROP_M = 0.25

# Gazebo physical battery (segment C). Opt-in only. When enabled, a
# gz-sim LinearBatteryPlugin is injected into the x500 SDF so Gazebo models a
# real-capacity (Ah) / voltage battery, and the probe reads its state topic as
# a SEPARATE observed signal (never overwriting the PX4 battery_status field).
# Motor-load coupling (segment C, full) drains the battery as a function of
# actual rotor effort instead of the fixed <power_load>. It is provided by the
# custom MotorLoadBatteryCoupler gz-sim System under
# simulators/gazebo/plugins/motor_load_battery_coupler/, which owns the battery
# integration and publishes BatteryState directly (gz-sim 8 has no proportional
# power-set API on the stock LinearBatteryPlugin). Live-verified against gz-sim
# 8.11.0: idle->hover drives current ~0.48A->~7.4A (12W->180W). To enable at
# runtime the compiled coupler .so must be on GZ_SIM_SYSTEM_PLUGIN_PATH (bake it
# into the image or mount it via GZ_COUPLER_PLUGIN_SO_ENV). This is a Gazebo
# simulation model, not real power-module endurance evidence.
GZ_PHYSICAL_BATTERY_ENV = "MISSIONOS_AUTO_RUNTIME_GZ_PHYSICAL_BATTERY"
GZ_BATTERY_MOTOR_COUPLING_ENV = "MISSIONOS_AUTO_RUNTIME_GZ_BATTERY_MOTOR_COUPLING"
GZ_COUPLER_PLUGIN_SO_ENV = "MISSIONOS_AUTO_RUNTIME_GZ_COUPLER_PLUGIN_SO"
GZ_COUPLER_PLUGIN_CONTAINER_PATH = "/tmp/boiled-claw-gz-plugins"
GZ_BATTERY_NAME = "linear_battery"
GZ_BATTERY_STATE_TOPIC = f"/model/x500_0/battery/{GZ_BATTERY_NAME}/state"
GZ_BATTERY_STATE_SOURCE = "gz-sim:linear_battery_plugin_sitl_simulated"
DEFAULT_GZ_BATTERY_CAPACITY_AH = 5.2
DEFAULT_GZ_BATTERY_VOLTAGE_V = 25.2
DEFAULT_GZ_BATTERY_POWER_LOAD_W = 110.0
# Motor-load coupling power model (see MotorLoadBatteryCoupler README).
GZ_COUPLER_IDLE_POWER_W = 12.0
GZ_COUPLER_HOVER_POWER_W = 180.0
GZ_COUPLER_HOVER_ROTOR_RAD_S = 700.0
# PX4 x500's MulticopterMotorModel spins the *visual* rotor joint at
# (true_motor_speed / rotorVelocitySlowdownSim); rotorVelocitySlowdownSim=10 in
# the stock x500 model. The coupler reads JointVelocity (the slowed value) and
# multiplies it back by this factor to recover the true motor speed.
GZ_COUPLER_ROTOR_VELOCITY_SLOWDOWN = 10.0
GZ_COUPLER_MAX_POWER_W = 600.0
GZ_COUPLER_VOLTAGE_FULL_V = 25.2
GZ_COUPLER_VOLTAGE_EMPTY_V = 21.0
GZ_COUPLER_PUBLISH_RATE_HZ = 5.0
GZ_COUPLER_ROTOR_JOINTS = (
    "rotor_0_joint",
    "rotor_1_joint",
    "rotor_2_joint",
    "rotor_3_joint",
)
WIND_MEAN_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS"
WIND_DIRECTION_DEG_ENV = "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG"
WIND_GUST_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_GUST_MPS"
WIND_VARIANCE_ENV = "MISSION_DESIGNER_REALISM_WIND_VARIANCE"
TEMPERATURE_C_ENV = "MISSION_DESIGNER_REALISM_TEMPERATURE_C"
PRESSURE_HPA_ENV = "MISSION_DESIGNER_REALISM_PRESSURE_HPA"
PRECIPITATION_MM_PER_HOUR_ENV = "MISSION_DESIGNER_REALISM_PRECIPITATION_MM_PER_HOUR"
RAIN_VISUAL_MODE_ENV = "MISSION_DESIGNER_REALISM_RAIN_VISUAL_MODE"
RAIN_BATTERY_DRAIN_FACTOR_ENV = "MISSION_DESIGNER_REALISM_RAIN_BATTERY_DRAIN_FACTOR"
RAIN_SENSOR_DEGRADATION_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_RAIN_SENSOR_DEGRADATION_FACTOR"
)
RAIN_LANDING_RISK_FACTOR_ENV = "MISSION_DESIGNER_REALISM_RAIN_LANDING_RISK_FACTOR"
THERMAL_BATTERY_DRAIN_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_THERMAL_BATTERY_DRAIN_FACTOR"
)
THERMAL_MOTOR_DERATE_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_THERMAL_MOTOR_DERATE_FACTOR"
)

MT_FUJI_OPERATOR_ROUTE = {
    "schema_version": "mission_designer_coordinate_pair_route.v1",
    "route_id": "mission_designer_coordinate_pair_route_mt_fuji_auto_runtime_probe",
    "takeoff_latitude": 35.3195,
    "takeoff_longitude": 138.7435,
    "dropoff_latitude": 35.3606,
    "dropoff_longitude": 138.7274,
    "dropoff_roof_height_agl_m": 10.0,
    "derived_route_distance_m": 4797.766,
}


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the AUTO runtime probe.")


def _strict_asserts_enabled() -> bool:
    return (os.getenv(STRICT_ASSERTS_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _auto_runtime_smoke_validation_failures(payload: Mapping[str, Any]) -> list[str]:
    summary = dict(payload.get("summary") or {})
    sitl_delivery_gate = dict(payload.get("sitl_delivery_gate") or {})
    waypoint_gate = dict(payload.get("waypoint_gate") or {})
    dropoff_gate = dict(payload.get("dropoff_gate") or {})
    payload_release_sim_gate = dict(payload.get("payload_release_sim_gate") or {})
    failures: list[str] = []
    expected = (
        (
            "schema_version",
            summary.get("schema_version"),
            MISSIONOS_AUTO_MISSION_RUNTIME_MONITOR_SUMMARY_SCHEMA_VERSION,
        ),
        ("mission_upload_accepted", summary.get("mission_upload_accepted"), True),
        ("mission_ack_result", summary.get("mission_ack_result"), MAV_MISSION_ACCEPTED),
        ("arm_command_ack_result", summary.get("arm_command_ack_result"), MAV_RESULT_ACCEPTED),
        (
            "auto_mission_mode_ack_result",
            summary.get("auto_mission_mode_ack_result"),
            MAV_RESULT_ACCEPTED,
        ),
        ("auto_mission_started", summary.get("auto_mission_started"), True),
        ("monitor_loop_started", summary.get("monitor_loop_started"), True),
        ("route_completed_claimed", summary.get("route_completed_claimed"), False),
        (
            "delivery_completion_claimed",
            summary.get("delivery_completion_claimed"),
            False,
        ),
        (
            "sitl_delivery_gate.delivery_completion_claimed",
            sitl_delivery_gate.get("delivery_completion_claimed"),
            False,
        ),
    )
    for field, observed, wanted in expected:
        if observed != wanted:
            failures.append(f"{field}_expected_{wanted!r}_observed_{observed!r}")
    if int(summary.get("telemetry_sample_count") or 0) <= 0:
        failures.append("telemetry_sample_count_not_positive")
    if summary.get("payload_release_command_acked"):
        if summary.get("recovery_command_ack_result") != MAV_RESULT_ACCEPTED:
            failures.append("recovery_command_ack_result_not_accepted")
        if waypoint_gate.get("route_completed_claimed") is not True:
            failures.append("waypoint_gate_route_completed_claimed_false")
        if dropoff_gate.get("dropoff_verified") is not True:
            failures.append("dropoff_gate_dropoff_verified_false")
        if sitl_delivery_gate.get("sitl_delivery_claimed") is not True:
            failures.append("sitl_delivery_gate_sitl_delivery_claimed_false")
        if _l1_cargo_enabled():
            if payload_release_sim_gate.get("payload_release_observed_sim") is not True:
                failures.append("payload_release_sim_observed_false")
        elif sitl_delivery_gate.get("payload_release_observed_sim") is not False:
            failures.append("sitl_delivery_gate_payload_release_observed_sim_not_false")
    else:
        if summary.get("recovery_command_ack_result") != MAV_RESULT_ACCEPTED:
            failures.append("recovery_command_ack_result_not_accepted")
        if summary.get("final_landing_safe") is not True:
            failures.append("final_landing_safe_false")
    return failures


def _run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(os.getenv(ARTIFACT_ROOT_ENV) or RUN_ROOT)
    path = root / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _operator_route() -> dict[str, Any]:
    raw = os.getenv(OPERATOR_ROUTE_JSON_ENV)
    if not raw:
        return dict(MT_FUJI_OPERATOR_ROUTE)
    try:
        route = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{OPERATOR_ROUTE_JSON_ENV} is not valid JSON") from exc
    if not isinstance(route, dict):
        raise RuntimeError(f"{OPERATOR_ROUTE_JSON_ENV} must be a JSON object")
    required = (
        "takeoff_latitude",
        "takeoff_longitude",
        "dropoff_latitude",
        "dropoff_longitude",
        "dropoff_roof_height_agl_m",
    )
    missing = [key for key in required if key not in route]
    if missing:
        raise RuntimeError(
            f"{OPERATOR_ROUTE_JSON_ENV} missing required fields: {', '.join(missing)}"
        )
    normalized = {
        "schema_version": route.get(
            "schema_version", "mission_designer_coordinate_pair_route.v1"
        ),
        "route_id": route.get("route_id", "mission_designer_coordinate_pair_route_gui"),
        "takeoff_latitude": float(route["takeoff_latitude"]),
        "takeoff_longitude": float(route["takeoff_longitude"]),
        "dropoff_latitude": float(route["dropoff_latitude"]),
        "dropoff_longitude": float(route["dropoff_longitude"]),
        "dropoff_roof_height_agl_m": float(route["dropoff_roof_height_agl_m"]),
    }
    for key in ("derived_route_distance_m", "actual_route_distance_m"):
        value = route.get(key)
        if isinstance(value, int | float) and float(value) > 0:
            normalized[key] = float(value)
    for key in ("auto_route_waypoint_count", "auto_waypoint_count", "route_waypoint_count"):
        value = route.get(key)
        if isinstance(value, int | float) and float(value) > 0:
            normalized[key] = int(value)
    for key in (
        "terrain_clearance_agl_m",
        "terrain_clearance_target_m",
        "minimum_terrain_clearance_m",
        "wind_speed_mps",
        "wind_direction_deg",
        "wind_gust_mps",
        "wind_variance",
        "temperature_c",
        "pressure_hpa",
        "precipitation_mm_per_hour",
        "rain_battery_drain_factor",
        "rain_sensor_degradation_factor",
        "rain_landing_risk_factor",
    ):
        value = route.get(key)
        if isinstance(value, int | float) and (
            key in {"temperature_c", "wind_direction_deg", "wind_variance"}
            or float(value) > 0
        ):
            normalized[key] = float(value)
    rain_visual_mode = str(route.get("rain_visual_mode") or "").strip().lower()
    if rain_visual_mode:
        normalized["rain_visual_mode"] = rain_visual_mode
    for key in ("landing_zone_blocked", "building_risk_detected"):
        if route.get(key) is not None:
            normalized[key] = bool(route.get(key))
    for key in (
        "obstacle_x_m",
        "obstacle_y_m",
        "obstacle_z_m",
        "obstacle_size_x_m",
        "obstacle_size_y_m",
        "obstacle_size_z_m",
    ):
        value = route.get(key)
        if isinstance(value, int | float) and math.isfinite(float(value)):
            normalized[key] = float(value)
    obstacle_manifest = route.get("obstacle_manifest")
    if isinstance(obstacle_manifest, dict):
        normalized["obstacle_manifest"] = dict(obstacle_manifest)
    obstacles = route.get("obstacles")
    if isinstance(obstacles, list):
        normalized["obstacles"] = [
            dict(item) for item in obstacles if isinstance(item, dict)
        ]
    terrain_profile = route.get("terrain_profile")
    if isinstance(terrain_profile, list):
        normalized["terrain_profile"] = [
            dict(sample) for sample in terrain_profile if isinstance(sample, dict)
        ]
    for key in ("terrain_profile_source", "terrain_profile_ref"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    source_refs = route.get("source_refs")
    if isinstance(source_refs, str) and source_refs.strip():
        normalized["source_refs"] = [source_refs.strip()]
    elif isinstance(source_refs, list):
        normalized["source_refs"] = [
            str(ref).strip() for ref in source_refs if str(ref).strip()
        ]
    return normalized


def _coerce_finite_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _clamp_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    parsed = _coerce_finite_float(value, default)
    return max(float(minimum), min(float(maximum), parsed))


def _route_dropoff_local_xy_m(route: Mapping[str, Any]) -> tuple[float, float]:
    earth_radius_m = 6_371_000.0
    takeoff_lat = math.radians(float(route["takeoff_latitude"]))
    dropoff_lat = math.radians(float(route["dropoff_latitude"]))
    takeoff_lon = math.radians(float(route["takeoff_longitude"]))
    dropoff_lon = math.radians(float(route["dropoff_longitude"]))
    north_m = (dropoff_lat - takeoff_lat) * earth_radius_m
    east_m = (dropoff_lon - takeoff_lon) * earth_radius_m * math.cos(takeoff_lat)
    return (round(north_m, 3), round(east_m, 3))


def _normalize_gazebo_obstacle(
    item: Mapping[str, Any],
    *,
    index: int,
    fallback_x_m: float,
    fallback_y_m: float,
    source: str,
) -> dict[str, Any]:
    size_x = _clamp_float(
        item.get("size_x_m", item.get("width_m")),
        default=18.0,
        minimum=0.5,
        maximum=200.0,
    )
    size_y = _clamp_float(
        item.get("size_y_m", item.get("depth_m")),
        default=18.0,
        minimum=0.5,
        maximum=200.0,
    )
    size_z = _clamp_float(
        item.get("size_z_m", item.get("height_m")),
        default=20.0,
        minimum=0.5,
        maximum=500.0,
    )
    x_m = _clamp_float(
        item.get("x_m", item.get("target_x_m")),
        default=fallback_x_m,
        minimum=-10_000.0,
        maximum=10_000.0,
    )
    y_m = _clamp_float(
        item.get("y_m", item.get("target_y_m")),
        default=fallback_y_m,
        minimum=-10_000.0,
        maximum=10_000.0,
    )
    z_m = _clamp_float(
        item.get("z_m"),
        default=size_z / 2.0,
        minimum=0.0,
        maximum=1_000.0,
    )
    name = str(item.get("name") or item.get("model_name") or "").strip()
    if not name:
        name = f"{GAZEBO_OBSTACLE_MODEL_PREFIX}_{index:02d}"
    kind = str(item.get("kind") or "building_box").strip() or "building_box"
    return {
        "name": name,
        "kind": kind,
        "frame": "gazebo_world_local_ned",
        "x_m": round(x_m, 3),
        "y_m": round(y_m, 3),
        "z_m": round(z_m, 3),
        "size_x_m": round(size_x, 3),
        "size_y_m": round(size_y, 3),
        "size_z_m": round(size_z, 3),
        "source": str(item.get("source") or source),
    }


def _gazebo_obstacle_manifest_from_route(route: Mapping[str, Any]) -> dict[str, Any]:
    dropoff_x_m, dropoff_y_m = _route_dropoff_local_xy_m(route)
    raw_manifest = route.get("obstacle_manifest")
    raw_manifest = raw_manifest if isinstance(raw_manifest, Mapping) else {}
    raw_obstacles = raw_manifest.get("obstacles")
    if not isinstance(raw_obstacles, list):
        raw_obstacles = route.get("obstacles")
    if not isinstance(raw_obstacles, list):
        raw_obstacles = []

    explicit_route_obstacle = any(
        key in route
        for key in (
            "obstacle_x_m",
            "obstacle_y_m",
            "obstacle_z_m",
            "obstacle_size_x_m",
            "obstacle_size_y_m",
            "obstacle_size_z_m",
        )
    )
    if explicit_route_obstacle:
        raw_obstacles = [
            *raw_obstacles,
            {
                "name": "missionos_route_obstacle",
                "x_m": route.get("obstacle_x_m"),
                "y_m": route.get("obstacle_y_m"),
                "z_m": route.get("obstacle_z_m"),
                "size_x_m": route.get("obstacle_size_x_m"),
                "size_y_m": route.get("obstacle_size_y_m"),
                "size_z_m": route.get("obstacle_size_z_m"),
                "source": "mission_designer_coordinate_route",
            },
        ]
    if not raw_obstacles and route.get("landing_zone_blocked") is True:
        raw_obstacles = [
            {
                "name": "missionos_landing_zone_blocker",
                "kind": "building_box",
                "x_m": dropoff_x_m,
                "y_m": dropoff_y_m,
                "size_x_m": 18.0,
                "size_y_m": 18.0,
                "size_z_m": 20.0,
                "source": "landing_zone_blocked",
            }
        ]

    obstacles = [
        _normalize_gazebo_obstacle(
            item,
            index=index,
            fallback_x_m=dropoff_x_m,
            fallback_y_m=dropoff_y_m,
            source="obstacle_manifest",
        )
        for index, item in enumerate(raw_obstacles, start=1)
        if isinstance(item, Mapping)
    ]
    manifest_status = "configured" if obstacles else "not_configured"
    return {
        "schema_version": GAZEBO_OBSTACLE_MANIFEST_SCHEMA_VERSION,
        "manifest_status": manifest_status,
        "source": str(
            raw_manifest.get("source")
            or ("landing_zone_blocked" if route.get("landing_zone_blocked") else "not_configured")
        ),
        "building_risk_detected": bool(
            raw_manifest.get("building_risk_detected")
            or route.get("building_risk_detected")
            or obstacles
        ),
        "landing_zone_blocked": bool(
            raw_manifest.get("landing_zone_blocked") or route.get("landing_zone_blocked")
        ),
        "dropoff_local_x_m": dropoff_x_m,
        "dropoff_local_y_m": dropoff_y_m,
        "obstacles": obstacles,
        "gazebo_obstacle_model_spawn_requested": bool(obstacles),
        "gazebo_obstacle_model_spawned": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }


def _docker_logs() -> str:
    return upload_smoke._run(
        ["docker", "logs", upload_smoke.CONTAINER_NAME],
        check=False,
        timeout=30,
    ).stdout


def _l1_cargo_enabled() -> bool:
    return os.getenv(L1_CARGO_ENV) == "1"


def _gz_physical_battery_enabled() -> bool:
    return os.getenv(GZ_PHYSICAL_BATTERY_ENV) == "1"


def _gz_battery_motor_coupling_enabled() -> bool:
    # Motor-load coupling only makes sense when the physical battery is present.
    return (
        _gz_physical_battery_enabled()
        and os.getenv(GZ_BATTERY_MOTOR_COUPLING_ENV) == "1"
    )


def _optional_float_env(name: str) -> float | None:
    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _wind_vector(*, mean_mps: float, direction_deg: float) -> tuple[float, float]:
    radians = math.radians(direction_deg)
    return (
        round(mean_mps * math.sin(radians), 6),
        round(mean_mps * math.cos(radians), 6),
    )


def _wind_requested_profile() -> dict[str, Any]:
    requested = {
        "wind_mean_mps": _optional_float_env(WIND_MEAN_MPS_ENV),
        "wind_direction_deg": _optional_float_env(WIND_DIRECTION_DEG_ENV),
        "wind_gust_mps": _optional_float_env(WIND_GUST_MPS_ENV),
        "wind_variance": _optional_float_env(WIND_VARIANCE_ENV),
    }
    if requested["wind_direction_deg"] is None:
        requested["wind_direction_deg"] = 0.0
    return {
        "schema_version": "environment_condition_profile.v1",
        "condition_id": "environment_condition_profile:missionos_auto_wind_gust",
        "condition_kind": "wind_gust",
        "requested": requested,
        "requested_present": any(
            value is not None
            for key, value in requested.items()
            if key != "wind_direction_deg"
        ),
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _rain_weather_requested_profile() -> dict[str, Any]:
    precipitation = _optional_float_env(PRECIPITATION_MM_PER_HOUR_ENV)
    if precipitation is not None:
        precipitation = max(0.0, min(500.0, precipitation))
    visual_mode = (os.getenv(RAIN_VISUAL_MODE_ENV) or "").strip().lower()
    if visual_mode not in ("", "rain", "visual_rain"):
        visual_mode = ""
    requested = {
        "precipitation_mm_per_hour": precipitation,
        "rain_visual_mode": visual_mode
        or ("rain" if precipitation and precipitation > 0 else ""),
        "rain_battery_drain_factor": _optional_float_env(RAIN_BATTERY_DRAIN_FACTOR_ENV),
        "rain_sensor_degradation_factor": _optional_float_env(
            RAIN_SENSOR_DEGRADATION_FACTOR_ENV
        ),
        "rain_landing_risk_factor": _optional_float_env(RAIN_LANDING_RISK_FACTOR_ENV),
    }
    if requested["rain_battery_drain_factor"] is not None:
        requested["rain_battery_drain_factor"] = max(
            0.1,
            min(10.0, float(requested["rain_battery_drain_factor"])),
        )
    if requested["rain_sensor_degradation_factor"] is not None:
        requested["rain_sensor_degradation_factor"] = max(
            0.0,
            min(1.0, float(requested["rain_sensor_degradation_factor"])),
        )
    if requested["rain_landing_risk_factor"] is not None:
        requested["rain_landing_risk_factor"] = max(
            1.0,
            min(10.0, float(requested["rain_landing_risk_factor"])),
        )
    requested_present = bool(
        (precipitation is not None and precipitation > 0.0)
        or requested["rain_visual_mode"]
        or requested["rain_battery_drain_factor"] is not None
        or requested["rain_sensor_degradation_factor"] is not None
        or requested["rain_landing_risk_factor"] is not None
    )
    return {
        "schema_version": "rain_weather_condition_profile.v1",
        "condition_id": "rain_weather_condition_profile:missionos_auto_rain",
        "condition_kind": "rain_weather",
        "requested": requested,
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "rain_physics_claimed": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _rain_battery_drain_factor_from_precipitation(
    precipitation_mm_per_hour: float | None,
) -> float | None:
    if precipitation_mm_per_hour is None or precipitation_mm_per_hour <= 0.0:
        return None
    return round(min(1.8, 1.0 + precipitation_mm_per_hour * 0.04), 3)


def _rain_sensor_degradation_factor_from_precipitation(
    precipitation_mm_per_hour: float | None,
) -> float | None:
    if precipitation_mm_per_hour is None or precipitation_mm_per_hour <= 0.0:
        return None
    return round(min(0.45, precipitation_mm_per_hour * 0.035), 3)


def _rain_landing_risk_factor_from_precipitation(
    precipitation_mm_per_hour: float | None,
) -> float | None:
    if precipitation_mm_per_hour is None or precipitation_mm_per_hour <= 0.0:
        return None
    return round(min(2.5, 1.0 + precipitation_mm_per_hour * 0.08), 3)


def _rain_weather_runtime_config(
    *,
    baseline_sim_bat_drain_seconds: float,
) -> dict[str, Any]:
    profile = _rain_weather_requested_profile()
    requested = profile["requested"]
    precipitation = requested.get("precipitation_mm_per_hour")
    requested_present = bool(profile.get("requested_present"))
    approximation_reasons: list[str] = []
    unsupported_reasons: list[str] = []
    battery_factor = requested.get("rain_battery_drain_factor")
    sensor_factor = requested.get("rain_sensor_degradation_factor")
    landing_factor = requested.get("rain_landing_risk_factor")
    if requested_present:
        if battery_factor is None:
            battery_factor = _rain_battery_drain_factor_from_precipitation(precipitation)
        if sensor_factor is None:
            sensor_factor = _rain_sensor_degradation_factor_from_precipitation(
                precipitation
            )
        if landing_factor is None:
            landing_factor = _rain_landing_risk_factor_from_precipitation(
                precipitation
            )
        if battery_factor is None:
            battery_factor = 1.0
        if sensor_factor is None:
            sensor_factor = 0.0
        if landing_factor is None:
            landing_factor = 1.0
        approximation_reasons.extend(
            [
                "rain_visual_is_sdf_marker_not_precipitation_physics",
                "rain_battery_drain_uses_bounded_sitl_model",
                "rain_sensor_degradation_is_agent_risk_context",
                "rain_landing_risk_is_agent_risk_context",
            ]
        )
    effective_drain_seconds = (
        max(
            60.0,
            round(
                float(baseline_sim_bat_drain_seconds)
                / max(float(battery_factor or 1.0), 0.1),
                3,
            ),
        )
        if requested_present
        else float(baseline_sim_bat_drain_seconds)
    )
    return {
        "profile": profile,
        "baseline_sim_bat_drain_seconds": float(baseline_sim_bat_drain_seconds),
        "effective_sim_bat_drain_seconds": effective_drain_seconds,
        "rain_battery_drain_factor": battery_factor,
        "rain_sensor_degradation_factor": sensor_factor,
        "rain_landing_risk_factor": landing_factor,
        "rain_effect_requested": requested_present,
        "approximation_reasons": approximation_reasons,
        "unsupported_reasons": unsupported_reasons,
    }


def _thermal_weather_requested_profile() -> dict[str, Any]:
    requested = {
        "temperature_c": _optional_float_env(TEMPERATURE_C_ENV),
        "pressure_hpa": _optional_float_env(PRESSURE_HPA_ENV),
        "thermal_battery_drain_factor": _optional_float_env(
            THERMAL_BATTERY_DRAIN_FACTOR_ENV
        ),
        "thermal_motor_derate_factor": _optional_float_env(
            THERMAL_MOTOR_DERATE_FACTOR_ENV
        ),
    }
    temperature = requested["temperature_c"]
    if temperature is not None and (temperature < -80.0 or temperature > 80.0):
        requested["temperature_c"] = None
    pressure = requested["pressure_hpa"]
    if pressure is not None and (pressure < 500.0 or pressure > 1100.0):
        requested["pressure_hpa"] = None
    if requested["thermal_battery_drain_factor"] is not None:
        requested["thermal_battery_drain_factor"] = max(
            0.1,
            min(10.0, float(requested["thermal_battery_drain_factor"])),
        )
    if requested["thermal_motor_derate_factor"] is not None:
        requested["thermal_motor_derate_factor"] = max(
            0.1,
            min(1.0, float(requested["thermal_motor_derate_factor"])),
        )
    return {
        "schema_version": "thermal_weather_condition_profile.v1",
        "condition_id": "thermal_weather_condition_profile:missionos_auto_temperature",
        "condition_kind": "thermal_weather",
        "requested": requested,
        "requested_present": any(value is not None for value in requested.values()),
        "source": "mission_designer_coordinate_route",
        "thermal_air_physics_claimed": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _thermal_battery_drain_factor_from_temperature(
    temperature_c: float | None,
) -> float | None:
    if temperature_c is None:
        return None
    if temperature_c >= 35.0:
        return round(min(2.5, 1.0 + (temperature_c - 25.0) * 0.04), 3)
    if temperature_c <= 0.0:
        return round(min(2.2, 1.0 + abs(temperature_c) * 0.03), 3)
    return 1.0


def _thermal_motor_derate_factor_from_temperature(
    temperature_c: float | None,
) -> float | None:
    if temperature_c is None:
        return None
    if temperature_c >= 40.0:
        return round(max(0.55, 1.0 - (temperature_c - 35.0) * 0.015), 3)
    return 1.0


def _thermal_weather_runtime_config(
    *,
    baseline_sim_bat_drain_seconds: float,
) -> dict[str, Any]:
    profile = _thermal_weather_requested_profile()
    requested = profile["requested"]
    temperature_c = requested.get("temperature_c")
    pressure_hpa = requested.get("pressure_hpa")
    explicit_battery_factor = requested.get("thermal_battery_drain_factor")
    explicit_motor_factor = requested.get("thermal_motor_derate_factor")
    thermal_effect_requested = any(
        value is not None
        for value in (temperature_c, explicit_battery_factor, explicit_motor_factor)
    )
    approximation_reasons: list[str] = []
    unsupported_reasons: list[str] = []
    battery_factor = None
    motor_factor = None
    effective_drain_seconds = float(baseline_sim_bat_drain_seconds)
    if pressure_hpa is not None:
        approximation_reasons.append("pressure_hpa_recorded_for_context_not_air_physics")
    if thermal_effect_requested:
        battery_factor = (
            float(explicit_battery_factor)
            if explicit_battery_factor is not None
            else _thermal_battery_drain_factor_from_temperature(temperature_c)
        )
        motor_factor = (
            float(explicit_motor_factor)
            if explicit_motor_factor is not None
            else _thermal_motor_derate_factor_from_temperature(temperature_c)
        )
        if battery_factor is None:
            battery_factor = 1.0
        if motor_factor is None:
            motor_factor = 1.0
        if explicit_battery_factor is None and temperature_c is not None:
            approximation_reasons.append(
                "temperature_to_battery_drain_factor_uses_bounded_sitl_model"
            )
        if explicit_motor_factor is None and temperature_c is not None:
            approximation_reasons.append(
                "temperature_to_motor_derate_factor_uses_bounded_sitl_model"
            )
        effective_drain_seconds = max(
            60.0,
            round(
                float(baseline_sim_bat_drain_seconds)
                / max(float(battery_factor), 0.1),
                3,
            ),
        )
    elif profile["requested_present"]:
        unsupported_reasons.append("thermal_battery_or_motor_condition_not_requested")
        if pressure_hpa is not None:
            unsupported_reasons.append(
                "pressure_physics_not_supported_by_bounded_sitl_model"
            )
    return {
        "profile": profile,
        "baseline_sim_bat_drain_seconds": float(baseline_sim_bat_drain_seconds),
        "effective_sim_bat_drain_seconds": effective_drain_seconds,
        "thermal_battery_drain_factor": battery_factor,
        "thermal_motor_derate_factor": motor_factor,
        "thermal_effect_requested": thermal_effect_requested,
        "approximation_reasons": approximation_reasons,
        "unsupported_reasons": unsupported_reasons,
    }


def _thermal_param_set_applied(result: Mapping[str, Any]) -> bool:
    output = (
        f"{result.get('stdout_tail') or ''}\n{result.get('stderr_tail') or ''}".lower()
    )
    return result.get("returncode") == 0 and "not found" not in output


def _thermal_weather_runtime_artifacts(
    *,
    config: Mapping[str, Any],
    probe_observed: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    profile = dict(config.get("profile") or {})
    requested_present = bool(profile.get("requested_present"))
    thermal_effect_requested = bool(config.get("thermal_effect_requested"))
    approximation_reasons = list(config.get("approximation_reasons") or ())
    unsupported_reasons = list(config.get("unsupported_reasons") or ())
    setup = list(probe_observed.get("battery_sim_setup") or ())
    applied_param_names = {
        str(item.get("param")) for item in setup if isinstance(item, Mapping)
    }
    params_set = all(_thermal_param_set_applied(item) for item in setup) if setup else False
    motor_materialized = "MPC_THR_MAX" in applied_param_names
    application_status = "not_requested"
    observation_status = "not_requested"
    thermal_capability_status = "not_requested"
    battery_drain_status = "not_requested"
    motor_derate_status = "not_requested"
    if requested_present and thermal_effect_requested:
        if params_set:
            application_status = "applied_with_approximations"
            observation_status = "thermal_condition_param_set_observed"
            thermal_capability_status = "supported"
            battery_drain_status = "supported"
            motor_derate_status = "supported" if motor_materialized else "not_materialized"
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            thermal_capability_status = "unsupported"
            battery_drain_status = "unsupported"
            motor_derate_status = "unsupported" if motor_materialized else "not_materialized"
            unsupported_reasons.append("px4_thermal_param_set_failed")
    elif requested_present:
        application_status = "unsupported"
        observation_status = "unsupported"
        thermal_capability_status = "unsupported"
    applied = {}
    observed = {}
    if requested_present:
        applied = {
            "method": "missionos_auto_px4_runtime_param_thermal_battery_motor_model",
            "target": "px4_runtime_params",
            "baseline_sim_bat_drain_seconds": config.get(
                "baseline_sim_bat_drain_seconds"
            ),
            "effective_sim_bat_drain_seconds": config.get(
                "effective_sim_bat_drain_seconds"
            ),
            "thermal_battery_drain_factor": config.get(
                "thermal_battery_drain_factor"
            ),
            "thermal_motor_derate_factor": config.get("thermal_motor_derate_factor"),
            "battery_sim_setup": setup,
            "thermal_air_physics_claimed": False,
            "motor_derate_param_materialized": motor_materialized,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        observed = {
            "source": "missionos-auto-probe-param-set-and-battery-status",
            "observed": params_set and thermal_effect_requested,
            "battery_remaining_percent": summary.get("battery_remaining_percent"),
            "battery_remaining_delta_percent": summary.get(
                "battery_remaining_delta_percent"
            ),
            "battery_remaining_sample_count": summary.get(
                "battery_remaining_sample_count"
            ),
            "battery_state_source": summary.get("battery_state_source"),
            "thermal_air_physics_claimed": False,
            "battery_sim_setup": setup,
        }
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:missionos_auto_thermal_weather",
        "thermal_weather": thermal_capability_status,
        "battery_drain_temperature_effect": battery_drain_status,
        "motor_derate_temperature_effect": motor_derate_status,
        "air_temperature_physics": "not_claimed",
        "pressure_physics": "not_claimed",
        "support_detection_method": (
            "px4_param_set_result_and_battery_status_listener"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": (
            "simulator_condition_application:missionos_auto_thermal_weather"
        ),
        "condition_kind": "thermal_weather",
        "application_status": application_status,
        "requested_condition_ref": profile.get("condition_id"),
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:missionos_auto_thermal_weather",
        "condition_kind": "thermal_weather",
        "observation_status": observation_status,
        "requested_condition_ref": profile.get("condition_id"),
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "thermal_weather_condition_profile": profile,
        "thermal_weather_simulator_capability_matrix": capability,
        "thermal_weather_simulator_condition_application": application,
        "observed_thermal_weather_evidence": evidence,
    }


def _condition_world_readback(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "auto_condition_world_readback.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _auto_wind_gust_runtime_artifacts(
    *,
    profile: Mapping[str, Any],
    probe_observed: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    requested_present = bool(profile.get("requested_present"))
    readback = _condition_world_readback(run_dir)
    monitor = dict(probe_observed.get("monitor") or {})
    wind_events = [
        dict(event)
        for event in monitor.get("wind_application_events") or ()
        if isinstance(event, Mapping)
    ]
    event_phases = {str(event.get("phase")) for event in wind_events}
    any_published = any(bool(event.get("published")) for event in wind_events)
    mean_wind_materialized = bool(
        event_phases
        & {
            "auto_mission_mean_wind",
            "auto_mission_mean_wind_after_takeoff_clearance",
            "preflight_mean_wind",
        }
    )
    mean_wind_delayed = bool(
        event_phases & {"auto_mission_mean_wind_delayed_until_takeoff_clearance"}
    )
    gust_window_materialized = (
        mean_wind_materialized
        and {"gust_window_start", "gust_window_end_return_to_mean"}.issubset(
            event_phases
        )
        and any_published
    )
    world_materialized = bool(readback.get("wind_effects_plugin_materialized"))
    vehicle_wind_enabled = bool(readback.get("wind_enabled_on_vehicle_links"))
    application_status = "not_requested"
    observation_status = "not_requested"
    wind_mean_status = "not_requested"
    wind_gust_status = "not_requested"
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    if requested_present:
        if gust_window_materialized and world_materialized and vehicle_wind_enabled:
            application_status = "applied_with_approximations"
            observation_status = "wind_gust_window_observed"
            wind_mean_status = "supported"
            wind_gust_status = "materialized_gz_wind_window"
        elif any_published:
            application_status = "applied_with_approximations"
            observation_status = "wind_topic_publish_observed"
            wind_mean_status = "supported"
            wind_gust_status = "partial"
            if not world_materialized:
                unsupported_reasons.append("wind_effects_world_sdf_not_observed")
            if not vehicle_wind_enabled:
                unsupported_reasons.append("wind_not_enabled_on_vehicle_links")
        elif mean_wind_delayed:
            application_status = "blocked"
            observation_status = "wind_delayed_until_takeoff_clearance_not_reached"
            wind_mean_status = "pending_takeoff_clearance"
            wind_gust_status = "not_materialized"
            unsupported_reasons.append("takeoff_clearance_not_observed_before_monitor_stop")
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            wind_mean_status = "unsupported"
            wind_gust_status = "unsupported"
            unsupported_reasons.append("gazebo_wind_topic_publish_not_observed")
        approximation_reasons.append(
            "auto_gust_uses_deterministic_gz_wind_topic_window_not_stochastic_weather"
        )
        approximation_reasons.append(
            "gz_wind_is_delayed_until_takeoff_clearance_to_avoid_ground_slide_overclaim"
        )
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:missionos_auto_wind_gust",
        "wind_mean": wind_mean_status,
        "wind_gust": wind_gust_status,
        "wind_variance": (
            "approximated"
            if requested_present
            and dict(profile.get("requested") or {}).get("wind_variance") is not None
            else "not_requested"
        ),
        "support_detection_method": (
            "gz_wind_topic_publish_events_and_sdf_readback"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:missionos_auto_wind_gust",
        "condition_kind": "wind_gust",
        "application_status": application_status,
        "requested_condition_ref": profile.get("condition_id"),
        "applied": (
            {
                "method": "gz_topic_wind_window",
                "target": "/world/default/wind",
                "wind_application_events": wind_events,
                "wind_mean_delayed_until_takeoff_clearance": mean_wind_delayed,
                "wind_effects_plugin_materialized": world_materialized,
                "wind_enabled_on_vehicle_links": vehicle_wind_enabled,
                "world_sdf_path": readback.get("world_sdf_path"),
                "world_sdf_sha256": readback.get("world_sdf_sha256"),
                "gust_physics_claimed": False,
            }
            if requested_present
            else {}
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:missionos_auto_wind_gust",
        "condition_kind": "wind_gust",
        "observation_status": observation_status,
        "requested_condition_ref": profile.get("condition_id"),
        "application_ref": application["application_id"],
        "observed": (
            {
                "source": "missionos-auto-probe-gz-wind-publish-and-sdf-readback",
                "observed": observation_status not in ("not_requested", "unsupported"),
                "wind_application_events": wind_events,
                "wind_gust_window_materialized": gust_window_materialized,
                "wind_mean_delayed_until_takeoff_clearance": mean_wind_delayed,
                "wind_effects_plugin_materialized": world_materialized,
                "wind_enabled_on_vehicle_links": vehicle_wind_enabled,
                "world_sdf_sha256": readback.get("world_sdf_sha256"),
                "gust_physics_claimed": False,
            }
            if requested_present
            else {}
        ),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "environment_condition_profile": dict(profile),
        "simulator_capability_matrix": capability,
        "simulator_condition_application": application,
        "observed_environment_evidence": evidence,
    }


def _rain_weather_runtime_artifacts(
    *,
    config: Mapping[str, Any],
    probe_observed: Mapping[str, Any],
    summary: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    profile = dict(config.get("profile") or {})
    requested = dict(profile.get("requested") or {})
    requested_present = bool(profile.get("requested_present"))
    rain_effect_requested = bool(config.get("rain_effect_requested"))
    approximation_reasons = list(config.get("approximation_reasons") or ())
    unsupported_reasons = list(config.get("unsupported_reasons") or ())
    setup = list(probe_observed.get("battery_sim_setup") or ())
    sim_bat_drain_results = [
        item
        for item in setup
        if isinstance(item, Mapping) and str(item.get("param")) == "SIM_BAT_DRAIN"
    ]
    battery_param_set = any(
        _thermal_param_set_applied(item) for item in sim_bat_drain_results
    )
    readback = _condition_world_readback(run_dir)
    visual_requested = bool(requested.get("rain_visual_mode"))
    visual_materialized = bool(readback.get("rain_visual_marker_materialized"))
    application_status = "not_requested"
    observation_status = "not_requested"
    rain_weather_status = "not_requested"
    visual_status = "not_requested"
    battery_status = "not_requested"
    sensor_status = "not_requested"
    landing_status = "not_requested"
    if requested_present and rain_effect_requested:
        if battery_param_set or visual_materialized:
            application_status = "applied_with_approximations"
            observation_status = (
                "rain_condition_marker_and_param_observed"
                if visual_materialized and battery_param_set
                else (
                    "rain_condition_visual_marker_observed"
                    if visual_materialized
                    else "rain_condition_param_set_observed"
                )
            )
            rain_weather_status = "supported"
            battery_status = "supported" if battery_param_set else "not_materialized"
            visual_status = (
                "supported"
                if visual_materialized
                else ("not_materialized" if visual_requested else "not_requested")
            )
            sensor_status = "approximated_agent_risk_context"
            landing_status = "approximated_agent_risk_context"
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            rain_weather_status = "unsupported"
            visual_status = "unsupported" if visual_requested else "not_requested"
            battery_status = "unsupported"
            sensor_status = "unsupported"
            landing_status = "unsupported"
            unsupported_reasons.append("rain_condition_runtime_application_not_observed")
        if visual_requested and not visual_materialized:
            unsupported_reasons.append("rain_visual_sdf_marker_not_observed")
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:missionos_auto_rain_weather",
        "rain_weather": rain_weather_status,
        "visual_rain": visual_status,
        "precipitation_physics": "not_claimed",
        "battery_drain_rain_effect": battery_status,
        "sensor_degradation_rain_effect": sensor_status,
        "landing_risk_rain_effect": landing_status,
        "support_detection_method": (
            "sdf_marker_readback_and_px4_param_set_result"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    applied = {}
    observed = {}
    if requested_present:
        applied = {
            "method": "missionos_auto_rain_visual_and_bounded_risk_model",
            "target": "gazebo_sdf_marker_and_px4_runtime_params",
            "precipitation_mm_per_hour": requested.get("precipitation_mm_per_hour"),
            "rain_visual_mode": requested.get("rain_visual_mode"),
            "baseline_sim_bat_drain_seconds": config.get(
                "baseline_sim_bat_drain_seconds"
            ),
            "effective_sim_bat_drain_seconds": config.get(
                "effective_sim_bat_drain_seconds"
            ),
            "rain_battery_drain_factor": config.get("rain_battery_drain_factor"),
            "rain_sensor_degradation_factor": config.get(
                "rain_sensor_degradation_factor"
            ),
            "rain_landing_risk_factor": config.get("rain_landing_risk_factor"),
            "battery_sim_setup": setup,
            "rain_visual_marker_materialized": visual_materialized,
            "world_sdf_path": readback.get("world_sdf_path"),
            "world_sdf_sha256": readback.get("world_sdf_sha256"),
            "rain_physics_claimed": False,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        observed = {
            "source": "missionos-auto-probe-sdf-readback-param-set-and-battery-status",
            "observed": observation_status not in ("not_requested", "unsupported"),
            "battery_remaining_percent": summary.get("battery_remaining_percent"),
            "battery_remaining_delta_percent": summary.get(
                "battery_remaining_delta_percent"
            ),
            "battery_remaining_sample_count": summary.get(
                "battery_remaining_sample_count"
            ),
            "battery_state_source": summary.get("battery_state_source"),
            "rain_visual_marker_materialized": visual_materialized,
            "battery_param_set_observed": battery_param_set,
            "rain_physics_claimed": False,
            "readback": readback,
        }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:missionos_auto_rain_weather",
        "condition_kind": "rain_weather",
        "application_status": application_status,
        "requested_condition_ref": profile.get("condition_id"),
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:missionos_auto_rain_weather",
        "condition_kind": "rain_weather",
        "observation_status": observation_status,
        "requested_condition_ref": profile.get("condition_id"),
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "rain_weather_condition_profile": profile,
        "rain_weather_simulator_capability_matrix": capability,
        "rain_weather_simulator_condition_application": application,
        "observed_rain_weather_evidence": evidence,
    }


def _gz_coupler_plugin_so() -> str:
    return os.getenv(GZ_COUPLER_PLUGIN_SO_ENV, "").strip()


def _gz_motor_load_coupler_sdf_patch(
    *,
    idle_power_w: float = GZ_COUPLER_IDLE_POWER_W,
    hover_power_w: float = GZ_COUPLER_HOVER_POWER_W,
    hover_rotor_rad_s: float = GZ_COUPLER_HOVER_ROTOR_RAD_S,
    rotor_velocity_slowdown: float = GZ_COUPLER_ROTOR_VELOCITY_SLOWDOWN,
    max_power_w: float = GZ_COUPLER_MAX_POWER_W,
    capacity_ah: float = DEFAULT_GZ_BATTERY_CAPACITY_AH,
    voltage_full_v: float = GZ_COUPLER_VOLTAGE_FULL_V,
    voltage_empty_v: float = GZ_COUPLER_VOLTAGE_EMPTY_V,
    publish_rate_hz: float = GZ_COUPLER_PUBLISH_RATE_HZ,
    rotor_joints: tuple[str, ...] = GZ_COUPLER_ROTOR_JOINTS,
) -> str:
    """MotorLoadBatteryCoupler block coupling rotor effort to battery discharge.

    This references the custom gz-sim System under
    ``simulators/gazebo/plugins/motor_load_battery_coupler/``. gz-sim 8 exposes
    no proportional power-set interface on the stock LinearBatteryPlugin, so the
    coupler OWNS the battery integration: it samples rotor joint velocities,
    discharges its own state-of-charge proportionally, and publishes
    ``BatteryState`` on ``GZ_BATTERY_STATE_TOPIC`` (the same topic the probe
    reads). Live-verified against gz-sim 8.11.0: idle->hover drives current
    ~0.48A->~7.4A (12W->180W). Still a Gazebo simulation model, not real
    power-module endurance evidence. Without the compiled ``.so`` on the plugin
    path gz-sim simply logs that the system failed to load (self-revealing).
    """

    rotor_lines = "\n".join(
        f"      <rotor_joint>{name}</rotor_joint>" for name in rotor_joints
    )
    return f"""
    <plugin filename="MotorLoadBatteryCoupler"
            name="boiled_claw::MotorLoadBatteryCoupler">
      <battery_name>{GZ_BATTERY_NAME}</battery_name>
      <state_topic>{GZ_BATTERY_STATE_TOPIC}</state_topic>
{rotor_lines}
      <idle_power_w>{idle_power_w:.3f}</idle_power_w>
      <hover_power_w>{hover_power_w:.3f}</hover_power_w>
      <hover_rotor_rad_s>{hover_rotor_rad_s:.3f}</hover_rotor_rad_s>
      <rotor_velocity_slowdown>{rotor_velocity_slowdown:.3f}</rotor_velocity_slowdown>
      <max_power_w>{max_power_w:.3f}</max_power_w>
      <capacity_ah>{capacity_ah:.4f}</capacity_ah>
      <voltage_full_v>{voltage_full_v:.3f}</voltage_full_v>
      <voltage_empty_v>{voltage_empty_v:.3f}</voltage_empty_v>
      <publish_rate_hz>{publish_rate_hz:.3f}</publish_rate_hz>
    </plugin>
"""


def _gz_battery_model_sdf_patch(
    *,
    capacity_ah: float = DEFAULT_GZ_BATTERY_CAPACITY_AH,
    voltage_v: float = DEFAULT_GZ_BATTERY_VOLTAGE_V,
    power_load_w: float = DEFAULT_GZ_BATTERY_POWER_LOAD_W,
) -> str:
    """gz-sim LinearBatteryPlugin block injected into the x500 model SDF.

    Models a real-capacity (Ah) / voltage battery in Gazebo and publishes
    ``BatteryState`` on ``GZ_BATTERY_STATE_TOPIC`` with a fixed ``<power_load>``.
    Used only when motor-load coupling is OFF; when coupling is ON the
    MotorLoadBatteryCoupler owns the battery and supplies a thrust-proportional
    discharge instead (see ``_gz_motor_load_coupler_sdf_patch``).
    """

    return f"""
    <plugin filename="gz-sim-linearbatteryplugin-system"
            name="gz::sim::systems::LinearBatteryPlugin">
      <battery_name>{GZ_BATTERY_NAME}</battery_name>
      <voltage>{voltage_v:.3f}</voltage>
      <open_circuit_voltage_constant_coef>{voltage_v:.3f}</open_circuit_voltage_constant_coef>
      <open_circuit_voltage_linear_coef>-3.0</open_circuit_voltage_linear_coef>
      <initial_charge>{capacity_ah:.4f}</initial_charge>
      <capacity>{capacity_ah:.4f}</capacity>
      <resistance>0.07</resistance>
      <smooth_current_tau>2.0</smooth_current_tau>
      <power_load>{power_load_w:.3f}</power_load>
      <start_draining>true</start_draining>
      <link_name>base_link</link_name>
    </plugin>
"""


def _payload_model_sdf_patch() -> str:
    return """
    <plugin filename="gz-sim-detachable-joint-system"
            name="gz::sim::systems::DetachableJoint">
      <parent_link>base_link</parent_link>
      <child_model>delivery_payload</child_model>
      <child_link>payload_link</child_link>
      <detach_topic>/model/x500_0/delivery_payload/detach</detach_topic>
      <attach_topic>/model/x500_0/delivery_payload/attach</attach_topic>
      <output_topic>/model/x500_0/delivery_payload/state</output_topic>
    </plugin>
"""


def _payload_world_sdf_patch(*, payload_mass_kg: float = 0.05) -> str:
    return f"""
    <model name="delivery_payload">
      <pose>0 0 0.04 0 0 0</pose>
      <static>false</static>
      <link name="payload_link">
        <inertial>
          <mass>{payload_mass_kg:.6f}</mass>
          <inertia>
            <ixx>0.0001</ixx><ixy>0</ixy><ixz>0</ixz>
            <iyy>0.0001</iyy><iyz>0</iyz><izz>0.0001</izz>
          </inertia>
        </inertial>
        <collision name="payload_collision">
          <geometry><box><size>0.12 0.12 0.08</size></box></geometry>
        </collision>
        <visual name="payload_visual">
          <geometry><box><size>0.12 0.12 0.08</size></box></geometry>
          <material><diffuse>0.1 0.5 1.0 1</diffuse></material>
        </visual>
      </link>
    </model>
"""


def _wind_effects_world_sdf_patch() -> str:
    return """
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics">
    </plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands">
    </plugin>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster">
    </plugin>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu">
    </plugin>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure">
    </plugin>
    <plugin filename="gz-sim-air-speed-system" name="gz::sim::systems::AirSpeed">
    </plugin>
    <wind>
      <linear_velocity>0 0 0</linear_velocity>
    </wind>
    <plugin filename="gz-sim-apply-link-wrench-system" name="gz::sim::systems::ApplyLinkWrench">
    </plugin>
    <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat">
    </plugin>
    <plugin filename="gz-sim-magnetometer-system" name="gz::sim::systems::Magnetometer">
    </plugin>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine>
    </plugin>
    <plugin filename="libOpticalFlowSystem.so" name="custom::OpticalFlowSystem">
    </plugin>
    <plugin filename="libGstCameraSystem.so" name="custom::GstCameraSystem">
    </plugin>
    <plugin filename="gz-sim-wind-effects-system" name="gz::sim::systems::WindEffects">
      <force_approximation_scaling_factor>1</force_approximation_scaling_factor>
      <horizontal>
        <magnitude>
          <time_for_rise>1</time_for_rise>
          <sin>
            <amplitude_percent>0.0</amplitude_percent>
            <period>60</period>
          </sin>
          <noise type="gaussian">
            <mean>0</mean>
            <stddev>0</stddev>
          </noise>
        </magnitude>
        <direction>
          <time_for_rise>1</time_for_rise>
          <sin>
            <amplitude>0</amplitude>
            <period>60</period>
          </sin>
          <noise type="gaussian">
            <mean>0</mean>
            <stddev>0</stddev>
          </noise>
        </direction>
      </horizontal>
      <vertical>
        <noise type="gaussian">
          <mean>0</mean>
          <stddev>0</stddev>
        </noise>
      </vertical>
    </plugin>
"""


def _enable_wind_on_x500_base(model_root: Path) -> dict[str, Any]:
    x500_base_sdf_path = model_root / "x500_base" / "model.sdf"
    if not x500_base_sdf_path.exists():
        return {
            "wind_enabled_on_vehicle_links": False,
            "wind_enabled_link_count": 0,
            "x500_base_sdf_path": str(x500_base_sdf_path),
            "error": "x500_base_model_sdf_missing",
        }
    x500_base_sdf = x500_base_sdf_path.read_text(encoding="utf-8")
    if "<enable_wind>true</enable_wind>" not in x500_base_sdf:
        x500_base_sdf = re.sub(
            r'(<link name="[^"]+">\n)',
            r"\1      <enable_wind>true</enable_wind>\n",
            x500_base_sdf,
        )
        x500_base_sdf_path.write_text(x500_base_sdf, encoding="utf-8")
    wind_enabled_link_count = x500_base_sdf_path.read_text(encoding="utf-8").count(
        "<enable_wind>true</enable_wind>"
    )
    return {
        "wind_enabled_on_vehicle_links": wind_enabled_link_count > 0,
        "wind_enabled_link_count": wind_enabled_link_count,
        "x500_base_sdf_path": str(x500_base_sdf_path),
    }


def _rain_visual_world_sdf_patch(*, precipitation_mm_per_hour: float | None) -> str:
    intensity = 0.35
    if precipitation_mm_per_hour is not None:
        intensity = max(0.15, min(1.0, float(precipitation_mm_per_hour) / 12.0))
    return f"""
    <model name="missionos_rain_visual_marker">
      <pose>0 0 18 0 0 0</pose>
      <static>true</static>
      <link name="rain_visual_link">
        <visual name="rain_visual_sheet">
          <geometry><box><size>80 80 0.03</size></box></geometry>
          <material>
            <ambient>0.25 0.45 0.95 {intensity:.3f}</ambient>
            <diffuse>0.25 0.45 0.95 {intensity:.3f}</diffuse>
          </material>
          <transparency>{max(0.15, 1.0 - intensity):.3f}</transparency>
        </visual>
      </link>
    </model>
"""


def _prepare_l1_payload_model_root(
    run_dir: Path,
    *,
    payload_enabled: bool,
    wind_profile: Mapping[str, Any],
    rain_config: Mapping[str, Any],
) -> Path:
    model_root = (run_dir / "auto_runtime_condition_models").resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    upload_smoke._run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{model_root}:/out",
            upload_smoke.PX4_GAZEBO_IMAGE,
            "-lc",
            (
                "rm -rf /out/x500 /out/x500_base /out/worlds; "
                "mkdir -p /out/worlds; "
                "cp -a /opt/px4-gazebo/share/gz/models/x500 /out/x500; "
                "cp -a /opt/px4-gazebo/share/gz/models/x500_base /out/x500_base; "
                "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /out/worlds/default.sdf"
            ),
        ],
        timeout=120,
    )
    sdf_path = model_root / "x500" / "model.sdf"
    sdf_text = sdf_path.read_text(encoding="utf-8")
    if payload_enabled and "delivery_payload" not in sdf_text:
        sdf_text = sdf_text.replace(
            "  </model>\n</sdf>",
            _payload_model_sdf_patch() + "  </model>\n</sdf>",
        )
        sdf_path.write_text(sdf_text, encoding="utf-8")
    if _gz_physical_battery_enabled() and GZ_BATTERY_NAME not in sdf_text:
        sdf_text = sdf_path.read_text(encoding="utf-8")
        if _gz_battery_motor_coupling_enabled():
            # Motor-load coupling: the coupler OWNS the battery integration and
            # publishes BatteryState itself (gz-sim 8 has no proportional
            # power-set API on the stock LinearBatteryPlugin), so we inject only
            # the coupler -- not the LinearBatteryPlugin.
            injection = _gz_motor_load_coupler_sdf_patch()
        else:
            # No coupling: a fixed-load LinearBatteryPlugin models the battery.
            injection = _gz_battery_model_sdf_patch()
        sdf_text = sdf_text.replace(
            "  </model>\n</sdf>",
            injection + "  </model>\n</sdf>",
        )
        sdf_path.write_text(sdf_text, encoding="utf-8")
    world_path = model_root / "worlds" / "default.sdf"
    world_text = world_path.read_text(encoding="utf-8")
    if payload_enabled and "delivery_payload" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _payload_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if wind_profile.get("requested_present"):
        if "gz::sim::systems::WindEffects" not in world_text:
            world_text = world_text.replace(
                "  </world>\n</sdf>",
                _wind_effects_world_sdf_patch()
                + "  </world>\n</sdf>",
            )
        _enable_wind_on_x500_base(model_root)
    rain_profile = dict(rain_config.get("profile") or {})
    rain_requested = dict(rain_profile.get("requested") or {})
    if (
        rain_profile.get("requested_present")
        and rain_requested.get("rain_visual_mode")
        and "missionos_rain_visual_marker" not in world_text
    ):
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _rain_visual_world_sdf_patch(
                precipitation_mm_per_hour=rain_requested.get(
                    "precipitation_mm_per_hour"
                )
            )
            + "  </world>\n</sdf>",
        )
    world_path.write_text(world_text, encoding="utf-8")
    condition_readback = {
        "schema_version": "missionos_auto_condition_world_readback.v1",
        "world_sdf_path": str(world_path),
        "world_sdf_sha256": hashlib.sha256(world_path.read_bytes()).hexdigest(),
        "x500_base_sdf_path": str(model_root / "x500_base" / "model.sdf"),
        "x500_base_sdf_sha256": (
            hashlib.sha256((model_root / "x500_base" / "model.sdf").read_bytes()).hexdigest()
            if (model_root / "x500_base" / "model.sdf").exists()
            else ""
        ),
        "rain_visual_marker_materialized": "missionos_rain_visual_marker"
        in world_path.read_text(encoding="utf-8"),
        "wind_effects_plugin_materialized": "gz::sim::systems::WindEffects"
        in world_path.read_text(encoding="utf-8"),
        "wind_enabled_on_vehicle_links": "<enable_wind>true</enable_wind>"
        in (model_root / "x500_base" / "model.sdf").read_text(encoding="utf-8"),
    }
    (run_dir / "auto_condition_world_readback.json").write_text(
        json.dumps(condition_readback, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return model_root


def _custom_condition_world_requested(
    *,
    wind_profile: Mapping[str, Any],
    rain_config: Mapping[str, Any],
) -> bool:
    return bool(
        _l1_cargo_enabled()
        or _gz_physical_battery_enabled()
        or wind_profile.get("requested_present")
        or (rain_config.get("profile") or {}).get("requested_present")
    )


def _start_container(
    run_dir: Path,
    *,
    wind_profile: Mapping[str, Any] | None = None,
    rain_config: Mapping[str, Any] | None = None,
) -> Path | None:
    wind_profile = wind_profile or _wind_requested_profile()
    rain_config = rain_config or _rain_weather_runtime_config(
        baseline_sim_bat_drain_seconds=DEFAULT_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS
    )
    if not _custom_condition_world_requested(
        wind_profile=wind_profile,
        rain_config=rain_config,
    ):
        upload_smoke._start_container()
        return None

    payload_model_root = _prepare_l1_payload_model_root(
        run_dir,
        payload_enabled=_l1_cargo_enabled(),
        wind_profile=wind_profile,
        rain_config=rain_config,
    )
    px4_home_env: list[str] = []
    for name in ("PX4_HOME_LAT", "PX4_HOME_LON", "PX4_HOME_ALT"):
        value = os.getenv(name)
        if value:
            px4_home_env.extend(["-e", f"{name}={value}"])
    # Motor-load coupling: mount a prebuilt MotorLoadBatteryCoupler .so (if the
    # operator supplied one) and extend the gz-sim system plugin path so the
    # injected <plugin filename="MotorLoadBatteryCoupler"> can load. Without a
    # provided .so the SDF reference is harmless and self-revealing (gz logs a
    # failed-to-load warning), keeping the run truthful.
    coupler_plugin_env: list[str] = []
    coupler_so = _gz_coupler_plugin_so()
    if _gz_battery_motor_coupling_enabled() and coupler_so and Path(coupler_so).is_file():
        so_name = Path(coupler_so).name
        coupler_plugin_env = [
            "-v",
            f"{Path(coupler_so).resolve()}:{GZ_COUPLER_PLUGIN_CONTAINER_PATH}/{so_name}:ro",
            "-e",
            f"GZ_SIM_SYSTEM_PLUGIN_PATH={GZ_COUPLER_PLUGIN_CONTAINER_PATH}",
        ]
    upload_smoke._run(["docker", "rm", "-f", upload_smoke.CONTAINER_NAME], check=False)
    upload_smoke._run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            upload_smoke.CONTAINER_NAME,
            "-p",
            "14540:14540/udp",
            "-v",
            f"{payload_model_root}:{PAYLOAD_MODEL_CONTAINER_PATH}:ro",
            "-e",
            f"PX4_GZ_MODELS={PAYLOAD_MODEL_CONTAINER_PATH}",
            "-e",
            (
                "GZ_SIM_RESOURCE_PATH="
                f"{PAYLOAD_MODEL_CONTAINER_PATH}:"
                "/opt/px4-gazebo/share/gz/models"
            ),
            "-e",
            f"PX4_GZ_WORLDS={PAYLOAD_MODEL_CONTAINER_PATH}/worlds",
            "-e",
            f"PX4_SIM_MODEL={upload_smoke.PX4_MODEL}",
            "-e",
            f"PX4_GZ_WORLD={upload_smoke.GAZEBO_WORLD}",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            *px4_home_env,
            *coupler_plugin_env,
            upload_smoke.PX4_GAZEBO_IMAGE,
            "-d",
        ],
        timeout=240,
    )
    upload_smoke._wait_for_startup()
    return payload_model_root


def _l1_payload_release_not_observed_event(
    *,
    payload_release_command_acked: bool,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    if not _l1_cargo_enabled():
        return {
            "payload_release_observed": False,
            "payload_release_event_source": "",
            "blocked_reasons": ("l1_payload_release_model_not_enabled",),
            "gazebo_detachable_joint_release_performed": False,
            "gazebo_detachable_joint_release_observed": False,
        }
    if not payload_release_command_acked:
        return {
            "payload_release_observed": False,
            "payload_release_event_source": "",
            "blocked_reasons": ("payload_release_command_not_acked",),
            "gazebo_detachable_joint_release_performed": False,
            "gazebo_detachable_joint_release_observed": False,
        }
    if payload_model_root is None:
        return {
            "payload_release_observed": False,
            "payload_release_event_source": "",
            "blocked_reasons": ("l1_payload_model_root_missing",),
            "gazebo_detachable_joint_release_performed": False,
            "gazebo_detachable_joint_release_observed": False,
        }
    return {
        "payload_release_observed": False,
        "payload_release_event_source": "",
        "blocked_reasons": ("payload_release_sim_event_not_observed",),
        "gazebo_detachable_joint_release_performed": False,
        "gazebo_detachable_joint_release_observed": False,
        "payload_model_root": _rel_to_root(payload_model_root),
    }


def _parse_int(text: str, field: str) -> int | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+)", text)
    return int(match.group(1)) if match else None


def _parse_float(text: str, field: str) -> float | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _parse_bool(text: str, field: str) -> bool | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(True|False)", text)
    return (match.group(1) == "True") if match else None


def _battery_remaining_percent(text: str) -> float | None:
    value = _parse_float(text, "remaining")
    if value is None:
        value = _parse_float(text, "battery_remaining_percent")
    if value is None:
        return None
    return round(value * 100.0 if value <= 1.0 else value, 3)


def _global_degrees(text: str, field: str) -> float | None:
    value = _parse_float(text, field)
    if value is None:
        return None
    if abs(value) > 180.0:
        value = value / 10_000_000.0
    return round(value, 7)


def _global_altitude_m(text: str) -> float | None:
    value = _parse_float(text, "alt")
    if value is None:
        value = _parse_float(text, "alt_ellipsoid")
    if value is None:
        return None
    if abs(value) > 10_000.0:
        value = value / 1000.0
    return round(value, 3)


def _mission_result_seq(text: str, key: str) -> int | None:
    value = _parse_int(text, key)
    if value is None:
        return None
    return int(value)


def _sample_from_observed(raw: dict[str, Any], index: int) -> MissionOSAutoMissionTelemetrySample:
    local_position = str(raw.get("vehicle_local_position") or "")
    vehicle_status = str(raw.get("vehicle_status") or "")
    mission_result = str(raw.get("mission_result") or "")
    battery_status = str(raw.get("battery_status") or "")
    global_position = str(raw.get("vehicle_global_position") or "")
    battery_remaining = _battery_remaining_percent(battery_status)
    return MissionOSAutoMissionTelemetrySample(
        sample_index=index,
        elapsed_seconds=float(raw.get("elapsed_seconds") or 0.0),
        nav_state=_parse_int(vehicle_status, "nav_state"),
        arming_state=_parse_int(vehicle_status, "arming_state"),
        landed_state=_parse_int(vehicle_status, "landed_state"),
        local_x_m=_parse_float(local_position, "x"),
        local_y_m=_parse_float(local_position, "y"),
        local_z_m=_parse_float(local_position, "z"),
        local_vx_mps=_parse_float(local_position, "vx"),
        local_vy_mps=_parse_float(local_position, "vy"),
        local_vz_mps=_parse_float(local_position, "vz"),
        global_latitude_deg=_global_degrees(global_position, "lat"),
        global_longitude_deg=_global_degrees(global_position, "lon"),
        global_altitude_m=_global_altitude_m(global_position),
        mission_current_seq=_mission_result_seq(mission_result, "seq_current"),
        mission_reached_seq=_mission_result_seq(mission_result, "seq_reached"),
        battery_status_observed=battery_remaining is not None
        or _parse_int(battery_status, "warning") is not None,
        battery_remaining_percent=battery_remaining,
        battery_warning=_parse_int(battery_status, "warning"),
        telemetry_stale=bool(raw.get("telemetry_stale")),
    )


def _post_abort_recovery_agent_evidence_window(
    *,
    probe_observed: dict[str, Any],
    post_abort_wait_seconds: float,
) -> dict[str, Any]:
    post_abort = dict(probe_observed.get("post_abort") or {})
    samples = tuple(
        raw for raw in (post_abort.get("samples") or ()) if isinstance(raw, dict)
    )
    metrics: list[dict[str, Any]] = []
    for index, raw in enumerate(samples):
        status = str(raw.get("vehicle_status") or "")
        local = str(raw.get("vehicle_local_position") or "")
        land_detected = str(raw.get("vehicle_land_detected") or "")
        x_m = _parse_float(local, "x")
        y_m = _parse_float(local, "y")
        z_m = _parse_float(local, "z")
        vx_mps = _parse_float(local, "vx")
        vy_mps = _parse_float(local, "vy")
        vz_mps = _parse_float(local, "vz")
        distance_to_home_m = (
            math.hypot(x_m, y_m) if x_m is not None and y_m is not None else None
        )
        metrics.append(
            {
                "sample_index": index,
                "heartbeat_observed": bool(raw.get("heartbeat_observed")),
                "local_timestamp": _parse_int(local, "timestamp"),
                "status_timestamp": _parse_int(status, "timestamp"),
                "nav_state": _parse_int(status, "nav_state"),
                "arming_state": _parse_int(status, "arming_state"),
                "landed": _parse_bool(land_detected, "landed"),
                "local_x_m": x_m,
                "local_y_m": y_m,
                "local_z_m": z_m,
                "altitude_above_home_m": (-z_m) if z_m is not None else None,
                "local_vx_mps": vx_mps,
                "local_vy_mps": vy_mps,
                "local_vz_mps": vz_mps,
                "speed_xy_mps": (
                    math.hypot(vx_mps, vy_mps)
                    if vx_mps is not None and vy_mps is not None
                    else None
                ),
                "distance_to_home_m": distance_to_home_m,
                "freefall": _parse_bool(land_detected, "freefall"),
                "ground_contact": _parse_bool(land_detected, "ground_contact"),
                "maybe_landed": _parse_bool(land_detected, "maybe_landed"),
                "in_ground_effect": _parse_bool(land_detected, "in_ground_effect"),
                "in_descend": _parse_bool(land_detected, "in_descend"),
                "has_low_throttle": _parse_bool(land_detected, "has_low_throttle"),
                "vertical_movement": _parse_bool(land_detected, "vertical_movement"),
                "horizontal_movement": _parse_bool(land_detected, "horizontal_movement"),
                "rotational_movement": _parse_bool(land_detected, "rotational_movement"),
                "close_to_ground_or_skipped_check": _parse_bool(
                    land_detected, "close_to_ground_or_skipped_check"
                ),
                "at_rest": _parse_bool(land_detected, "at_rest"),
            }
        )

    recovery_action = str(probe_observed.get("recovery_action") or "")
    land_recovery = recovery_action == "land"
    distances = [
        float(item["distance_to_home_m"])
        for item in metrics
        if item.get("distance_to_home_m") is not None
    ]
    z_values = [
        float(item["local_z_m"])
        for item in metrics
        if item.get("local_z_m") is not None
    ]
    closing_steps = 0
    opening_steps = 0
    for previous, current in zip(distances, distances[1:]):
        if current < previous - 1.0:
            closing_steps += 1
        elif current > previous + 1.0:
            opening_steps += 1
    descent_steps = 0
    climb_steps = 0
    for previous, current in zip(z_values, z_values[1:]):
        if current > previous + 0.25:
            descent_steps += 1
        elif current < previous - 0.25:
            climb_steps += 1

    stale_after_sample: int | None = None
    repeated_count = 0
    last_signature: tuple[Any, ...] | None = None
    for item in metrics:
        signature = (
            item.get("local_timestamp"),
            item.get("status_timestamp"),
            round(float(item["local_x_m"]), 3)
            if item.get("local_x_m") is not None
            else None,
            round(float(item["local_y_m"]), 3)
            if item.get("local_y_m") is not None
            else None,
            round(float(item["local_z_m"]), 3)
            if item.get("local_z_m") is not None
            else None,
        )
        if signature == last_signature:
            repeated_count += 1
        else:
            repeated_count = 0
            last_signature = signature
        if repeated_count >= 30:
            stale_after_sample = int(item["sample_index"]) - repeated_count
            break

    distance_start = distances[0] if distances else None
    distance_end = distances[-1] if distances else None
    distance_min = min(distances) if distances else None
    heartbeat_observed_count = sum(
        1 for item in metrics if item.get("heartbeat_observed") is True
    )
    latest_heartbeat_observed = (
        bool(metrics[-1].get("heartbeat_observed")) if metrics else None
    )
    return_progress = (
        max(0.0, distance_start - distance_min)
        if distance_start is not None and distance_min is not None
        else 0.0
    )
    telemetry_stale = stale_after_sample is not None
    return_started = return_progress > 1.0 or closing_steps > 0
    latest = metrics[-1] if metrics else {}
    disarm_observed = any(
        item.get("arming_state") is not None
        and item.get("arming_state") != PX4_ARMING_STATE_ARMED
        for item in metrics
    )
    latest_disarmed = bool(
        latest.get("arming_state") is not None
        and latest.get("arming_state") != PX4_ARMING_STATE_ARMED
    )
    ground_confirmation_observed = any(
        item.get("landed") is True or item.get("maybe_landed") is True
        for item in metrics
    )
    latest_ground_confirmed = bool(
        latest.get("landed") is True or latest.get("maybe_landed") is True
    )
    safe = bool(latest_disarmed and latest_ground_confirmed)
    cached_post_abort_safe = bool(post_abort.get("safe"))
    force_disarm_accepted = False
    assists = post_abort.get("operator_recovery_assists")
    if isinstance(assists, list):
        for assist in assists:
            if not isinstance(assist, dict):
                continue
            force_result = assist.get("low_altitude_force_disarm_ack_result")
            force_command = assist.get("low_altitude_force_disarm_command")
            if force_result == MAV_RESULT_ACCEPTED or (
                isinstance(force_command, dict)
                and force_command.get("ack_result") == MAV_RESULT_ACCEPTED
            ):
                force_disarm_accepted = True
                break
    force_disarm_no_ground_confirmation = bool(
        force_disarm_accepted and not latest_ground_confirmed
    )
    landing_started = bool(
        land_recovery
        and (
            latest.get("nav_state") == 18
            or any(item.get("in_descend") is True for item in metrics)
            or any(item.get("ground_contact") is True for item in metrics)
            or any(item.get("maybe_landed") is True for item in metrics)
        )
    )
    landing_in_progress = bool(
        landing_started
        and (
            descent_steps > 0
            or latest.get("in_descend") is True
            or latest.get("vertical_movement") is True
            or latest.get("in_ground_effect") is True
        )
    )
    observation_lost = telemetry_stale and not safe
    if not observation_lost:
        observation_loss_classification = "none"
    elif latest_heartbeat_observed is True:
        observation_loss_classification = "topic_stale_heartbeat_alive"
    elif latest_heartbeat_observed is False:
        observation_loss_classification = "topic_stale_heartbeat_missing"
    else:
        observation_loss_classification = "topic_stale_heartbeat_unknown"
    if safe:
        incomplete_reason = None
    elif observation_lost:
        incomplete_reason = "recovery_observation_lost_before_safe_landing"
    elif land_recovery and force_disarm_no_ground_confirmation:
        incomplete_reason = "force_disarm_without_ground_confirmation"
    elif land_recovery and disarm_observed and not latest_disarmed:
        incomplete_reason = "recovery_rearmed_after_disarm"
    elif land_recovery and landing_in_progress:
        incomplete_reason = "land_in_progress_before_wait_window"
    elif land_recovery:
        incomplete_reason = "land_command_ack_but_landed_or_disarm_not_observed"
    elif distance_end is not None and distance_end > 20.0:
        incomplete_reason = "recovery_home_not_reached_before_wait_window"
    else:
        incomplete_reason = "recovery_final_landing_not_observed"

    recovery_command = dict(
        probe_observed.get("recovery_command")
        or probe_observed.get("land_abort_command")
        or {}
    )
    telemetry_snapshot = {
        "telemetry": {"stale": telemetry_stale, "dropout": False},
        "recovery": {
            "source": "auto_mission_runtime_post_abort",
            "action": recovery_action,
            "path": str(probe_observed.get("recovery_path") or ""),
            "command_ack_observed": bool(recovery_command.get("ack_observed")),
            "command_ack_result": recovery_command.get("ack_result"),
            "final_landing_safe": safe,
            "post_abort_safe_reported": cached_post_abort_safe,
            "recovery_disarm_observed": disarm_observed,
            "recovery_latest_disarmed": latest_disarmed,
            "recovery_ground_confirmation_observed": ground_confirmation_observed,
            "recovery_latest_ground_confirmed": latest_ground_confirmed,
            "force_disarm_no_ground_confirmation": (
                force_disarm_no_ground_confirmation
            ),
            "return_started": return_started,
            "landing_started": landing_started,
            "landing_in_progress": landing_in_progress,
            "recovery_return_progress_m": round(return_progress, 3),
            "distance_to_home_start_m": (
                round(distance_start, 3) if distance_start is not None else None
            ),
            "distance_to_home_end_m": (
                round(distance_end, 3) if distance_end is not None else None
            ),
            "distance_to_home_min_m": (
                round(distance_min, 3) if distance_min is not None else None
            ),
            "distance_to_home_closing_steps": closing_steps,
            "distance_to_home_opening_steps": opening_steps,
            "local_z_start_m": round(z_values[0], 3) if z_values else None,
            "local_z_end_m": round(z_values[-1], 3) if z_values else None,
            "local_z_min_m": round(min(z_values), 3) if z_values else None,
            "local_z_max_m": round(max(z_values), 3) if z_values else None,
            "local_z_descent_steps": descent_steps,
            "local_z_climb_steps": climb_steps,
            "telemetry_stale": telemetry_stale,
            "telemetry_stale_after_sample": stale_after_sample,
            "heartbeat_observed_count": heartbeat_observed_count,
            "latest_heartbeat_observed": latest_heartbeat_observed,
            "observation_lost": observation_lost,
            "recovery_observation_lost": observation_lost,
            "observation_loss_classification": observation_loss_classification,
            "recovery_observation_lost_after_sample": stale_after_sample
            if observation_lost
            else None,
            "recovery_incomplete_reason": incomplete_reason,
            "latest_nav_state": latest.get("nav_state"),
            "latest_arming_state": latest.get("arming_state"),
            "latest_landed": latest.get("landed"),
            "latest_ground_contact": latest.get("ground_contact"),
            "latest_maybe_landed": latest.get("maybe_landed"),
            "latest_in_ground_effect": latest.get("in_ground_effect"),
            "latest_in_descend": latest.get("in_descend"),
            "latest_vertical_movement": latest.get("vertical_movement"),
            "latest_close_to_ground_or_skipped_check": latest.get(
                "close_to_ground_or_skipped_check"
            ),
            "latest_at_rest": latest.get("at_rest"),
            "latest_altitude_above_home_m": (
                round(float(latest["altitude_above_home_m"]), 3)
                if latest.get("altitude_above_home_m") is not None
                else None
            ),
            "latest_local_vz_mps": (
                round(float(latest["local_vz_mps"]), 3)
                if latest.get("local_vz_mps") is not None
                else None
            ),
            "latest_speed_xy_mps": (
                round(float(latest["speed_xy_mps"]), 3)
                if latest.get("speed_xy_mps") is not None
                else None
            ),
        },
    }
    return {
        "schema_version": "missionos_auto_recovery_agent_evidence_window.v1",
        "source": "auto_mission_runtime_post_abort",
        "post_abort_wait_seconds": float(post_abort_wait_seconds),
        "post_abort_sample_count": len(metrics),
        "recovery_action": recovery_action,
        "recovery_path": str(probe_observed.get("recovery_path") or ""),
        "recovery_command_ack_observed": bool(recovery_command.get("ack_observed")),
        "recovery_command_ack_result": recovery_command.get("ack_result"),
        "final_landing_safe": safe,
        "post_abort_safe_reported": cached_post_abort_safe,
        "recovery_disarm_observed": disarm_observed,
        "recovery_latest_disarmed": latest_disarmed,
        "recovery_ground_confirmation_observed": ground_confirmation_observed,
        "recovery_latest_ground_confirmed": latest_ground_confirmed,
        "force_disarm_no_ground_confirmation": force_disarm_no_ground_confirmation,
        "recovery_return_started": return_started,
        "recovery_landing_started": landing_started,
        "recovery_landing_in_progress": landing_in_progress,
        "recovery_return_progress_m": round(return_progress, 3),
        "recovery_distance_to_home_start_m": (
            round(distance_start, 3) if distance_start is not None else None
        ),
        "recovery_distance_to_home_end_m": (
            round(distance_end, 3) if distance_end is not None else None
        ),
        "recovery_distance_to_home_min_m": (
            round(distance_min, 3) if distance_min is not None else None
        ),
        "recovery_distance_to_home_closing_steps": closing_steps,
        "recovery_distance_to_home_opening_steps": opening_steps,
        "recovery_local_z_start_m": round(z_values[0], 3) if z_values else None,
        "recovery_local_z_end_m": round(z_values[-1], 3) if z_values else None,
        "recovery_local_z_min_m": round(min(z_values), 3) if z_values else None,
        "recovery_local_z_max_m": round(max(z_values), 3) if z_values else None,
        "recovery_local_z_descent_steps": descent_steps,
        "recovery_local_z_climb_steps": climb_steps,
        "recovery_telemetry_stale": telemetry_stale,
        "recovery_telemetry_stale_after_sample": stale_after_sample,
        "recovery_heartbeat_observed_count": heartbeat_observed_count,
        "recovery_latest_heartbeat_observed": latest_heartbeat_observed,
        "recovery_observation_lost": observation_lost,
        "recovery_observation_loss_classification": observation_loss_classification,
        "recovery_observation_lost_after_sample": stale_after_sample
        if observation_lost
        else None,
        "recovery_incomplete_reason": incomplete_reason,
        "latest_recovery_sample": latest,
        "telemetry_snapshot": telemetry_snapshot,
    }


def _configure_operator_route_home(route: dict[str, Any]) -> None:
    os.environ["PX4_HOME_LAT"] = str(route["takeoff_latitude"])
    os.environ["PX4_HOME_LON"] = str(route["takeoff_longitude"])
    os.environ.setdefault("PX4_HOME_ALT", "0.0")


def _rtl_recovery_wait_seconds(
    *,
    base_wait_seconds: float,
    return_distance_m: float | None,
    cruise_speed_mps: float | None,
    safety_factor: float = DEFAULT_RTL_RECOVERY_WAIT_SAFETY_FACTOR,
    landing_allowance_seconds: float = DEFAULT_RTL_RECOVERY_LANDING_ALLOWANCE_SECONDS,
) -> float:
    base = max(0.0, float(base_wait_seconds))
    if (
        return_distance_m is None
        or cruise_speed_mps is None
        or float(return_distance_m) <= 0.0
        or float(cruise_speed_mps) <= 0.0
    ):
        return base
    expected_return_seconds = float(return_distance_m) / float(cruise_speed_mps)
    sized = expected_return_seconds * float(safety_factor) + float(
        landing_allowance_seconds
    )
    return round(max(base, sized), 3)


def _inner_runtime_probe_script(
    *,
    dropoff_dwell_mission_seq: int,
    land_mission_seq: int,
    release_altitude_target_m: float,
    release_altitude_tolerance_m: float,
    required_dwell_seconds: float,
    monitor_seconds: float,
    min_progress_m: float,
    no_progress_grace_seconds: float,
    min_route_altitude_m: float,
    altitude_grace_seconds: float,
    min_battery_remaining_percent: float,
    post_abort_wait_seconds: float,
    land_post_abort_wait_seconds: float,
    rtl_post_abort_wait_seconds: float,
    rtl_recovery_min_progress_m: float,
    sim_battery_min_remaining_percent: float,
    sim_battery_drain_seconds: float,
    thermal_motor_derate_factor: float | None,
    wind_mean_mps: float | None,
    wind_direction_deg: float | None,
    wind_gust_mps: float | None,
    wind_variance: float | None,
    gz_physical_battery_enabled: bool,
    gz_battery_motor_coupling_enabled: bool = False,
    obstacle_manifest: Mapping[str, Any] | None = None,
) -> str:
    arm_params = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    disarm_params = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    force_disarm_params = [
        0.0,
        float(OPERATOR_RECOVERY_ASSIST_FORCE_DISARM_MAGIC),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    auto_params = [
        float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
        float(PX4_CUSTOM_MAIN_MODE_AUTO),
        float(PX4_CUSTOM_SUB_MODE_AUTO_MISSION),
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    land_params = [0.0, 0.0, 0.0, 0.0, "nan", "nan", 0.0]
    rtl_params = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    offboard_params = [
        float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
        float(PX4_CUSTOM_MAIN_MODE_OFFBOARD),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    release_params = [
        float(DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID),
        float(MAV_GRIPPER_ACTION_RELEASE),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    obstacle_manifest_json = json.dumps(
        dict(obstacle_manifest or {}),
        ensure_ascii=True,
        sort_keys=True,
    )
    return textwrap.dedent(
        f"""
        import json, math, os, re, socket, struct, subprocess, time
        from datetime import datetime, timezone
        MAVLINK2_MAGIC=0xFD
        MAVLINK_MSG_ID_HEARTBEAT=0
        MAVLINK_MSG_ID_COMMAND_LONG=76
        MAVLINK_MSG_ID_COMMAND_ACK=77
        MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED=84
        MAV_CMD_DO_CHANGE_SPEED={MAV_CMD_DO_CHANGE_SPEED}
        MAV_FRAME_LOCAL_NED=1
        CRC_EXTRA={{0:50,76:152,77:143,84:143}}
        PX4_MAVLINK_PORT={upload_smoke.PX4_MAVLINK_PORT}
        GCS_MAVLINK_PORT={upload_smoke.GCS_MAVLINK_PORT}
        NAV_AUTO_MISSION={PX4_NAVIGATION_STATE_AUTO_MISSION}
        NAV_OFFBOARD={PX4_NAVIGATION_STATE_OFFBOARD}
        ARMING_ARMED={PX4_ARMING_STATE_ARMED}
        DROPOFF_DWELL_SEQ={int(dropoff_dwell_mission_seq)}
        LAND_SEQ={int(land_mission_seq)}
        RELEASE_ALTITUDE_TARGET_M={float(release_altitude_target_m)}
        RELEASE_ALTITUDE_TOLERANCE_M={float(release_altitude_tolerance_m)}
        REQUIRED_DWELL_SECONDS={float(required_dwell_seconds)}
        MONITOR_SECONDS={float(monitor_seconds)}
        MIN_PROGRESS_M={float(min_progress_m)}
        NO_PROGRESS_GRACE_SECONDS={float(no_progress_grace_seconds)}
        MIN_ROUTE_ALTITUDE_M={float(min_route_altitude_m)}
        ALTITUDE_GRACE_SECONDS={float(altitude_grace_seconds)}
        MIN_BATTERY_REMAINING_PERCENT={float(min_battery_remaining_percent)}
        HEARTBEAT_LIVENESS_WINDOW_SECONDS=2.5
        POST_ABORT_WAIT_SECONDS={float(post_abort_wait_seconds)}
        LAND_POST_ABORT_WAIT_SECONDS={float(land_post_abort_wait_seconds)}
        RTL_POST_ABORT_WAIT_SECONDS={float(rtl_post_abort_wait_seconds)}
        RTL_RECOVERY_MIN_PROGRESS_M={float(rtl_recovery_min_progress_m)}
        OPERATOR_RECOVERY_ASSIST_TRIGGER_SECONDS={float(OPERATOR_RECOVERY_ASSIST_TRIGGER_SECONDS)}
        OPERATOR_RECOVERY_ASSIST_LAND_FINALIZE_SECONDS={float(OPERATOR_RECOVERY_ASSIST_LAND_FINALIZE_SECONDS)}
        OPERATOR_RECOVERY_ASSIST_PRESTREAM_FRAMES={int(OPERATOR_RECOVERY_ASSIST_PRESTREAM_FRAMES)}
        OPERATOR_RECOVERY_ASSIST_MAX_SECONDS={float(OPERATOR_RECOVERY_ASSIST_MAX_SECONDS)}
        OPERATOR_RECOVERY_ASSIST_LAND_MAX_SECONDS={float(OPERATOR_RECOVERY_ASSIST_LAND_MAX_SECONDS)}
        OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS={float(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)}
        OPERATOR_RECOVERY_ASSIST_RTL_HOME_RADIUS_M={float(OPERATOR_RECOVERY_ASSIST_RTL_HOME_RADIUS_M)}
        OPERATOR_RECOVERY_ASSIST_LAND_ALTITUDE_M={float(OPERATOR_RECOVERY_ASSIST_LAND_ALTITUDE_M)}
        OPERATOR_RECOVERY_ASSIST_LAND_TARGET_Z_M={float(OPERATOR_RECOVERY_ASSIST_LAND_TARGET_Z_M)}
        OPERATOR_RECOVERY_ASSIST_DISARM_MAX_ATTEMPTS={int(OPERATOR_RECOVERY_ASSIST_DISARM_MAX_ATTEMPTS)}
        OPERATOR_RECOVERY_ASSIST_DISARM_RETRY_SECONDS={float(OPERATOR_RECOVERY_ASSIST_DISARM_RETRY_SECONDS)}
        OPERATOR_RECOVERY_ASSIST_FORCE_DISARM_MAGIC={float(OPERATOR_RECOVERY_ASSIST_FORCE_DISARM_MAGIC)}
        SIM_BATTERY_MIN_REMAINING_PERCENT={float(sim_battery_min_remaining_percent)}
        SIM_BATTERY_DRAIN_SECONDS={float(sim_battery_drain_seconds)}
        THERMAL_MOTOR_DERATE_FACTOR={thermal_motor_derate_factor!r}
        WIND_MEAN_MPS={wind_mean_mps!r}
        WIND_DIRECTION_DEG={wind_direction_deg!r}
        WIND_GUST_MPS={wind_gust_mps!r}
        WIND_VARIANCE={wind_variance!r}
        WIND_GUST_START_SECONDS=max(5.0, MONITOR_SECONDS*0.35)
        WIND_GUST_DURATION_SECONDS=max(8.0, min(30.0, MONITOR_SECONDS*0.15))
        WIND_TARGET_TOPIC='/world/default/wind'
        GZ_PHYSICAL_BATTERY_ENABLED={bool(gz_physical_battery_enabled)!r}
        GZ_BATTERY_MOTOR_COUPLING_REQUESTED={bool(gz_battery_motor_coupling_enabled)!r}
        GZ_BATTERY_STATE_TOPIC={GZ_BATTERY_STATE_TOPIC!r}
        GZ_BATTERY_STATE_SOURCE={GZ_BATTERY_STATE_SOURCE!r}
        OPERATOR_RECOVERY_REQUEST_PATH={os.getenv(OPERATOR_RECOVERY_REQUEST_PATH_ENV)!r}
        L1_CARGO_ENABLED={_l1_cargo_enabled()!r}
        PAYLOAD_DETACH_TOPIC={PAYLOAD_DETACH_TOPIC!r}
        PAYLOAD_RELEASE_MIN_Z_DROP_M={float(PAYLOAD_RELEASE_MIN_Z_DROP_M)}
        OBSTACLE_MANIFEST=json.loads({obstacle_manifest_json!r})
        GAZEBO_OBSTACLE_APPLICATION_SCHEMA_VERSION={GAZEBO_OBSTACLE_APPLICATION_SCHEMA_VERSION!r}

        def crc_accumulate(byte, crc):
            tmp = byte ^ (crc & 0xFF); tmp = (tmp ^ (tmp << 4)) & 0xFF
            return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF

        def x25(data, extra):
            crc=0xFFFF
            for b in data: crc=crc_accumulate(b, crc)
            return crc_accumulate(extra, crc)

        def frame(msg_id, payload, seq):
            h=bytes([len(payload),0,0,seq&255,255,190,msg_id&255,(msg_id>>8)&255,(msg_id>>16)&255])
            c=x25(h+payload, CRC_EXTRA[msg_id])
            return bytes([MAVLINK2_MAGIC])+h+payload+struct.pack('<H', c)

        def heartbeat(seq):
            return frame(MAVLINK_MSG_ID_HEARTBEAT, struct.pack('<IBBBBB',0,6,8,0,4,3), seq)

        def decode(data):
            if len(data)<12 or data[0]!=MAVLINK2_MAGIC: return None
            l=data[1]; mid=data[7]|(data[8]<<8)|(data[9]<<16)
            return mid, data[10:10+l]

        def observe_heartbeat(sock, duration_seconds=0.05):
            observed=False
            deadline=time.monotonic()+float(duration_seconds)
            while time.monotonic()<deadline:
                try:
                    sock.settimeout(max(0.01, deadline-time.monotonic()))
                    data,_addr=sock.recvfrom(4096)
                except socket.timeout:
                    break
                decoded=decode(data)
                if not decoded: continue
                mid,_payload=decoded
                if mid==MAVLINK_MSG_ID_HEARTBEAT:
                    observed=True
            sock.settimeout(0.2)
            return observed

        def command_long(command_id, params, seq):
            resolved=[]
            for value in params:
                resolved.append(math.nan if value == 'nan' else float(value))
            payload=struct.pack('<fffffffHBBB', *resolved, int(command_id), 1, 1, 0)
            return frame(MAVLINK_MSG_ID_COMMAND_LONG, payload, seq)

        def listener(topic, count=1):
            result=subprocess.run(
                ['/opt/px4-gazebo/bin/px4-listener', topic, str(count)],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            return (result.stdout + result.stderr).strip()

        def param_set(name, value):
            result=subprocess.run(
                ['/opt/px4-gazebo/bin/px4-param', 'set', str(name), repr(float(value))],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            return {{'param': str(name),
                'value': float(value),
                'returncode': int(result.returncode),
                'stdout_tail': (result.stdout or '')[-200:],
                'stderr_tail': (result.stderr or '')[-200:]}}

        def parse_int(text, field):
            import re
            m=re.search(r'\\b'+re.escape(field)+r':\\s*(-?\\d+)', text)
            return int(m.group(1)) if m else None

        def parse_float(text, field):
            import re
            m=re.search(r'\\b'+re.escape(field)+r':\\s*(-?\\d+(?:\\.\\d+)?)', text)
            return float(m.group(1)) if m else None

        def parse_bool(text, field):
            import re
            m=re.search(r'\\b'+re.escape(field)+r':\\s*(True|False)', text)
            return (m.group(1) == 'True') if m else None

        def battery_remaining_percent(text):
            value=parse_float(text, 'remaining')
            if value is None: return None
            return value*100.0 if value <= 1.0 else value

        def dict_number(mapping, field):
            if not isinstance(mapping, dict):
                return None
            value=mapping.get(field)
            try:
                numeric=float(value)
            except (TypeError, ValueError):
                return None
            return numeric if math.isfinite(numeric) else None

        def gz_payload_pose_sample():
            result=subprocess.run(
                ['gz', 'topic', '-e', '-t', '/world/default/pose/info', '-n', '1'],
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
            text=(result.stdout or '') + (result.stderr or '')
            if result.returncode != 0:
                return {{'observed': False, 'error': text[-300:]}}
            pose=parse_gz_entity_pose(text, 'delivery_payload')
            if pose is None:
                return {{'observed': False, 'error': 'delivery_payload pose not observed'}}
            return {{'observed': True, 'pose': pose}}

        # Persistent BatteryState subscriber. A per-sample `gz topic -e -n 1`
        # redoes gz-transport discovery every call and BLOCKS the monitor loop
        # for up to its timeout -- under a full PX4 world that starved the loop
        # so badly PX4 dropped AUTO.MISSION mode and the flight guard-aborted.
        # Instead subscribe ONCE in the background (one discovery, continuous
        # stream to a file) and let each sample parse the latest message from the
        # file tail -- a fast, non-blocking read that never stalls the loop.
        GZ_BATTERY_STREAM={{'proc': None, 'path': '/tmp/gz_battery_state_stream.txt'}}

        def ensure_gz_battery_stream():
            if GZ_BATTERY_STREAM['proc'] is not None:
                return
            try:
                fh=open(GZ_BATTERY_STREAM['path'], 'w')
                GZ_BATTERY_STREAM['fh']=fh
                GZ_BATTERY_STREAM['proc']=subprocess.Popen(
                    ['gz', 'topic', '-e', '-t', GZ_BATTERY_STATE_TOPIC],
                    stdout=fh, stderr=subprocess.STDOUT, text=True)
            except OSError as exc:
                GZ_BATTERY_STREAM['proc']='error'
                GZ_BATTERY_STREAM['error']=str(exc)[-200:]

        def gz_battery_state_sample():
            # Observed-only readout of the gz coupler BatteryState. Reported as a
            # SEPARATE signal; it never overwrites the PX4 battery_status field.
            if not GZ_PHYSICAL_BATTERY_ENABLED:
                return {{'gz_battery_state_observed': False,
                    'gz_battery_state_source': GZ_BATTERY_STATE_SOURCE,
                    'gz_battery_read_error': 'gz_physical_battery_not_enabled'}}
            ensure_gz_battery_stream()
            if GZ_BATTERY_STREAM['proc']=='error':
                return {{'gz_battery_state_observed': False,
                    'gz_battery_state_source': GZ_BATTERY_STATE_SOURCE,
                    'gz_battery_read_error': 'gz_battery_stream_spawn_failed:'+GZ_BATTERY_STREAM.get('error','')}}
            try:
                with open(GZ_BATTERY_STREAM['path'], 'r') as fh:
                    data=fh.read()
            except OSError:
                data=''
            # The coupler publishes BatteryState with no header, so each text
            # message begins with the first field ('voltage:'). Walk from the end
            # to the latest block that also carries 'percentage:' (a complete
            # message, not a half-written tail).
            text=None
            chunks=data.split('voltage:')
            for chunk in reversed(chunks[1:]):
                candidate='voltage:'+chunk
                if 'percentage:' in candidate:
                    text=candidate
                    break
            if text is None:
                return {{'gz_battery_state_observed': False,
                    'gz_battery_state_source': GZ_BATTERY_STATE_SOURCE,
                    'gz_battery_read_error': 'gz_battery_stream_no_data_yet'}}
            percentage=parse_float(text, 'percentage')
            percent=None
            if percentage is not None:
                percent=round(percentage*100.0 if percentage <= 1.0 else percentage, 3)
            voltage=parse_float(text, 'voltage')
            current=parse_float(text, 'current')
            charge=parse_float(text, 'charge')
            return {{'gz_battery_state_observed': percent is not None,
                'gz_battery_percent': percent,
                'gz_battery_voltage_v': (round(voltage, 3) if voltage is not None else None),
                'gz_battery_current_a': (round(current, 3) if current is not None else None),
                'gz_battery_charge_ah': (round(charge, 4) if charge is not None else None),
                'gz_battery_state_source': GZ_BATTERY_STATE_SOURCE,
                'gz_battery_read_error': None if percent is not None else 'gz_battery_percentage_not_parsed'}}

        def wind_vector(speed_mps):
            direction=float(WIND_DIRECTION_DEG or 0.0)
            radians=math.radians(direction)
            return round(float(speed_mps)*math.sin(radians), 6), round(float(speed_mps)*math.cos(radians), 6)

        def publish_gazebo_wind(speed_mps, phase):
            if speed_mps is None:
                return {{'phase': phase, 'published': False, 'reason': 'wind_speed_not_requested'}}
            wind_x, wind_y=wind_vector(speed_mps)
            message=f'enable_wind: true linear_velocity {{{{ x: {{wind_x}} y: {{wind_y}} z: 0 }}}}'
            result=subprocess.run(
                ['gz', 'topic', '-t', WIND_TARGET_TOPIC, '-m', 'gz.msgs.Wind', '-p', message],
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
            return {{'phase': phase,
                'published': result.returncode == 0,
                'returncode': int(result.returncode),
                'target_topic': WIND_TARGET_TOPIC,
                'message_type': 'gz.msgs.Wind',
                'wind_speed_mps': float(speed_mps),
                'wind_direction_deg': float(WIND_DIRECTION_DEG or 0.0),
                'wind_vector_x_mps': wind_x,
                'wind_vector_y_mps': wind_y,
                'message': message,
                'stdout_tail': (result.stdout or '')[-200:],
                'stderr_tail': (result.stderr or '')[-200:]}}

        def parse_gz_entity_pose(text, entity_name):
            blocks=[]
            current=[]
            depth=0
            in_pose=False
            for line in text.splitlines():
                stripped=line.strip()
                if not in_pose and stripped == 'pose {{':
                    in_pose=True
                    current=[line]
                    depth=1
                    continue
                if not in_pose:
                    continue
                current.append(line)
                depth += stripped.count('{{') - stripped.count('}}')
                if depth <= 0:
                    blocks.append('\\n'.join(current))
                    current=[]
                    in_pose=False
            for block in blocks:
                name=re.search(r'name:\\s*"([^"]+)"', block)
                if not name or name.group(1) != entity_name:
                    continue
                position=re.search(r'position\\s*\\{{(?P<body>.*?)\\n\\s*\\}}', block, re.S)
                if not position:
                    return None
                body=position.group('body')
                def component(key):
                    match=re.search(r'\\b'+re.escape(key)+r':\\s*([-+0-9.eE]+)', body)
                    return float(match.group(1)) if match else 0.0
                return {{'x': component('x'), 'y': component('y'), 'z': component('z')}}
            return None

        def sanitize_gazebo_model_name(value, fallback):
            text=str(value or '').strip()
            text=re.sub(r'[^A-Za-z0-9_]+', '_', text)
            text=text.strip('_')
            return text or fallback

        def gazebo_obstacle_models_from_manifest(manifest):
            if not isinstance(manifest, dict):
                return []
            obstacles=manifest.get('obstacles')
            if not isinstance(obstacles, list):
                return []
            models=[]
            for index, item in enumerate(obstacles, start=1):
                if not isinstance(item, dict):
                    continue
                name=sanitize_gazebo_model_name(item.get('name'), f'missionos_obstacle_{{index:02d}}')
                try:
                    x=float(item.get('x_m', 0.0))
                    y=float(item.get('y_m', 0.0))
                    z=float(item.get('z_m', item.get('size_z_m', 20.0)/2.0))
                    sx=max(0.5, min(200.0, float(item.get('size_x_m', 18.0))))
                    sy=max(0.5, min(200.0, float(item.get('size_y_m', 18.0))))
                    sz=max(0.5, min(500.0, float(item.get('size_z_m', 20.0))))
                except (TypeError, ValueError):
                    continue
                if not all(math.isfinite(value) for value in (x, y, z, sx, sy, sz)):
                    continue
                models.append({{
                    'name': name,
                    'kind': str(item.get('kind') or 'building_box'),
                    'x_m': round(x, 3),
                    'y_m': round(y, 3),
                    'z_m': round(z, 3),
                    'size_x_m': round(sx, 3),
                    'size_y_m': round(sy, 3),
                    'size_z_m': round(sz, 3),
                    'source': str(item.get('source') or 'obstacle_manifest'),
                }})
            return models

        def gazebo_box_obstacle_sdf(model):
            sx=model['size_x_m']; sy=model['size_y_m']; sz=model['size_z_m']
            x=model['x_m']; y=model['y_m']; z=model['z_m']
            name=model['name']
            return (
                '<?xml version="1.0"?>'
                '<sdf version="1.9">'
                f'<model name="{{name}}">'
                '<static>true</static>'
                f'<pose>{{x}} {{y}} {{z}} 0 0 0</pose>'
                '<link name="link">'
                '<collision name="collision">'
                '<geometry><box>'
                f'<size>{{sx}} {{sy}} {{sz}}</size>'
                '</box></geometry>'
                '</collision>'
                '<visual name="visual">'
                '<geometry><box>'
                f'<size>{{sx}} {{sy}} {{sz}}</size>'
                '</box></geometry>'
                '<material>'
                '<ambient>0.75 0.12 0.10 1</ambient>'
                '<diffuse>0.75 0.12 0.10 1</diffuse>'
                '</material>'
                '</visual>'
                '</link>'
                '</model>'
                '</sdf>'
            )

        def observe_gazebo_entity_pose(entity_name, timeout_seconds=5.0):
            deadline=time.monotonic()+float(timeout_seconds)
            last_error=''
            while time.monotonic()<deadline:
                result=subprocess.run(
                    ['gz', 'topic', '-e', '-t', '/world/default/pose/info', '-n', '1'],
                    text=True,
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                text=(result.stdout or '') + (result.stderr or '')
                if result.returncode != 0:
                    last_error=text[-300:]
                    time.sleep(0.3)
                    continue
                pose=parse_gz_entity_pose(text, entity_name)
                if pose is not None:
                    return {{'observed': True, 'pose': pose, 'error': ''}}
                last_error='entity pose not observed'
                time.sleep(0.3)
            return {{'observed': False, 'pose': None, 'error': last_error}}

        def spawn_gazebo_obstacle_model(model):
            name=model['name']
            sdf_path=f'/tmp/{{name}}.sdf'
            try:
                with open(sdf_path, 'w') as fh:
                    fh.write(gazebo_box_obstacle_sdf(model))
            except OSError as exc:
                return {{
                    **model,
                    'spawn_requested': True,
                    'spawn_performed': False,
                    'spawn_request_accepted': False,
                    'pose_readback_observed': False,
                    'blocked_reason': 'sdf_write_failed:'+str(exc)[-160:],
                }}
            request=f'sdf_filename: "{{sdf_path}}" name: "{{name}}" allow_renaming: false'
            result=subprocess.run(
                [
                    'gz',
                    'service',
                    '-s',
                    '/world/default/create',
                    '--reqtype',
                    'gz.msgs.EntityFactory',
                    '--reptype',
                    'gz.msgs.Boolean',
                    '--timeout',
                    '5000',
                    '--req',
                    request,
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            combined=((result.stdout or '') + '\\n' + (result.stderr or '')).lower()
            accepted=(
                result.returncode == 0
                and ('data: true' in combined or 'true' in combined or not combined.strip())
            )
            readback=observe_gazebo_entity_pose(name) if accepted else {{'observed': False, 'pose': None, 'error': 'spawn_request_not_accepted'}}
            return {{
                **model,
                'spawn_requested': True,
                'spawn_performed': bool(accepted),
                'spawn_request_accepted': bool(accepted),
                'pose_readback_observed': bool(readback.get('observed')),
                'pose_readback': readback.get('pose') or {{}},
                'sdf_path': sdf_path,
                'service': '/world/default/create',
                'request_type': 'gz.msgs.EntityFactory',
                'returncode': int(result.returncode),
                'stdout_tail': (result.stdout or '')[-300:],
                'stderr_tail': (result.stderr or '')[-300:],
                'blocked_reason': '' if readback.get('observed') else (readback.get('error') or 'pose_readback_not_observed'),
            }}

        def spawn_gazebo_obstacle_models(manifest):
            models=gazebo_obstacle_models_from_manifest(manifest)
            if not models:
                return {{
                    'schema_version': GAZEBO_OBSTACLE_APPLICATION_SCHEMA_VERSION,
                    'application_status': 'not_requested',
                    'gazebo_obstacle_model_spawn_requested': False,
                    'gazebo_obstacle_model_spawned': False,
                    'spawned_model_count': 0,
                    'requested_model_count': 0,
                    'obstacle_manifest': manifest if isinstance(manifest, dict) else {{}},
                    'models': [],
                    'observed_at': datetime.now(timezone.utc).isoformat(),
                }}
            results=[spawn_gazebo_obstacle_model(model) for model in models]
            spawned=[item for item in results if item.get('pose_readback_observed')]
            application_status=(
                'applied'
                if len(spawned) == len(results)
                else 'partially_applied'
                if spawned
                else 'unsupported'
            )
            manifest_payload=dict(manifest)
            manifest_payload['obstacles']=models
            manifest_payload['gazebo_obstacle_model_spawn_requested']=True
            manifest_payload['gazebo_obstacle_model_spawned']=bool(spawned)
            manifest_payload['spawned_model_count']=len(spawned)
            return {{
                'schema_version': GAZEBO_OBSTACLE_APPLICATION_SCHEMA_VERSION,
                'application_status': application_status,
                'gazebo_obstacle_model_spawn_requested': True,
                'gazebo_obstacle_model_spawned': bool(spawned),
                'spawned_model_count': len(spawned),
                'requested_model_count': len(results),
                'obstacle_manifest': manifest_payload,
                'models': results,
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'physical_execution_invoked': False,
                'delivery_completion_claimed': False,
            }}

        def trigger_l1_payload_release():
            if not L1_CARGO_ENABLED:
                return None
            before=gz_payload_pose_sample()
            observed_at=datetime.now(timezone.utc).isoformat()
            detach=subprocess.run(
                ['gz', 'topic', '-t', PAYLOAD_DETACH_TOPIC, '-m', 'gz.msgs.Empty', '-p', ''],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            time.sleep(1.5)
            after=gz_payload_pose_sample()
            before_pose=before.get('pose') if before.get('observed') else None
            after_pose=after.get('pose') if after.get('observed') else None
            z_drop=None
            if before_pose is not None and after_pose is not None:
                z_drop=float(before_pose.get('z', 0.0))-float(after_pose.get('z', 0.0))
            performed=(detach.returncode == 0)
            observed=(
                performed
                and z_drop is not None
                and z_drop >= PAYLOAD_RELEASE_MIN_Z_DROP_M
            )
            blocked=[]
            if not before.get('observed'):
                blocked.append('payload_pose_before_release_not_observed')
            if not performed:
                blocked.append('gazebo_detachable_joint_detach_publish_failed')
            if not after.get('observed'):
                blocked.append('payload_pose_after_release_not_observed')
            if performed and after.get('observed') and not observed:
                blocked.append('gazebo_detachable_joint_release_not_observed')
            event={{'payload_release_observed': observed,
                'payload_release_event_source': 'gazebo_detachable_joint_detach_event' if performed else '',
                'payload_id': 'pkg-auto-sitl-dropoff',
                'payload_detach_topic': PAYLOAD_DETACH_TOPIC,
                'payload_pose_before_release': before_pose or {{}},
                'payload_pose_after_release': after_pose or {{}},
                'payload_release_position_x_m': after_pose.get('x') if after_pose else None,
                'payload_release_position_y_m': after_pose.get('y') if after_pose else None,
                'payload_release_position_z_m': after_pose.get('z') if after_pose else None,
                'payload_release_z_drop_m': z_drop,
                'payload_release_observed_at': observed_at if observed else '',
                'gazebo_detachable_joint_release_performed': performed,
                'gazebo_detachable_joint_release_observed': observed,
                'gazebo_entity_mutation_performed': False,
                'trigger_source': 'dropoff_dwell_l0_ack_gazebo_detach_topic',
                'detach_publish_returncode': detach.returncode,
                'detach_publish_stderr_tail': (detach.stderr or '')[-300:],
                'blocked_reasons': blocked}}
            return event

        def local_progress(first_local, current_local):
            fx=parse_float(first_local, 'x') or 0.0
            fy=parse_float(first_local, 'y') or 0.0
            cx=parse_float(current_local, 'x') if parse_float(current_local, 'x') is not None else fx
            cy=parse_float(current_local, 'y') if parse_float(current_local, 'y') is not None else fy
            return math.hypot(cx-fx, cy-fy)

        def monitor_progress(monitor):
            samples=monitor.get('samples') or []
            if not samples: return 0.0
            return float(samples[-1].get('progress_m') or 0.0)

        def monitor_return_home_projection(monitor):
            reserve_percent=15.0
            snapshot=monitor.get('terminal_snapshot') or {{}}
            progress=dict_number(snapshot, 'progress_m')
            distance_to_home=dict_number(snapshot, 'distance_to_home_m')
            battery_remaining=dict_number(snapshot, 'battery_remaining_percent')
            battery_delta=dict_number(snapshot, 'battery_remaining_delta_percent')
            if not snapshot:
                samples=monitor.get('samples') or []
                latest=samples[-1] if samples else {{}}
                progress=dict_number(latest, 'progress_m')
                local=latest.get('vehicle_local_position') or ''
                x=parse_float(local, 'x')
                y=parse_float(local, 'y')
                if x is not None and y is not None:
                    distance_to_home=math.hypot(float(x), float(y))
                battery_remaining=battery_remaining_percent(
                    latest.get('battery_status') or ''
                )
                first_remaining=None
                for sample in samples:
                    candidate=battery_remaining_percent(
                        sample.get('battery_status') or ''
                    )
                    if candidate is not None:
                        first_remaining=candidate
                        break
                if first_remaining is not None and battery_remaining is not None:
                    battery_delta=float(battery_remaining)-float(first_remaining)
            if (
                progress is None
                or distance_to_home is None
                or battery_remaining is None
                or battery_delta is None
            ):
                return {{'projection_status': 'insufficient_observation',
                    'battery_reserve_required_percent': reserve_percent}}
            consumed_percent=abs(float(battery_delta))
            if consumed_percent <= 0.0 or float(progress) <= 0.0:
                return {{'projection_status': 'insufficient_observation',
                    'progress_m': round(float(progress), 3),
                    'distance_to_home_m': round(float(distance_to_home), 3),
                    'battery_remaining_percent': round(float(battery_remaining), 3),
                    'battery_reserve_required_percent': reserve_percent}}
            burn_percent_per_m=consumed_percent/float(progress)
            projected_required=burn_percent_per_m*float(distance_to_home)
            projected_arrival=float(battery_remaining)-projected_required
            projected_margin=projected_arrival-reserve_percent
            return {{'projection_status': 'computed',
                'progress_m': round(float(progress), 3),
                'distance_to_home_m': round(float(distance_to_home), 3),
                'battery_consumed_percent': round(consumed_percent, 3),
                'battery_remaining_percent': round(float(battery_remaining), 3),
                'battery_burn_percent_per_km': round(burn_percent_per_m*1000.0, 3),
                'projected_return_battery_required_percent': round(projected_required, 3),
                'projected_return_arrival_battery_percent': round(projected_arrival, 3),
                'battery_reserve_required_percent': reserve_percent,
                'projected_return_reserve_margin_percent': round(projected_margin, 3),
                'projected_insufficient_for_return_home': projected_margin < 0.0}}

        obstacle_application=spawn_gazebo_obstacle_models(OBSTACLE_MANIFEST)

        def send_command(sock, remote, command_id, params, seq, timeout_seconds):
            deadline=time.monotonic()+float(timeout_seconds)
            sent=False
            ack=None
            ack_history=[]
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                if not sent:
                    sock.sendto(command_long(command_id, params, seq), remote); seq+=1
                    sent=True
                try:
                    data,_addr=sock.recvfrom(4096)
                except socket.timeout:
                    continue
                decoded=decode(data)
                if not decoded: continue
                mid,payload=decoded
                if mid==MAVLINK_MSG_ID_COMMAND_ACK and len(payload)>=3:
                    ack_command=struct.unpack('<H', payload[:2])[0]
                    result=int(payload[2])
                    if ack_command == int(command_id):
                        ack=result
                        ack_history.append(result)
                        if result == {MAV_RESULT_IN_PROGRESS}:
                            continue
                        break
            return {{'command_id': int(command_id), 'attempted': sent, 'ack_observed': ack is not None, 'ack_result': ack, 'ack_result_history': ack_history, 'next_seq': seq}}

        def setpoint_local_ned(x, y, z, sequence, vx=0.0, vy=0.0, vz=0.0):
            type_mask_position_only=0b0000110111111000
            type_mask_position_velocity=0b0000110111000000
            type_mask=(
                type_mask_position_velocity
                if any(abs(float(value)) > 0.0 for value in (vx, vy, vz))
                else type_mask_position_only
            )
            payload=struct.pack(
                '<IfffffffffffHBBB',
                0,
                float(x),
                float(y),
                float(z),
                float(vx),
                float(vy),
                float(vz),
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                type_mask,
                1,
                1,
                MAV_FRAME_LOCAL_NED,
            )
            return frame(MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED, payload, sequence)

        def send_command_with_recovery_setpoints(sock, remote, command_id, params, seq, timeout_seconds, target):
            deadline=time.monotonic()+float(timeout_seconds)
            sent=False
            ack=None
            ack_history=[]
            setpoint_frames_sent=0
            original_timeout=sock.gettimeout()
            sock.settimeout(max(0.01, float(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)))
            try:
                while time.monotonic()<deadline:
                    sock.sendto(heartbeat(seq), remote); seq+=1
                    sock.sendto(
                        setpoint_local_ned(
                            target['target_x_m'],
                            target['target_y_m'],
                            target['target_z_m'],
                            seq,
                            target['feed_forward_vx_mps'],
                            target['feed_forward_vy_mps'],
                            target['feed_forward_vz_mps'],
                        ),
                        remote,
                    ); seq+=1
                    setpoint_frames_sent+=1
                    if not sent:
                        sock.sendto(command_long(command_id, params, seq), remote); seq+=1
                        sent=True
                    try:
                        data,_addr=sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    decoded=decode(data)
                    if not decoded:
                        continue
                    mid,payload=decoded
                    if mid==MAVLINK_MSG_ID_COMMAND_ACK and len(payload)>=3:
                        ack_command=struct.unpack('<H', payload[:2])[0]
                        result=int(payload[2])
                        if ack_command == int(command_id):
                            ack=result
                            ack_history.append(result)
                            if result == {MAV_RESULT_IN_PROGRESS}:
                                continue
                            break
            finally:
                sock.settimeout(original_timeout)
            return {{
                'command_id': int(command_id),
                'attempted': sent,
                'ack_observed': ack is not None,
                'ack_result': ack,
                'ack_result_history': ack_history,
                'next_seq': seq,
                'setpoint_frames_sent': setpoint_frames_sent,
            }}

        def recovery_outcome_status(
            action,
            home_distance_delta,
            altitude_delta,
            observation_seconds,
            landed,
            arming=None,
            altitude=None,
        ):
            if action == 'return_to_launch':
                if home_distance_delta is not None and home_distance_delta <= -1.0:
                    return 'return_progress_observed'
                if observation_seconds is not None and observation_seconds >= OPERATOR_RECOVERY_ASSIST_TRIGGER_SECONDS:
                    return 'return_progress_not_observed'
                return 'return_observation_pending'
            if action == 'land':
                if landed is True:
                    return 'landing_landed_observed'
                if (
                    arming is not None
                    and arming != ARMING_ARMED
                    and altitude is not None
                    and altitude <= (OPERATOR_RECOVERY_ASSIST_LAND_ALTITUDE_M + 1.0)
                ):
                    return 'landing_disarmed_observed'
                if altitude_delta is not None and altitude_delta <= -1.0:
                    if (
                        observation_seconds is not None
                        and observation_seconds >= OPERATOR_RECOVERY_ASSIST_LAND_FINALIZE_SECONDS
                    ):
                        return 'landing_not_landed_after_descent'
                    return 'landing_descent_observed'
                if observation_seconds is not None and observation_seconds >= OPERATOR_RECOVERY_ASSIST_TRIGGER_SECONDS:
                    return 'landing_descent_not_observed'
                return 'landing_observation_pending'
            return 'recovery_outcome_pending'

        def recovery_assist_target(action, local_x, local_y, local_z, altitude):
            if local_x is None or local_y is None or local_z is None:
                return None
            if action == 'land':
                return {{
                    'assist_kind': 'bounded_offboard_land_descent',
                    'target_x_m': float(local_x),
                    'target_y_m': float(local_y),
                    'target_z_m': OPERATOR_RECOVERY_ASSIST_LAND_TARGET_Z_M,
                    'feed_forward_vx_mps': 0.0,
                    'feed_forward_vy_mps': 0.0,
                    'feed_forward_vz_mps': 0.35,
                }}
            if action == 'return_to_launch':
                target_altitude=max(
                    float(MIN_ROUTE_ALTITUDE_M),
                    float(altitude) if altitude is not None else float(MIN_ROUTE_ALTITUDE_M),
                )
                return {{
                    'assist_kind': 'bounded_offboard_rtl_home_setpoint',
                    'target_x_m': 0.0,
                    'target_y_m': 0.0,
                    'target_z_m': -target_altitude,
                    'feed_forward_vx_mps': 0.0,
                    'feed_forward_vy_mps': 0.0,
                    'feed_forward_vz_mps': 0.0,
                }}
            return None

        def run_bounded_recovery_assist(sock, remote, seq, action, local_x, local_y, local_z, altitude):
            target=recovery_assist_target(action, local_x, local_y, local_z, altitude)
            if target is None:
                return {{
                    'attempted': False,
                    'status': 'skipped_missing_local_position',
                    'next_seq': seq,
                    'setpoint_frames_sent': 0,
                    'setpoint_stream_duration_seconds': 0.0,
                }}
            started=time.monotonic()
            frames_sent=0
            for _ in range(OPERATOR_RECOVERY_ASSIST_PRESTREAM_FRAMES):
                sock.sendto(heartbeat(seq), remote); seq+=1
                sock.sendto(
                    setpoint_local_ned(
                        target['target_x_m'],
                        target['target_y_m'],
                        target['target_z_m'],
                        seq,
                        target['feed_forward_vx_mps'],
                        target['feed_forward_vy_mps'],
                        target['feed_forward_vz_mps'],
                    ),
                    remote,
                ); seq+=1
                frames_sent+=1
                time.sleep(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)
            offboard=send_command_with_recovery_setpoints(
                sock,
                remote,
                {MAV_CMD_DO_SET_MODE},
                {offboard_params!r},
                seq,
                5.0,
                target,
            )
            seq=offboard.get('next_seq', seq)
            frames_sent+=int(offboard.get('setpoint_frames_sent') or 0)
            status_after_offboard=listener('vehicle_status', 1)
            offboard_nav_state=parse_int(status_after_offboard, 'nav_state')
            offboard_state_observed=offboard_nav_state == NAV_OFFBOARD
            if (
                offboard.get('ack_result') != {MAV_RESULT_ACCEPTED}
                and not offboard_state_observed
            ):
                return {{
                    'attempted': True,
                    'status': 'offboard_mode_not_accepted',
                    'assist_kind': target.get('assist_kind'),
                    'next_seq': seq,
                    'offboard_command': offboard,
                    'offboard_ack_observed': offboard.get('ack_observed'),
                    'offboard_ack_result': offboard.get('ack_result'),
                    'offboard_state_observed': offboard_state_observed,
                    'offboard_nav_state': offboard_nav_state,
                    'setpoint_frames_sent': frames_sent,
                    'setpoint_stream_duration_seconds': round(time.monotonic()-started, 3),
                    'target': target,
                }}
            stream_started=time.monotonic()
            next_observe_at=stream_started
            last_home_distance=None
            last_altitude=None
            last_landed=None
            last_nav_state=None
            target_reached=False
            low_altitude_reached=False
            low_altitude_disarm=None
            low_altitude_force_disarm=None
            low_altitude_disarm_attempts=[]
            assist_max_seconds=(
                OPERATOR_RECOVERY_ASSIST_LAND_MAX_SECONDS
                if action == 'land'
                else OPERATOR_RECOVERY_ASSIST_MAX_SECONDS
            )
            while (time.monotonic()-stream_started) < assist_max_seconds:
                sock.sendto(heartbeat(seq), remote); seq+=1
                sock.sendto(
                    setpoint_local_ned(
                        target['target_x_m'],
                        target['target_y_m'],
                        target['target_z_m'],
                        seq,
                        target['feed_forward_vx_mps'],
                        target['feed_forward_vy_mps'],
                        target['feed_forward_vz_mps'],
                    ),
                    remote,
                ); seq+=1
                frames_sent+=1
                now=time.monotonic()
                if now >= next_observe_at:
                    local=listener('vehicle_local_position', 1)
                    status=listener('vehicle_status', 1)
                    land_detected=listener('vehicle_land_detected', 1)
                    sample_x=parse_float(local, 'x')
                    sample_y=parse_float(local, 'y')
                    sample_z=parse_float(local, 'z')
                    if sample_x is not None and sample_y is not None:
                        last_home_distance=math.hypot(sample_x, sample_y)
                    last_altitude=(-sample_z) if sample_z is not None else None
                    last_landed=parse_bool(land_detected, 'landed')
                    last_nav_state=parse_int(status, 'nav_state')
                    if action == 'land':
                        if last_landed is True:
                            target_reached=True
                            break
                        if (
                            last_altitude is not None
                            and last_altitude <= OPERATOR_RECOVERY_ASSIST_LAND_ALTITUDE_M
                        ):
                            low_altitude_reached=True
                            target_reached=True
                            next_observe_at=now+1.0
                            continue
                    if action == 'return_to_launch' and (
                        last_home_distance is not None
                        and last_home_distance <= OPERATOR_RECOVERY_ASSIST_RTL_HOME_RADIUS_M
                    ):
                        target_reached=True
                        break
                    next_observe_at=now+1.0
                time.sleep(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)
            if action == 'land' and target_reached:
                for attempt_index in range(OPERATOR_RECOVERY_ASSIST_DISARM_MAX_ATTEMPTS):
                    low_altitude_disarm=send_command_with_recovery_setpoints(
                        sock,
                        remote,
                        {MAV_CMD_COMPONENT_ARM_DISARM},
                        {disarm_params!r},
                        seq,
                        5.0,
                        target,
                    )
                    seq=low_altitude_disarm.get('next_seq', seq)
                    frames_sent+=int(low_altitude_disarm.get('setpoint_frames_sent') or 0)
                    low_altitude_disarm_attempts.append(low_altitude_disarm)
                    local=listener('vehicle_local_position', 1)
                    status=listener('vehicle_status', 1)
                    land_detected=listener('vehicle_land_detected', 1)
                    sample_x=parse_float(local, 'x')
                    sample_y=parse_float(local, 'y')
                    sample_z=parse_float(local, 'z')
                    if sample_x is not None and sample_y is not None:
                        last_home_distance=math.hypot(sample_x, sample_y)
                    last_altitude=(-sample_z) if sample_z is not None else None
                    last_landed=parse_bool(land_detected, 'landed')
                    last_nav_state=parse_int(status, 'nav_state')
                    if low_altitude_disarm.get('ack_result') == {MAV_RESULT_ACCEPTED}:
                        break
                    if attempt_index + 1 >= OPERATOR_RECOVERY_ASSIST_DISARM_MAX_ATTEMPTS:
                        break
                    retry_started=time.monotonic()
                    while (
                        time.monotonic()-retry_started
                    ) < OPERATOR_RECOVERY_ASSIST_DISARM_RETRY_SECONDS:
                        sock.sendto(heartbeat(seq), remote); seq+=1
                        sock.sendto(
                            setpoint_local_ned(
                                target['target_x_m'],
                                target['target_y_m'],
                                target['target_z_m'],
                                seq,
                                target['feed_forward_vx_mps'],
                                target['feed_forward_vy_mps'],
                                target['feed_forward_vz_mps'],
                            ),
                            remote,
                        ); seq+=1
                        frames_sent+=1
                        time.sleep(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)
            if (
                action == 'land'
                and target_reached
                and low_altitude_reached
                and last_landed is not True
                and (
                    low_altitude_disarm is None
                    or low_altitude_disarm.get('ack_result') != {MAV_RESULT_ACCEPTED}
                )
            ):
                low_altitude_force_disarm=send_command_with_recovery_setpoints(
                    sock,
                    remote,
                    {MAV_CMD_COMPONENT_ARM_DISARM},
                    {force_disarm_params!r},
                    seq,
                    5.0,
                    target,
                )
                seq=low_altitude_force_disarm.get('next_seq', seq)
                frames_sent+=int(low_altitude_force_disarm.get('setpoint_frames_sent') or 0)
                local=listener('vehicle_local_position', 1)
                status=listener('vehicle_status', 1)
                land_detected=listener('vehicle_land_detected', 1)
                sample_x=parse_float(local, 'x')
                sample_y=parse_float(local, 'y')
                sample_z=parse_float(local, 'z')
                if sample_x is not None and sample_y is not None:
                    last_home_distance=math.hypot(sample_x, sample_y)
                last_altitude=(-sample_z) if sample_z is not None else None
                last_landed=parse_bool(land_detected, 'landed')
                last_nav_state=parse_int(status, 'nav_state')
            assist_status='target_reached' if target_reached else 'stream_window_complete'
            if action == 'land' and target_reached:
                assist_status=(
                    'landed_observed'
                    if last_landed is True
                    else 'low_altitude_reached'
                    if low_altitude_reached
                    else assist_status
                )
            if (
                low_altitude_disarm is not None
                and low_altitude_disarm.get('ack_result') == {MAV_RESULT_ACCEPTED}
            ):
                assist_status='target_reached_disarm_sent'
            elif (
                low_altitude_force_disarm is not None
                and low_altitude_force_disarm.get('ack_result') == {MAV_RESULT_ACCEPTED}
            ):
                assist_status='target_reached_force_disarm_sent'
            elif action == 'land' and low_altitude_disarm is not None and last_landed is not True:
                assist_status='low_altitude_reached_disarm_not_accepted'
            return {{
                'attempted': True,
                'status': assist_status,
                'assist_kind': target.get('assist_kind'),
                'next_seq': seq,
                'offboard_command': offboard,
                'offboard_ack_observed': offboard.get('ack_observed'),
                'offboard_ack_result': offboard.get('ack_result'),
                'offboard_state_observed': offboard_state_observed,
                'offboard_nav_state': offboard_nav_state,
                'setpoint_frames_sent': frames_sent,
                'setpoint_stream_duration_seconds': round(time.monotonic()-started, 3),
                'target': target,
                'low_altitude_disarm_command': low_altitude_disarm,
                'low_altitude_force_disarm_command': low_altitude_force_disarm,
                'low_altitude_reached': low_altitude_reached,
                'low_altitude_disarm_attempt_count': len(low_altitude_disarm_attempts),
                'low_altitude_disarm_ack_result_history': [
                    item.get('ack_result') for item in low_altitude_disarm_attempts
                ],
                'low_altitude_disarm_ack_observed': (
                    low_altitude_disarm.get('ack_observed')
                    if isinstance(low_altitude_disarm, dict)
                    else None
                ),
                'low_altitude_disarm_ack_result': (
                    low_altitude_disarm.get('ack_result')
                    if isinstance(low_altitude_disarm, dict)
                    else None
                ),
                'low_altitude_force_disarm_ack_observed': (
                    low_altitude_force_disarm.get('ack_observed')
                    if isinstance(low_altitude_force_disarm, dict)
                    else None
                ),
                'low_altitude_force_disarm_ack_result': (
                    low_altitude_force_disarm.get('ack_result')
                    if isinstance(low_altitude_force_disarm, dict)
                    else None
                ),
                'last_distance_to_home_m': (
                    round(last_home_distance, 3)
                    if last_home_distance is not None
                    else None
                ),
                'last_altitude_above_home_m': (
                    round(last_altitude, 3)
                    if last_altitude is not None
                    else None
                ),
                'last_landed': last_landed,
                'last_nav_state': last_nav_state,
            }}

        def recovery_parameter(request, *keys):
            parameters=request.get('recovery_parameters')
            if not isinstance(parameters, dict):
                parameters={{}}
            for key in keys:
                value=parameters.get(key)
                if value is None or value == '':
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
            return None

        def operator_maneuver_target(request, local_x, local_y, local_z, altitude):
            action=str(request.get('recovery_action') or '')
            if action == 'adjust_altitude':
                target_altitude=recovery_parameter(request, 'target_altitude_m', 'altitude_m')
                if target_altitude is None:
                    return None, 'target_altitude_m_required'
                return {{
                    'assist_kind': 'bounded_offboard_adjust_altitude',
                    'target_x_m': float(local_x or 0.0),
                    'target_y_m': float(local_y or 0.0),
                    'target_z_m': -float(target_altitude),
                    'feed_forward_vx_mps': 0.0,
                    'feed_forward_vy_mps': 0.0,
                    'feed_forward_vz_mps': 0.0,
                }}, None
            if action in ('reroute', 'avoid_obstacle'):
                target_x=recovery_parameter(request, 'target_x_m', 'x_m')
                target_y=recovery_parameter(request, 'target_y_m', 'y_m')
                if target_x is None or target_y is None:
                    return None, 'target_x_m_and_target_y_m_required'
                target_altitude=recovery_parameter(request, 'target_altitude_m', 'altitude_m')
                if target_altitude is None:
                    target_altitude=(
                        float(altitude)
                        if altitude is not None
                        else float(MIN_ROUTE_ALTITUDE_M)
                    )
                return {{
                    'assist_kind': (
                        'bounded_offboard_obstacle_avoidance_reroute'
                        if action == 'avoid_obstacle'
                        else 'bounded_offboard_reroute'
                    ),
                    'target_x_m': float(target_x),
                    'target_y_m': float(target_y),
                    'target_z_m': -float(target_altitude),
                    'feed_forward_vx_mps': 0.0,
                    'feed_forward_vy_mps': 0.0,
                    'feed_forward_vz_mps': 0.0,
                }}, None
            return None, 'unsupported_operator_recovery_maneuver'

        def run_operator_maneuver(sock, remote, seq, request, local_x, local_y, local_z, altitude):
            action=str(request.get('recovery_action') or '')
            if action == 'adjust_speed':
                target_speed=recovery_parameter(request, 'target_speed_mps', 'speed_mps')
                if target_speed is None:
                    return {{
                        'command_id': MAV_CMD_DO_CHANGE_SPEED,
                        'attempted': False,
                        'ack_observed': False,
                        'ack_result': None,
                        'next_seq': seq,
                        'blocked_reasons': ['target_speed_mps_required'],
                        'recovery_path': 'MAV_CMD_DO_CHANGE_SPEED',
                    }}
                params=[0.0, float(target_speed), -1.0, 0.0, 0.0, 0.0, 0.0]
                command=send_command(sock, remote, MAV_CMD_DO_CHANGE_SPEED, params, seq, 10.0)
                return {{
                    **command,
                    'recovery_path': 'MAV_CMD_DO_CHANGE_SPEED',
                    'target_speed_mps': float(target_speed),
                }}
            target, blocked_reason=operator_maneuver_target(
                request,
                local_x,
                local_y,
                local_z,
                altitude,
            )
            if target is None:
                return {{
                    'command_id': None,
                    'attempted': False,
                    'ack_observed': False,
                    'ack_result': None,
                    'next_seq': seq,
                    'blocked_reasons': [blocked_reason],
                    'recovery_path': 'SET_POSITION_TARGET_LOCAL_NED:blocked',
                }}
            if local_x is None or local_y is None or local_z is None:
                return {{
                    'command_id': {MAV_CMD_DO_SET_MODE},
                    'attempted': False,
                    'ack_observed': False,
                    'ack_result': None,
                    'next_seq': seq,
                    'blocked_reasons': ['local_position_required'],
                    'recovery_path': 'SET_POSITION_TARGET_LOCAL_NED:blocked',
                    'target': target,
                }}
            started=time.monotonic()
            frames_sent=0
            for _ in range(OPERATOR_RECOVERY_ASSIST_PRESTREAM_FRAMES):
                sock.sendto(heartbeat(seq), remote); seq+=1
                sock.sendto(
                    setpoint_local_ned(
                        target['target_x_m'],
                        target['target_y_m'],
                        target['target_z_m'],
                        seq,
                        target['feed_forward_vx_mps'],
                        target['feed_forward_vy_mps'],
                        target['feed_forward_vz_mps'],
                    ),
                    remote,
                ); seq+=1
                frames_sent+=1
                time.sleep(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)
            offboard=send_command_with_recovery_setpoints(
                sock,
                remote,
                {MAV_CMD_DO_SET_MODE},
                {offboard_params!r},
                seq,
                5.0,
                target,
            )
            seq=offboard.get('next_seq', seq)
            frames_sent+=int(offboard.get('setpoint_frames_sent') or 0)
            status_after_offboard=listener('vehicle_status', 1)
            offboard_nav_state=parse_int(status_after_offboard, 'nav_state')
            offboard_state_observed=offboard_nav_state == NAV_OFFBOARD
            if (
                offboard.get('ack_result') != {MAV_RESULT_ACCEPTED}
                and not offboard_state_observed
            ):
                return {{
                    **offboard,
                    'status': 'offboard_mode_not_accepted',
                    'recovery_path': 'SET_POSITION_TARGET_LOCAL_NED:' + action,
                    'target': target,
                    'assist_kind': target.get('assist_kind'),
                    'offboard_ack_observed': offboard.get('ack_observed'),
                    'offboard_ack_result': offboard.get('ack_result'),
                    'offboard_state_observed': offboard_state_observed,
                    'offboard_nav_state': offboard_nav_state,
                    'setpoint_frames_sent': frames_sent,
                    'setpoint_stream_duration_seconds': round(time.monotonic()-started, 3),
                }}
            stream_started=time.monotonic()
            next_observe_at=stream_started
            target_reached=False
            first_x=float(local_x)
            first_y=float(local_y)
            first_altitude=float(altitude) if altitude is not None else None
            last_x=first_x
            last_y=first_y
            last_altitude=first_altitude
            last_distance_to_target=None
            last_altitude_error=None
            maneuver_samples=[]
            while (time.monotonic()-stream_started) < OPERATOR_RECOVERY_ASSIST_MAX_SECONDS:
                sock.sendto(heartbeat(seq), remote); seq+=1
                sock.sendto(
                    setpoint_local_ned(
                        target['target_x_m'],
                        target['target_y_m'],
                        target['target_z_m'],
                        seq,
                        target['feed_forward_vx_mps'],
                        target['feed_forward_vy_mps'],
                        target['feed_forward_vz_mps'],
                    ),
                    remote,
                ); seq+=1
                frames_sent+=1
                now=time.monotonic()
                if now >= next_observe_at:
                    local_sample=listener('vehicle_local_position', 1)
                    status_sample=listener('vehicle_status', 1)
                    sample_x=parse_float(local_sample, 'x')
                    sample_y=parse_float(local_sample, 'y')
                    sample_z=parse_float(local_sample, 'z')
                    sample_altitude=(-sample_z) if sample_z is not None else None
                    sample_nav=parse_int(status_sample, 'nav_state')
                    if sample_x is not None:
                        last_x=float(sample_x)
                    if sample_y is not None:
                        last_y=float(sample_y)
                    if sample_altitude is not None:
                        last_altitude=float(sample_altitude)
                    if sample_x is not None and sample_y is not None:
                        last_distance_to_target=math.hypot(
                            float(sample_x)-float(target['target_x_m']),
                            float(sample_y)-float(target['target_y_m']),
                        )
                    if sample_altitude is not None:
                        last_altitude_error=abs(
                            float(sample_altitude)-abs(float(target['target_z_m']))
                        )
                    maneuver_samples.append({{
                        'elapsed_seconds': round(time.monotonic()-started, 3),
                        'x_m': sample_x,
                        'y_m': sample_y,
                        'altitude_above_home_m': sample_altitude,
                        'nav_state': sample_nav,
                        'distance_to_target_m': (
                            round(last_distance_to_target, 3)
                            if last_distance_to_target is not None
                            else None
                        ),
                        'altitude_error_m': (
                            round(last_altitude_error, 3)
                            if last_altitude_error is not None
                            else None
                        ),
                    }})
                    if action == 'adjust_altitude':
                        target_reached=(
                            last_altitude_error is not None
                            and last_altitude_error <= 1.5
                        )
                    else:
                        target_reached=(
                            last_distance_to_target is not None
                            and last_distance_to_target <= 5.0
                            and (
                                last_altitude_error is None
                                or last_altitude_error <= 2.0
                            )
                        )
                    if target_reached:
                        break
                    next_observe_at=now+1.0
                time.sleep(OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS)
            maneuver_status='target_reached' if target_reached else 'stream_window_complete'
            resume_auto={{
                'command_id': {MAV_CMD_DO_SET_MODE},
                'attempted': False,
                'ack_observed': False,
                'ack_result': None,
                'ack_result_history': [],
                'next_seq': seq,
            }}
            resume_nav_wait={{'observed': False, 'samples': [], 'next_seq': seq, 'status': ''}}
            resume_auto_status='not_attempted_target_not_reached'
            should_resume_auto = target_reached or action in (
                'adjust_altitude',
                'reroute',
                'avoid_obstacle',
            )
            if should_resume_auto:
                resume_auto=send_command(
                    sock,
                    remote,
                    {MAV_CMD_DO_SET_MODE},
                    {auto_params!r},
                    seq,
                    10.0,
                )
                seq=resume_auto.get('next_seq', seq)
                resume_nav_wait=wait_nav_state(sock, remote, seq, NAV_AUTO_MISSION, 5.0)
                seq=resume_nav_wait.get('next_seq', seq)
                if (
                    resume_auto.get('ack_result') == {MAV_RESULT_ACCEPTED}
                    and resume_nav_wait.get('observed') is True
                ):
                    resume_auto_status='resumed_auto_mission'
                elif not resume_auto.get('ack_observed'):
                    resume_auto_status='resume_auto_ack_timeout'
                elif resume_auto.get('ack_result') != {MAV_RESULT_ACCEPTED}:
                    resume_auto_status='resume_auto_not_accepted'
                else:
                    resume_auto_status='resume_auto_nav_state_not_observed'
            return {{
                **offboard,
                'status': maneuver_status,
                'recovery_path': 'SET_POSITION_TARGET_LOCAL_NED:' + action,
                'target': target,
                'assist_kind': target.get('assist_kind'),
                'offboard_ack_observed': offboard.get('ack_observed'),
                'offboard_ack_result': offboard.get('ack_result'),
                'offboard_state_observed': offboard_state_observed,
                'offboard_nav_state': offboard_nav_state,
                'setpoint_frames_sent': frames_sent,
                'setpoint_stream_duration_seconds': round(time.monotonic()-started, 3),
                'target_reached': target_reached,
                'target_distance_m': (
                    round(last_distance_to_target, 3)
                    if last_distance_to_target is not None
                    else None
                ),
                'target_altitude_m': abs(float(target['target_z_m'])),
                'altitude_error_m': (
                    round(last_altitude_error, 3)
                    if last_altitude_error is not None
                    else None
                ),
                'first_local_x_m': round(first_x, 3),
                'first_local_y_m': round(first_y, 3),
                'first_altitude_above_home_m': (
                    round(first_altitude, 3)
                    if first_altitude is not None
                    else None
                ),
                'last_local_x_m': round(last_x, 3),
                'last_local_y_m': round(last_y, 3),
                'last_altitude_above_home_m': (
                    round(last_altitude, 3)
                    if last_altitude is not None
                    else None
                ),
                'local_delta_x_m': round(last_x-first_x, 3),
                'local_delta_y_m': round(last_y-first_y, 3),
                'altitude_delta_m': (
                    round(last_altitude-first_altitude, 3)
                    if last_altitude is not None and first_altitude is not None
                    else None
                ),
                'resume_auto_attempted': resume_auto.get('attempted'),
                'resume_auto_command_id': resume_auto.get('command_id'),
                'resume_auto_ack_observed': resume_auto.get('ack_observed'),
                'resume_auto_ack_result': resume_auto.get('ack_result'),
                'resume_auto_ack_result_history': resume_auto.get('ack_result_history'),
                'resume_auto_nav_state_observed': resume_nav_wait.get('observed'),
                'resume_auto_nav_state': parse_int(
                    str(resume_nav_wait.get('status') or ''),
                    'nav_state',
                ),
                'resume_auto_status': resume_auto_status,
                'maneuver_observation_samples': maneuver_samples[-5:],
            }}

        def execute_operator_recovery_request(sock, remote, seq, request, local_x=None, local_y=None, local_z=None, altitude=None):
            action=str(request.get('recovery_action') or '')
            if action == 'return_to_launch':
                command=send_command(sock, remote, {MAV_CMD_NAV_RETURN_TO_LAUNCH}, {rtl_params!r}, seq, 15.0)
                return command, 'MAV_CMD_NAV_RETURN_TO_LAUNCH', True
            if action == 'land':
                command=send_command(sock, remote, {MAV_CMD_NAV_LAND}, {land_params!r}, seq, 15.0)
                return command, 'MAV_CMD_NAV_LAND', True
            if action in ('adjust_altitude', 'adjust_speed', 'reroute', 'avoid_obstacle'):
                command=run_operator_maneuver(
                    sock,
                    remote,
                    seq,
                    request,
                    local_x,
                    local_y,
                    local_z,
                    altitude,
                )
                return command, command.get('recovery_path') or action, False
            return {{
                'command_id': None,
                'attempted': False,
                'ack_observed': False,
                'ack_result': None,
                'next_seq': seq,
                'blocked_reasons': ['unsupported_operator_recovery_action'],
            }}, 'unsupported', False

        def wait_preflight_ready(sock, remote, seq):
            samples=[]
            deadline=time.monotonic()+30.0
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                status=listener('vehicle_status', 1)
                samples.append(status)
                if parse_bool(status, 'pre_flight_checks_pass') is True and parse_bool(status, 'gcs_connection_lost') is False:
                    return {{'ready': True, 'samples': samples, 'next_seq': seq, 'status': status}}
                time.sleep(0.5)
            return {{'ready': False, 'samples': samples, 'next_seq': seq, 'status': samples[-1] if samples else ''}}

        def wait_nav_state(sock, remote, seq, expected_nav_state, timeout_seconds):
            samples=[]
            deadline=time.monotonic()+float(timeout_seconds)
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                status=listener('vehicle_status', 1)
                samples.append(status)
                if parse_int(status, 'nav_state') == int(expected_nav_state):
                    return {{'observed': True, 'samples': samples, 'next_seq': seq, 'status': status}}
                time.sleep(0.25)
            return {{'observed': False, 'samples': samples, 'next_seq': seq, 'status': samples[-1] if samples else ''}}

        def clear_operator_recovery_request():
            if not OPERATOR_RECOVERY_REQUEST_PATH:
                return
            for suffix in ('', '.consumed'):
                try:
                    os.unlink(OPERATOR_RECOVERY_REQUEST_PATH + suffix)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

        def read_operator_recovery_request():
            if not OPERATOR_RECOVERY_REQUEST_PATH:
                return None
            try:
                with open(OPERATOR_RECOVERY_REQUEST_PATH, 'r', encoding='utf-8') as handle:
                    payload=json.load(handle)
            except FileNotFoundError:
                return None
            except (OSError, json.JSONDecodeError) as exc:
                return {{
                    'request_status': 'invalid',
                    'invalid_reason': type(exc).__name__,
                    'request_path': OPERATOR_RECOVERY_REQUEST_PATH,
                }}
            if not isinstance(payload, dict):
                return {{
                    'request_status': 'invalid',
                    'invalid_reason': 'operator_recovery_request_not_object',
                    'request_path': OPERATOR_RECOVERY_REQUEST_PATH,
                }}
            action=str(payload.get('recovery_action') or '')
            supported_actions=(
                'land',
                'return_to_launch',
                'adjust_altitude',
                'adjust_speed',
                'reroute',
                'avoid_obstacle',
            )
            if action not in supported_actions:
                payload=dict(payload)
                payload.update({{
                    'request_status': 'invalid',
                    'invalid_reason': 'unsupported_recovery_action',
                    'request_path': OPERATOR_RECOVERY_REQUEST_PATH,
                }})
            return payload

        def mark_operator_recovery_request_consumed(request, command):
            if not OPERATOR_RECOVERY_REQUEST_PATH:
                return
            payload={{
                'schema_version': 'missionos_auto_operator_recovery_request_consumed.v1',
                'request': request,
                'command': command,
                'consumed_at': datetime.now(timezone.utc).isoformat(),
            }}
            try:
                with open(OPERATOR_RECOVERY_REQUEST_PATH + '.consumed', 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, sort_keys=True)
                os.unlink(OPERATOR_RECOVERY_REQUEST_PATH)
            except FileNotFoundError:
                pass
            except OSError:
                pass

        def monitor_auto(sock, remote, seq):
            samples=[]
            # Warm up the background BatteryState subscriber before the loop so
            # gz-transport discovery completes off the critical path and early
            # samples already have data.
            if GZ_PHYSICAL_BATTERY_ENABLED:
                ensure_gz_battery_stream()
            wind_application_events=[]
            wind_effect_requested=bool(WIND_MEAN_MPS is not None or WIND_GUST_MPS is not None)
            wind_mean_started=False
            wind_mean_publish_attempted=False
            wind_mean_pending_reason='waiting_for_takeoff_clearance' if wind_effect_requested else None
            wind_takeoff_clearance_min_altitude_m=min(float(MIN_ROUTE_ALTITUDE_M), 8.0)
            wind_mean_application_elapsed_seconds=None
            wind_mean_application_altitude_m=None
            guard_reason=None
            payload_release={{
                'attempted': False,
                'command_id': {MAV_CMD_DO_GRIPPER},
                'ack_observed': False,
                'ack_result': None,
                'trigger_sample_index': None,
                'trigger_elapsed_seconds': None,
                'trigger_reason': None,
                'payload_release_sim_event': None,
            }}
            first_local=None
            dwell_started_at=None
            monitor_stop_reason=None
            battery_remaining_first=None
            battery_remaining_last=None
            battery_warning_last=None
            battery_remaining_sample_count=0
            heartbeat_last_observed_at=None
            last_snapshot_payload=None
            terminal_snapshot_payload=None
            route_progress_started_at=None
            gust_started=False
            gust_ended=False
            gust_active=False
            operator_recovery={{
                'request_observed': False,
                'request': None,
                'command': None,
            }}
            started=time.monotonic()
            while time.monotonic() - started < MONITOR_SECONDS:
                elapsed=time.monotonic() - started
                sock.sendto(heartbeat(seq), remote); seq+=1
                heartbeat_instant_observed=observe_heartbeat(sock)
                if heartbeat_instant_observed:
                    heartbeat_last_observed_at=elapsed
                heartbeat_observed=bool(
                    heartbeat_last_observed_at is not None
                    and (elapsed - heartbeat_last_observed_at) <= HEARTBEAT_LIVENESS_WINDOW_SECONDS
                )
                status=listener('vehicle_status', 1)
                local=listener('vehicle_local_position', 1)
                global_position=listener('vehicle_global_position', 1)
                mission_result=listener('mission_result', 1)
                battery=listener('battery_status', 1)
                statustext=listener('mavlink_log', 1)
                if first_local is None and parse_float(local, 'x') is not None:
                    first_local=local
                progress=local_progress(first_local or local, local)
                nav=parse_int(status, 'nav_state')
                z=parse_float(local, 'z')
                altitude=(-z) if z is not None else None
                remaining=battery_remaining_percent(battery)
                warning=parse_int(battery, 'warning')
                battery_sample_rejected_reason=None
                battery_sample_accepted=bool(remaining is not None)
                if (
                    battery_sample_accepted
                    and battery_remaining_last is not None
                    and abs(float(remaining)-float(battery_remaining_last)) > 5.0
                ):
                    battery_sample_accepted=False
                    battery_sample_rejected_reason='battery_remaining_discontinuous_jump'
                if remaining is None:
                    battery_sample_rejected_reason='battery_remaining_not_observed'
                if battery_sample_accepted:
                    if battery_remaining_first is None:
                        battery_remaining_first=remaining
                    battery_remaining_last=remaining
                    battery_warning_last=warning
                    battery_remaining_sample_count+=1
                battery_remaining_delta=None
                battery_remaining_dynamic=None
                if battery_remaining_first is not None and battery_remaining_last is not None:
                    battery_remaining_delta=round(float(battery_remaining_last)-float(battery_remaining_first), 3)
                    if battery_remaining_sample_count >= 2:
                        battery_remaining_dynamic=abs(float(battery_remaining_delta)) > 0.001
                mission_current=parse_int(mission_result, 'seq_current')
                mission_reached=parse_int(mission_result, 'seq_reached')
                if (
                    wind_effect_requested
                    and not wind_mean_started
                    and not wind_mean_publish_attempted
                ):
                    if altitude is not None and altitude >= wind_takeoff_clearance_min_altitude_m:
                        event=publish_gazebo_wind(
                            WIND_MEAN_MPS if WIND_MEAN_MPS is not None else WIND_GUST_MPS,
                            'auto_mission_mean_wind_after_takeoff_clearance',
                        )
                        event.update({{
                            'trigger_elapsed_seconds': round(elapsed, 3),
                            'trigger_altitude_above_home_m': round(float(altitude), 3),
                            'takeoff_clearance_min_altitude_m': wind_takeoff_clearance_min_altitude_m,
                        }})
                        wind_application_events.append(event)
                        wind_mean_publish_attempted=True
                        wind_mean_started=bool(event.get('published'))
                        wind_mean_pending_reason=None if wind_mean_started else 'mean_wind_publish_failed'
                        if wind_mean_started:
                            wind_mean_application_elapsed_seconds=round(elapsed, 3)
                            wind_mean_application_altitude_m=round(float(altitude), 3)
                    else:
                        wind_mean_pending_reason='waiting_for_takeoff_clearance'
                if (
                    wind_effect_requested
                    and wind_mean_started
                    and WIND_GUST_MPS is not None
                    and not gust_started
                    and elapsed >= WIND_GUST_START_SECONDS
                ):
                    wind_application_events.append(
                        publish_gazebo_wind(WIND_GUST_MPS, 'gust_window_start')
                    )
                    gust_started=True
                    gust_active=True
                if (
                    wind_effect_requested
                    and wind_mean_started
                    and WIND_GUST_MPS is not None
                    and gust_started
                    and not gust_ended
                    and elapsed >= WIND_GUST_START_SECONDS + WIND_GUST_DURATION_SECONDS
                ):
                    wind_application_events.append(
                        publish_gazebo_wind(
                            WIND_MEAN_MPS if WIND_MEAN_MPS is not None else WIND_GUST_MPS,
                            'gust_window_end_return_to_mean',
                        )
                    )
                    gust_ended=True
                    gust_active=False
                route_progress_guard_active=(
                    mission_current is not None
                    and mission_current >= 1
                    and mission_current < LAND_SEQ
                    and altitude is not None
                    and altitude >= MIN_ROUTE_ALTITUDE_M
                )
                if route_progress_guard_active and route_progress_started_at is None:
                    route_progress_started_at=elapsed
                in_dwell_seq=(mission_current == DROPOFF_DWELL_SEQ)
                dwell_mission_reached=(
                    mission_reached is not None
                    and mission_reached >= DROPOFF_DWELL_SEQ
                )
                altitude_in_release_band=(
                    altitude is not None
                    and abs(float(altitude)-RELEASE_ALTITUDE_TARGET_M) <= RELEASE_ALTITUDE_TOLERANCE_M
                )
                if in_dwell_seq and altitude_in_release_band:
                    if dwell_started_at is None:
                        dwell_started_at=elapsed
                else:
                    dwell_started_at=None
                dwell_seconds=(elapsed-dwell_started_at) if dwell_started_at is not None else 0.0
                sample={{
                    'elapsed_seconds': round(elapsed, 3),
                    'vehicle_status': status,
                    'vehicle_local_position': local,
                    'vehicle_global_position': global_position,
                    'mission_result': mission_result,
                    'battery_status': battery,
                    'statustext': statustext,
                    'progress_m': round(progress, 3),
                    'telemetry_stale': False,
                    'heartbeat_observed': heartbeat_observed,
                    'heartbeat_instant_observed': heartbeat_instant_observed,
                    'heartbeat_liveness_window_seconds': HEARTBEAT_LIVENESS_WINDOW_SECONDS,
                    'heartbeat_last_observed_elapsed_seconds': heartbeat_last_observed_at,
                    'dropoff_dwell_candidate': bool(in_dwell_seq and altitude_in_release_band),
                    'dropoff_dwell_seconds': round(dwell_seconds, 3),
                    'gust_active': bool(gust_active),
                    'gust_started': bool(gust_started),
                    'gust_ended': bool(gust_ended),
                    'wind_mean_started': bool(wind_mean_started),
                    'wind_mean_pending_reason': wind_mean_pending_reason,
                    'wind_takeoff_clearance_min_altitude_m': wind_takeoff_clearance_min_altitude_m if wind_effect_requested else None,
                    'wind_mean_application_elapsed_seconds': wind_mean_application_elapsed_seconds,
                    'wind_mean_application_altitude_m': wind_mean_application_altitude_m,
                    'gazebo_obstacle_model_spawned': obstacle_application.get('gazebo_obstacle_model_spawned'),
                    'gazebo_obstacle_model_spawn_requested': obstacle_application.get('gazebo_obstacle_model_spawn_requested'),
                    'gazebo_obstacle_application_status': obstacle_application.get('application_status'),
                    'obstacle_manifest': obstacle_application.get('obstacle_manifest') or {{}},
                    'gazebo_obstacle_application': obstacle_application,
                }}
                samples.append(sample)
                _snap_x=parse_float(local, 'x')
                _snap_y=parse_float(local, 'y')
                gz_battery=gz_battery_state_sample() if GZ_PHYSICAL_BATTERY_ENABLED else {{}}
                operator_recovery_command=(
                    operator_recovery.get('command')
                    if isinstance(operator_recovery.get('command'), dict)
                    else {{}}
                )
                # Persist the gz battery reading into the per-sample record so the
                # proportional drain is auditable across the WHOLE flight -- the
                # running_snapshot.json keeps only the latest sample, so without
                # this a single late read timeout would erase all evidence.
                if gz_battery:
                    sample.update(gz_battery)
                if GZ_PHYSICAL_BATTERY_ENABLED:
                    sample['gz_battery_motor_coupling_requested']=GZ_BATTERY_MOTOR_COUPLING_REQUESTED
                snapshot_payload={{
                    'sample_index': len(samples)-1,
                    'elapsed_seconds': round(elapsed, 3),
                    'progress_m': round(progress, 3),
                    'mission_current_seq': mission_current,
                    'mission_reached_seq': mission_reached,
                    'altitude_above_home_m': (round(altitude, 3) if altitude is not None else None),
                    'local_x_m': _snap_x,
                    'local_y_m': _snap_y,
                    'local_z_m': z,
                    'distance_to_home_m': (round(math.hypot(_snap_x, _snap_y), 3) if _snap_x is not None and _snap_y is not None else None),
                    'nav_state': nav,
                    'battery_remaining_percent': battery_remaining_last,
                    'battery_remaining_first_percent': battery_remaining_first,
                    'battery_remaining_latest_percent': battery_remaining_last,
                    'battery_remaining_delta_percent': battery_remaining_delta,
                    'battery_remaining_sample_count': battery_remaining_sample_count,
                    'battery_remaining_dynamic': battery_remaining_dynamic,
                    'battery_state_source': 'px4-listener:battery_status_sitl_simulated',
                    'battery_sample_accepted': battery_sample_accepted,
                    'battery_sample_rejected_reason': battery_sample_rejected_reason,
                    'battery_warning': battery_warning_last,
                    'heartbeat_observed': heartbeat_observed,
                    'heartbeat_instant_observed': heartbeat_instant_observed,
                    'heartbeat_liveness_window_seconds': HEARTBEAT_LIVENESS_WINDOW_SECONDS,
                    'heartbeat_last_observed_elapsed_seconds': heartbeat_last_observed_at,
                    'dropoff_dwell_candidate': bool(in_dwell_seq and altitude_in_release_band),
                    'gust_active': bool(gust_active),
                    'gust_started': bool(gust_started),
                    'gust_ended': bool(gust_ended),
                    'wind_mean_started': bool(wind_mean_started),
                    'wind_mean_pending_reason': wind_mean_pending_reason,
                    'wind_takeoff_clearance_min_altitude_m': wind_takeoff_clearance_min_altitude_m if wind_effect_requested else None,
                    'wind_mean_application_elapsed_seconds': wind_mean_application_elapsed_seconds,
                    'wind_mean_application_altitude_m': wind_mean_application_altitude_m,
                    'gazebo_obstacle_model_spawned': obstacle_application.get('gazebo_obstacle_model_spawned'),
                    'gazebo_obstacle_model_spawn_requested': obstacle_application.get('gazebo_obstacle_model_spawn_requested'),
                    'gazebo_obstacle_application_status': obstacle_application.get('application_status'),
                    'obstacle_manifest': obstacle_application.get('obstacle_manifest') or {{}},
                    'gazebo_obstacle_application': obstacle_application,
                    'gust_window_start_seconds': round(WIND_GUST_START_SECONDS, 3) if WIND_GUST_MPS is not None else None,
                    'gust_window_duration_seconds': round(WIND_GUST_DURATION_SECONDS, 3) if WIND_GUST_MPS is not None else None,
                    'operator_recovery_request_observed': bool(operator_recovery.get('request_observed')),
                    'operator_recovery_action': (
                        (operator_recovery.get('request') or {{}}).get('recovery_action')
                        if isinstance(operator_recovery.get('request'), dict)
                        else None
                    ),
                    'operator_recovery_parameters': (
                        (operator_recovery.get('request') or {{}}).get('recovery_parameters')
                        if isinstance(operator_recovery.get('request'), dict)
                        else None
                    ),
                    'operator_recovery_command_ack_observed': (
                        operator_recovery_command.get('ack_observed')
                    ),
                    'operator_recovery_command_ack_result': (
                        operator_recovery_command.get('ack_result')
                    ),
                    'operator_recovery_path': operator_recovery_command.get('recovery_path'),
                    'operator_recovery_target': operator_recovery_command.get('target'),
                    'operator_recovery_assist_attempted': operator_recovery_command.get('attempted'),
                    'operator_recovery_assist_status': operator_recovery_command.get('status'),
                    'operator_recovery_assist_kind': operator_recovery_command.get('assist_kind'),
                    'operator_recovery_assist_offboard_ack_observed': operator_recovery_command.get('offboard_ack_observed'),
                    'operator_recovery_assist_offboard_ack_result': operator_recovery_command.get('offboard_ack_result'),
                    'operator_recovery_assist_offboard_state_observed': operator_recovery_command.get('offboard_state_observed'),
                    'operator_recovery_assist_offboard_nav_state': operator_recovery_command.get('offboard_nav_state'),
                    'operator_recovery_assist_setpoint_frames_sent': operator_recovery_command.get('setpoint_frames_sent'),
                    'operator_recovery_assist_stream_duration_seconds': operator_recovery_command.get('setpoint_stream_duration_seconds'),
                    'operator_recovery_target_reached': operator_recovery_command.get('target_reached'),
                    'operator_recovery_target_distance_m': operator_recovery_command.get('target_distance_m'),
                    'operator_recovery_target_altitude_m': operator_recovery_command.get('target_altitude_m'),
                    'operator_recovery_altitude_error_m': operator_recovery_command.get('altitude_error_m'),
                    'operator_recovery_local_delta_x_m': operator_recovery_command.get('local_delta_x_m'),
                    'operator_recovery_local_delta_y_m': operator_recovery_command.get('local_delta_y_m'),
                    'operator_recovery_altitude_delta_m': operator_recovery_command.get('altitude_delta_m'),
                    'operator_recovery_terminal': operator_recovery_command.get('terminal_recovery'),
                    'operator_recovery_resume_auto_attempted': operator_recovery_command.get('resume_auto_attempted'),
                    'operator_recovery_resume_auto_ack_observed': operator_recovery_command.get('resume_auto_ack_observed'),
                    'operator_recovery_resume_auto_ack_result': operator_recovery_command.get('resume_auto_ack_result'),
                    'operator_recovery_resume_auto_nav_state_observed': operator_recovery_command.get('resume_auto_nav_state_observed'),
                    'operator_recovery_resume_auto_nav_state': operator_recovery_command.get('resume_auto_nav_state'),
                    'operator_recovery_resume_auto_status': operator_recovery_command.get('resume_auto_status'),
                }}
                if gz_battery:
                    snapshot_payload.update(gz_battery)
                if GZ_PHYSICAL_BATTERY_ENABLED:
                    snapshot_payload['gz_battery_motor_coupling_requested']=GZ_BATTERY_MOTOR_COUPLING_REQUESTED
                last_snapshot_payload=snapshot_payload
                print(json.dumps({{'auto_running_snapshot': snapshot_payload}}, sort_keys=True), flush=True)
                recovery_request=read_operator_recovery_request()
                if recovery_request is not None:
                    operator_recovery['request_observed']=True
                    operator_recovery['request']=recovery_request
                    command,recovery_path,terminal_recovery=execute_operator_recovery_request(
                        sock,
                        remote,
                        seq,
                        recovery_request,
                        _snap_x,
                        _snap_y,
                        z,
                        altitude,
                    )
                    seq=command.get('next_seq', seq)
                    command={{**command, 'recovery_path': recovery_path, 'terminal_recovery': terminal_recovery}}
                    operator_recovery['command']=command
                    operator_recovery['terminal_recovery']=terminal_recovery
                    operator_recovery_command=command
                    mark_operator_recovery_request_consumed(recovery_request, command)
                    if terminal_recovery:
                        if command.get('ack_result') == {MAV_RESULT_ACCEPTED}:
                            monitor_stop_reason='operator_recovery_dispatch_acked'
                        elif not command.get('ack_observed'):
                            monitor_stop_reason='operator_recovery_dispatch_ack_timeout'
                        else:
                            monitor_stop_reason='operator_recovery_dispatch_not_accepted'
                        break
                if (
                    not payload_release['attempted']
                    and altitude_in_release_band
                    and (
                        (
                            in_dwell_seq
                            and dwell_seconds >= REQUIRED_DWELL_SECONDS
                        )
                        or dwell_mission_reached
                    )
                ):
                    trigger_reason=(
                        'dropoff_dwell_mission_reached_release_envelope'
                        if dwell_mission_reached
                        else 'dropoff_dwell_release_envelope'
                    )
                    release=send_command(sock, remote, {MAV_CMD_DO_GRIPPER}, {release_params!r}, seq, 10.0)
                    seq=release['next_seq']
                    l1_release_event=(
                        trigger_l1_payload_release()
                        if release.get('ack_result') == {MAV_RESULT_ACCEPTED}
                        else None
                    )
                    payload_release={{
                        **release,
                        'attempted': True,
                        'trigger_sample_index': len(samples)-1,
                        'trigger_elapsed_seconds': round(elapsed, 3),
                        'trigger_reason': trigger_reason,
                        'mission_current_seq': mission_current,
                        'mission_reached_seq': mission_reached,
                        'dwell_seconds': round(dwell_seconds, 3),
                        'payload_release_sim_event': l1_release_event,
                    }}
                    monitor_stop_reason=(
                        'payload_release_command_acked'
                        if release.get('ack_result') == {MAV_RESULT_ACCEPTED}
                        else (
                            'payload_release_command_ack_timeout'
                            if not release.get('ack_observed')
                            else 'payload_release_command_not_accepted'
                        )
                    )
                    break
                if nav is not None and nav != NAV_AUTO_MISSION:
                    guard_reason='auto_mission_mode_lost'
                    break
                if remaining is not None and remaining < MIN_BATTERY_REMAINING_PERCENT:
                    guard_reason='auto_mission_battery_reserve_low'
                    break
                if warning is not None and warning > 0:
                    guard_reason='auto_mission_battery_warning'
                    break
                route_altitude_guard_active=(
                    mission_current is None
                    or mission_current < LAND_SEQ
                )
                if (
                    route_altitude_guard_active
                    and elapsed >= ALTITUDE_GRACE_SECONDS
                    and altitude is not None
                    and altitude < MIN_ROUTE_ALTITUDE_M
                ):
                    guard_reason='auto_mission_altitude_below_min'
                    break
                if (
                    route_progress_started_at is not None
                    and (elapsed - route_progress_started_at) >= NO_PROGRESS_GRACE_SECONDS
                    and progress < MIN_PROGRESS_M
                ):
                    guard_reason='auto_mission_no_progress'
                    break
                time.sleep(1.0)
            if monitor_stop_reason is None:
                monitor_stop_reason=guard_reason or 'monitor_window_complete'
            if wind_effect_requested and not wind_mean_started:
                wind_application_events.append({{
                    'phase': 'auto_mission_mean_wind_delayed_until_takeoff_clearance',
                    'published': False,
                    'reason': wind_mean_pending_reason or 'takeoff_clearance_not_reached',
                    'takeoff_clearance_min_altitude_m': wind_takeoff_clearance_min_altitude_m,
                    'monitor_stop_reason': monitor_stop_reason,
                    'last_altitude_above_home_m': (
                        last_snapshot_payload.get('altitude_above_home_m')
                        if last_snapshot_payload is not None else None
                    ),
                    'last_progress_m': (
                        last_snapshot_payload.get('progress_m')
                        if last_snapshot_payload is not None else None
                    ),
                }})
            if last_snapshot_payload is not None:
                terminal_snapshot_payload=dict(last_snapshot_payload)
                terminal_snapshot_payload['sample_index']=len(samples)
                terminal_snapshot_payload['monitor_window_ended']=True
                terminal_snapshot_payload['monitor_stop_reason']=monitor_stop_reason
                terminal_snapshot_payload['operator_recovery_request_observed']=bool(operator_recovery.get('request_observed'))
                terminal_snapshot_payload['operator_recovery_action']=(
                    (operator_recovery.get('request') or {{}}).get('recovery_action')
                    if isinstance(operator_recovery.get('request'), dict)
                    else None
                )
                terminal_snapshot_payload['operator_recovery_parameters']=(
                    (operator_recovery.get('request') or {{}}).get('recovery_parameters')
                    if isinstance(operator_recovery.get('request'), dict)
                    else None
                )
                terminal_snapshot_payload['operator_recovery_command_ack_observed']=(
                    operator_recovery_command.get('ack_observed')
                )
                terminal_snapshot_payload['operator_recovery_command_ack_result']=(
                    operator_recovery_command.get('ack_result')
                )
                terminal_snapshot_payload['operator_recovery_path']=operator_recovery_command.get('recovery_path')
                terminal_snapshot_payload['operator_recovery_target']=operator_recovery_command.get('target')
                terminal_snapshot_payload['operator_recovery_assist_attempted']=operator_recovery_command.get('attempted')
                terminal_snapshot_payload['operator_recovery_assist_status']=operator_recovery_command.get('status')
                terminal_snapshot_payload['operator_recovery_assist_kind']=operator_recovery_command.get('assist_kind')
                terminal_snapshot_payload['operator_recovery_assist_offboard_ack_observed']=operator_recovery_command.get('offboard_ack_observed')
                terminal_snapshot_payload['operator_recovery_assist_offboard_ack_result']=operator_recovery_command.get('offboard_ack_result')
                terminal_snapshot_payload['operator_recovery_assist_offboard_state_observed']=operator_recovery_command.get('offboard_state_observed')
                terminal_snapshot_payload['operator_recovery_assist_offboard_nav_state']=operator_recovery_command.get('offboard_nav_state')
                terminal_snapshot_payload['operator_recovery_assist_setpoint_frames_sent']=operator_recovery_command.get('setpoint_frames_sent')
                terminal_snapshot_payload['operator_recovery_assist_stream_duration_seconds']=operator_recovery_command.get('setpoint_stream_duration_seconds')
                terminal_snapshot_payload['operator_recovery_target_reached']=operator_recovery_command.get('target_reached')
                terminal_snapshot_payload['operator_recovery_target_distance_m']=operator_recovery_command.get('target_distance_m')
                terminal_snapshot_payload['operator_recovery_target_altitude_m']=operator_recovery_command.get('target_altitude_m')
                terminal_snapshot_payload['operator_recovery_altitude_error_m']=operator_recovery_command.get('altitude_error_m')
                terminal_snapshot_payload['operator_recovery_local_delta_x_m']=operator_recovery_command.get('local_delta_x_m')
                terminal_snapshot_payload['operator_recovery_local_delta_y_m']=operator_recovery_command.get('local_delta_y_m')
                terminal_snapshot_payload['operator_recovery_altitude_delta_m']=operator_recovery_command.get('altitude_delta_m')
                terminal_snapshot_payload['operator_recovery_terminal']=operator_recovery_command.get('terminal_recovery')
                terminal_snapshot_payload['operator_recovery_resume_auto_attempted']=operator_recovery_command.get('resume_auto_attempted')
                terminal_snapshot_payload['operator_recovery_resume_auto_ack_observed']=operator_recovery_command.get('resume_auto_ack_observed')
                terminal_snapshot_payload['operator_recovery_resume_auto_ack_result']=operator_recovery_command.get('resume_auto_ack_result')
                terminal_snapshot_payload['operator_recovery_resume_auto_nav_state_observed']=operator_recovery_command.get('resume_auto_nav_state_observed')
                terminal_snapshot_payload['operator_recovery_resume_auto_nav_state']=operator_recovery_command.get('resume_auto_nav_state')
                terminal_snapshot_payload['operator_recovery_resume_auto_status']=operator_recovery_command.get('resume_auto_status')
                print(json.dumps({{'auto_running_snapshot': terminal_snapshot_payload}}, sort_keys=True), flush=True)
            return {{'samples': samples, 'guard_reason': guard_reason, 'monitor_stop_reason': monitor_stop_reason, 'next_seq': seq, 'monitor_elapsed_seconds': round(time.monotonic()-started, 3), 'payload_release': payload_release, 'terminal_snapshot': terminal_snapshot_payload, 'wind_application_events': wind_application_events, 'operator_recovery': operator_recovery}}

        def wait_land_or_disarm(sock, remote, seq, wait_seconds):
            samples=[]
            disarm=None
            operator_recovery_overrides=[]
            operator_recovery_assists=[]
            active_recovery_assist=None
            started=time.monotonic()
            deadline=time.monotonic()+float(wait_seconds)
            post_abort_baseline_action=None
            post_abort_baseline_home_distance_m=None
            post_abort_baseline_altitude_m=None
            post_abort_baseline_elapsed_seconds=None
            try:
                monitor_elapsed_base=float((monitor or {{}}).get('monitor_elapsed_seconds') or 0.0)
            except Exception:
                monitor_elapsed_base=0.0
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                heartbeat_observed=observe_heartbeat(sock)
                status=listener('vehicle_status', 1)
                local=listener('vehicle_local_position', 1)
                mission_result=listener('mission_result', 1)
                battery=listener('battery_status', 1)
                land_detected=listener('vehicle_land_detected', 1)
                samples.append({{'vehicle_status': status, 'vehicle_local_position': local, 'mission_result': mission_result, 'battery_status': battery, 'vehicle_land_detected': land_detected, 'heartbeat_observed': heartbeat_observed}})
                recovery_request=read_operator_recovery_request()
                if recovery_request is not None:
                    local_x=parse_float(local, 'x')
                    local_y=parse_float(local, 'y')
                    local_z=parse_float(local, 'z')
                    altitude=(-local_z) if local_z is not None else None
                    command,recovery_path,_terminal_recovery=execute_operator_recovery_request(
                        sock,
                        remote,
                        seq,
                        recovery_request,
                        local_x,
                        local_y,
                        local_z,
                        altitude,
                    )
                    seq=command.get('next_seq', seq)
                    command={{**command, 'recovery_path': recovery_path}}
                    override={{
                        'request': recovery_request,
                        'command': command,
                        'sample_index': len(samples)-1,
                        'elapsed_seconds': round(time.monotonic()-started, 3),
                    }}
                    operator_recovery_overrides.append(override)
                    mark_operator_recovery_request_consumed(recovery_request, command)
                local_x=parse_float(local, 'x')
                local_y=parse_float(local, 'y')
                local_z=parse_float(local, 'z')
                altitude=(-local_z) if local_z is not None else None
                home_distance=(
                    math.hypot(local_x, local_y)
                    if local_x is not None and local_y is not None
                    else None
                )
                active_recovery_action=str(recovery_action or '')
                active_recovery_command=recovery if isinstance(recovery, dict) else {{}}
                if operator_recovery_overrides:
                    last_override=operator_recovery_overrides[-1]
                    last_request=last_override.get('request') if isinstance(last_override.get('request'), dict) else {{}}
                    active_recovery_action=str(last_request.get('recovery_action') or active_recovery_action)
                    active_recovery_command=(
                        last_override.get('command')
                        if isinstance(last_override.get('command'), dict)
                        else active_recovery_command
                    )
                ack_observed=active_recovery_command.get('ack_observed')
                ack_result=active_recovery_command.get('ack_result')
                stop_reason=(
                    'operator_recovery_dispatch_acked'
                    if ack_result == {MAV_RESULT_ACCEPTED}
                    else (
                        'operator_recovery_dispatch_ack_timeout'
                        if active_recovery_command and not ack_observed
                        else 'operator_recovery_dispatch_not_accepted'
                        if active_recovery_command
                        else 'post_abort_waiting_for_recovery_outcome'
                    )
                )
                battery_remaining=battery_remaining_percent(battery)
                landed=parse_bool(land_detected, 'landed')
                arming=parse_int(status, 'arming_state')
                now_post_abort_elapsed=time.monotonic()-started
                if active_recovery_action != post_abort_baseline_action:
                    post_abort_baseline_action=active_recovery_action
                    post_abort_baseline_home_distance_m=home_distance
                    post_abort_baseline_altitude_m=altitude
                    post_abort_baseline_elapsed_seconds=now_post_abort_elapsed
                observation_seconds=(
                    now_post_abort_elapsed - post_abort_baseline_elapsed_seconds
                    if post_abort_baseline_elapsed_seconds is not None
                    else None
                )
                home_distance_delta=(
                    home_distance - post_abort_baseline_home_distance_m
                    if home_distance is not None
                    and post_abort_baseline_home_distance_m is not None
                    else None
                )
                altitude_delta=(
                    altitude - post_abort_baseline_altitude_m
                    if altitude is not None
                    and post_abort_baseline_altitude_m is not None
                    else None
                )
                outcome_status=recovery_outcome_status(
                    active_recovery_action,
                    home_distance_delta,
                    altitude_delta,
                    observation_seconds,
                    landed,
                    arming=arming,
                    altitude=altitude,
                )
                if (
                    active_recovery_assist is None
                    and ack_result == {MAV_RESULT_ACCEPTED}
                    and active_recovery_action in ('return_to_launch', 'land')
                    and outcome_status in (
                        'return_progress_not_observed',
                        'landing_descent_not_observed',
                        'landing_not_landed_after_descent',
                    )
                ):
                    active_recovery_assist=run_bounded_recovery_assist(
                        sock,
                        remote,
                        seq,
                        active_recovery_action,
                        local_x,
                        local_y,
                        local_z,
                        altitude,
                    )
                    seq=active_recovery_assist.get('next_seq', seq)
                    operator_recovery_assists.append(active_recovery_assist)
                    status=listener('vehicle_status', 1)
                    local=listener('vehicle_local_position', 1)
                    mission_result=listener('mission_result', 1)
                    battery=listener('battery_status', 1)
                    land_detected=listener('vehicle_land_detected', 1)
                    local_x=parse_float(local, 'x')
                    local_y=parse_float(local, 'y')
                    local_z=parse_float(local, 'z')
                    altitude=(-local_z) if local_z is not None else None
                    home_distance=(
                        math.hypot(local_x, local_y)
                        if local_x is not None and local_y is not None
                        else None
                    )
                    landed=parse_bool(land_detected, 'landed')
                    arming=parse_int(status, 'arming_state')
                    battery_remaining=battery_remaining_percent(battery)
                    home_distance_delta=(
                        home_distance - post_abort_baseline_home_distance_m
                        if home_distance is not None
                        and post_abort_baseline_home_distance_m is not None
                        else None
                    )
                    altitude_delta=(
                        altitude - post_abort_baseline_altitude_m
                        if altitude is not None
                        and post_abort_baseline_altitude_m is not None
                        else None
                    )
                    outcome_status=recovery_outcome_status(
                        active_recovery_action,
                        home_distance_delta,
                        altitude_delta,
                        observation_seconds,
                        landed,
                        arming=arming,
                        altitude=altitude,
                    )
                post_abort_snapshot={{
                    'sample_index': 100000 + len(samples),
                    'elapsed_seconds': round(monitor_elapsed_base + now_post_abort_elapsed, 3),
                    'progress_m': (
                        round(home_distance, 3)
                        if home_distance is not None
                        else None
                    ),
                    'mission_current_seq': parse_int(mission_result, 'seq_current'),
                    'mission_reached_seq': parse_int(mission_result, 'seq_reached'),
                    'altitude_above_home_m': (
                        round(altitude, 3) if altitude is not None else None
                    ),
                    'local_x_m': local_x,
                    'local_y_m': local_y,
                    'local_z_m': local_z,
                    'distance_to_home_m': (
                        round(home_distance, 3)
                        if home_distance is not None
                        else None
                    ),
                    'nav_state': parse_int(status, 'nav_state'),
                    'battery_remaining_percent': battery_remaining,
                    'heartbeat_observed': heartbeat_observed,
                    'monitor_window_ended': True,
                    'monitor_stop_reason': stop_reason,
                    'post_abort_tracking': True,
                    'post_abort_elapsed_seconds': round(now_post_abort_elapsed, 3),
                    'post_abort_observation_seconds': (
                        round(observation_seconds, 3)
                        if observation_seconds is not None
                        else None
                    ),
                    'post_abort_home_distance_delta_m': (
                        round(home_distance_delta, 3)
                        if home_distance_delta is not None
                        else None
                    ),
                    'post_abort_altitude_delta_m': (
                        round(altitude_delta, 3)
                        if altitude_delta is not None
                        else None
                    ),
                    'post_abort_outcome_status': outcome_status,
                    'operator_recovery_request_observed': bool(active_recovery_command),
                    'operator_recovery_action': active_recovery_action or None,
                    'operator_recovery_parameters': (
                        (last_request or {{}}).get('recovery_parameters')
                        if operator_recovery_overrides
                        else (
                            (operator_recovery_request or {{}}).get('recovery_parameters')
                            if isinstance(operator_recovery_request, dict)
                            else None
                        )
                    ),
                    'operator_recovery_command_ack_observed': ack_observed,
                    'operator_recovery_command_ack_result': ack_result,
                    'operator_recovery_path': active_recovery_command.get('recovery_path'),
                    'operator_recovery_target': active_recovery_command.get('target'),
                    'operator_recovery_assist_attempted': active_recovery_command.get('attempted'),
                    'operator_recovery_assist_status': active_recovery_command.get('status'),
                    'operator_recovery_assist_kind': active_recovery_command.get('assist_kind'),
                    'operator_recovery_assist_offboard_ack_observed': active_recovery_command.get('offboard_ack_observed'),
                    'operator_recovery_assist_offboard_ack_result': active_recovery_command.get('offboard_ack_result'),
                    'operator_recovery_assist_offboard_state_observed': active_recovery_command.get('offboard_state_observed'),
                    'operator_recovery_assist_offboard_nav_state': active_recovery_command.get('offboard_nav_state'),
                    'operator_recovery_assist_setpoint_frames_sent': active_recovery_command.get('setpoint_frames_sent'),
                    'operator_recovery_assist_stream_duration_seconds': active_recovery_command.get('setpoint_stream_duration_seconds'),
                    'operator_recovery_target_reached': active_recovery_command.get('target_reached'),
                    'operator_recovery_target_distance_m': active_recovery_command.get('target_distance_m'),
                    'operator_recovery_target_altitude_m': active_recovery_command.get('target_altitude_m'),
                    'operator_recovery_altitude_error_m': active_recovery_command.get('altitude_error_m'),
                    'operator_recovery_local_delta_x_m': active_recovery_command.get('local_delta_x_m'),
                    'operator_recovery_local_delta_y_m': active_recovery_command.get('local_delta_y_m'),
                    'operator_recovery_altitude_delta_m': active_recovery_command.get('altitude_delta_m'),
                    'operator_recovery_terminal': active_recovery_command.get('terminal_recovery'),
                    'operator_recovery_resume_auto_attempted': active_recovery_command.get('resume_auto_attempted'),
                    'operator_recovery_resume_auto_ack_observed': active_recovery_command.get('resume_auto_ack_observed'),
                    'operator_recovery_resume_auto_ack_result': active_recovery_command.get('resume_auto_ack_result'),
                    'operator_recovery_resume_auto_nav_state_observed': active_recovery_command.get('resume_auto_nav_state_observed'),
                    'operator_recovery_resume_auto_nav_state': active_recovery_command.get('resume_auto_nav_state'),
                    'operator_recovery_resume_auto_status': active_recovery_command.get('resume_auto_status'),
                    'gazebo_obstacle_model_spawned': obstacle_application.get('gazebo_obstacle_model_spawned'),
                    'gazebo_obstacle_model_spawn_requested': obstacle_application.get('gazebo_obstacle_model_spawn_requested'),
                    'gazebo_obstacle_application_status': obstacle_application.get('application_status'),
                    'obstacle_manifest': obstacle_application.get('obstacle_manifest') or {{}},
                    'gazebo_obstacle_application': obstacle_application,
                    'operator_recovery_assist_attempted': (
                        active_recovery_assist.get('attempted')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_status': (
                        active_recovery_assist.get('status')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_kind': (
                        active_recovery_assist.get('assist_kind')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_offboard_ack_observed': (
                        active_recovery_assist.get('offboard_ack_observed')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_offboard_ack_result': (
                        active_recovery_assist.get('offboard_ack_result')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_offboard_state_observed': (
                        active_recovery_assist.get('offboard_state_observed')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_offboard_nav_state': (
                        active_recovery_assist.get('offboard_nav_state')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_setpoint_frames_sent': (
                        active_recovery_assist.get('setpoint_frames_sent')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_stream_duration_seconds': (
                        active_recovery_assist.get('setpoint_stream_duration_seconds')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_low_altitude_disarm_ack_observed': (
                        active_recovery_assist.get('low_altitude_disarm_ack_observed')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_low_altitude_disarm_ack_result': (
                        active_recovery_assist.get('low_altitude_disarm_ack_result')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_low_altitude_force_disarm_ack_observed': (
                        active_recovery_assist.get('low_altitude_force_disarm_ack_observed')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'operator_recovery_assist_low_altitude_force_disarm_ack_result': (
                        active_recovery_assist.get('low_altitude_force_disarm_ack_result')
                        if isinstance(active_recovery_assist, dict)
                        else None
                    ),
                    'landed': landed,
                    'maybe_landed': parse_bool(land_detected, 'maybe_landed'),
                    'ground_contact': parse_bool(land_detected, 'ground_contact'),
                    'arming_state': arming,
                }}
                print(json.dumps({{'auto_running_snapshot': post_abort_snapshot}}, sort_keys=True), flush=True)
                maybe_landed=parse_bool(land_detected, 'maybe_landed')
                ground_confirmed=landed is True or maybe_landed is True
                disarmed=arming is not None and arming != ARMING_ARMED
                if disarmed and ground_confirmed:
                    return {{'safe': True, 'samples': samples, 'disarm_command': disarm, 'next_seq': seq, 'status': status, 'local': local, 'land_detected': land_detected, 'configured_wait_seconds': float(wait_seconds), 'operator_recovery_overrides': operator_recovery_overrides, 'operator_recovery_assists': operator_recovery_assists}}
                if landed is True:
                    disarm=send_command(sock, remote, {MAV_CMD_COMPONENT_ARM_DISARM}, {disarm_params!r}, seq, 5.0)
                    seq=disarm['next_seq']
                    time.sleep(1.0)
                time.sleep(1.0)
            return {{'safe': False, 'samples': samples, 'disarm_command': disarm, 'next_seq': seq, 'status': samples[-1]['vehicle_status'] if samples else '', 'local': samples[-1]['vehicle_local_position'] if samples else '', 'land_detected': samples[-1]['vehicle_land_detected'] if samples else '', 'configured_wait_seconds': float(wait_seconds), 'operator_recovery_overrides': operator_recovery_overrides, 'operator_recovery_assists': operator_recovery_assists}}

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.2)
            sock.bind(('127.0.0.1', GCS_MAVLINK_PORT))
            remote=('127.0.0.1', PX4_MAVLINK_PORT)
            seq=120
            battery_sim_setup=[
                param_set('SIM_BAT_MIN_PCT', SIM_BATTERY_MIN_REMAINING_PERCENT),
                param_set('SIM_BAT_DRAIN', SIM_BATTERY_DRAIN_SECONDS),
            ]
            if THERMAL_MOTOR_DERATE_FACTOR is not None and THERMAL_MOTOR_DERATE_FACTOR < 0.999:
                battery_sim_setup.append(
                    param_set('MPC_THR_MAX', THERMAL_MOTOR_DERATE_FACTOR)
                )
            before_status=listener('vehicle_status', 1)
            before_local=listener('vehicle_local_position', 1)
            preflight=wait_preflight_ready(sock, remote, seq)
            seq=preflight['next_seq']
            arm=send_command(sock, remote, {MAV_CMD_COMPONENT_ARM_DISARM}, {arm_params!r}, seq, 10.0)
            seq=arm['next_seq']
            time.sleep(0.5)
            after_arm_status=listener('vehicle_status', 1)
            mode=send_command(sock, remote, {MAV_CMD_DO_SET_MODE}, {auto_params!r}, seq, 10.0)
            seq=mode['next_seq']
            nav_wait=wait_nav_state(sock, remote, seq, NAV_AUTO_MISSION, 3.0)
            seq=nav_wait['next_seq']
            monitor=monitor_auto(sock, remote, seq) if nav_wait['observed'] else {{'samples': [], 'guard_reason': 'auto_mission_nav_state_not_observed', 'next_seq': seq, 'monitor_elapsed_seconds': 0.0}}
            seq=monitor['next_seq']
            progress=monitor_progress(monitor)
            payload_release=monitor.get('payload_release') or {{}}
            return_projection=monitor_return_home_projection(monitor)
            return_home_insufficient=(
                return_projection.get('projected_insufficient_for_return_home') is True
            )
            recovery_decision_basis=[]
            operator_recovery=monitor.get('operator_recovery') or {{}}
            operator_recovery_request=(
                operator_recovery.get('request')
                if isinstance(operator_recovery, dict)
                else None
            )
            operator_recovery_command=(
                operator_recovery.get('command')
                if isinstance(operator_recovery, dict)
                else None
            )
            operator_recovery_terminal=(
                bool(operator_recovery.get('terminal_recovery'))
                if isinstance(operator_recovery, dict)
                else False
            )
            if (
                operator_recovery_terminal
                and isinstance(operator_recovery_command, dict)
                and operator_recovery_command.get('attempted')
            ):
                recovery_action=str((operator_recovery_request or {{}}).get('recovery_action') or 'land')
                recovery_path=str(operator_recovery_command.get('recovery_path') or (
                    'MAV_CMD_NAV_RETURN_TO_LAUNCH'
                    if recovery_action == 'return_to_launch'
                    else 'MAV_CMD_NAV_LAND'
                ))
                recovery_wait_seconds=(
                    RTL_POST_ABORT_WAIT_SECONDS
                    if recovery_action == 'return_to_launch'
                    else LAND_POST_ABORT_WAIT_SECONDS
                )
                recovery_decision_basis.append('operator_approved_runtime_recovery_dispatch')
                recovery=operator_recovery_command
            elif return_home_insufficient:
                recovery_action='land'
                recovery_path='MAV_CMD_NAV_LAND'
                recovery_wait_seconds=LAND_POST_ABORT_WAIT_SECONDS
                recovery_decision_basis.append(
                    'payload_release_return_home_battery_projected_insufficient'
                    if payload_release.get('ack_result') == {MAV_RESULT_ACCEPTED}
                    else 'return_home_battery_projected_insufficient'
                )
                recovery=send_command(sock, remote, {MAV_CMD_NAV_LAND}, {land_params!r}, seq, 10.0)
            elif payload_release.get('ack_result') == {MAV_RESULT_ACCEPTED}:
                recovery_action='return_to_launch'
                recovery_path='MAV_CMD_NAV_RETURN_TO_LAUNCH'
                recovery_wait_seconds=RTL_POST_ABORT_WAIT_SECONDS
                recovery_decision_basis.append('payload_release_acked_return_home_allowed')
                recovery=send_command(sock, remote, {MAV_CMD_NAV_RETURN_TO_LAUNCH}, {rtl_params!r}, seq, 10.0)
            elif progress >= RTL_RECOVERY_MIN_PROGRESS_M:
                recovery_action='return_to_launch'
                recovery_path='MAV_CMD_NAV_RETURN_TO_LAUNCH'
                recovery_wait_seconds=RTL_POST_ABORT_WAIT_SECONDS
                recovery_decision_basis.append('route_progress_return_home_allowed')
                recovery=send_command(sock, remote, {MAV_CMD_NAV_RETURN_TO_LAUNCH}, {rtl_params!r}, seq, 10.0)
            else:
                recovery_action='land'
                recovery_path='MAV_CMD_NAV_LAND'
                recovery_wait_seconds=LAND_POST_ABORT_WAIT_SECONDS
                recovery_decision_basis.append('route_progress_below_rtl_threshold')
                recovery=send_command(sock, remote, {MAV_CMD_NAV_LAND}, {land_params!r}, seq, 10.0)
            seq=recovery['next_seq']
            post_abort=wait_land_or_disarm(sock, remote, seq, recovery_wait_seconds)
            seq=post_abort['next_seq']

        print(json.dumps({{
            'battery_sim_setup': battery_sim_setup,
            'before_status': before_status,
            'before_local_position': before_local,
            'preflight_wait': preflight,
            'arm_command': arm,
            'after_arm_status': after_arm_status,
            'auto_mission_mode_command': mode,
            'auto_mission_nav_wait': nav_wait,
            'monitor': monitor,
            'obstacle_manifest': obstacle_application.get('obstacle_manifest') or {{}},
            'gazebo_obstacle_application': obstacle_application,
            'recovery_action': recovery_action,
            'recovery_path': recovery_path,
            'recovery_decision_basis': recovery_decision_basis,
            'post_release_return_projection': return_projection,
            'recovery_command': recovery,
            'land_abort_command': recovery,
            'post_abort': post_abort,
            'final_status': listener('vehicle_status', 1),
            'final_local_position': listener('vehicle_local_position', 1),
        }}, sort_keys=True))
        """
    )


RUNNING_SNAPSHOT_SCHEMA_VERSION = "missionos_auto_mission_running_snapshot.v1"
RUNNING_SNAPSHOT_MARKER_KEY = "auto_running_snapshot"


def _build_running_snapshot(
    marker: Mapping[str, Any],
    *,
    waypoint_total: int,
) -> dict[str, Any]:
    """Shape one in-flight monitor marker into the host-written running snapshot.

    Pure transform so it can be unit-tested without a live container. The marker
    is emitted once per monitor sample by the in-container probe loop.
    """

    monitor_window_ended = bool(marker.get("monitor_window_ended"))
    return {
        "schema_version": RUNNING_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_status": "monitor_window_ended" if monitor_window_ended else "running",
        "monitor_window_ended": monitor_window_ended,
        "monitor_stop_reason": marker.get("monitor_stop_reason"),
        "sample_index": int(marker.get("sample_index") or 0),
        "waypoint_total": int(waypoint_total),
        "elapsed_seconds": marker.get("elapsed_seconds"),
        "progress_m": marker.get("progress_m"),
        "mission_current_seq": marker.get("mission_current_seq"),
        "mission_reached_seq": marker.get("mission_reached_seq"),
        "altitude_above_home_m": marker.get("altitude_above_home_m"),
        "local_x_m": marker.get("local_x_m"),
        "local_y_m": marker.get("local_y_m"),
        "local_z_m": marker.get("local_z_m"),
        "distance_to_home_m": marker.get("distance_to_home_m"),
        "nav_state": marker.get("nav_state"),
        "battery_remaining_percent": marker.get("battery_remaining_percent"),
        "battery_remaining_first_percent": marker.get("battery_remaining_first_percent"),
        "battery_remaining_latest_percent": marker.get("battery_remaining_latest_percent"),
        "battery_remaining_delta_percent": marker.get("battery_remaining_delta_percent"),
        "battery_remaining_sample_count": marker.get("battery_remaining_sample_count"),
        "battery_remaining_dynamic": marker.get("battery_remaining_dynamic"),
        "battery_state_source": marker.get("battery_state_source"),
        "gz_battery_state_observed": marker.get("gz_battery_state_observed"),
        "gz_battery_percent": marker.get("gz_battery_percent"),
        "gz_battery_voltage_v": marker.get("gz_battery_voltage_v"),
        "gz_battery_current_a": marker.get("gz_battery_current_a"),
        "gz_battery_charge_ah": marker.get("gz_battery_charge_ah"),
        "gz_battery_state_source": marker.get("gz_battery_state_source"),
        "gz_battery_read_error": marker.get("gz_battery_read_error"),
        "gz_battery_motor_coupling_requested": marker.get(
            "gz_battery_motor_coupling_requested"
        ),
        "battery_sample_accepted": marker.get("battery_sample_accepted"),
        "battery_sample_rejected_reason": marker.get(
            "battery_sample_rejected_reason"
        ),
        "battery_warning": marker.get("battery_warning"),
        "heartbeat_observed": marker.get("heartbeat_observed"),
        "dropoff_dwell_candidate": marker.get("dropoff_dwell_candidate"),
        "wind_gust_active": marker.get("gust_active"),
        "wind_gust_started": marker.get("gust_started"),
        "wind_gust_ended": marker.get("gust_ended"),
        "wind_mean_started": marker.get("wind_mean_started"),
        "wind_mean_pending_reason": marker.get("wind_mean_pending_reason"),
        "wind_takeoff_clearance_min_altitude_m": marker.get(
            "wind_takeoff_clearance_min_altitude_m"
        ),
        "wind_mean_application_elapsed_seconds": marker.get(
            "wind_mean_application_elapsed_seconds"
        ),
        "wind_mean_application_altitude_m": marker.get(
            "wind_mean_application_altitude_m"
        ),
        "wind_gust_window_start_seconds": marker.get("gust_window_start_seconds"),
        "wind_gust_window_duration_seconds": marker.get(
            "gust_window_duration_seconds"
        ),
        "gazebo_obstacle_model_spawned": marker.get(
            "gazebo_obstacle_model_spawned"
        ),
        "gazebo_obstacle_model_spawn_requested": marker.get(
            "gazebo_obstacle_model_spawn_requested"
        ),
        "gazebo_obstacle_application_status": marker.get(
            "gazebo_obstacle_application_status"
        ),
        "obstacle_manifest": marker.get("obstacle_manifest"),
        "gazebo_obstacle_application": marker.get("gazebo_obstacle_application"),
        "operator_recovery_request_observed": marker.get(
            "operator_recovery_request_observed"
        ),
        "operator_recovery_action": marker.get("operator_recovery_action"),
        "operator_recovery_parameters": marker.get("operator_recovery_parameters"),
        "operator_recovery_command_ack_observed": marker.get(
            "operator_recovery_command_ack_observed"
        ),
        "operator_recovery_command_ack_result": marker.get(
            "operator_recovery_command_ack_result"
        ),
        "operator_recovery_path": marker.get("operator_recovery_path"),
        "operator_recovery_target": marker.get("operator_recovery_target"),
        "operator_recovery_assist_attempted": marker.get(
            "operator_recovery_assist_attempted"
        ),
        "operator_recovery_assist_status": marker.get(
            "operator_recovery_assist_status"
        ),
        "operator_recovery_assist_kind": marker.get("operator_recovery_assist_kind"),
        "operator_recovery_assist_offboard_ack_observed": marker.get(
            "operator_recovery_assist_offboard_ack_observed"
        ),
        "operator_recovery_assist_offboard_ack_result": marker.get(
            "operator_recovery_assist_offboard_ack_result"
        ),
        "operator_recovery_assist_offboard_state_observed": marker.get(
            "operator_recovery_assist_offboard_state_observed"
        ),
        "operator_recovery_assist_offboard_nav_state": marker.get(
            "operator_recovery_assist_offboard_nav_state"
        ),
        "operator_recovery_assist_setpoint_frames_sent": marker.get(
            "operator_recovery_assist_setpoint_frames_sent"
        ),
        "operator_recovery_assist_stream_duration_seconds": marker.get(
            "operator_recovery_assist_stream_duration_seconds"
        ),
        "operator_recovery_target_reached": marker.get(
            "operator_recovery_target_reached"
        ),
        "operator_recovery_target_distance_m": marker.get(
            "operator_recovery_target_distance_m"
        ),
        "operator_recovery_target_altitude_m": marker.get(
            "operator_recovery_target_altitude_m"
        ),
        "operator_recovery_altitude_error_m": marker.get(
            "operator_recovery_altitude_error_m"
        ),
        "operator_recovery_local_delta_x_m": marker.get(
            "operator_recovery_local_delta_x_m"
        ),
        "operator_recovery_local_delta_y_m": marker.get(
            "operator_recovery_local_delta_y_m"
        ),
        "operator_recovery_altitude_delta_m": marker.get(
            "operator_recovery_altitude_delta_m"
        ),
        "operator_recovery_terminal": marker.get("operator_recovery_terminal"),
        "operator_recovery_resume_auto_attempted": marker.get(
            "operator_recovery_resume_auto_attempted"
        ),
        "operator_recovery_resume_auto_ack_observed": marker.get(
            "operator_recovery_resume_auto_ack_observed"
        ),
        "operator_recovery_resume_auto_ack_result": marker.get(
            "operator_recovery_resume_auto_ack_result"
        ),
        "operator_recovery_resume_auto_nav_state_observed": marker.get(
            "operator_recovery_resume_auto_nav_state_observed"
        ),
        "operator_recovery_resume_auto_nav_state": marker.get(
            "operator_recovery_resume_auto_nav_state"
        ),
        "operator_recovery_resume_auto_status": marker.get(
            "operator_recovery_resume_auto_status"
        ),
        "operator_recovery_assist_low_altitude_disarm_ack_observed": marker.get(
            "operator_recovery_assist_low_altitude_disarm_ack_observed"
        ),
        "operator_recovery_assist_low_altitude_disarm_ack_result": marker.get(
            "operator_recovery_assist_low_altitude_disarm_ack_result"
        ),
        "operator_recovery_assist_low_altitude_force_disarm_ack_observed": marker.get(
            "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
        ),
        "operator_recovery_assist_low_altitude_force_disarm_ack_result": marker.get(
            "operator_recovery_assist_low_altitude_force_disarm_ack_result"
        ),
        "post_abort_tracking": marker.get("post_abort_tracking"),
        "post_abort_elapsed_seconds": marker.get("post_abort_elapsed_seconds"),
        "post_abort_observation_seconds": marker.get("post_abort_observation_seconds"),
        "post_abort_home_distance_delta_m": marker.get("post_abort_home_distance_delta_m"),
        "post_abort_altitude_delta_m": marker.get("post_abort_altitude_delta_m"),
        "post_abort_outcome_status": marker.get("post_abort_outcome_status"),
        "landed": marker.get("landed"),
        "maybe_landed": marker.get("maybe_landed"),
        "ground_contact": marker.get("ground_contact"),
        "arming_state": marker.get("arming_state"),
    }


def _write_running_snapshot(run_dir: Path, snapshot: Mapping[str, Any]) -> None:
    """Atomically overwrite ``running_snapshot.json`` for in-flight polling."""

    path = run_dir / "running_snapshot.json"
    tmp = run_dir / "running_snapshot.json.tmp"
    tmp.write_text(json.dumps(dict(snapshot), sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _running_snapshot_failure_digest(run_dir: Path) -> str:
    path = run_dir / "running_snapshot.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "last_snapshot=unavailable"
    if not isinstance(payload, Mapping):
        return "last_snapshot=invalid"
    fields = [
        "snapshot_status",
        "monitor_window_ended",
        "monitor_stop_reason",
        "sample_index",
        "elapsed_seconds",
        "progress_m",
        "mission_current_seq",
        "mission_reached_seq",
        "waypoint_total",
        "nav_state",
        "battery_remaining_percent",
    ]
    parts = [f"{field}={payload.get(field)!r}" for field in fields]
    return "last_snapshot={" + ", ".join(parts) + "}"


def _route_inner_probe_stdout_line(
    line: str,
    *,
    run_dir: Path,
    waypoint_total: int,
) -> dict[str, Any] | None:
    """Route one probe stdout line.

    In-flight snapshot markers are written to ``running_snapshot.json`` and
    return None; the final result JSON line is returned so the caller can keep
    the last one. Garbage/non-JSON lines are ignored.
    """

    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if RUNNING_SNAPSHOT_MARKER_KEY in obj:
        marker = obj.get(RUNNING_SNAPSHOT_MARKER_KEY)
        if isinstance(marker, Mapping):
            try:
                _write_running_snapshot(
                    run_dir,
                    _build_running_snapshot(marker, waypoint_total=waypoint_total),
                )
            except OSError:
                pass
        return None
    return obj


def _stream_actual_runtime_probe(
    *,
    script: str,
    timeout_seconds: int,
    run_dir: Path,
    waypoint_total: int,
) -> dict[str, Any]:
    """Run the in-container probe, routing in-flight snapshot markers to disk.

    Unlike a blocking capture, this reads stdout incrementally so the host can
    overwrite ``running_snapshot.json`` once per monitor sample while the long
    AUTO route is still flying. The final result is the last non-marker JSON
    line. This docker-streaming wiring is the live-only seam.
    """

    process = subprocess.Popen(
        ["docker", "exec", "-i", upload_smoke.CONTAINER_NAME, "python3", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(script)
    process.stdin.close()
    final_result: dict[str, Any] | None = None
    line_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stderr_lines: list[str] = []

    def _read_stream(name: str, stream: Any) -> None:
        try:
            for stream_line in stream:
                line_queue.put((name, stream_line))
        finally:
            line_queue.put((name, None))

    streams: set[str] = {"stdout"}
    stdout_thread = threading.Thread(
        target=_read_stream, args=("stdout", process.stdout), daemon=True
    )
    stdout_thread.start()
    if process.stderr is not None:
        streams.add("stderr")
        stderr_thread = threading.Thread(
            target=_read_stream, args=("stderr", process.stderr), daemon=True
        )
        stderr_thread.start()

    deadline = time.monotonic() + float(timeout_seconds)
    while streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            stderr_text = "".join(stderr_lines)
            raise RuntimeError(
                "AUTO runtime probe timed out: "
                f"{_running_snapshot_failure_digest(run_dir)}; "
                f"stderr_tail={stderr_text[-1500:]!r}"
            )
        try:
            stream_name, line = line_queue.get(timeout=min(0.2, remaining))
        except queue.Empty:
            continue
        if line is None:
            streams.discard(stream_name)
            continue
        if stream_name == "stderr":
            stderr_lines.append(line)
            continue
        routed = _route_inner_probe_stdout_line(
            line, run_dir=run_dir, waypoint_total=waypoint_total
        )
        if routed is not None:
            final_result = routed
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    stderr_text = "".join(stderr_lines)
    if process.returncode != 0:
        raise RuntimeError(
            "AUTO runtime probe failed: "
            f"returncode={process.returncode}; "
            f"{_running_snapshot_failure_digest(run_dir)}; "
            f"stderr_tail={stderr_text[-1500:]!r}"
        )
    if final_result is None:
        raise RuntimeError(
            "AUTO runtime probe produced no result line: " + stderr_text[-500:]
        )
    return final_result


def _actual_runtime_probe(
    *,
    dropoff_dwell_mission_seq: int,
    land_mission_seq: int,
    release_altitude_target_m: float,
    release_altitude_tolerance_m: float,
    required_dwell_seconds: float,
    monitor_seconds: float,
    min_progress_m: float,
    no_progress_grace_seconds: float,
    min_route_altitude_m: float,
    altitude_grace_seconds: float,
    min_battery_remaining_percent: float,
    post_abort_wait_seconds: float,
    land_post_abort_wait_seconds: float,
    rtl_post_abort_wait_seconds: float,
    rtl_recovery_min_progress_m: float,
    sim_battery_min_remaining_percent: float,
    sim_battery_drain_seconds: float,
    thermal_motor_derate_factor: float | None,
    wind_mean_mps: float | None,
    wind_direction_deg: float | None,
    wind_gust_mps: float | None,
    wind_variance: float | None,
    gz_physical_battery_enabled: bool,
    gz_battery_motor_coupling_enabled: bool,
    obstacle_manifest: Mapping[str, Any] | None,
    run_dir: Path,
    waypoint_total: int,
) -> dict[str, Any]:
    return _stream_actual_runtime_probe(
        script=_inner_runtime_probe_script(
            dropoff_dwell_mission_seq=dropoff_dwell_mission_seq,
            land_mission_seq=land_mission_seq,
            release_altitude_target_m=release_altitude_target_m,
            release_altitude_tolerance_m=release_altitude_tolerance_m,
            required_dwell_seconds=required_dwell_seconds,
            monitor_seconds=monitor_seconds,
            min_progress_m=min_progress_m,
            no_progress_grace_seconds=no_progress_grace_seconds,
            min_route_altitude_m=min_route_altitude_m,
            altitude_grace_seconds=altitude_grace_seconds,
            min_battery_remaining_percent=min_battery_remaining_percent,
            post_abort_wait_seconds=post_abort_wait_seconds,
            land_post_abort_wait_seconds=land_post_abort_wait_seconds,
            rtl_post_abort_wait_seconds=rtl_post_abort_wait_seconds,
            rtl_recovery_min_progress_m=rtl_recovery_min_progress_m,
            sim_battery_min_remaining_percent=sim_battery_min_remaining_percent,
            sim_battery_drain_seconds=sim_battery_drain_seconds,
            thermal_motor_derate_factor=thermal_motor_derate_factor,
            wind_mean_mps=wind_mean_mps,
            wind_direction_deg=wind_direction_deg,
            wind_gust_mps=wind_gust_mps,
            wind_variance=wind_variance,
            gz_physical_battery_enabled=gz_physical_battery_enabled,
            gz_battery_motor_coupling_enabled=gz_battery_motor_coupling_enabled,
            obstacle_manifest=obstacle_manifest,
        ),
        timeout_seconds=int(
            max(
                180.0,
                monitor_seconds
                + max(
                    post_abort_wait_seconds,
                    land_post_abort_wait_seconds,
                    rtl_post_abort_wait_seconds,
                )
                + 90.0,
            )
        ),
        run_dir=run_dir,
        waypoint_total=waypoint_total,
    )


def _gazebo_obstacle_runtime_artifacts(
    *,
    route: Mapping[str, Any],
    probe_observed: Mapping[str, Any],
) -> dict[str, Any]:
    application = probe_observed.get("gazebo_obstacle_application")
    application = application if isinstance(application, Mapping) else {}
    manifest = application.get("obstacle_manifest")
    manifest = (
        dict(manifest)
        if isinstance(manifest, Mapping)
        else _gazebo_obstacle_manifest_from_route(route)
    )
    requested = bool(manifest.get("gazebo_obstacle_model_spawn_requested"))
    spawned = bool(application.get("gazebo_obstacle_model_spawned"))
    models = list(application.get("models") or []) if isinstance(application, Mapping) else []
    requested_count = int(application.get("requested_model_count") or len(models) or 0)
    spawned_count = int(application.get("spawned_model_count") or 0)
    application_status = str(
        application.get("application_status")
        or ("not_requested" if not requested else "unsupported")
    )
    unsupported_reasons = [
        str(model.get("blocked_reason"))
        for model in models
        if isinstance(model, Mapping) and str(model.get("blocked_reason") or "").strip()
    ]
    capability = {
        "schema_version": "gazebo_world_capability_matrix.v1",
        "capability_id": "gazebo_world_capability_matrix:missionos_static_obstacles",
        "condition_kind": "static_obstacle_or_building",
        "source_backed_obstacle_required": True,
        "gazebo_entity_factory_create_supported": "attempted" if requested else "not_requested",
        "static_collision_box_supported": "attempted" if requested else "not_requested",
        "pose_readback_required_for_spawn_claim": True,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    world_profile = {
        "schema_version": "gazebo_world_condition_profile.v1",
        "condition_id": "gazebo_world_condition_profile:missionos_static_obstacles",
        "condition_kind": "static_obstacle_or_building",
        "requested_present": requested,
        "source": manifest.get("source") or "not_configured",
        "landing_zone_blocked": bool(manifest.get("landing_zone_blocked")),
        "building_risk_detected": bool(manifest.get("building_risk_detected")),
        "obstacle_count": len(manifest.get("obstacles") or []),
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    application_payload = {
        **dict(application),
        "schema_version": GAZEBO_OBSTACLE_APPLICATION_SCHEMA_VERSION,
        "application_id": "gazebo_world_application:missionos_static_obstacles",
        "application_status": application_status,
        "requested_model_count": requested_count,
        "spawned_model_count": spawned_count,
        "gazebo_obstacle_model_spawn_requested": requested,
        "gazebo_obstacle_model_spawned": spawned,
        "unsupported_reasons": unsupported_reasons,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    manifest_payload = {
        **manifest,
        "schema_version": GAZEBO_OBSTACLE_MANIFEST_SCHEMA_VERSION,
        "gazebo_obstacle_model_spawn_requested": requested,
        "gazebo_obstacle_model_spawned": spawned,
        "spawned_model_count": spawned_count,
    }
    evidence = {
        "schema_version": "observed_world_condition_evidence.v1",
        "evidence_id": "observed_world_condition_evidence:missionos_static_obstacles",
        "observation_status": (
            "gazebo_obstacle_pose_readback_observed"
            if spawned
            else "not_requested"
            if not requested
            else "gazebo_obstacle_pose_readback_not_observed"
        ),
        "application_ref": application_payload["application_id"],
        "pose_readbacks": [
            {
                "name": str(model.get("name") or ""),
                "observed": bool(model.get("pose_readback_observed")),
                "pose": dict(model.get("pose_readback") or {}),
            }
            for model in models
            if isinstance(model, Mapping)
        ],
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    return {
        "gazebo_world_condition_profile": world_profile,
        "gazebo_world_capability_matrix": capability,
        "gazebo_world_application": application_payload,
        "obstacle_manifest": manifest_payload,
        "observed_world_condition_evidence": evidence,
    }


def _build_summary_payload(
    *,
    run_dir: Path,
    route: dict[str, Any],
    upload_observed: dict[str, Any],
    probe_observed: dict[str, Any],
    payload_release_event: dict[str, Any] | None,
    monitor_seconds: float,
    min_progress_m: float,
    no_progress_grace_seconds: float,
    min_route_altitude_m: float,
    altitude_grace_seconds: float,
    min_battery_remaining_percent: float,
    post_abort_wait_seconds: float,
    land_post_abort_wait_seconds: float,
    rtl_post_abort_wait_seconds: float,
    rtl_recovery_min_progress_m: float,
    thermal_weather_config: Mapping[str, Any] | None = None,
    rain_weather_config: Mapping[str, Any] | None = None,
    wind_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    compilation = compile_operator_coordinate_route_auto_mission(route)
    raw_samples = list((probe_observed.get("monitor") or {}).get("samples") or [])
    samples = tuple(
        _sample_from_observed(raw, index) for index, raw in enumerate(raw_samples)
    )
    pose_path = run_dir / "auto_mission_pose_samples.jsonl"
    global_path = run_dir / "auto_mission_global_position_samples.jsonl"
    raw_path = run_dir / "auto_mission_raw_samples.jsonl"
    pose_path.write_text(
        "\n".join(json.dumps(sample.model_dump(mode="json"), sort_keys=True) for sample in samples)
        + ("\n" if samples else ""),
        encoding="utf-8",
    )
    global_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "elapsed_seconds": sample.elapsed_seconds,
                    "global_altitude_m": sample.global_altitude_m,
                    "global_latitude_deg": sample.global_latitude_deg,
                    "global_longitude_deg": sample.global_longitude_deg,
                    "sample_index": sample.sample_index,
                    "schema_version": sample.schema_version,
                },
                sort_keys=True,
            )
            for sample in samples
            if sample.global_latitude_deg is not None
            or sample.global_longitude_deg is not None
            or sample.global_altitude_m is not None
        )
        + (
            "\n"
            if any(
                sample.global_latitude_deg is not None
                or sample.global_longitude_deg is not None
                or sample.global_altitude_m is not None
                for sample in samples
            )
            else ""
        ),
        encoding="utf-8",
    )
    raw_path.write_text(
        "\n".join(json.dumps(sample, sort_keys=True) for sample in raw_samples)
        + ("\n" if raw_samples else ""),
        encoding="utf-8",
    )
    arm = dict(probe_observed.get("arm_command") or {})
    mode = dict(probe_observed.get("auto_mission_mode_command") or {})
    recovery = dict(
        probe_observed.get("recovery_command")
        or probe_observed.get("land_abort_command")
        or {}
    )
    payload_release = dict(
        (probe_observed.get("monitor") or {}).get("payload_release") or {}
    )
    post_abort = dict(probe_observed.get("post_abort") or {})
    effective_post_abort_wait_seconds = float(
        post_abort.get("configured_wait_seconds") or post_abort_wait_seconds
    )
    recovery_agent_window = _post_abort_recovery_agent_evidence_window(
        probe_observed=probe_observed,
        post_abort_wait_seconds=effective_post_abort_wait_seconds,
    )
    recovery_agent_window_path = run_dir / "recovery_agent_evidence_window.json"
    recovery_agent_window_path.write_text(
        json.dumps(recovery_agent_window, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    statustext = tuple(
        str(raw.get("statustext"))
        for raw in raw_samples
        if str(raw.get("statustext") or "").strip()
    )
    summary = build_auto_mission_runtime_monitor_summary(
        compilation=compilation,
        mission_upload_accepted=upload_observed.get("mission_ack_type")
        == MAV_MISSION_ACCEPTED,
        mission_ack_observed=bool(upload_observed.get("mission_ack_observed")),
        mission_ack_result=upload_observed.get("mission_ack_type"),
        arm_command_ack_observed=bool(arm.get("ack_observed")),
        arm_command_ack_result=arm.get("ack_result"),
        auto_mission_mode_ack_observed=bool(mode.get("ack_observed")),
        auto_mission_mode_ack_result=mode.get("ack_result"),
        samples=samples,
        monitor_target_seconds=monitor_seconds,
        monitor_elapsed_seconds=float(
            (probe_observed.get("monitor") or {}).get("monitor_elapsed_seconds") or 0.0
        ),
        heartbeat_samples=len(samples),
        operator_coordinate_execution=True,
        statustext_during_auto=statustext,
        local_ned_pose_samples_path=_rel_to_root(pose_path),
        global_position_samples_path=_rel_to_root(global_path),
        mavlink_event_log_path=_rel_to_root(raw_path),
        abort_ack_observed=bool(recovery.get("ack_observed")),
        abort_ack_result=recovery.get("ack_result"),
        abort_policy_selected_action=str(probe_observed.get("recovery_action") or "land"),
        recovery_path_taken=str(probe_observed.get("recovery_path") or "MAV_CMD_NAV_LAND"),
        final_landing_safe=bool(recovery_agent_window.get("final_landing_safe")),
        recovery_agent_evidence_window=recovery_agent_window,
        recovery_agent_evidence_window_path=str(
            _rel_to_root(recovery_agent_window_path)
        ),
        payload_release_command_frame_sent=bool(payload_release.get("attempted")),
        payload_release_command_ack_observed=bool(payload_release.get("ack_observed")),
        payload_release_command_ack_result=payload_release.get("ack_result"),
        probe_stop_reason_override=(probe_observed.get("monitor") or {}).get(
            "monitor_stop_reason"
        ),
        abort_retry_count=0,
        min_progress_m=min_progress_m,
        no_progress_grace_seconds=no_progress_grace_seconds,
        min_route_altitude_m=min_route_altitude_m,
        altitude_grace_seconds=altitude_grace_seconds,
        min_battery_remaining_percent=min_battery_remaining_percent,
    )
    waypoint_gate = build_auto_mission_waypoint_gate_summary_from_runtime(summary)
    dropoff_gate = build_auto_mission_dropoff_gate_summary(
        dropoff_latitude_deg=float(route["dropoff_latitude"]),
        dropoff_longitude_deg=float(route["dropoff_longitude"]),
        release_altitude_target_m=float(compilation.cruise_altitude_m),
        samples=samples,
        route_completed_claimed=waypoint_gate.route_completed_claimed,
    )
    sitl_delivery_gate = build_auto_mission_sitl_delivery_gate_summary(
        route_completed_claimed=waypoint_gate.route_completed_claimed,
        dropoff_verified=dropoff_gate.dropoff_verified,
        payload_release_command_acked=summary.payload_release_command_acked,
    )
    payload_release_sim_gate = build_auto_mission_payload_release_sim_gate_summary(
        route_completed_claimed=waypoint_gate.route_completed_claimed,
        dropoff_verified=dropoff_gate.dropoff_verified,
        payload_release_command_acked=summary.payload_release_command_acked,
        payload_release_event=payload_release_event,
    )
    summary_payload = summary.model_dump(mode="json")
    thermal_artifacts = _thermal_weather_runtime_artifacts(
        config=thermal_weather_config or _thermal_weather_runtime_config(
            baseline_sim_bat_drain_seconds=DEFAULT_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS
        ),
        probe_observed=probe_observed,
        summary=summary_payload,
    )
    rain_artifacts = _rain_weather_runtime_artifacts(
        config=rain_weather_config
        or _rain_weather_runtime_config(
            baseline_sim_bat_drain_seconds=DEFAULT_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS
        ),
        probe_observed=probe_observed,
        summary=summary_payload,
        run_dir=run_dir,
    )
    wind_artifacts = _auto_wind_gust_runtime_artifacts(
        profile=wind_profile or _wind_requested_profile(),
        probe_observed=probe_observed,
        run_dir=run_dir,
    )
    obstacle_artifacts = _gazebo_obstacle_runtime_artifacts(
        route=route,
        probe_observed=probe_observed,
    )
    return {
        "summary": summary_payload,
        "waypoint_gate": waypoint_gate.model_dump(mode="json"),
        "dropoff_gate": dropoff_gate.model_dump(mode="json"),
        "sitl_delivery_gate": sitl_delivery_gate.model_dump(mode="json"),
        "payload_release_sim_gate": payload_release_sim_gate.model_dump(mode="json"),
        **wind_artifacts,
        **thermal_artifacts,
        **rain_artifacts,
        **obstacle_artifacts,
        "upload_observed": upload_observed,
        "probe_observed": probe_observed,
        "payload_release_observed": payload_release,
        "payload_release_event": payload_release_event or {},
        "compilation": compilation.model_dump(mode="json"),
        "artifact_dir": str(run_dir),
        "px4_home_alignment": {
            "PX4_HOME_LAT": os.getenv("PX4_HOME_LAT"),
            "PX4_HOME_LON": os.getenv("PX4_HOME_LON"),
            "PX4_HOME_ALT": os.getenv("PX4_HOME_ALT"),
            "operator_route_requires_sitl_home_alignment": True,
        },
        "guard_config": {
            "monitor_seconds": monitor_seconds,
            "min_progress_m": min_progress_m,
            "no_progress_grace_seconds": no_progress_grace_seconds,
            "min_route_altitude_m": min_route_altitude_m,
            "altitude_grace_seconds": altitude_grace_seconds,
            "min_battery_remaining_percent": min_battery_remaining_percent,
            "post_abort_wait_seconds": post_abort_wait_seconds,
            "land_post_abort_wait_seconds": land_post_abort_wait_seconds,
            "rtl_post_abort_wait_seconds": rtl_post_abort_wait_seconds,
            "effective_post_abort_wait_seconds": effective_post_abort_wait_seconds,
            "rtl_recovery_min_progress_m": rtl_recovery_min_progress_m,
            "abort_reason": AUTO_RUNTIME_ABORT_REASON,
            "probe_stop_reason_when_no_guard": (
                AUTO_RUNTIME_PROBE_STOP_REASON_MONITOR_WINDOW_COMPLETE
            ),
        },
    }


def main() -> int:
    _require_opt_in()
    monitor_seconds = float(
        os.getenv("MISSIONOS_AUTO_RUNTIME_MONITOR_SECONDS", DEFAULT_AUTO_RUNTIME_MONITOR_SECONDS)
    )
    min_progress_m = float(
        os.getenv("MISSIONOS_AUTO_RUNTIME_MIN_PROGRESS_M", DEFAULT_AUTO_RUNTIME_MIN_PROGRESS_M)
    )
    no_progress_grace_seconds = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_NO_PROGRESS_GRACE_SECONDS",
            DEFAULT_AUTO_RUNTIME_NO_PROGRESS_GRACE_SECONDS,
        )
    )
    min_route_altitude_m = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_MIN_ROUTE_ALTITUDE_M",
            DEFAULT_AUTO_RUNTIME_MIN_ROUTE_ALTITUDE_M,
        )
    )
    altitude_grace_seconds = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_ALTITUDE_GRACE_SECONDS",
            DEFAULT_AUTO_RUNTIME_ALTITUDE_GRACE_SECONDS,
        )
    )
    min_battery_remaining_percent = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_MIN_BATTERY_REMAINING_PERCENT",
            DEFAULT_AUTO_RUNTIME_BATTERY_MIN_REMAINING_PERCENT,
        )
    )
    # Without an explicit drain config, PX4 SITL's simulated battery floors at
    # SIM_BAT_MIN_PCT (default ~50%), so the live panel pins at ~50% for the
    # whole route. We set a low floor and gradual full->empty drain so SITL
    # telemetry shows an observable trend. This is not real power-module
    # endurance evidence.
    sim_battery_min_remaining_percent = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_SIM_BATTERY_MIN_PCT",
            DEFAULT_AUTO_RUNTIME_SIM_BATTERY_MIN_PCT,
        )
    )
    sim_battery_drain_seconds = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS",
            DEFAULT_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS,
        )
    )
    thermal_weather_config = _thermal_weather_runtime_config(
        baseline_sim_bat_drain_seconds=sim_battery_drain_seconds
    )
    sim_battery_drain_seconds = float(
        thermal_weather_config["effective_sim_bat_drain_seconds"]
    )
    rain_weather_config = _rain_weather_runtime_config(
        baseline_sim_bat_drain_seconds=sim_battery_drain_seconds
    )
    sim_battery_drain_seconds = float(
        rain_weather_config["effective_sim_bat_drain_seconds"]
    )
    post_abort_wait_seconds = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_POST_ABORT_WAIT_SECONDS",
            DEFAULT_POST_ABORT_WAIT_SECONDS,
        )
    )
    land_post_abort_wait_seconds = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_LAND_POST_ABORT_WAIT_SECONDS",
            DEFAULT_LAND_POST_ABORT_WAIT_SECONDS,
        )
    )
    rtl_recovery_min_progress_m = float(
        os.getenv(
            "MISSIONOS_AUTO_RUNTIME_RTL_RECOVERY_MIN_PROGRESS_M",
            DEFAULT_RTL_RECOVERY_MIN_PROGRESS_M,
        )
    )
    route = _operator_route()
    _configure_operator_route_home(route)
    run_dir = _run_dir()
    obstacle_manifest = _gazebo_obstacle_manifest_from_route(route)
    wind_profile = _wind_requested_profile()
    payload_model_root = _start_container(
        run_dir,
        wind_profile=wind_profile,
        rain_config=rain_weather_config,
    )
    try:
        compilation = compile_operator_coordinate_route_auto_mission(route)
        rtl_post_abort_wait_seconds = _rtl_recovery_wait_seconds(
            base_wait_seconds=post_abort_wait_seconds,
            return_distance_m=float(compilation.planned_route_m),
            cruise_speed_mps=float(compilation.cruise_speed_mps),
        )
        upload_observed = upload_smoke._actual_upload(items=compilation.mission_items)
        probe_observed = _actual_runtime_probe(
            dropoff_dwell_mission_seq=int(compilation.dropoff_dwell_mission_seq or 0),
            land_mission_seq=int(
                compilation.land_mission_seq or len(compilation.mission_items) - 1
            ),
            release_altitude_target_m=float(compilation.cruise_altitude_m),
            release_altitude_tolerance_m=(
                DEFAULT_DROPOFF_RELEASE_ALTITUDE_TOLERANCE_M
            ),
            required_dwell_seconds=float(
                compilation.dropoff_release_min_dwell_seconds
            ),
            monitor_seconds=monitor_seconds,
            min_progress_m=min_progress_m,
            no_progress_grace_seconds=no_progress_grace_seconds,
            min_route_altitude_m=min_route_altitude_m,
            altitude_grace_seconds=altitude_grace_seconds,
            min_battery_remaining_percent=min_battery_remaining_percent,
            post_abort_wait_seconds=post_abort_wait_seconds,
            land_post_abort_wait_seconds=land_post_abort_wait_seconds,
            rtl_post_abort_wait_seconds=rtl_post_abort_wait_seconds,
            rtl_recovery_min_progress_m=rtl_recovery_min_progress_m,
            sim_battery_min_remaining_percent=sim_battery_min_remaining_percent,
            sim_battery_drain_seconds=sim_battery_drain_seconds,
            thermal_motor_derate_factor=thermal_weather_config.get(
                "thermal_motor_derate_factor"
            ),
            wind_mean_mps=(wind_profile.get("requested") or {}).get("wind_mean_mps"),
            wind_direction_deg=(wind_profile.get("requested") or {}).get(
                "wind_direction_deg"
            ),
            wind_gust_mps=(wind_profile.get("requested") or {}).get("wind_gust_mps"),
            wind_variance=(wind_profile.get("requested") or {}).get("wind_variance"),
            gz_physical_battery_enabled=_gz_physical_battery_enabled(),
            gz_battery_motor_coupling_enabled=_gz_battery_motor_coupling_enabled(),
            obstacle_manifest=obstacle_manifest,
            run_dir=run_dir,
            waypoint_total=len(compilation.mission_items),
        )
        (run_dir / "compilation.json").write_text(
            json.dumps(compilation.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "upload_observed.json").write_text(
            json.dumps(upload_observed, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "probe_observed.json").write_text(
            json.dumps(probe_observed, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        monitor_payload_release = dict(
            (probe_observed.get("monitor") or {}).get("payload_release") or {}
        )
        payload_release_command_acked = (
            bool(monitor_payload_release.get("attempted"))
            and bool(monitor_payload_release.get("ack_observed"))
            and monitor_payload_release.get("ack_result") == MAV_RESULT_ACCEPTED
        )
        payload_release_event = monitor_payload_release.get("payload_release_sim_event")
        if not isinstance(payload_release_event, dict):
            payload_release_event = (
                _l1_payload_release_not_observed_event(
                    payload_release_command_acked=payload_release_command_acked,
                    payload_model_root=payload_model_root,
                )
                if _l1_cargo_enabled()
                else None
            )
        (run_dir / "payload_release_event.json").write_text(
            json.dumps(payload_release_event or {}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        payload = _build_summary_payload(
            run_dir=run_dir,
            route=route,
            upload_observed=upload_observed,
            probe_observed=probe_observed,
            payload_release_event=payload_release_event,
            monitor_seconds=monitor_seconds,
            min_progress_m=min_progress_m,
            no_progress_grace_seconds=no_progress_grace_seconds,
            min_route_altitude_m=min_route_altitude_m,
            altitude_grace_seconds=altitude_grace_seconds,
            min_battery_remaining_percent=min_battery_remaining_percent,
            post_abort_wait_seconds=post_abort_wait_seconds,
            land_post_abort_wait_seconds=land_post_abort_wait_seconds,
            rtl_post_abort_wait_seconds=rtl_post_abort_wait_seconds,
            rtl_recovery_min_progress_m=rtl_recovery_min_progress_m,
            thermal_weather_config=thermal_weather_config,
            rain_weather_config=rain_weather_config,
            wind_profile=wind_profile,
        )
        (run_dir / "summary.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "px4_docker.log").write_text(_docker_logs(), encoding="utf-8")
        summary = payload["summary"]
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("AUTO_RUNTIME_MONITOR_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
        print(
            "AUTO_SITL_DELIVERY_GATE_JSON "
            + json.dumps(payload["sitl_delivery_gate"], sort_keys=True)
        )
        print(
            "AUTO_PAYLOAD_RELEASE_SIM_GATE_JSON "
            + json.dumps(payload["payload_release_sim_gate"], sort_keys=True)
        )
        validation_failures = _auto_runtime_smoke_validation_failures(payload)
        print(
            "AUTO_RUNTIME_SMOKE_VALIDATION_JSON "
            + json.dumps(
                {
                    "schema_version": "missionos_auto_runtime_smoke_validation.v1",
                    "validation_status": "failed"
                    if validation_failures
                    else "passed",
                    "strict_asserts_enabled": _strict_asserts_enabled(),
                    "failures": validation_failures,
                },
                sort_keys=True,
            )
        )
        if validation_failures and _strict_asserts_enabled():
            raise SystemExit(
                "AUTO runtime strict validation failed: "
                + ", ".join(validation_failures)
            )
        return 0
    finally:
        upload_smoke._stop_container()


if __name__ == "__main__":
    raise SystemExit(main())
