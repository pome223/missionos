#!/usr/bin/env python3
"""Opt-in smoke that observes Digital Twin SITL waypoint reach after takeoff."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import time
from typing import Any, Callable, Literal

from scripts import smoke_digital_twin_arm_takeoff_observed as takeoff_smoke
from scripts import smoke_digital_twin_world_bound_sitl_e2e as fixture_e2e
from scripts import smoke_px4_gazebo_horizontal_route_delivery as horizontal_delivery_smoke
from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose
from src.runtime.gz_sim_log_collector import GzSimLogCollectorError
from src.runtime.digital_twin_mission_environment import (
    build_digital_twin_stage1_environment,
    build_real_world_target_resolution,
    build_weather_source_snapshot,
    weather_source_snapshot_ref,
)
from src.runtime.digital_twin_sitl_arm_takeoff import (
    build_digital_twin_sitl_arm_takeoff_receipt,
    digital_twin_sitl_arm_takeoff_receipt_ref,
)
from src.runtime.digital_twin_sitl_execution_result import build_digital_twin_sitl_execution_result
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
    build_digital_twin_sitl_mission_upload_receipt,
    digital_twin_candidate_upload_items,
    digital_twin_sitl_mission_upload_receipt_ref,
)
from src.runtime.digital_twin_sitl_process_runner import build_digital_twin_sitl_process_run_from_observed_container
from src.runtime.digital_twin_sitl_waypoint_reach import (
    DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION,
    build_digital_twin_sitl_waypoint_reach_observation,
    digital_twin_sitl_waypoint_reach_ref,
)
from src.runtime.delivery_mission_contract import (
    DeliveryMissionContract,
    build_delivery_mission_contract,
)
from src.runtime.flight_readiness_package import build_flight_readiness_package, flight_readiness_package_ref
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    build_px4_gazebo_sitl_dropoff_flight_fact,
    build_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_payload_release_event,
)


OPT_IN_ENV = "RUN_DIGITAL_TWIN_WAYPOINT_REACH_E2E"
CONTAINER_NAME = "boiled-claw-digital-twin-waypoint-reach"
ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT_DIR / "output/digital_twin/waypoint_reach_observed"
PROMPT = "10km先の3000mの山小屋に水3kgを届ける"
PROMPT_REF = "px4_gazebo_mission_prompt_request:digital_twin_waypoint_reach"
NOW = datetime(2026, 5, 8, 3, 0, tzinfo=timezone.utc)
PAYLOAD_DETACH_TOPIC = horizontal_delivery_smoke.PAYLOAD_DETACH_TOPIC
DROPOFF_WAYPOINT_SEQ = 1
LANDING_ITEM_SEQ = 2
ROUTE_DISTANCE_ENV = "DIGITAL_TWIN_WAYPOINT_ROUTE_DISTANCE_M"
TERRAIN_SOURCE_ENV = "DIGITAL_TWIN_TERRAIN_SOURCE"
GSI_DEM_FETCH_MODE_ENV = "DIGITAL_TWIN_GSI_DEM_FETCH_MODE"
WEATHER_SCENARIO_ENV = "DIGITAL_TWIN_WEATHER_SCENARIO"
WIND_SPEED_ENV = "DIGITAL_TWIN_WIND_SPEED_MPS"
WIND_DIRECTION_ENV = "DIGITAL_TWIN_WIND_DIRECTION_DEG"
SOURCE_WEATHER_FETCH_MODE_ENV = "DIGITAL_TWIN_SOURCE_WEATHER_FETCH_MODE"
FLIGHT_PATH_TRACE_PATH_ENV = "DIGITAL_TWIN_FLIGHT_PATH_TRACE_PATH"
QUICK_ROUTE_DISTANCE_M = 500.0
DEFAULT_ROUTE_DISTANCE_M = 3000.0
LONG_RANGE_ROUTE_DISTANCE_M = 10000.0
GENERATED_FIXTURE_TERRAIN_SOURCE = "generated_fixture"
GSI_DEM_TERRAIN_SOURCE = "gsi_dem"
NO_WEATHER_SCENARIO = "none"
FIXED_WIND_SCENARIO = "fixed_wind"
SOURCE_WEATHER_WIND_SCENARIO = "source_weather_wind"
DELIVERY_COMPLETION_CLAIMED: Literal[False] = False
WIND_IMPACT_TELEMETRY_FIELDS: tuple[str, ...] = (
    "vehicle_local_position_x_m",
    "vehicle_local_position_y_m",
    "vehicle_local_position_z_m",
    "vehicle_local_velocity_x_mps",
    "vehicle_local_velocity_y_mps",
    "vehicle_local_velocity_z_mps",
    "vehicle_horizontal_speed_mps",
    "vehicle_nav_state",
    "mission_result_seq_current",
    "mission_result_seq_reached",
    "final_roll_rad",
    "final_pitch_rad",
    "final_yaw_rad",
)
FIXED_WIND_MATRIX_CASES: tuple[tuple[str, bool, bool], ...] = (
    ("A", False, False),
    ("B", True, False),
    ("C", False, True),
    ("D", True, True),
)
SOURCE_BACKED_TARGET_LATITUDE = 35.3606
SOURCE_BACKED_TARGET_LONGITUDE = 138.7274
GSI_DEM_SAMPLE = "\n".join(
    (
        "2820.1,2821.2,2822.3,2823.4",
        "2824.5,e,2825.6,2826.7",
        "2827.8,2828.9,2829.0,2830.1",
    )
)
OPEN_METEO_JMA_WEATHER_SAMPLE = json.dumps(
    {
        "latitude": SOURCE_BACKED_TARGET_LATITUDE,
        "longitude": SOURCE_BACKED_TARGET_LONGITUDE,
        "current": {
            "time": "2026-05-08T03:00",
            "temperature_2m": 12.5,
            "precipitation": 0.0,
            "wind_speed_10m": 7.2,
            "wind_direction_10m": 245.0,
            "wind_gusts_10m": 18.0,
            "surface_pressure": 900.0,
        },
    }
)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run Digital Twin waypoint reach smoke.")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _route_distance_m_from_env() -> float:
    raw = os.getenv(ROUTE_DISTANCE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_ROUTE_DISTANCE_M
    distance_m = float(raw)
    if distance_m <= 0:
        raise ValueError(f"{ROUTE_DISTANCE_ENV} must be greater than zero")
    return distance_m


def _terrain_source_from_env() -> str:
    raw = os.getenv(TERRAIN_SOURCE_ENV, GENERATED_FIXTURE_TERRAIN_SOURCE).strip()
    terrain_source = raw or GENERATED_FIXTURE_TERRAIN_SOURCE
    if terrain_source not in {GENERATED_FIXTURE_TERRAIN_SOURCE, GSI_DEM_TERRAIN_SOURCE}:
        raise ValueError(
            f"{TERRAIN_SOURCE_ENV} must be one of "
            f"{GENERATED_FIXTURE_TERRAIN_SOURCE!r}, {GSI_DEM_TERRAIN_SOURCE!r}"
        )
    return terrain_source


def _wind_vector_from_speed_direction(speed_mps: float, direction_deg: float) -> tuple[float, float]:
    radians = math.radians(direction_deg)
    return (
        round(speed_mps * math.cos(radians), 6),
        round(speed_mps * math.sin(radians), 6),
    )


def _source_weather_fetcher_from_env() -> Callable[[str], tuple[str, str]] | None:
    mode = os.getenv(SOURCE_WEATHER_FETCH_MODE_ENV, "live").strip() or "live"
    if mode == "live":
        return None
    if mode == "fixture":
        return lambda _url: ("fixture_open_meteo_jma_sample", OPEN_METEO_JMA_WEATHER_SAMPLE)
    raise ValueError(f"{SOURCE_WEATHER_FETCH_MODE_ENV} must be one of 'live', 'fixture'")


def _fixed_wind_scenario_from_env() -> dict[str, Any]:
    scenario = os.getenv(WEATHER_SCENARIO_ENV, NO_WEATHER_SCENARIO).strip() or NO_WEATHER_SCENARIO
    if scenario not in {NO_WEATHER_SCENARIO, FIXED_WIND_SCENARIO, SOURCE_WEATHER_WIND_SCENARIO}:
        raise ValueError(
            f"{WEATHER_SCENARIO_ENV} must be one of "
            f"{NO_WEATHER_SCENARIO!r}, {FIXED_WIND_SCENARIO!r}, "
            f"{SOURCE_WEATHER_WIND_SCENARIO!r}"
        )
    if scenario == NO_WEATHER_SCENARIO:
        return {
            "weather_scenario": NO_WEATHER_SCENARIO,
            "weather_scenario_enabled": False,
            "wind_scenario_enabled": False,
            "wind_speed_mps": 0.0,
            "wind_direction_deg": None,
            "wind_vector_x_mps": 0.0,
            "wind_vector_y_mps": 0.0,
            "wind_vector_z_mps": 0.0,
            "wind_plugin_injected": False,
            "wind_effects_plugin": "",
        }
    if scenario == SOURCE_WEATHER_WIND_SCENARIO:
        return {
            "weather_scenario": SOURCE_WEATHER_WIND_SCENARIO,
            "weather_scenario_enabled": True,
            "wind_scenario_enabled": True,
            "wind_speed_mps": None,
            "wind_direction_deg": None,
            "wind_vector_x_mps": None,
            "wind_vector_y_mps": None,
            "wind_vector_z_mps": 0.0,
            "wind_plugin_injected": False,
            "wind_effects_plugin": "gz-sim-wind-effects-system",
            "source_weather_wind_requested": True,
            "source_weather_fetch_mode": os.getenv(
                SOURCE_WEATHER_FETCH_MODE_ENV,
                "live",
            ).strip()
            or "live",
        }

    speed_mps = float(os.getenv(WIND_SPEED_ENV, "3.0"))
    if speed_mps < 0:
        raise ValueError(f"{WIND_SPEED_ENV} must be greater than or equal to zero")
    direction_deg = float(os.getenv(WIND_DIRECTION_ENV, "270.0"))
    if direction_deg < 0 or direction_deg >= 360:
        raise ValueError(f"{WIND_DIRECTION_ENV} must be in [0, 360)")
    wind_x, wind_y = _wind_vector_from_speed_direction(speed_mps, direction_deg)
    return {
        "weather_scenario": FIXED_WIND_SCENARIO,
        "weather_scenario_enabled": True,
        "wind_scenario_enabled": True,
        "wind_speed_mps": speed_mps,
        "wind_direction_deg": direction_deg,
        "wind_vector_x_mps": wind_x,
        "wind_vector_y_mps": wind_y,
        "wind_vector_z_mps": 0.0,
        "wind_plugin_injected": False,
        "wind_effects_plugin": "gz-sim-wind-effects-system",
    }


def _resolve_source_weather_wind_scenario(
    weather: dict[str, Any],
    *,
    digital_twin: dict[str, Any],
    target_item: dict[str, Any],
) -> dict[str, Any]:
    if weather.get("weather_scenario") != SOURCE_WEATHER_WIND_SCENARIO:
        return weather

    target = digital_twin["real_world_mission_target"]
    target_resolution = build_real_world_target_resolution(
        target=target,
        latitude=float(target_item["latitude_deg"]),
        longitude=float(target_item["longitude_deg"]),
        altitude_m=float(target_item["altitude_m"]) if target_item.get("altitude_m") is not None else None,
        now=NOW,
    )
    weather_source = build_weather_source_snapshot(
        target=target,
        target_resolution=target_resolution,
        now=NOW,
        fetcher=_source_weather_fetcher_from_env(),
    )
    snapshot = weather_source.model_dump(mode="json")
    if weather_source.source_unavailable or not weather_source.source_backed_weather:
        raise RuntimeError(
            "source weather wind unavailable: "
            + json.dumps(
                {
                    "provider": weather_source.provider,
                    "snapshot_status": weather_source.snapshot_status,
                    "provider_response_status": weather_source.provider_response_status,
                    "source_url": weather_source.source_url,
                },
                sort_keys=True,
            )
        )
    if weather_source.wind_speed_mps is None or weather_source.wind_direction_deg is None:
        raise RuntimeError(
            "source weather wind unavailable: Open-Meteo snapshot missing wind speed or direction"
        )
    speed_mps = float(weather_source.wind_speed_mps)
    direction_deg = float(weather_source.wind_direction_deg)
    wind_x, wind_y = _wind_vector_from_speed_direction(speed_mps, direction_deg)
    return {
        **weather,
        "source_weather_wind_requested": True,
        "source_weather_fetch_mode": os.getenv(SOURCE_WEATHER_FETCH_MODE_ENV, "live").strip()
        or "live",
        "source_weather_wind_snapshot_ref": weather_source_snapshot_ref(weather_source),
        "source_weather_wind_snapshot_status": weather_source.snapshot_status,
        "source_weather_wind_provider": weather_source.provider,
        "source_weather_wind_provider_response_status": weather_source.provider_response_status,
        "source_weather_wind_source_url": weather_source.source_url,
        "source_weather_wind_valid_at": (
            weather_source.valid_at.isoformat() if weather_source.valid_at else None
        ),
        "source_weather_wind_captured_at": weather_source.captured_at.isoformat(),
        "source_weather_wind_source_backed_weather": weather_source.source_backed_weather,
        "source_weather_wind_source_unavailable": weather_source.source_unavailable,
        "source_weather_wind_precipitation_mm_per_hour": (
            weather_source.precipitation_mm_per_hour
        ),
        "source_weather_wind_gust_mps": weather_source.wind_gust_mps,
        "source_weather_wind_temperature_c": weather_source.temperature_c,
        "source_weather_wind_pressure_hpa": weather_source.pressure_hpa,
        "source_weather_wind_snapshot": snapshot,
        "wind_speed_mps": speed_mps,
        "wind_direction_deg": direction_deg,
        "wind_vector_x_mps": wind_x,
        "wind_vector_y_mps": wind_y,
        "wind_vector_z_mps": 0.0,
        "wind_effects_plugin": "gz-sim-wind-effects-system",
    }


def _wind_effects_requested(weather: dict[str, Any]) -> bool:
    return bool(weather.get("wind_scenario_enabled")) and weather.get("weather_scenario") != NO_WEATHER_SCENARIO


def _gsi_dem_fetcher_from_env() -> Callable[[str], tuple[str, str]] | None:
    mode = os.getenv(GSI_DEM_FETCH_MODE_ENV, "live").strip() or "live"
    if mode == "live":
        return None
    if mode == "fixture":
        return lambda _url: ("fixture_gsi_dem_sample", GSI_DEM_SAMPLE)
    raise ValueError(f"{GSI_DEM_FETCH_MODE_ENV} must be one of 'live', 'fixture'")


def _stage1_kwargs_for_terrain_source(terrain_source: str) -> dict[str, Any]:
    if terrain_source == GENERATED_FIXTURE_TERRAIN_SOURCE:
        return {}
    if terrain_source == GSI_DEM_TERRAIN_SOURCE:
        return {
            "source_backed_target_latitude": SOURCE_BACKED_TARGET_LATITUDE,
            "source_backed_target_longitude": SOURCE_BACKED_TARGET_LONGITUDE,
            "source_backed_dem_fetcher": _gsi_dem_fetcher_from_env(),
        }
    raise ValueError(f"unsupported terrain source: {terrain_source}")


def _terrain_source_summary(
    digital_twin: dict[str, Any],
    *,
    requested_terrain_source: str,
) -> dict[str, Any]:
    dem = digital_twin.get("terrain_dem_source_snapshot") or {}
    heightmap_file = digital_twin.get("terrain_heightmap_file_artifact") or {}
    world = digital_twin.get("gazebo_world_artifact") or {}
    terrain_scale = world.get("terrain_scale") or ()
    return {
        "terrain_source": requested_terrain_source,
        "terrain_source_requested": requested_terrain_source,
        "source_backed_terrain": bool(dem.get("source_backed_terrain")),
        "terrain_dem_source_snapshot_status": dem.get("snapshot_status", ""),
        "terrain_dem_source_snapshot_provider": dem.get("provider", ""),
        "terrain_dem_source_snapshot_source_url": dem.get("source_url", ""),
        "terrain_dem_source_snapshot_provider_response_status": dem.get(
            "provider_response_status",
            "",
        ),
        "terrain_source_unavailable": bool(dem.get("source_unavailable")),
        "heightmap_file_path_or_artifact_uri": heightmap_file.get(
            "gazebo_dem_file_path_or_artifact_uri",
            "",
        ),
        "gazebo_world_file_path_or_artifact_uri": world.get(
            "world_file_path_or_artifact_uri",
            "",
        ),
        "terrain_world_scale_x_m": (
            float(terrain_scale[0]) if len(terrain_scale) >= 1 else None
        ),
        "terrain_world_scale_y_m": (
            float(terrain_scale[1]) if len(terrain_scale) >= 2 else None
        ),
        "terrain_world_scale_z_m": (
            float(terrain_scale[2]) if len(terrain_scale) >= 3 else None
        ),
    }


def _assert_terrain_source_ready(
    digital_twin: dict[str, Any],
    *,
    requested_terrain_source: str,
) -> None:
    if requested_terrain_source != GSI_DEM_TERRAIN_SOURCE:
        return
    summary = _terrain_source_summary(
        digital_twin,
        requested_terrain_source=requested_terrain_source,
    )
    if summary["source_backed_terrain"] is not True or not digital_twin.get(
        "gazebo_world_artifact"
    ):
        raise RuntimeError(
            "GSI DEM terrain unavailable; fail-closed without fixture fallback: "
            + json.dumps(summary, sort_keys=True)
        )


def _prompt_for_route_distance(distance_m: float) -> str:
    distance_km = distance_m / 1000.0
    if distance_km.is_integer():
        distance_text = f"{int(distance_km)}km"
    else:
        distance_text = f"{distance_km:g}km"
    return f"{distance_text}先の3000mの山小屋に水3kgを届ける"


def _route_mode(distance_m: float) -> str:
    if math.isclose(distance_m, QUICK_ROUTE_DISTANCE_M):
        return "quick"
    if math.isclose(distance_m, LONG_RANGE_ROUTE_DISTANCE_M):
        return "long_range"
    return "routine"


def _cruise_progress_threshold_m(route_distance_m: float) -> float:
    return min(1000.0, max(100.0, float(route_distance_m) * 0.5))


def _listener_field(output: str, field: str) -> float | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+(?:\.\d+)?)", output)
    return float(match.group(1)) if match else None


def _safe_px4_listener(topic: str, samples: int = 1) -> str:
    try:
        return takeoff_smoke._docker_exec_px4_listener(topic, samples)
    except Exception:
        return ""


def _listener_indexed_field(output: str, field: str, index: int) -> float | None:
    patterns = (
        rf"\b{re.escape(field)}\[{index}\]:\s*(-?\d+(?:\.\d+)?)",
        rf"\b{re.escape(field)}:\s*\[[^\]]*\]",
    )
    match = re.search(patterns[0], output)
    if match:
        return float(match.group(1))
    array_match = re.search(patterns[1], output)
    if not array_match:
        return None
    values = re.findall(r"-?\d+(?:\.\d+)?", array_match.group(0))
    return float(values[index]) if len(values) > index else None


def _round_optional(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def _attitude_euler_from_quaternion(
    w: float | None,
    x: float | None,
    y: float | None,
    z: float | None,
) -> dict[str, float | None]:
    if None in (w, x, y, z):
        return {
            "final_roll_rad": None,
            "final_pitch_rad": None,
            "final_yaw_rad": None,
        }
    qw = float(w)
    qx = float(x)
    qy = float(y)
    qz = float(z)
    sinr_cosp = 2 * (qw * qx + qy * qz)
    cosr_cosp = 1 - 2 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    sinp = 2 * (qw * qy - qz * qx)
    pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)

    siny_cosp = 2 * (qw * qz + qx * qy)
    cosy_cosp = 1 - 2 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return {
        "final_roll_rad": round(roll, 6),
        "final_pitch_rad": round(pitch, 6),
        "final_yaw_rad": round(yaw, 6),
    }


def _wind_impact_telemetry_from_listener_outputs(
    *,
    mission_result: str,
    local_position: str,
    vehicle_status: str,
    vehicle_attitude: str,
    seq_reached: float | None,
    distance_progress_m: float | None = None,
    flight_duration_s: float | None = None,
) -> dict[str, float | None]:
    local_x = _listener_field(local_position, "x")
    local_y = _listener_field(local_position, "y")
    local_z = _listener_field(local_position, "z")
    local_vx = _listener_field(local_position, "vx")
    local_vy = _listener_field(local_position, "vy")
    local_vz = _listener_field(local_position, "vz")
    progress_speed = (
        float(distance_progress_m) / float(flight_duration_s)
        if distance_progress_m is not None
        and flight_duration_s is not None
        and flight_duration_s > 0
        else None
    )
    return {
        "vehicle_local_position_x_m": _round_optional(local_x),
        "vehicle_local_position_y_m": _round_optional(local_y),
        "vehicle_local_position_z_m": _round_optional(local_z),
        "vehicle_local_velocity_x_mps": _round_optional(local_vx),
        "vehicle_local_velocity_y_mps": _round_optional(local_vy),
        "vehicle_local_velocity_z_mps": _round_optional(local_vz),
        "vehicle_horizontal_speed_mps": (
            round(math.hypot(local_vx, local_vy), 3)
            if local_vx is not None and local_vy is not None
            else None
        ),
        "progress_speed_mps": _round_optional(progress_speed),
        "vehicle_nav_state": _round_optional(
            _listener_field(vehicle_status, "nav_state"), 0
        ),
        "mission_result_seq_current": _round_optional(
            _listener_field(mission_result, "seq_current"), 0
        ),
        "mission_result_seq_reached": _round_optional(seq_reached, 0),
        **_attitude_euler_from_quaternion(
            _listener_indexed_field(vehicle_attitude, "q", 0),
            _listener_indexed_field(vehicle_attitude, "q", 1),
            _listener_indexed_field(vehicle_attitude, "q", 2),
            _listener_indexed_field(vehicle_attitude, "q", 3),
        ),
    }


def _flight_path_trace_path_from_env() -> Path | None:
    raw = os.getenv(FLIGHT_PATH_TRACE_PATH_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _append_flight_path_trace_sample(trace_path: Path | None, sample: dict[str, Any]) -> None:
    if trace_path is None:
        return
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, sort_keys=True, ensure_ascii=False) + "\n")


def _launch_gcs_heartbeat_keepalive(*, duration_s: float) -> subprocess.Popen[str]:
    script = textwrap.dedent(f"""
        import socket, struct, time
        MAVLINK2_MAGIC=0xFD
        MAVLINK_MSG_ID_HEARTBEAT=0
        CRC_EXTRA={{0:50}}
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
        remote=('127.0.0.1',{takeoff_smoke._ArmTakeoffUploadShim.PX4_MAVLINK_PORT})
        deadline=time.monotonic()+float({duration_s!r})
        seq=0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote)
                seq=(seq+1)&255
                time.sleep(1.0)
    """)
    process = subprocess.Popen(
        ["docker", "exec", "-i", CONTAINER_NAME, "python3", "-"],
        cwd=ROOT_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(script)
    process.stdin.close()
    process.stdin = None
    return process


def _enable_detachable_payload(world_root: Path) -> None:
    """Reuse the existing Gazebo detachable-joint payload model in the DT world."""

    x500_sdf_path = world_root / "models/x500/model.sdf"
    x500_sdf = x500_sdf_path.read_text(encoding="utf-8")
    if "<model name=\"delivery_payload\">" not in x500_sdf:
        x500_sdf = x500_sdf.replace(
            "  </model>\n</sdf>",
            _digital_twin_payload_nested_model_sdf_patch()
            + horizontal_delivery_smoke._payload_model_sdf_patch()
            + "  </model>\n</sdf>",
        )
        x500_sdf_path.write_text(x500_sdf, encoding="utf-8")

    world_path = world_root / "worlds/default.sdf"
    world_text = world_path.read_text(encoding="utf-8")
    world_text_without_top_level_payload = re.sub(
        r"\n    <model name=\"delivery_payload\">.*?\n    </model>\n",
        "\n",
        world_text,
        flags=re.S,
    )
    if world_text_without_top_level_payload != world_text:
        world_path.write_text(world_text_without_top_level_payload, encoding="utf-8")


def _fresh_px4_world_assets(run_dir: Path) -> Path:
    world_root = run_dir / "px4_world"
    if world_root.exists():
        shutil.rmtree(world_root)
    return fixture_e2e._copy_px4_world_assets(run_dir)


def _digital_twin_payload_nested_model_sdf_patch() -> str:
    return """
    <model name="delivery_payload">
      <pose>0 0 -0.12 0 0 0</pose>
      <static>false</static>
      <link name="payload_link">
        <inertial>
          <mass>0.05</mass>
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


def _wind_effects_sdf_patch(weather: dict[str, Any]) -> str:
    return f"""
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics">
    </plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands">
    </plugin>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster">
    </plugin>
    <plugin filename="gz-sim-contact-system" name="gz::sim::systems::Contact">
    </plugin>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu">
    </plugin>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure">
    </plugin>
    <plugin filename="gz-sim-air-speed-system" name="gz::sim::systems::AirSpeed">
    </plugin>
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
    <wind>
      <linear_velocity>{weather["wind_vector_x_mps"]} {weather["wind_vector_y_mps"]} {weather["wind_vector_z_mps"]}</linear_velocity>
    </wind>
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


def _enable_wind_on_x500_base(world_root: Path) -> bool:
    x500_base_sdf_path = world_root / "models/x500_base/model.sdf"
    if not x500_base_sdf_path.exists():
        raise RuntimeError("x500_base model.sdf missing; cannot enable wind on vehicle links")
    x500_base_sdf = x500_base_sdf_path.read_text(encoding="utf-8")
    if "<enable_wind>true</enable_wind>" in x500_base_sdf:
        return False
    x500_base_sdf = re.sub(
        r'(<link name="[^"]+">\n)',
        r"\1      <enable_wind>true</enable_wind>\n",
        x500_base_sdf,
    )
    x500_base_sdf_path.write_text(x500_base_sdf, encoding="utf-8")
    return True


def _enable_fixed_wind_scenario(
    *,
    world_root: Path,
    prepared_world_path: Path,
    weather: dict[str, Any],
) -> dict[str, Any]:
    summary = dict(weather)
    if not _wind_effects_requested(weather):
        return summary
    world_text = prepared_world_path.read_text(encoding="utf-8")
    if "gz::sim::systems::WindEffects" in world_text:
        summary["wind_plugin_injected"] = False
    else:
        world_text = world_text.replace(
            "  </world>",
            _wind_effects_sdf_patch(weather) + "  </world>",
        )
        prepared_world_path.write_text(world_text, encoding="utf-8")
        summary["wind_plugin_injected"] = True
    summary["wind_enabled_on_vehicle_links"] = _enable_wind_on_x500_base(world_root)
    return summary


def _fixed_wind_readiness_diagnostic() -> dict[str, Any]:
    def _docker_exec(command: str) -> dict[str, Any]:
        completed = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "sh", "-lc", command],
            cwd=ROOT_DIR,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }

    topics = _docker_exec("gz topic -l | sort")
    topic_lines = topics["stdout"].splitlines()
    wind_topic = _docker_exec("timeout 2 gz topic -i -t /world/default/wind 2>&1")
    pose_info = _docker_exec("timeout 2 gz topic -i -t /world/default/pose/info 2>&1")
    scene_info = _docker_exec("timeout 2 gz service -i --service /world/default/scene/info 2>&1")
    return {
        "wind_readiness_failure": True,
        "wind_readiness_expected_scene_topic": "/world/default/scene/info",
        "wind_readiness_expected_pose_topic": "/world/default/pose/info",
        "wind_readiness_expected_wind_topic": "/world/default/wind",
        "wind_readiness_scene_info_available": "/world/default/scene/info" in topic_lines,
        "wind_readiness_pose_info_available": "/world/default/pose/info" in topic_lines,
        "wind_readiness_wind_topic_available": "/world/default/wind" in topic_lines,
        "wind_readiness_topics": topic_lines,
        "wind_readiness_scene_info_probe": scene_info,
        "wind_readiness_pose_info_probe": pose_info,
        "wind_readiness_wind_topic_probe": wind_topic,
    }


def _docker_exec_for_fixed_wind_probe(container_name: str, command: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "exec", container_name, "sh", "-lc", command],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _probe_fixed_wind_readiness_case(
    *,
    case_root: Path,
    case_name: str,
    wind_effects: bool,
    enable_wind: bool,
) -> dict[str, Any]:
    container_name = f"{CONTAINER_NAME}-wind-matrix-{case_name.lower()}"
    fixture_e2e._run(["docker", "rm", "-f", container_name], check=False)
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "-e",
        "GZ_SIM_RESOURCE_PATH=/dt:/dt/models:/opt/px4-gazebo/share/gz/models",
        "-v",
        f"{case_root.resolve()}:/dt",
        "-v",
        f"{(case_root / 'models/x500').resolve()}:/opt/px4-gazebo/share/gz/models/x500:ro",
        "--entrypoint",
        "sh",
        fixture_e2e.PX4_GAZEBO_IMAGE,
        "-lc",
        "gz sim --verbose=${GZ_VERBOSE:=1} -r -s /dt/worlds/default.sdf & echo gz_pid=$!; sleep 45",
    ]
    completed = fixture_e2e._run(command, timeout=120, check=False)
    time.sleep(float(os.getenv("DIGITAL_TWIN_FIXED_WIND_MATRIX_SETTLE_SECONDS", "8")))
    topics = _docker_exec_for_fixed_wind_probe(container_name, "gz topic -l | sort")
    wind_topic = _docker_exec_for_fixed_wind_probe(
        container_name,
        "timeout 2 gz topic -i -t /world/default/wind 2>&1",
    )
    pose_info = _docker_exec_for_fixed_wind_probe(
        container_name,
        "timeout 2 gz topic -i -t /world/default/pose/info 2>&1",
    )
    scene_info = _docker_exec_for_fixed_wind_probe(
        container_name,
        "timeout 2 gz service -i --service /world/default/scene/info 2>&1",
    )
    logs = fixture_e2e._run(
        ["docker", "logs", "--tail", "240", container_name],
        check=False,
    ).stdout
    fixture_e2e._run(["docker", "rm", "-f", container_name], check=False)
    topic_lines = topics["stdout"].splitlines()
    scene_available = "/world/default/scene/info" in topic_lines
    pose_available = "/world/default/pose/info" in topic_lines
    wind_available = "/world/default/wind" in topic_lines
    return {
        "case": case_name,
        "wind_effects": wind_effects,
        "enable_wind": enable_wind,
        "container_start_returncode": completed.returncode,
        "world_name_default": 'world name="default"' in (case_root / "worlds/default.sdf").read_text(
            encoding="utf-8"
        ),
        "wind_topic_available": wind_available,
        "scene_info_available": scene_available,
        "pose_info_available": pose_available,
        "topics": topics["stdout"].splitlines(),
        "result": "ready" if scene_available and pose_available and (wind_available == wind_effects) else "not_ready",
        "scene_info_probe": scene_info,
        "pose_info_probe": pose_info,
        "wind_topic_probe": wind_topic,
        "gazebo_log_tail": logs[-4000:],
    }


def _fixed_wind_readiness_matrix(
    *,
    base_world_root: Path,
    run_dir: Path,
    weather: dict[str, Any],
) -> dict[str, Any]:
    matrix_root = run_dir / "fixed_wind_readiness_matrix"
    if matrix_root.exists():
        shutil.rmtree(matrix_root)
    matrix_root.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for case_name, wind_effects, enable_wind in FIXED_WIND_MATRIX_CASES:
        case_root = matrix_root / f"case_{case_name.lower()}"
        shutil.copytree(base_world_root, case_root)
        prepared_world_path = case_root / "worlds/default.sdf"
        if wind_effects:
            world_text = prepared_world_path.read_text(encoding="utf-8")
            prepared_world_path.write_text(
                world_text.replace("  </world>", _wind_effects_sdf_patch(weather) + "  </world>"),
                encoding="utf-8",
            )
        if enable_wind:
            _enable_wind_on_x500_base(case_root)
        cases.append(
            _probe_fixed_wind_readiness_case(
                case_root=case_root,
                case_name=case_name,
                wind_effects=wind_effects,
                enable_wind=enable_wind,
            )
        )
    return {
        "matrix_cases": cases,
        "ready_cases": [case["case"] for case in cases if case["result"] == "ready"],
    }


def _start_fixed_wind_world_bound_container(
    world_root: Path,
    prepared_world_path: Path,
) -> tuple[list[str], int, str, bool]:
    fixture_e2e._run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    px4_home_env: list[str] = []
    for name in ("PX4_HOME_LAT", "PX4_HOME_LON", "PX4_HOME_ALT"):
        value = os.getenv(f"DIGITAL_TWIN_{name}")
        if value:
            px4_home_env.extend(["-e", f"{name}={value}"])
    prelaunch_script = """
        set -eu
        gz sim --verbose=${GZ_VERBOSE:=1} -r -s /dt/worlds/default.sdf &
        gz_pid=$!
        echo "INFO  [dt_wind] Prelaunched Gazebo with pid ${gz_pid}"
        for i in $(seq 1 60); do
            topics=$(gz topic -l | sort || true)
            if echo "$topics" | grep -q "^/world/default/scene/info$" && echo "$topics" | grep -q "^/world/default/pose/info$" && echo "$topics" | grep -q "^/world/default/wind$"; then
                echo "INFO  [dt_wind] WindEffects Gazebo world is ready"
                break
            fi
            if [ "$i" -eq 60 ]; then
                echo "ERROR [dt_wind] Timed out waiting for WindEffects Gazebo readiness"
                echo "$topics"
                exit 1
            fi
            sleep 1
        done
        PX4_GZ_STANDALONE=1 /opt/px4-gazebo/bin/px4-entrypoint.sh -d &
        px4_pid=$!
        echo "INFO  [dt_wind] Started PX4 standalone with pid ${px4_pid}"
        wait "$px4_pid"
    """
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-e",
        "PX4_SIM_MODEL=gz_x500",
        "-e",
        "PX4_GZ_WORLD=default",
        "-e",
        "HEADLESS=1",
        "-e",
        "PX4_GZ_NO_FOLLOW=1",
        "-e",
        "PX4_GZ_WORLDS=/dt/worlds",
        "-e",
        "GZ_SIM_RESOURCE_PATH=/dt:/dt/models:/opt/px4-gazebo/share/gz/models",
        *px4_home_env,
        "-v",
        f"{world_root.resolve()}:/dt",
        "-v",
        f"{(world_root / 'models/x500').resolve()}:/opt/px4-gazebo/share/gz/models/x500:ro",
        "--entrypoint",
        "sh",
        fixture_e2e.PX4_GAZEBO_IMAGE,
        "-lc",
        textwrap.dedent(prelaunch_script),
    ]
    fixture_e2e._run(command, timeout=240)
    logs, started = fixture_e2e._wait_for_world_bound_startup(
        prepared_world_path,
        float(os.getenv("DIGITAL_TWIN_WORLD_BOUND_STARTUP_TIMEOUT", "90")),
    )
    inspect = fixture_e2e._run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER_NAME],
        check=False,
    )
    pid = int(inspect.stdout.strip() or "0")
    return command, pid, logs, started


def _payload_vehicle_offset_m(
    *,
    payload_pose: dict[str, float],
    vehicle_pose: dict[str, float],
) -> dict[str, float]:
    return {
        "dx_m": round(float(payload_pose["x"]) - float(vehicle_pose["x"]), 3),
        "dy_m": round(float(payload_pose["y"]) - float(vehicle_pose["y"]), 3),
        "dz_m": round(float(payload_pose["z"]) - float(vehicle_pose["z"]), 3),
        "horizontal_m": round(
            math.hypot(
                float(payload_pose["x"]) - float(vehicle_pose["x"]),
                float(payload_pose["y"]) - float(vehicle_pose["y"]),
            ),
            3,
        )
    }


def _payload_world_pose_from_vehicle_relative_pose(
    *,
    payload_relative_pose: dict[str, float],
    vehicle_pose: dict[str, float],
) -> dict[str, float]:
    return {
        "x": float(vehicle_pose["x"]) + float(payload_relative_pose["x"]),
        "y": float(vehicle_pose["y"]) + float(payload_relative_pose["y"]),
        "z": float(vehicle_pose["z"]) + float(payload_relative_pose["z"]),
    }


def _entity_pose_sample(entity_name: str) -> dict[str, float]:
    entity_names = (entity_name,)
    if entity_name == "delivery_payload":
        entity_names = (
            "delivery_payload",
            "x500_0::delivery_payload",
            "x500::delivery_payload",
            "payload_link",
            "x500_0::payload_link",
            "x500::payload_link",
            "x500_0::delivery_payload::payload_link",
            "x500::delivery_payload::payload_link",
        )
    sample = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    errors: list[str] = []
    for candidate in entity_names:
        try:
            pose = parse_gz_sim_entity_pose(sample, entity_name=candidate)
            return {key: float(pose[key]) for key in ("x", "y", "z")}
        except GzSimLogCollectorError as exc:
            errors.append(str(exc))
    raise GzSimLogCollectorError("; ".join(errors))


def _payload_pose_sample() -> dict[str, float]:
    return _entity_pose_sample("delivery_payload")


def _gazebo_local_xy_from_wgs84(
    *,
    latitude_deg: float,
    longitude_deg: float,
    coordinate_transform: dict[str, Any],
) -> tuple[float, float]:
    return (
        float(coordinate_transform["world_origin_x_m"])
        + (
            float(longitude_deg)
            - float(coordinate_transform["origin_longitude"])
        )
        * float(coordinate_transform["meters_per_degree_lon"]),
        float(coordinate_transform["world_origin_y_m"])
        + (
            float(latitude_deg)
            - float(coordinate_transform["origin_latitude"])
        )
        * float(coordinate_transform["meters_per_degree_lat"]),
    )


def _px4_gazebo_local_xy_from_takeoff_anchor(
    *,
    takeoff_item: dict[str, Any],
    target_item: dict[str, Any],
    coordinate_transform: dict[str, Any],
) -> tuple[float, float]:
    """Map mission WGS84 target into the PX4/Gazebo local frame.

    The Digital Twin coordinate transform frame is terrain-centered. PX4/Gazebo
    SITL spawns x500_0 at the takeoff anchor, so dropoff verification must use
    the target position relative to the uploaded takeoff mission item.
    """

    return (
        (
            float(target_item["longitude_deg"])
            - float(takeoff_item["longitude_deg"])
        )
        * float(coordinate_transform["meters_per_degree_lon"]),
        (
            float(target_item["latitude_deg"])
            - float(takeoff_item["latitude_deg"])
        )
        * float(coordinate_transform["meters_per_degree_lat"]),
    )


def _trigger_payload_release() -> dict[str, Any]:
    vehicle_at_release = _entity_pose_sample("x500_0")
    before = _payload_pose_sample()
    payload_release_pose = _payload_world_pose_from_vehicle_relative_pose(
        payload_relative_pose=before,
        vehicle_pose=vehicle_at_release,
    )
    observed_at = datetime.now(timezone.utc).isoformat()
    subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"gz topic -t {PAYLOAD_DETACH_TOPIC} -m gz.msgs.Empty -p ''",
        ],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    time.sleep(1.0)
    after = _payload_pose_sample()
    return {
        "payload_release_observed": True,
        "payload_release_event_source": "gazebo_detachable_joint_detach_event",
        "payload_id": "pkg-digital-twin-water",
        "payload_detach_topic": PAYLOAD_DETACH_TOPIC,
        "payload_pose_before_release": before,
        "payload_pose_before_release_frame": "x500_0_nested_model_relative",
        "payload_world_pose_source": "x500_0_pose_plus_nested_payload_relative_pose_at_detach",
        "payload_vehicle_offset_before_release_m": _payload_vehicle_offset_m(
            payload_pose=payload_release_pose,
            vehicle_pose=vehicle_at_release,
        ),
        "payload_release_position_x_m": payload_release_pose["x"],
        "payload_release_position_y_m": payload_release_pose["y"],
        "payload_release_position_z_m": payload_release_pose["z"],
        "payload_pose_after_detach": after,
        "payload_pose_after_detach_frame": "x500_0_nested_model_relative",
        "payload_vehicle_offset_after_release_m": _payload_vehicle_offset_m(
            payload_pose=_payload_world_pose_from_vehicle_relative_pose(
                payload_relative_pose=after,
                vehicle_pose=vehicle_at_release,
            ),
            vehicle_pose=vehicle_at_release,
        ),
        "payload_release_observed_at": observed_at,
        "dropoff_target_x_m": vehicle_at_release["x"],
        "dropoff_target_y_m": vehicle_at_release["y"],
        "dropoff_target_altitude_m": vehicle_at_release["z"],
        "dropoff_target_pose_source": "gazebo_pose_info_x500_0_at_waypoint_reach",
        "dropoff_vehicle_position_x_m": vehicle_at_release["x"],
        "dropoff_vehicle_position_y_m": vehicle_at_release["y"],
        "dropoff_vehicle_position_z_m": vehicle_at_release["z"],
        "dropoff_vehicle_position_source": "gazebo_pose_info_x500_0_at_payload_release",
        "gazebo_detachable_joint_release_performed": True,
        "gazebo_detachable_joint_release_observed": True,
        "gazebo_entity_mutation_performed": False,
    }


def _digital_twin_delivery_contract(
    *,
    takeoff_item: dict[str, Any],
    target_item: dict[str, Any],
) -> DeliveryMissionContract:
    return build_delivery_mission_contract(
        mission_id="digital-twin-mountain-hut-water",
        pickup_location={
            "location_id": "digital-twin-takeoff-anchor",
            "latitude": float(takeoff_item["latitude_deg"]),
            "longitude": float(takeoff_item["longitude_deg"]),
            "altitude_m": float(takeoff_item["altitude_m"]),
        },
        dropoff_location={
            "location_id": "digital-twin-mountain-hut",
            "latitude": float(target_item["latitude_deg"]),
            "longitude": float(target_item["longitude_deg"]),
            "altitude_m": float(target_item["altitude_m"]),
        },
        delivery_window={
            "earliest_pickup_at": NOW.isoformat(),
            "latest_dropoff_at": "2026-05-08T04:00:00+00:00",
        },
        package_constraints={
            "package_id": "pkg-digital-twin-water",
            "max_weight_kg": 3.0,
        },
        weather_constraints={
            "max_wind_speed_mps": 12.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1000.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 8.0,
            "accepted_surface_kinds": ["digital_twin_terrain"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "mission_item_reached",
                "payload_release_event",
                "landing_observed",
            ],
            "max_freshness_seconds": 10.0,
        },
        now=NOW,
        metadata={
            "source": "digital_twin_waypoint_reach_smoke",
            "delivery_completion_claimed": DELIVERY_COMPLETION_CLAIMED,
        },
    )


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _parse_utc_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _required_float(
    mapping: dict[str, Any],
    key: str,
    missing: list[str],
) -> float:
    value = mapping.get(key)
    if value is None:
        missing.append(key)
        return 0.0
    return float(value)


def _dropoff_verification_from_digital_twin_evidence(
    *,
    waypoint_observed: dict[str, Any],
    delivery_mission_contract: DeliveryMissionContract | None,
    sitl_mission_upload_receipt_ref: str,
    telemetry_ref: str,
) -> dict[str, Any]:
    """Adapt Digital Twin smoke facts into the existing SITL dropoff verifier.

    The existing verifier requires vehicle, target, and payload release positions
    in one Gazebo-local frame. The Digital Twin waypoint smoke must not infer
    that frame from WGS84 or PX4 local NED samples.
    """

    missing: list[str] = []
    payload_summary = waypoint_observed.get("payload_release_summary")
    if not isinstance(payload_summary, dict):
        payload_summary = {}
    evidence = dict(waypoint_observed)
    for key, value in payload_summary.items():
        if (
            key
            in {
                "dropoff_target_x_m",
                "dropoff_target_y_m",
                "dropoff_target_pose_source",
            }
            and payload_summary.get("dropoff_target_pose_source")
            != "gazebo_pose_info_x500_0_at_waypoint_reach"
        ):
            continue
        evidence[key] = value
    if delivery_mission_contract is None:
        missing.append("delivery_mission_contract")
    if evidence.get("payload_release_observed") is not True:
        missing.append("payload_release_observed")
    event_source = str(evidence.get("payload_release_event_source") or "")
    if not event_source:
        missing.append("payload_release_event_source")
    observed_at = str(evidence.get("payload_release_observed_at") or "")
    if not observed_at:
        missing.append("payload_release_observed_at")
    payload_id = str(evidence.get("payload_id") or "")
    if not payload_id:
        missing.append("payload_id")

    release_x = _required_float(evidence, "payload_release_position_x_m", missing)
    release_y = _required_float(evidence, "payload_release_position_y_m", missing)
    release_z = _required_float(evidence, "payload_release_position_z_m", missing)
    target_x = _required_float(evidence, "dropoff_target_x_m", missing)
    target_y = _required_float(evidence, "dropoff_target_y_m", missing)
    target_z = float(evidence.get("dropoff_target_altitude_m") or 0.0)
    vehicle_x = _required_float(
        evidence,
        "dropoff_vehicle_position_x_m",
        missing,
    )
    vehicle_y = _required_float(
        evidence,
        "dropoff_vehicle_position_y_m",
        missing,
    )
    vehicle_z = _required_float(
        evidence,
        "dropoff_vehicle_position_z_m",
        missing,
    )
    mission_item_reached_at = _first_present(
        evidence,
        "dropoff_mission_item_reached_at",
        "target_waypoint_reached_at",
    )
    if mission_item_reached_at is None:
        missing.append("dropoff_mission_item_reached_at")

    if missing:
        return {
            "dropoff_verification_applied": False,
            "dropoff_verified": False,
            "dropoff_verification_status": "missing_evidence",
            "dropoff_verification_blocked_reasons": tuple(
                f"missing_{item}" for item in dict.fromkeys(missing)
            ),
            "expected_mission_item_seq": DROPOFF_WAYPOINT_SEQ,
            "landing_item_seq": LANDING_ITEM_SEQ,
            "landing_observed": bool(waypoint_observed.get("landing_observed")),
            "delivery_completion_claimed": DELIVERY_COMPLETION_CLAIMED,
        }

    release = build_px4_gazebo_sitl_payload_release_event(
        event_source=event_source,  # type: ignore[arg-type]
        payload_id=payload_id,
        release_position_x_m=release_x,
        release_position_y_m=release_y,
        release_position_z_m=release_z,
        observed_at=_parse_utc_timestamp(observed_at),
        metadata={
            "source": "digital_twin_waypoint_reach_smoke",
            "payload_detach_topic": evidence.get("payload_detach_topic", ""),
        },
    )
    fact = build_px4_gazebo_sitl_dropoff_flight_fact(
        vehicle_id="x500_0",
        dropoff_zone_id="digital-twin-mountain-hut",
        position_x_m=vehicle_x,
        position_y_m=vehicle_y,
        position_z_m=vehicle_z,
        dropoff_target_x_m=target_x,
        dropoff_target_y_m=target_y,
        dropoff_target_altitude_m=target_z,
        mission_item_reached_observed=DROPOFF_WAYPOINT_SEQ in tuple(
            int(item) for item in waypoint_observed.get("mission_item_reached_seq", ())
        ),
        mission_item_reached_seq=DROPOFF_WAYPOINT_SEQ,
        mission_item_reached_at=_parse_utc_timestamp(mission_item_reached_at),
        payload_release_event=release,
        telemetry_ref=telemetry_ref,
        sitl_mission_upload_receipt_ref=sitl_mission_upload_receipt_ref,
        observed_at=_parse_utc_timestamp(mission_item_reached_at),
        metadata={
            "source": "digital_twin_waypoint_reach_smoke",
            "land_item_seq": LANDING_ITEM_SEQ,
            "land_item_is_landing_only": True,
        },
    )
    verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=delivery_mission_contract,
        dropoff_flight_fact=fact,
        payload_release_event=release,
        expected_mission_item_seq=DROPOFF_WAYPOINT_SEQ,
        now=_parse_utc_timestamp(observed_at),
    )
    return {
        "dropoff_verification_applied": True,
        "dropoff_verified": verification.dropoff_verified,
        "dropoff_verification_status": verification.status.value,
        "dropoff_verification_ref": (
            "px4_gazebo_sitl_dropoff_verification:"
            + verification.verification_id
        ),
        "dropoff_flight_fact_ref": (
            "px4_gazebo_sitl_dropoff_flight_fact:" + fact.fact_id
        ),
        "payload_release_event_ref": (
            "px4_gazebo_sitl_payload_release_event:" + release.event_id
        ),
        "dropoff_verification_blocked_reasons": verification.blocked_reasons,
        "expected_mission_item_seq": verification.expected_mission_item_seq,
        "landing_item_seq": LANDING_ITEM_SEQ,
        "landing_observed": bool(waypoint_observed.get("landing_observed")),
        "delivery_completion_claimed": DELIVERY_COMPLETION_CLAIMED,
        "observed_distance_to_dropoff_m": verification.observed_distance_to_dropoff_m,
        "observed_altitude_error_m": verification.observed_altitude_error_m,
    }


def _observe_waypoint(
    *,
    target_lat: float,
    target_lon: float,
    route_distance_m: float,
    timeout_s: float,
    landing_seq: int | None = None,
    payload_release_callback: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    reached: set[int] = set()
    reached_at_by_seq: dict[int, str] = {}
    positions: list[dict[str, Any]] = []
    altitude: list[dict[str, Any]] = []
    min_distance = 999999999.0
    first_distance: float | None = None
    max_relative_alt = 0.0
    waypoint_reached = False
    payload_release_summary: dict[str, Any] | None = None
    post_waypoint_min_relative_alt: float | None = None
    cruise_progress_threshold_m = _cruise_progress_threshold_m(route_distance_m)
    final_wind_impact_telemetry: dict[str, float | None] = {}
    flight_path_trace_path = _flight_path_trace_path_from_env()
    if flight_path_trace_path is not None:
        flight_path_trace_path.parent.mkdir(parents=True, exist_ok=True)
        flight_path_trace_path.write_text("", encoding="utf-8")
    while time.monotonic() - start < timeout_s:
        mission_result = takeoff_smoke._docker_exec_px4_listener("mission_result", 1)
        global_position = takeoff_smoke._docker_exec_px4_listener("vehicle_global_position", 1)
        local_position = takeoff_smoke._docker_exec_px4_listener("vehicle_local_position", 1)
        vehicle_status = _safe_px4_listener("vehicle_status", 1)
        vehicle_attitude = _safe_px4_listener("vehicle_attitude", 1)
        seq_reached = _listener_field(mission_result, "seq_reached")
        local_z = _listener_field(local_position, "z")
        final_wind_impact_telemetry = _wind_impact_telemetry_from_listener_outputs(
            mission_result=mission_result,
            local_position=local_position,
            vehicle_status=vehicle_status,
            vehicle_attitude=vehicle_attitude,
            seq_reached=seq_reached,
        )
        if seq_reached is not None and seq_reached >= 0:
            reached_seq = int(seq_reached)
            reached.add(reached_seq)
            reached_at_by_seq.setdefault(
                reached_seq,
                datetime.now(timezone.utc).isoformat(),
            )
        lat = _listener_field(global_position, "lat")
        lon = _listener_field(global_position, "lon")
        alt = _listener_field(global_position, "alt")
        rel_alt = max(0.0, -(local_z or 0.0))
        max_relative_alt = max(max_relative_alt, rel_alt)
        if waypoint_reached:
            post_waypoint_min_relative_alt = (
                rel_alt
                if post_waypoint_min_relative_alt is None
                else min(post_waypoint_min_relative_alt, rel_alt)
            )
        if lat is not None and lon is not None:
            # px4-listener prints vehicle_global_position lat/lon in 1e-7 degree ints.
            lat_deg = lat / 1e7 if abs(lat) > 180 else lat
            lon_deg = lon / 1e7 if abs(lon) > 180 else lon
            distance = _haversine_m(lat_deg, lon_deg, target_lat, target_lon)
            if first_distance is None:
                first_distance = distance
            min_distance = min(min_distance, distance)
            sample = {
                "elapsed_s": round(time.monotonic() - start, 3),
                "latitude_deg": round(lat_deg, 7),
                "longitude_deg": round(lon_deg, 7),
                "altitude_m": round(alt or 0.0, 3),
                "relative_alt_m": round(rel_alt, 3),
                "distance_to_target_m": round(distance, 3),
                "seq_reached": int(seq_reached) if seq_reached is not None else None,
            }
            positions.append(sample)
            _append_flight_path_trace_sample(flight_path_trace_path, sample)
            altitude.append({"elapsed_s": sample["elapsed_s"], "relative_alt_m": sample["relative_alt_m"]})
            if 1 in reached and distance <= 25.0:
                waypoint_reached = True
                if payload_release_callback is not None and payload_release_summary is None:
                    payload_release_summary = payload_release_callback()
                if landing_seq is None:
                    break
            if (
                waypoint_reached
                and landing_seq is not None
                and (
                    landing_seq in reached
                    or (
                        post_waypoint_min_relative_alt is not None
                        and post_waypoint_min_relative_alt <= 2.0
                    )
                )
            ):
                break
        time.sleep(5.0)
    duration = time.monotonic() - start
    distance_progress = max(0.0, (first_distance or min_distance) - min_distance)
    progress_speed = distance_progress / duration if duration > 0 else None
    waypoint_proximity_observed = (
        DROPOFF_WAYPOINT_SEQ in reached and min_distance <= 25.0
    )
    cruise_observed = (
        duration > 60.0
        and distance_progress >= cruise_progress_threshold_m
        and (max_relative_alt > 50.0 or waypoint_proximity_observed)
    )
    return {
        "flight_duration_s": round(duration, 3),
        "mission_item_reached_seq": tuple(sorted(reached)),
        "mission_item_reached_at_by_seq": {
            str(seq): reached_at_by_seq[seq] for seq in sorted(reached_at_by_seq)
        },
        "target_waypoint_reached_at": reached_at_by_seq.get(DROPOFF_WAYPOINT_SEQ, ""),
        "position_sample_count": len(positions),
        "distance_to_target_min_m": round(min_distance, 3),
        "distance_progress_m": round(distance_progress, 3),
        "cruise_observed": cruise_observed,
        "cruise_progress_threshold_m": round(cruise_progress_threshold_m, 3),
        "altitude_profile": tuple(altitude),
        "position_profile": tuple(positions),
        "flight_path_trace_path": str(flight_path_trace_path or ""),
        "observation_timeout_s": float(timeout_s),
        "max_relative_alt_m": round(max_relative_alt, 3),
        "payload_release_observed": bool(
            payload_release_summary
            and payload_release_summary.get("payload_release_observed")
        ),
        "payload_release_summary": dict(payload_release_summary or {}),
        "payload_release_event_source": (
            str(payload_release_summary.get("payload_release_event_source") or "")
            if payload_release_summary
            else ""
        ),
        "payload_release_observed_at": (
            str(payload_release_summary.get("payload_release_observed_at") or "")
            if payload_release_summary
            else ""
        ),
        "payload_release_position_x_m": (
            float(payload_release_summary["payload_release_position_x_m"])
            if payload_release_summary
            and payload_release_summary.get("payload_release_position_x_m") is not None
            else None
        ),
        "payload_release_position_y_m": (
            float(payload_release_summary["payload_release_position_y_m"])
            if payload_release_summary
            and payload_release_summary.get("payload_release_position_y_m") is not None
            else None
        ),
        "payload_release_position_z_m": (
            float(payload_release_summary["payload_release_position_z_m"])
            if payload_release_summary
            and payload_release_summary.get("payload_release_position_z_m") is not None
            else None
        ),
        "dropoff_vehicle_position_x_m": (
            float(payload_release_summary["dropoff_vehicle_position_x_m"])
            if payload_release_summary
            and payload_release_summary.get("dropoff_vehicle_position_x_m") is not None
            else None
        ),
        "dropoff_vehicle_position_y_m": (
            float(payload_release_summary["dropoff_vehicle_position_y_m"])
            if payload_release_summary
            and payload_release_summary.get("dropoff_vehicle_position_y_m") is not None
            else None
        ),
        "dropoff_vehicle_position_z_m": (
            float(payload_release_summary["dropoff_vehicle_position_z_m"])
            if payload_release_summary
            and payload_release_summary.get("dropoff_vehicle_position_z_m") is not None
            else None
        ),
        "landing_observed": bool(
            landing_seq is not None
            and (
                landing_seq in reached
                or (
                    post_waypoint_min_relative_alt is not None
                    and post_waypoint_min_relative_alt <= 2.0
                )
            )
        ),
        "post_waypoint_min_relative_alt_m": (
            round(post_waypoint_min_relative_alt, 3)
            if post_waypoint_min_relative_alt is not None
            else None
        ),
        **final_wind_impact_telemetry,
        "progress_speed_mps": _round_optional(progress_speed),
    }


def run_smoke() -> dict[str, Any]:
    _require_opt_in()
    fixture_e2e.CONTAINER_NAME = CONTAINER_NAME
    takeoff_smoke.fixture_e2e.CONTAINER_NAME = CONTAINER_NAME
    takeoff_smoke.CONTAINER_NAME = CONTAINER_NAME
    takeoff_smoke._ArmTakeoffUploadShim.CONTAINER_NAME = CONTAINER_NAME
    target = takeoff_smoke._build_px4_gazebo_backend()
    run_dir = RUN_ROOT / NOW.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    route_distance_m = _route_distance_m_from_env()
    route_prompt = _prompt_for_route_distance(route_distance_m)
    terrain_source = _terrain_source_from_env()
    weather_scenario = _fixed_wind_scenario_from_env()
    digital_twin = build_digital_twin_stage1_environment(
        prompt=route_prompt,
        prompt_request_ref=PROMPT_REF,
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=NOW,
        **_stage1_kwargs_for_terrain_source(terrain_source),
    )
    _assert_terrain_source_ready(
        digital_twin,
        requested_terrain_source=terrain_source,
    )
    terrain_summary = _terrain_source_summary(
        digital_twin,
        requested_terrain_source=terrain_source,
    )
    mission_item = digital_twin["digital_twin_px4_mission_item_candidate"]
    target_item = mission_item["candidate_items"][1]
    takeoff_item = mission_item["candidate_items"][0]
    weather_scenario = _resolve_source_weather_wind_scenario(
        weather_scenario,
        digital_twin=digital_twin,
        target_item=target_item,
    )
    delivery_contract = _digital_twin_delivery_contract(
        takeoff_item=takeoff_item,
        target_item=target_item,
    )
    os.environ["DIGITAL_TWIN_PX4_HOME_LAT"] = str(takeoff_item["latitude_deg"])
    os.environ["DIGITAL_TWIN_PX4_HOME_LON"] = str(takeoff_item["longitude_deg"])
    os.environ["DIGITAL_TWIN_PX4_HOME_ALT"] = str(mission_item["takeoff_terrain_elevation_m"])

    source_summary = takeoff_smoke._frp_gate_summary_for_takeoff_smoke(digital_twin)
    world_artifact = digital_twin["gazebo_world_artifact"]
    generated_world_path = ROOT_DIR / world_artifact["world_file_path_or_artifact_uri"]
    world_root = _fresh_px4_world_assets(run_dir)
    prepared_world_path = fixture_e2e._inject_digital_twin_terrain(world_root, generated_world_path)
    _enable_detachable_payload(world_root)
    wind_readiness_matrix: dict[str, Any] | None = None
    if _wind_effects_requested(weather_scenario):
        wind_readiness_matrix = _fixed_wind_readiness_matrix(
            base_world_root=world_root,
            run_dir=run_dir,
            weather=weather_scenario,
        )
    weather_summary = _enable_fixed_wind_scenario(
        world_root=world_root,
        prepared_world_path=prepared_world_path,
        weather=weather_scenario,
    )
    if wind_readiness_matrix is not None:
        weather_summary["wind_readiness_matrix"] = wind_readiness_matrix
        weather_summary["wind_readiness_ready_cases"] = wind_readiness_matrix["ready_cases"]
        weather_summary["wind_effects_active"] = "D" in wind_readiness_matrix["ready_cases"]
        weather_summary["wind_injection_applied"] = weather_summary["wind_effects_active"]
    started_at = datetime.now(timezone.utc)
    command=[]; pid=0; startup_ok=False; exit_code=0; stopped_at=None
    upload_observed=None; arm_observed=None; waypoint_observed=None; final_logs=""
    heartbeat_keepalive_process: subprocess.Popen[str] | None = None
    heartbeat_keepalive_stderr = ""
    try:
        if _wind_effects_requested(weather_summary):
            command, pid, _logs, startup_ok = _start_fixed_wind_world_bound_container(
                world_root,
                prepared_world_path,
            )
        else:
            command, pid, _logs, startup_ok = fixture_e2e._start_world_bound_container(world_root, prepared_world_path)
        if not startup_ok:
            if _wind_effects_requested(weather_summary):
                diagnostic = _fixed_wind_readiness_diagnostic()
                if wind_readiness_matrix is not None:
                    diagnostic["wind_readiness_matrix"] = wind_readiness_matrix
                raise RuntimeError(
                    "Digital Twin PX4/Gazebo fixed-wind startup failed: "
                    + json.dumps(diagnostic, sort_keys=True)
                )
            raise RuntimeError("Digital Twin PX4/Gazebo startup failed")
        items = digital_twin_candidate_upload_items(mission_item)
        upload_observed = target.upload_mission(items)
        if upload_observed.get("mission_ack_observed") is not True:
            raise RuntimeError("Digital Twin upload did not observe ACK")
        time.sleep(2.0)
        timeout_s = float(os.getenv("DIGITAL_TWIN_WAYPOINT_REACH_TIMEOUT_SECONDS", "900"))
        if target.requires_gcs_heartbeat:
            heartbeat_keepalive_process = _launch_gcs_heartbeat_keepalive(
                duration_s=timeout_s + 60.0
            )
        time.sleep(1.0)
        arm_observed = takeoff_smoke._docker_exec_arm_auto_takeoff_split(
            target=target,
        )
        waypoint_observed = _observe_waypoint(
            target_lat=float(target_item["latitude_deg"]),
            target_lon=float(target_item["longitude_deg"]),
            route_distance_m=route_distance_m,
            timeout_s=timeout_s,
            landing_seq=2 if len(mission_item["candidate_items"]) > 2 else None,
            payload_release_callback=_trigger_payload_release,
        )
        target_x, target_y = _px4_gazebo_local_xy_from_takeoff_anchor(
            takeoff_item=takeoff_item,
            target_item=target_item,
            coordinate_transform=digital_twin["coordinate_transform_candidate"],
        )
        waypoint_observed.update(
            {
                "planned_dropoff_target_x_m": target_x,
                "planned_dropoff_target_y_m": target_y,
                "planned_dropoff_target_pose_source": "takeoff_anchor_relative_wgs84_to_px4_gazebo_local",
                "dropoff_target_x_m": target_x,
                "dropoff_target_y_m": target_y,
                "dropoff_target_altitude_m": 0.0,
                "dropoff_target_pose_source": "takeoff_anchor_relative_wgs84_to_px4_gazebo_local",
            }
        )
    finally:
        if heartbeat_keepalive_process is not None:
            if heartbeat_keepalive_process.poll() is None:
                heartbeat_keepalive_process.terminate()
            try:
                _stdout, heartbeat_keepalive_stderr = heartbeat_keepalive_process.communicate(
                    timeout=5
                )
            except subprocess.TimeoutExpired:
                heartbeat_keepalive_process.kill()
                _stdout, heartbeat_keepalive_stderr = heartbeat_keepalive_process.communicate(
                    timeout=5
                )
        final_logs, exit_code = fixture_e2e._stop_world_bound_container()
        stopped_at = datetime.now(timezone.utc)
        (run_dir / "px4_gazebo.stdout.log").write_text(final_logs, encoding="utf-8")
        (run_dir / "px4_gazebo.stderr.log").write_text(
            heartbeat_keepalive_stderr,
            encoding="utf-8",
        )

    process_run = build_digital_twin_sitl_process_run_from_observed_container(
        gazebo_world_artifact=world_artifact,
        command=command,
        process_pids=(pid,),
        stdout_ref=str(run_dir / "px4_gazebo.stdout.log"),
        stderr_ref=str(run_dir / "px4_gazebo.stderr.log"),
        started_at=started_at,
        stopped_at=stopped_at,
        exit_status="terminated_after_startup_window",
        exit_code=exit_code,
        startup_error_observed=not startup_ok,
        px4_process_invoked=True,
        world_artifact_load_mode="terrain_injection_into_default_world",
        px4_loaded_world_file_path=str(prepared_world_path),
        repo_root=ROOT_DIR,
    )
    receipt = build_digital_twin_sitl_mission_upload_receipt(
        px4_mission_item_candidate=mission_item,
        sitl_process_run=process_run,
        target_endpoint=DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        operator_approved=True,
        server_opt_in=True,
        same_run_binding_ref="digital_twin_sitl_binding_gate:" + digital_twin["digital_twin_sitl_binding_gate"]["gate_id"],
        uploader=takeoff_smoke._ObservedUploader(upload_observed or {}),
        timeout_seconds=5.0,
        now=NOW,
    )
    execution_result = build_digital_twin_sitl_execution_result(
        gazebo_world_artifact=world_artifact,
        coordinate_transform_candidate=digital_twin["coordinate_transform_candidate"],
        px4_mission_item_candidate=mission_item,
        sitl_binding_gate=digital_twin["digital_twin_sitl_binding_gate"],
        sitl_process_run=process_run,
        mission_upload_receipt=receipt,
        source_backed_inputs_summary=source_summary,
        now=NOW,
    )
    package = build_flight_readiness_package(execution_result=execution_result, now=NOW)
    if arm_observed is not None:
        climb = 0.0
        import re
        match = re.search(r"Climb to\s+([0-9.]+)\s+meters above home", final_logs)
        if match:
            climb = float(match.group(1))
        if "Armed by external command" in final_logs:
            arm_observed["arm_observed"] = True
        if "Executing Mission" in final_logs:
            arm_observed["auto_mission_mode_observed"] = True
            arm_observed["mission_start_observed"] = True
        if "Takeoff detected" in final_logs:
            arm_observed["takeoff_observed"] = True
            arm_observed["altitude_rise_m"] = max(float(arm_observed.get("altitude_rise_m") or 0.0), climb)
            arm_observed["home_altitude_m"] = float(arm_observed.get("home_altitude_m") or os.environ["DIGITAL_TWIN_PX4_HOME_ALT"])
            arm_observed["takeoff_altitude_max_m"] = float(arm_observed["home_altitude_m"]) + float(arm_observed["altitude_rise_m"])
    takeoff_receipt = build_digital_twin_sitl_arm_takeoff_receipt(
        flight_readiness_package=package,
        mission_upload_receipt=receipt,
        execution_result=execution_result,
        target_endpoint=DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        operator_approved=True,
        server_opt_in=True,
        observed=arm_observed or {},
        now=NOW,
    )
    observation = build_digital_twin_sitl_waypoint_reach_observation(
        flight_readiness_package=package,
        arm_takeoff_receipt=takeoff_receipt,
        mission_upload_receipt=receipt,
        execution_result=execution_result,
        target_waypoint_seq=1,
        target_latitude_deg=float(target_item["latitude_deg"]),
        target_longitude_deg=float(target_item["longitude_deg"]),
        observed=waypoint_observed or {},
        now=NOW,
    )
    dropoff_verification_summary = _dropoff_verification_from_digital_twin_evidence(
        waypoint_observed=waypoint_observed or {},
        delivery_mission_contract=delivery_contract,
        sitl_mission_upload_receipt_ref=digital_twin_sitl_mission_upload_receipt_ref(
            receipt
        ),
        telemetry_ref=digital_twin_sitl_waypoint_reach_ref(observation),
    )
    payload_summary_for_summary = (
        waypoint_observed.get("payload_release_summary")
        if waypoint_observed
        and isinstance(waypoint_observed.get("payload_release_summary"), dict)
        else {}
    )
    summary = {
        "schema_version": observation.schema_version,
        "schema_version_expected": DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION,
        "waypoint_reach_ref": digital_twin_sitl_waypoint_reach_ref(observation),
        "flight_readiness_package_ref": flight_readiness_package_ref(package),
        "arm_takeoff_receipt_ref": digital_twin_sitl_arm_takeoff_receipt_ref(takeoff_receipt),
        "takeoff_observed": takeoff_receipt.takeoff_observed,
        "route_distance_m": route_distance_m,
        "route_mode": _route_mode(route_distance_m),
        "prompt": route_prompt,
        **terrain_summary,
        **weather_summary,
        "altitude_rise_m": takeoff_receipt.altitude_rise_m,
        "target_waypoint_seq": observation.target_waypoint_seq,
        "mission_item_reached_seq": list(observation.mission_item_reached_seq),
        "distance_to_target_min_m": observation.distance_to_target_min_m,
        "distance_progress_m": waypoint_observed.get("distance_progress_m") if waypoint_observed else None,
        "progress_speed_mps": (
            waypoint_observed.get("progress_speed_mps") if waypoint_observed else None
        ),
        **{
            key: waypoint_observed.get(key) if waypoint_observed else None
            for key in WIND_IMPACT_TELEMETRY_FIELDS
        },
        "cruise_observed": observation.cruise_observed,
        "cruise_progress_threshold_m": (
            waypoint_observed.get("cruise_progress_threshold_m")
            if waypoint_observed
            else None
        ),
        "gcs_heartbeat_keepalive_started": heartbeat_keepalive_process is not None,
        "gcs_link_loss_observed": "Connection to ground station lost" in final_logs,
        "rtl_observed": "RTL: start return" in final_logs,
        "mountain_hut_waypoint_reached": observation.mountain_hut_waypoint_reached,
        "landing_observed": bool(
            waypoint_observed
            and (
                waypoint_observed.get("landing_observed")
                or "Disarmed by landing" in final_logs
            )
        ),
        "payload_release_observed": bool(
            waypoint_observed
            and waypoint_observed.get("payload_release_observed")
        ),
        "payload_release_event_source": (
            waypoint_observed.get("payload_release_event_source")
            if waypoint_observed
            else ""
        ),
        "payload_release_observed_at": (
            waypoint_observed.get("payload_release_observed_at")
            if waypoint_observed
            else ""
        ),
        "payload_release_position_x_m": (
            waypoint_observed.get("payload_release_position_x_m")
            if waypoint_observed
            else None
        ),
        "payload_release_position_y_m": (
            waypoint_observed.get("payload_release_position_y_m")
            if waypoint_observed
            else None
        ),
        "payload_release_position_z_m": (
            waypoint_observed.get("payload_release_position_z_m")
            if waypoint_observed
            else None
        ),
        "payload_release_summary": (
            payload_summary_for_summary
        ),
        "dropoff_target_x_m": (
            _first_present(
                payload_summary_for_summary,
                "dropoff_target_x_m",
            )
            if waypoint_observed
            else None
        ),
        "dropoff_target_y_m": (
            _first_present(
                payload_summary_for_summary,
                "dropoff_target_y_m",
            )
            if waypoint_observed
            else None
        ),
        "dropoff_target_pose_source": (
            _first_present(
                payload_summary_for_summary,
                "dropoff_target_pose_source",
            )
            if waypoint_observed
            else ""
        ),
        "planned_dropoff_target_x_m": (
            waypoint_observed.get("planned_dropoff_target_x_m")
            if waypoint_observed
            else None
        ),
        "planned_dropoff_target_y_m": (
            waypoint_observed.get("planned_dropoff_target_y_m")
            if waypoint_observed
            else None
        ),
        "planned_dropoff_target_pose_source": (
            waypoint_observed.get("planned_dropoff_target_pose_source")
            if waypoint_observed
            else ""
        ),
        "dropoff_vehicle_position_x_m": (
            waypoint_observed.get("dropoff_vehicle_position_x_m")
            if waypoint_observed
            else None
        ),
        "dropoff_vehicle_position_y_m": (
            waypoint_observed.get("dropoff_vehicle_position_y_m")
            if waypoint_observed
            else None
        ),
        "dropoff_vehicle_position_z_m": (
            waypoint_observed.get("dropoff_vehicle_position_z_m")
            if waypoint_observed
            else None
        ),
        "payload_world_pose_source": (
            _first_present(payload_summary_for_summary, "payload_world_pose_source")
            if waypoint_observed
            else ""
        ),
        "payload_vehicle_offset_before_release_m": (
            payload_summary_for_summary.get("payload_vehicle_offset_before_release_m")
            if waypoint_observed
            else None
        ),
        "payload_vehicle_offset_after_release_m": (
            payload_summary_for_summary.get("payload_vehicle_offset_after_release_m")
            if waypoint_observed
            else None
        ),
        "delivery_completion_claimed": DELIVERY_COMPLETION_CLAIMED,
        "dropoff_verification_summary": dropoff_verification_summary,
        "dropoff_verification_applied": dropoff_verification_summary[
            "dropoff_verification_applied"
        ],
        "dropoff_verified": dropoff_verification_summary["dropoff_verified"],
        "dropoff_verification_status": dropoff_verification_summary[
            "dropoff_verification_status"
        ],
        "post_waypoint_min_relative_alt_m": (
            waypoint_observed.get("post_waypoint_min_relative_alt_m")
            if waypoint_observed
            else None
        ),
        "max_relative_alt_m": (
            waypoint_observed.get("max_relative_alt_m")
            if waypoint_observed
            else None
        ),
        "position_sample_count": observation.position_sample_count,
        "position_profile": list(observation.position_profile),
        "flight_path_profile": list(observation.position_profile),
        "flight_path_latest": (
            dict(observation.position_profile[-1])
            if observation.position_profile
            else {}
        ),
        "flight_path_trace_path": (
            waypoint_observed.get("flight_path_trace_path")
            if waypoint_observed
            else ""
        ),
        "observation_timeout_s": observation.observation_timeout_s,
        "blocked_reasons": list(observation.blocked_reasons),
        "hardware_target_allowed": observation.hardware_target_allowed,
        "physical_execution_invoked": observation.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": observation.approval_free_stronger_execution_allowed,
        "receipt_hash_equals_sha256": observation.receipt_hash == observation.sha256,
        "px4_log_tail": [line for line in final_logs.splitlines() if "navigator" in line or "commander" in line][-20:],
    }
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True, ensure_ascii=False))
    assert summary["takeoff_observed"] is True
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["receipt_hash_equals_sha256"] is True
    return summary


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
