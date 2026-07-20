"""Read-only mission map models derived from task artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import math

import click

from .battery_truth import battery_truth_model
from .job_status import (
    _as_bool,
    _as_float,
    _as_int,
    _first_numeric,
    _first_present,
    _task_artifacts,
    _task_record,
    _task_status,
    _terrain_profile_samples_for_watch,
)


TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "recovered", "blocked", "failed", "cancelled", "canceled"}
)


def _status_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


_FLIGHT_MAP_TRAIL_LIMIT = 4000
MISSION_MAP_POLL_INTERVAL = 1.0

MISSION_MAP_PROVIDERS: dict[str, dict[str, str]] = {
    "osm": {
        "label": "OpenStreetMap",
        "url_template": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
    },
    "gsi": {
        "label": "GSI Maps",
        "url_template": "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        "attribution": "GSI Tiles",
        "attribution_url": "https://maps.gsi.go.jp/development/ichiran.html",
    },
}


def _project_flight_points(
    points: list[tuple[float, float]],
    *,
    width: int,
    height: int,
) -> list[tuple[int, int]]:
    """Project NED points (north_x, east_y) onto a (row, col) character grid.

    North is up (smaller row), East is right (larger col). One uniform scale is
    used for both axes so geometry is not distorted; rows count double because
    terminal cells are roughly twice as tall as they are wide.
    """
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    span_x = max(xmax - xmin, 1e-6)
    span_y = max(ymax - ymin, 1e-6)
    scale = max(span_y / max(width - 1, 1), span_x / max((height - 1) * 2, 1)) or 1.0
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    projected: list[tuple[int, int]] = []
    for north_x, east_y in points:
        col = round((width - 1) / 2.0 + (east_y - cy) / scale)
        row = round((height - 1) / 2.0 - (north_x - cx) / (scale * 2.0))
        col = min(max(col, 0), width - 1)
        row = min(max(row, 0), height - 1)
        projected.append((row, col))
    return projected


def _dropoff_ned_from_route(artifacts: dict[str, Any]) -> tuple[float, float] | None:
    """Approximate dropoff position in NED metres relative to takeoff (home)."""
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    tlat = _as_float(route.get("takeoff_latitude") or route.get("takeoff_latitude_deg"))
    tlon = _as_float(route.get("takeoff_longitude") or route.get("takeoff_longitude_deg"))
    dlat = _as_float(route.get("dropoff_latitude") or route.get("dropoff_latitude_deg"))
    dlon = _as_float(route.get("dropoff_longitude") or route.get("dropoff_longitude_deg"))
    if None in (tlat, tlon, dlat, dlon):
        return None
    north = (dlat - tlat) * 111320.0
    east = (dlon - tlon) * 111320.0 * math.cos(math.radians(tlat))
    return (north, east)


def _mission_map_latlon_to_local(
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    lat: float,
    lon: float,
) -> tuple[float, float]:
    north = (lat - takeoff_lat) * 111320.0
    east = (lon - takeoff_lon) * 111320.0 * math.cos(math.radians(takeoff_lat))
    return north, east


def _mission_command_label(command: Any) -> str:
    command_id = _as_int(command)
    labels = {
        16: "waypoint",
        19: "dropoff_loiter",
        21: "land",
        22: "takeoff",
    }
    return labels.get(command_id, f"command_{command_id}") if command_id is not None else "-"


def _mission_map_planned_points(
    artifacts: dict[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
) -> list[dict[str, Any]]:
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, dict) else {}
    points: list[dict[str, Any]] = []
    mission_items = compilation.get("mission_items")
    if isinstance(mission_items, list):
        for idx, item in enumerate(mission_items):
            if not isinstance(item, dict):
                continue
            latlon = _mission_map_sample_latlon(
                item,
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
            )
            if latlon is None:
                continue
            lat, lon, source = latlon
            seq = _as_int(item.get("seq"))
            command = _as_int(item.get("command"))
            planned_source = (
                "planned_wgs84"
                if source == "observed_wgs84"
                else "planned_from_local_ned"
                if source == "estimated_from_local_ned"
                else f"planned_{source}"
            )
            points.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "source": planned_source,
                    "phase": _mission_command_label(command),
                    "seq": seq if seq is not None else idx,
                    "command": command,
                    "alt_m": _as_float(
                        item.get("altitude_m") or item.get("relative_alt_m") or item.get("z_m")
                    ),
                }
            )
    if len(points) >= 2:
        return points
    return [
        {
            "lat": takeoff_lat,
            "lon": takeoff_lon,
            "source": "planned_route_takeoff",
            "phase": "takeoff",
            "seq": 0,
            "command": None,
            "alt_m": 0.0,
        },
        {
            "lat": dropoff_lat,
            "lon": dropoff_lon,
            "source": "planned_route_dropoff",
            "phase": "dropoff",
            "seq": 1,
            "command": None,
            "alt_m": None,
        },
    ]


def _mission_obstacle_records_from_artifacts(
    artifacts: dict[str, Any],
) -> list[dict[str, Any]]:
    def obstacle_xy(record: dict[str, Any]) -> tuple[float, float] | None:
        pose = record.get("pose_readback")
        pose = pose if isinstance(pose, dict) else {}
        x_m = _first_numeric(
            record.get("x_m"),
            record.get("local_x_m"),
            record.get("x"),
            pose.get("x"),
        )
        y_m = _first_numeric(
            record.get("y_m"),
            record.get("local_y_m"),
            record.get("y"),
            pose.get("y"),
        )
        if x_m is None or y_m is None:
            return None
        return float(x_m), float(y_m)

    sources: list[tuple[str, dict[str, Any], bool | None]] = []
    direct = artifacts.get("obstacle_manifest")
    if isinstance(direct, dict):
        sources.append(
            ("obstacle_manifest", direct, _as_bool(direct.get("gazebo_obstacle_model_spawned")))
        )
    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    probe_manifest = probe.get("obstacle_manifest")
    if isinstance(probe_manifest, dict):
        sources.append(
            (
                "probe_observed.obstacle_manifest",
                probe_manifest,
                _as_bool(probe_manifest.get("gazebo_obstacle_model_spawned")),
            )
        )
    gazebo_application = probe.get("gazebo_obstacle_application")
    gazebo_application = gazebo_application if isinstance(gazebo_application, dict) else {}
    app_manifest = gazebo_application.get("obstacle_manifest")
    if isinstance(app_manifest, dict):
        sources.append(
            (
                "gazebo_obstacle_application.obstacle_manifest",
                app_manifest,
                _as_bool(
                    _first_present(
                        app_manifest.get("gazebo_obstacle_model_spawned"),
                        gazebo_application.get("gazebo_obstacle_model_spawned"),
                    )
                ),
            )
        )
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_manifest = snapshot.get("obstacle_manifest")
    if isinstance(snapshot_manifest, dict):
        sources.append(
            (
                "runtime_snapshot.obstacle_manifest",
                snapshot_manifest,
                _as_bool(snapshot_manifest.get("gazebo_obstacle_model_spawned")),
            )
        )
    snapshot_app = snapshot.get("gazebo_obstacle_application")
    snapshot_app = snapshot_app if isinstance(snapshot_app, dict) else {}
    snapshot_app_manifest = snapshot_app.get("obstacle_manifest")
    if isinstance(snapshot_app_manifest, dict):
        sources.append(
            (
                "runtime_snapshot.gazebo_obstacle_application.obstacle_manifest",
                snapshot_app_manifest,
                _as_bool(
                    _first_present(
                        snapshot_app_manifest.get("gazebo_obstacle_model_spawned"),
                        snapshot_app.get("gazebo_obstacle_model_spawned"),
                    )
                ),
            )
        )

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float]] = set()
    for source, manifest, manifest_spawned in sources:
        obstacles = manifest.get("obstacles")
        if not isinstance(obstacles, list):
            continue
        for idx, obstacle in enumerate(obstacles):
            if not isinstance(obstacle, dict):
                continue
            xy = obstacle_xy(obstacle)
            if xy is None:
                continue
            x_m, y_m = xy
            name = _status_text(obstacle.get("name"), f"obstacle_{idx}")
            key = (name, round(x_m, 2), round(y_m, 2))
            if key in seen:
                continue
            seen.add(key)
            spawned = _as_bool(obstacle.get("gazebo_obstacle_model_spawned"))
            if spawned is None:
                spawned = manifest_spawned
            records.append(
                {
                    "name": name,
                    "kind": _status_text(obstacle.get("kind"), "obstacle"),
                    "source": _status_text(obstacle.get("source"), source),
                    "source_ref": source,
                    "x_m": x_m,
                    "y_m": y_m,
                    "z_m": _as_float(obstacle.get("z_m") or obstacle.get("z")),
                    "size_x_m": _as_float(obstacle.get("size_x_m")),
                    "size_y_m": _as_float(obstacle.get("size_y_m")),
                    "size_z_m": _as_float(obstacle.get("size_z_m")),
                    "spawned": spawned,
                    "collision_enabled": _as_bool(obstacle.get("collision_enabled")),
                    "visual_only": _as_bool(obstacle.get("visual_only")),
                }
            )

    models = gazebo_application.get("models")
    if isinstance(models, list):
        for idx, model in enumerate(models):
            if not isinstance(model, dict):
                continue
            xy = obstacle_xy(model)
            if xy is None:
                continue
            x_m, y_m = xy
            name = _status_text(model.get("name"), f"gazebo_model_{idx}")
            key = (name, round(x_m, 2), round(y_m, 2))
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "name": name,
                    "kind": _status_text(model.get("kind"), "obstacle"),
                    "source": _status_text(
                        model.get("source"), "gazebo_obstacle_application.models"
                    ),
                    "source_ref": "gazebo_obstacle_application.models",
                    "x_m": x_m,
                    "y_m": y_m,
                    "z_m": _as_float(model.get("z_m") or model.get("z")),
                    "size_x_m": _as_float(model.get("size_x_m")),
                    "size_y_m": _as_float(model.get("size_y_m")),
                    "size_z_m": _as_float(model.get("size_z_m")),
                    "spawned": _as_bool(
                        _first_present(
                            model.get("pose_readback_observed"),
                            model.get("spawn_request_accepted"),
                            model.get("spawn_performed"),
                        )
                    ),
                    "collision_enabled": _as_bool(model.get("collision_enabled")),
                    "visual_only": _as_bool(model.get("visual_only")),
                }
            )

    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    if not records and _as_bool(route.get("landing_zone_blocked")) is True:
        dropoff = _dropoff_ned_from_route(artifacts)
        if dropoff is not None:
            records.append(
                {
                    "name": "landing_zone_blocked",
                    "kind": "landing_zone_risk",
                    "source": "mission_designer_coordinate_pair_route",
                    "source_ref": "route.landing_zone_blocked",
                    "x_m": dropoff[0],
                    "y_m": dropoff[1],
                    "z_m": None,
                    "size_x_m": None,
                    "size_y_m": None,
                    "size_z_m": None,
                    "spawned": False,
                }
            )
    return records


def _operator_recovery_local_maneuver_from_record(
    *,
    operator_recovery: dict[str, Any],
    snapshot: dict[str, Any],
    recovery_start: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command = operator_recovery.get("command")
    command = command if isinstance(command, dict) else {}
    request = operator_recovery.get("request")
    request = request if isinstance(request, dict) else {}

    recovery_path = _status_text(
        command.get("recovery_path") or snapshot.get("operator_recovery_path")
    )
    action = _status_text(
        command.get("action")
        or request.get("recovery_action")
        or snapshot.get("operator_recovery_action")
    )
    if "avoid_obstacle" in recovery_path:
        action = "avoid_obstacle"
    target = command.get("target")
    target = target if isinstance(target, dict) else {}
    snapshot_target = snapshot.get("operator_recovery_target")
    snapshot_target = snapshot_target if isinstance(snapshot_target, dict) else {}
    parameters = request.get("recovery_parameters")
    if not isinstance(parameters, dict):
        parameters = snapshot.get("operator_recovery_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    recovery_start = recovery_start if isinstance(recovery_start, dict) else {}
    command_start = command.get("recovery_start_position")
    command_start = command_start if isinstance(command_start, dict) else {}

    target_x = _first_numeric(
        target.get("target_x_m"),
        target.get("x_m"),
        target.get("x"),
        snapshot_target.get("target_x_m"),
        snapshot_target.get("x_m"),
        parameters.get("target_x_m"),
    )
    target_y = _first_numeric(
        target.get("target_y_m"),
        target.get("y_m"),
        target.get("y"),
        snapshot_target.get("target_y_m"),
        snapshot_target.get("y_m"),
        parameters.get("target_y_m"),
    )
    target_altitude = _first_numeric(
        command.get("target_altitude_m"),
        target.get("target_altitude_m"),
        snapshot_target.get("target_altitude_m"),
        parameters.get("target_altitude_m"),
    )
    target_z = _first_numeric(target.get("target_z_m"), snapshot_target.get("target_z_m"))
    if target_altitude is None and target_z is not None:
        target_altitude = abs(float(target_z))

    samples: list[dict[str, Any]] = []
    raw_samples = command.get("maneuver_observation_samples")
    if isinstance(raw_samples, list):
        for idx, sample in enumerate(raw_samples):
            if not isinstance(sample, dict):
                continue
            x_m = _first_numeric(sample.get("local_x_m"), sample.get("x_m"), sample.get("x"))
            y_m = _first_numeric(sample.get("local_y_m"), sample.get("y_m"), sample.get("y"))
            if x_m is None or y_m is None:
                continue
            samples.append(
                {
                    "x_m": float(x_m),
                    "y_m": float(y_m),
                    "altitude_m": _as_float(
                        sample.get("altitude_above_home_m")
                        or sample.get("relative_alt_m")
                        or sample.get("local_z_m")
                        or sample.get("z_m")
                    ),
                    "distance_to_target_m": _as_float(sample.get("distance_to_target_m")),
                    "elapsed_s": sample.get("elapsed_seconds")
                    or sample.get("elapsed_s")
                    or sample.get("sample_time_s")
                    or idx,
                    "nav_state": sample.get("nav_state"),
                }
            )

    if target_x is None and target_y is None and not samples:
        return {}
    target_point = None
    if target_x is not None and target_y is not None:
        target_point = {
            "x_m": float(target_x),
            "y_m": float(target_y),
            "altitude_m": float(target_altitude) if target_altitude is not None else None,
        }
    start_x = _first_numeric(
        command_start.get("local_x_m"),
        command_start.get("x_m"),
        command.get("first_local_x_m"),
        recovery_start.get("local_x_m"),
        recovery_start.get("x_m"),
    )
    start_y = _first_numeric(
        command_start.get("local_y_m"),
        command_start.get("y_m"),
        command.get("first_local_y_m"),
        recovery_start.get("local_y_m"),
        recovery_start.get("y_m"),
    )
    start_point = None
    if start_x is not None and start_y is not None:
        start_source = _status_text(command_start.get("source"), "")
        if not start_source and command.get("first_local_x_m") is not None:
            start_source = "dispatch_current_position_observation"
        if not start_source:
            start_source = "dispatch_revalidation_current_position"
        start_point = {
            "x_m": start_x,
            "y_m": start_y,
            "altitude_m": _first_numeric(
                command_start.get("altitude_above_home_m"),
                command.get("first_altitude_above_home_m"),
                recovery_start.get("altitude_above_home_m"),
            ),
            "source": start_source,
            "observed": _as_bool(command_start.get("observed")) is not False,
        }
    elif samples:
        start_point = {
            "x_m": samples[0]["x_m"],
            "y_m": samples[0]["y_m"],
            "altitude_m": samples[0].get("altitude_m"),
            "source": "first_saved_recovery_observation",
        }
    observation_start_gap = None
    if start_point and samples:
        first_sample = samples[0]
        start_gap_distance_m = math.hypot(
            float(first_sample["x_m"]) - float(start_point["x_m"]),
            float(first_sample["y_m"]) - float(start_point["y_m"]),
        )
        first_observation_delay_s = _as_float(first_sample.get("elapsed_s"))
        if (
            start_gap_distance_m > 10.0
            and first_observation_delay_s is not None
            and first_observation_delay_s > 5.0
        ):
            observation_start_gap = {
                "from": start_point,
                "to": first_sample,
                "reason": "recovery_observation_started_late",
                "distance_m": round(start_gap_distance_m, 3),
                "elapsed_gap_s": round(first_observation_delay_s, 3),
                "evidence_status": "not_observed_between_endpoints",
            }
    return {
        "proposal_id": _status_text(request.get("proposal_id")),
        "source_obstacle_name": _status_text(parameters.get("source_obstacle_name")),
        "action": action,
        "status": _status_text(
            command.get("status") or snapshot.get("operator_recovery_assist_status")
        ),
        "recovery_path": recovery_path,
        "start": start_point,
        "target": target_point,
        "samples": samples,
        "observation_start_gap": observation_start_gap,
        "maneuver_observation_sample_count": _as_int(
            command.get("maneuver_observation_sample_count")
        )
        or len(samples),
        "target_reached": _as_bool(
            _first_present(
                command.get("target_reached"),
                snapshot.get("operator_recovery_target_reached"),
            )
        ),
        "target_distance_m": _as_float(
            command.get("target_distance_m") or snapshot.get("operator_recovery_target_distance_m")
        ),
        "resume_auto_status": _status_text(
            command.get("resume_auto_status")
            or snapshot.get("operator_recovery_resume_auto_status")
        ),
        "source": "operator_recovery_command"
        if command
        else "missionos_auto_mission_runtime_snapshot",
    }


def _operator_recovery_local_maneuver_model(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    monitor = probe.get("monitor")
    monitor = monitor if isinstance(monitor, dict) else {}
    operator_recovery = monitor.get("operator_recovery")
    operator_recovery = operator_recovery if isinstance(operator_recovery, dict) else {}
    proposal_revalidation = artifacts.get("missionos_runtime_recovery_proposal_revalidation")
    proposal_revalidation = proposal_revalidation if isinstance(proposal_revalidation, dict) else {}
    recovery_start = proposal_revalidation.get("current_position")
    recovery_start = recovery_start if isinstance(recovery_start, dict) else {}
    if not recovery_start:
        last_proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
        last_proposal = last_proposal if isinstance(last_proposal, dict) else {}
        recovery_start = last_proposal.get("origin_position")
        recovery_start = recovery_start if isinstance(recovery_start, dict) else {}
    return _operator_recovery_local_maneuver_from_record(
        operator_recovery=operator_recovery,
        snapshot=snapshot,
        recovery_start=recovery_start,
    )


def _operator_recovery_local_maneuver_models(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every persisted, observed recovery maneuver in execution order."""

    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    monitor = probe.get("monitor")
    monitor = monitor if isinstance(monitor, dict) else {}
    attempts = monitor.get("operator_recovery_attempts")
    maneuvers: list[dict[str, Any]] = []
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            request = attempt.get("request")
            request = request if isinstance(request, dict) else {}
            if _status_text(request.get("recovery_action")) not in {
                "avoid_obstacle",
                "reroute",
                "adjust_altitude",
            }:
                continue
            maneuver = _operator_recovery_local_maneuver_from_record(
                operator_recovery=attempt,
                snapshot={},
            )
            if maneuver and maneuver.get("samples"):
                maneuvers.append(maneuver)
    if maneuvers:
        return maneuvers
    maneuver = _operator_recovery_local_maneuver_model(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    return [maneuver] if maneuver else []


def _mission_map_latlon_from_route(
    artifacts: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    takeoff_lat = _first_numeric(route.get("takeoff_latitude"), route.get("takeoff_latitude_deg"))
    takeoff_lon = _first_numeric(route.get("takeoff_longitude"), route.get("takeoff_longitude_deg"))
    dropoff_lat = _first_numeric(route.get("dropoff_latitude"), route.get("dropoff_latitude_deg"))
    dropoff_lon = _first_numeric(route.get("dropoff_longitude"), route.get("dropoff_longitude_deg"))
    if None in (takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon):
        return None
    return (
        float(takeoff_lat),
        float(takeoff_lon),
        float(dropoff_lat),
        float(dropoff_lon),
    )


def _mission_map_local_to_latlon(
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    north_m: float,
    east_m: float,
) -> tuple[float, float]:
    lat = takeoff_lat + north_m / 111320.0
    lon_scale = max(1e-9, 111320.0 * math.cos(math.radians(takeoff_lat)))
    lon = takeoff_lon + east_m / lon_scale
    return lat, lon


def _mission_map_sample_latlon(
    sample: dict[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
) -> tuple[float, float, str] | None:
    lat = _first_numeric(
        sample.get("latitude_deg"),
        sample.get("global_latitude_deg"),
        sample.get("lat"),
        sample.get("latitude"),
    )
    lon = _first_numeric(
        sample.get("longitude_deg"),
        sample.get("global_longitude_deg"),
        sample.get("lon"),
        sample.get("longitude"),
    )
    if lat is not None and lon is not None:
        return float(lat), float(lon), "observed_wgs84"
    north = _first_numeric(sample.get("local_x_m"), sample.get("x_m"), sample.get("x"))
    east = _first_numeric(sample.get("local_y_m"), sample.get("y_m"), sample.get("y"))
    if north is None or east is None:
        return None
    lat, lon = _mission_map_local_to_latlon(
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        north_m=float(north),
        east_m=float(east),
    )
    return lat, lon, "estimated_from_local_ned"


def _mission_map_flight_samples(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    for key in (
        "missionos_auto_mission_runtime_replay",
        "auto_mission_runtime_replay",
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        "mission_designer_live_telemetry_snapshot",
    ):
        candidate = artifacts.get(key)
        candidate = candidate if isinstance(candidate, dict) else {}
        for samples_key in (
            "flight_path_profile",
            "position_profile",
            "route_preview_waypoints",
        ):
            samples = candidate.get(samples_key)
            if isinstance(samples, list) and samples:
                return [sample for sample in samples if isinstance(sample, dict)]
    return []


def _mission_map_live_trajectory_samples(
    artifacts: dict[str, Any],
) -> list[dict[str, Any]]:
    trajectory = artifacts.get("missionos_auto_mission_live_trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    samples = trajectory.get("samples")
    if not isinstance(samples, list):
        return []
    return [dict(sample) for sample in samples if isinstance(sample, dict)]


def _mission_map_observed_segments(
    *,
    artifacts: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> list[list[dict[str, Any]]]:
    """Build observed path segments without joining unobserved gaps."""

    return [
        list(segment.get("points") or [])
        for segment in _mission_map_observed_trace(
            artifacts=artifacts,
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
        )["segments"]
    ]


def _mission_map_observed_trace(
    *,
    artifacts: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> dict[str, Any]:
    """Build source-backed trajectory segments and explicit observation gaps.

    The bounded runtime replay can preserve a terminal point while omitting the
    Recovery and rejoin interval before it.  When the durable live trajectory
    covers the replay sample range and contains more observations, it is the
    stronger display source.  Missing intervals are represented separately;
    their endpoints must never become a solid observed path.
    """

    segments: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    def point(sample: dict[str, Any], index: int, source_prefix: str) -> dict[str, Any] | None:
        latlon = _mission_map_sample_latlon(
            sample,
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
        )
        if latlon is None:
            return None
        lat, lon, source = latlon
        return {
            "lat": lat,
            "lon": lon,
            "source": f"{source_prefix}:{source}",
            "phase": _status_text(sample.get("phase"), f"sample_{index}"),
            "alt_m": _as_float(
                sample.get("relative_alt_m")
                or sample.get("altitude_above_home_m")
                or sample.get("local_z_m")
                or sample.get("z_m")
                or sample.get("z")
            ),
            "elapsed_s": sample.get("elapsed_s")
            or sample.get("elapsed_seconds")
            or sample.get("sample_time_s")
            or sample.get("sample_index"),
            "sample_index": sample.get("sample_index"),
            "segment_index": sample.get("segment_index"),
            "segment_break_reason": _status_text(sample.get("segment_break_reason"), ""),
        }

    replay_samples = _mission_map_flight_samples(artifacts)
    live_samples = _mission_map_live_trajectory_samples(artifacts)

    def numeric_indices(samples: list[dict[str, Any]]) -> list[int]:
        return [
            int(value)
            for sample in samples
            if isinstance((value := sample.get("sample_index")), (int, float))
        ]

    replay_indices = numeric_indices(replay_samples)
    live_indices = numeric_indices(live_samples)
    live_covers_replay = bool(
        live_samples
        and replay_samples
        and len(live_samples) > len(replay_samples)
        and live_indices
        and replay_indices
        and min(live_indices) <= min(replay_indices)
        and max(live_indices) >= max(replay_indices)
    )
    streams: list[tuple[str, list[dict[str, Any]]]] = []
    if live_covers_replay or (live_samples and not replay_samples):
        streams.append(("live_trajectory", live_samples))
        trace_source = "missionos_auto_mission_live_trajectory"
    else:
        if replay_samples:
            streams.append(("runtime_replay", replay_samples))
        max_replay_index = max(replay_indices) if replay_indices else None
        later_live_samples = [
            sample
            for sample in live_samples
            if max_replay_index is None
            or not isinstance(sample.get("sample_index"), (int, float))
            or int(sample["sample_index"]) > max_replay_index
        ]
        if later_live_samples:
            streams.append(("live_trajectory", later_live_samples))
        trace_source = "runtime_replay_with_later_live_segments" if streams else "unavailable"

    def point_distance_m(left: dict[str, Any], right: dict[str, Any]) -> float:
        north_m = (float(right["lat"]) - float(left["lat"])) * 111320.0
        lon_scale = 111320.0 * math.cos(math.radians(takeoff_lat))
        east_m = (float(right["lon"]) - float(left["lon"])) * lon_scale
        return math.hypot(north_m, east_m)

    def elapsed_gap_s(left: dict[str, Any], right: dict[str, Any]) -> float | None:
        left_elapsed = _as_float(left.get("elapsed_s"))
        right_elapsed = _as_float(right.get("elapsed_s"))
        if left_elapsed is None or right_elapsed is None:
            return None
        return max(0.0, right_elapsed - left_elapsed)

    previous_stream_last: dict[str, Any] | None = None
    for source, samples in streams:
        current_points: list[dict[str, Any]] = []
        current_segment_index: int | None = None
        current_break_reason = ""

        def finish_segment() -> None:
            nonlocal current_points, current_break_reason
            if not current_points:
                return
            segments.append(
                {
                    "points": current_points,
                    "source": source,
                    "segment_index": current_segment_index,
                    "break_reason": current_break_reason,
                }
            )
            current_points = []
            current_break_reason = ""

        for index, sample in enumerate(samples):
            mapped = point(sample, index, source)
            if mapped is None:
                continue
            mapped_segment_index = (
                int(sample["segment_index"])
                if isinstance(sample.get("segment_index"), (int, float))
                else None
            )
            previous = current_points[-1] if current_points else previous_stream_last
            source_transition = bool(previous is not None and not current_points)
            explicit_break = bool(sample.get("segment_break_reason")) or bool(
                current_points
                and mapped_segment_index is not None
                and current_segment_index is not None
                and mapped_segment_index != current_segment_index
            )
            distance_m = point_distance_m(previous, mapped) if previous else 0.0
            gap_seconds = elapsed_gap_s(previous, mapped) if previous else None
            inferred_gap = bool(
                previous and gap_seconds is not None and gap_seconds > 5.0 and distance_m > 10.0
            )
            if explicit_break or inferred_gap or source_transition:
                if current_points:
                    finish_segment()
                reason = _status_text(sample.get("segment_break_reason"), "")
                if not reason:
                    reason = (
                        "source_transition_not_observed"
                        if source_transition
                        else "telemetry_time_and_distance_gap"
                    )
                current_break_reason = reason
                if previous and (inferred_gap or distance_m > 10.0):
                    gaps.append(
                        {
                            "from": previous,
                            "to": mapped,
                            "reason": reason,
                            "distance_m": round(distance_m, 3),
                            "elapsed_gap_s": (
                                round(gap_seconds, 3) if gap_seconds is not None else None
                            ),
                            "from_sample_index": previous.get("sample_index"),
                            "to_sample_index": mapped.get("sample_index"),
                            "evidence_status": "not_observed_between_endpoints",
                        }
                    )
            if not current_points:
                current_segment_index = mapped_segment_index
            current_points.append(mapped)
        finish_segment()
        if segments:
            points = segments[-1].get("points") or []
            if points:
                previous_stream_last = points[-1]

    return {
        "segments": segments,
        "gaps": gaps,
        "source": trace_source,
        "live_trajectory_preferred": live_covers_replay,
    }


def _mission_map_battery_model(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Return battery display truth with provenance and reset detection."""

    return battery_truth_model(snapshot=snapshot, artifacts=artifacts)


def _mission_map_recovery_provenance(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Expose source-backed recovery provenance for read-only map evidence."""

    proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
    proposal = proposal if isinstance(proposal, dict) else {}
    origin = proposal.get("proposal_origin")
    origin = origin if isinstance(origin, dict) else {}
    dispatch = artifacts.get("missionos_runtime_recovery_dispatch_request")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    attempt = artifacts.get("missionos_runtime_recovery_last_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    safety = attempt.get("resume_safety_verification")
    safety = safety if isinstance(safety, dict) else {}
    if not any((proposal, dispatch, attempt)):
        return {}
    return {
        "proposal_id": proposal.get("proposal_id") or dispatch.get("proposal_id"),
        "origin_kind": origin.get("origin_kind"),
        "provider": origin.get("provider"),
        "model_id": origin.get("model_id"),
        "invocation_kind": origin.get("invocation_kind"),
        "proposal_origin_sha256": proposal.get("proposal_origin_sha256")
        or dispatch.get("proposal_origin_sha256"),
        "operator_approved": _as_bool(dispatch.get("operator_approved")) is True,
        "explicit_recovery_dispatch_approval": (
            _as_bool(dispatch.get("explicit_recovery_dispatch_approval")) is True
        ),
        "approval_ref": dispatch.get("approval_ref"),
        "recovery_action": attempt.get("recovery_action") or dispatch.get("recovery_action"),
        "target_reached": _as_bool(attempt.get("target_reached")) is True,
        "resume_status": attempt.get("resume_status"),
        "resume_mission_current_seq": _as_int(safety.get("resume_mission_current_seq")),
        "resume_mission_seq_after_obstacle": _as_int(
            safety.get("resume_mission_seq_after_obstacle")
        ),
        "resume_mission_current_seq_observed": (
            _as_bool(safety.get("resume_mission_current_seq_observed")) is True
        ),
        "simulator_execution_observed": (
            _as_bool(attempt.get("simulator_execution_observed")) is True
        ),
        "source_refs": [
            "missionos_runtime_recovery_last_proposal.proposal_origin",
            "missionos_runtime_recovery_dispatch_request",
            "missionos_runtime_recovery_last_attempt.resume_safety_verification",
        ],
        "claim_boundary": (
            "Recovery provenance is display evidence only. Proposal, approval, "
            "dispatch, execution, and verification remain distinct source facts."
        ),
    }


def _mission_map_obstacles(
    artifacts: dict[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
) -> list[dict[str, Any]]:
    obstacles: list[dict[str, Any]] = []
    for record in _mission_obstacle_records_from_artifacts(artifacts):
        x_m = _as_float(record.get("x_m"))
        y_m = _as_float(record.get("y_m"))
        if x_m is None or y_m is None:
            continue
        lat, lon = _mission_map_local_to_latlon(
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
            north_m=x_m,
            east_m=y_m,
        )
        half_x_m = (_as_float(record.get("size_x_m")) or 0.0) / 2.0
        half_y_m = (_as_float(record.get("size_y_m")) or 0.0) / 2.0
        footprint = []
        if half_x_m > 0.0 and half_y_m > 0.0:
            for corner_x_m, corner_y_m in (
                (x_m - half_x_m, y_m - half_y_m),
                (x_m - half_x_m, y_m + half_y_m),
                (x_m + half_x_m, y_m + half_y_m),
                (x_m + half_x_m, y_m - half_y_m),
            ):
                corner_lat, corner_lon = _mission_map_local_to_latlon(
                    takeoff_lat=takeoff_lat,
                    takeoff_lon=takeoff_lon,
                    north_m=corner_x_m,
                    east_m=corner_y_m,
                )
                footprint.append({"lat": corner_lat, "lon": corner_lon})
        obstacle_z_m = _as_float(record.get("z_m"))
        obstacle_height_m = _as_float(record.get("size_z_m"))
        obstacles.append(
            {
                **record,
                "lat": lat,
                "lon": lon,
                "footprint": footprint,
                "top_altitude_m": (
                    obstacle_z_m + obstacle_height_m / 2.0
                    if obstacle_z_m is not None and obstacle_height_m is not None
                    else obstacle_height_m
                ),
                "source": _status_text(record.get("source")),
                "coincident_with_dropoff": (
                    math.hypot(
                        (lat - dropoff_lat) * 111320.0,
                        (lon - dropoff_lon) * 111320.0 * math.cos(math.radians(takeoff_lat)),
                    )
                    <= max(
                        3.0,
                        (_as_float(record.get("size_x_m")) or 0.0) / 2.0,
                        (_as_float(record.get("size_y_m")) or 0.0) / 2.0,
                    )
                ),
            }
        )
    return obstacles


def _mission_map_maneuver_from_local(
    *,
    maneuver: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> dict[str, Any]:
    if not maneuver:
        return {}
    samples: list[dict[str, Any]] = []
    for sample in maneuver.get("samples") or []:
        x_m = _as_float(sample.get("x_m"))
        y_m = _as_float(sample.get("y_m"))
        if x_m is None or y_m is None:
            continue
        lat, lon = _mission_map_local_to_latlon(
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
            north_m=x_m,
            east_m=y_m,
        )
        samples.append({**sample, "lat": lat, "lon": lon})
    target = maneuver.get("target")
    target_point = None
    if isinstance(target, dict):
        x_m = _as_float(target.get("x_m"))
        y_m = _as_float(target.get("y_m"))
        if x_m is not None and y_m is not None:
            lat, lon = _mission_map_local_to_latlon(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                north_m=x_m,
                east_m=y_m,
            )
            target_point = {**target, "lat": lat, "lon": lon}
    start = maneuver.get("start")
    start_point = None
    if isinstance(start, dict):
        x_m = _as_float(start.get("x_m"))
        y_m = _as_float(start.get("y_m"))
        if x_m is not None and y_m is not None:
            lat, lon = _mission_map_local_to_latlon(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                north_m=x_m,
                east_m=y_m,
            )
            start_point = {**start, "lat": lat, "lon": lon}
    observation_start_gap = maneuver.get("observation_start_gap")
    converted_start_gap = None
    if isinstance(observation_start_gap, dict):
        gap_from = observation_start_gap.get("from")
        gap_to = observation_start_gap.get("to")

        def converted_gap_point(raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None
            x_m = _as_float(raw.get("x_m"))
            y_m = _as_float(raw.get("y_m"))
            if x_m is None or y_m is None:
                return None
            lat, lon = _mission_map_local_to_latlon(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                north_m=x_m,
                east_m=y_m,
            )
            return {**raw, "lat": lat, "lon": lon}

        converted_from = converted_gap_point(gap_from)
        converted_to = converted_gap_point(gap_to)
        if converted_from and converted_to:
            converted_start_gap = {
                **observation_start_gap,
                "from": converted_from,
                "to": converted_to,
            }
    return {
        **maneuver,
        "start": start_point,
        "target": target_point,
        "samples": samples,
        "observation_start_gap": converted_start_gap,
    }


def _mission_map_maneuvers(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> list[dict[str, Any]]:
    return [
        converted
        for maneuver in _operator_recovery_local_maneuver_models(
            artifacts=artifacts,
            snapshot=snapshot,
        )
        if (
            converted := _mission_map_maneuver_from_local(
                maneuver=maneuver,
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
            )
        )
    ]


def _mission_map_maneuver(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> dict[str, Any]:
    maneuvers = _mission_map_maneuvers(
        artifacts=artifacts,
        snapshot=snapshot,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
    )
    return maneuvers[-1] if maneuvers else {}


def _mission_map_telemetry_model(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    agl = _as_float(snapshot.get("terrain_clearance_m"))
    agl_target = _as_float(snapshot.get("terrain_clearance_target_m"))
    agl_margin = _as_float(snapshot.get("terrain_clearance_margin_m"))
    samples, _planned_route_m = _terrain_profile_samples_for_watch(artifacts)
    first_terrain = samples[0]["terrain_elevation_m"] if samples else None
    current_amsl = (
        terrain + agl
        if terrain is not None and agl is not None
        else first_terrain + alt_home
        if first_terrain is not None and alt_home is not None
        else None
    )
    destination_target_amsl = next(
        (
            sample.get("target_amsl_m")
            for sample in reversed(samples)
            if sample.get("target_amsl_m") is not None
        ),
        None,
    )
    climb_to_destination = (
        destination_target_amsl - current_amsl
        if destination_target_amsl is not None and current_amsl is not None
        else None
    )
    return {
        "altitude_amsl_m": current_amsl,
        "home_relative_altitude_m": alt_home,
        "terrain_elevation_amsl_m": terrain,
        "agl_m": agl,
        "agl_target_m": agl_target,
        "agl_margin_m": agl_margin,
        "agl_status": _status_text(snapshot.get("terrain_clearance_status")),
        "destination_target_amsl_m": destination_target_amsl,
        "climb_to_destination_m": climb_to_destination,
    }


def _mission_map_weather_model(artifacts: dict[str, Any]) -> dict[str, Any]:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    keys = (
        "wind_speed_mps",
        "wind_direction_deg",
        "wind_gust_mps",
        "wind_variance",
        "temperature_c",
        "pressure_hpa",
        "precipitation_mm_per_hour",
    )
    if not any(route.get(key) not in (None, "") for key in keys):
        return {}
    return {
        "wind_speed_mps": _as_float(route.get("wind_speed_mps")),
        "wind_direction_deg": _as_float(route.get("wind_direction_deg")),
        "wind_gust_mps": _as_float(route.get("wind_gust_mps")),
        "wind_variance": _status_text(route.get("wind_variance")),
        "temperature_c": _as_float(route.get("temperature_c")),
        "pressure_hpa": _as_float(route.get("pressure_hpa")),
        "precipitation_mm_per_hour": _as_float(route.get("precipitation_mm_per_hour")),
    }


def _turtlebot3_indoor_map_model_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    # Progress callbacks update summary first while a synchronous Nav2 dispatch
    # is still running. Prefer that newest copy; the top-level artifact remains
    # the previous stable snapshot until finalization.
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    summary_embedded = summary.get("turtlebot3_indoor_map_model")
    if isinstance(summary_embedded, dict):
        return _repair_turtlebot3_indoor_map_display_alignment(dict(summary_embedded))
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    embedded = execution.get("turtlebot3_indoor_map_model")
    if isinstance(embedded, dict):
        return _repair_turtlebot3_indoor_map_display_alignment(dict(embedded))
    direct = artifacts.get("turtlebot3_indoor_map_model")
    if isinstance(direct, dict):
        return _repair_turtlebot3_indoor_map_display_alignment(dict(direct))
    return {}


def _normalize_turtlebot_robot_profile(raw: Any) -> str:
    profile = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if profile in {"turtlebot4", "tb4"}:
        return "turtlebot4"
    if profile in {"nova_carter", "novacarter", "carter", "isaac_carter"}:
        return "nova_carter"
    if profile in {"turtlebot3", "tb3"}:
        return "turtlebot3"
    return ""


def _turtlebot_robot_profile_from_artifacts(artifacts: dict[str, Any]) -> str:
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
    for source in (summary, execution, indoor_map):
        profile = _normalize_turtlebot_robot_profile(source.get("robot_profile"))
        if profile:
            return profile
    for source in (summary, execution, indoor_map):
        target = str(source.get("execution_target") or "").strip().lower()
        if target == "ros2_nav2_turtlebot4_sim":
            return "turtlebot4"
        if target == "isaac_ros_nav2_nova_carter_sim":
            return "nova_carter"
        if target == "ros2_nav2_turtlebot3_sim":
            return "turtlebot3"
    return "turtlebot3" if indoor_map else ""


def _turtlebot_robot_label_from_profile(profile: str) -> str:
    normalized = _normalize_turtlebot_robot_profile(profile)
    if normalized == "turtlebot4":
        return "TurtleBot4"
    if normalized == "nova_carter":
        return "Nova Carter"
    return "TurtleBot3"


def _turtlebot_robot_label_from_artifacts(artifacts: dict[str, Any]) -> str:
    return _turtlebot_robot_label_from_profile(_turtlebot_robot_profile_from_artifacts(artifacts))


def _turtlebot3_recovery_candidate_resolution_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Return the candidate resolution bound to the newest task summary.

    Recovery revisions update the summary/execution copies atomically while the
    top-level copy may still describe the superseded checkpoint. Presence of an
    empty current value is meaningful (for example, a return-home follow-up), so
    it must suppress rather than fall through to an older direct artifact.
    """

    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    revision_geometry = checkpoint.get("recovery_revision_geometry")
    revision_geometry = revision_geometry if isinstance(revision_geometry, dict) else {}
    for owner in (summary, execution, revision_geometry, artifacts):
        if "recovery_candidate_resolution" not in owner:
            continue
        resolution = owner.get("recovery_candidate_resolution")
        return dict(resolution) if isinstance(resolution, dict) else {}
    return {}


def _turtlebot3_xy(point: dict[str, Any], *, raw: bool = False) -> tuple[float, float] | None:
    x_key = "raw_x_m" if raw else "x_m"
    y_key = "raw_y_m" if raw else "y_m"
    x = _as_float(point.get(x_key))
    y = _as_float(point.get(y_key))
    if x is None or y is None:
        return None
    return x, y


def _turtlebot3_indoor_map_dropoff_xy(
    indoor_map: dict[str, Any],
) -> tuple[float, float] | None:
    planned = indoor_map.get("planned_points")
    if not isinstance(planned, list):
        return None
    candidates = [point for point in planned if isinstance(point, dict)]
    for point in reversed(candidates):
        label = " ".join(
            _status_text(point.get(key)).lower()
            for key in ("phase", "label", "name", "kind", "role")
        )
        if "dropoff" in label:
            xy = _turtlebot3_xy(point)
            if xy is not None:
                return xy
    for point in reversed(candidates):
        xy = _turtlebot3_xy(point)
        if xy is not None:
            return xy
    return None


def _repair_turtlebot3_indoor_map_points(
    points: Any,
) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    if not isinstance(points, list):
        return repaired
    for point in points:
        if not isinstance(point, dict):
            continue
        next_point = dict(point)
        raw_xy = _turtlebot3_xy(next_point, raw=True)
        if raw_xy is not None:
            next_point["x_m"], next_point["y_m"] = raw_xy
            next_point["frame_id"] = "map"
            next_point["display_alignment_applied"] = False
            next_point["display_alignment_repaired_from_raw_fields"] = True
        repaired.append(next_point)
    segment_counts: dict[str, int] = {}
    for point in repaired:
        segment_ref = _status_text(point.get("segment_ref"))
        if segment_ref:
            segment_counts[segment_ref] = segment_counts.get(segment_ref, 0) + 1
    pruned: list[dict[str, Any]] = []
    for point in repaired:
        segment_ref = _status_text(point.get("segment_ref"))
        sample_index = _as_float(point.get("sample_index"))
        if (
            point.get("display_alignment_repaired_from_raw_fields") is True
            and segment_counts.get(segment_ref, 0) > 1
            and sample_index == 0
        ):
            continue
        pruned.append(point)
    return pruned


def _repair_turtlebot3_indoor_map_display_alignment(
    indoor_map: dict[str, Any],
) -> dict[str, Any]:
    """Display-only repair for older maps that aligned map-frame samples as odom.

    Some saved TurtleBot3 artifacts contain mixed odom/map trajectory samples.
    Older display code aligned the whole observed path to home when the first
    sample was odom, which also shifted later map-frame samples. When the raw
    coordinates are clearly closer to the planned dropoff than the displayed
    coordinates, recover the read-only map from the preserved raw fields.
    """

    alignment = indoor_map.get("display_alignment")
    if not isinstance(alignment, dict):
        return indoor_map
    if alignment.get("method") != "first_observed_pose_to_planned_home":
        return indoor_map
    if alignment.get("applied") is not True:
        return indoor_map
    observed = indoor_map.get("observed_points")
    if not isinstance(observed, list) or not observed:
        return indoor_map
    observed_points = [point for point in observed if isinstance(point, dict)]
    if not observed_points:
        return indoor_map
    latest_observed = observed_points[-1]
    latest_display_xy = _turtlebot3_xy(latest_observed)
    latest_raw_xy = _turtlebot3_xy(latest_observed, raw=True)
    dropoff_xy = _turtlebot3_indoor_map_dropoff_xy(indoor_map)
    if latest_display_xy is None or latest_raw_xy is None or dropoff_xy is None:
        return indoor_map
    display_distance = math.hypot(
        latest_display_xy[0] - dropoff_xy[0],
        latest_display_xy[1] - dropoff_xy[1],
    )
    raw_distance = math.hypot(
        latest_raw_xy[0] - dropoff_xy[0],
        latest_raw_xy[1] - dropoff_xy[1],
    )
    if display_distance < 0.5 or raw_distance + 0.25 >= display_distance:
        return indoor_map

    repaired = dict(indoor_map)
    repaired_observed = _repair_turtlebot3_indoor_map_points(observed_points)
    repaired["observed_points"] = repaired_observed
    recovery = repaired.get("recovery")
    if isinstance(recovery, dict):
        repaired_recovery = dict(recovery)
        repaired_recovery["observed_points"] = _repair_turtlebot3_indoor_map_points(
            recovery.get("observed_points")
        )
        repaired["recovery"] = repaired_recovery
    if repaired_observed:
        latest = dict(repaired_observed[-1])
        latest["source"] = _status_text(
            latest.get("source"),
            "ros2_nav2_bridge_trajectory_samples",
        )
        repaired["current_pose"] = latest
    repaired["display_alignment"] = {
        "applied": False,
        "method": "map_frame_samples_recovered_from_raw_fields",
        "dx_m": 0.0,
        "dy_m": 0.0,
        "previous_method": "first_observed_pose_to_planned_home",
        "previous_dx_m": alignment.get("dx_m"),
        "previous_dy_m": alignment.get("dy_m"),
        "repaired_display_only": True,
        "claim_boundary": (
            "Display-only recovery for older TurtleBot3 indoor maps that "
            "preserved map-frame raw coordinates after odom-origin alignment. "
            "This does not rewrite runtime evidence or change completion claims."
        ),
    }
    return repaired


def _overlay_turtlebot3_live_telemetry(
    indoor_map: dict[str, Any],
    *,
    artifacts: dict[str, Any],
    trail: list[dict[str, Any]],
    alignment_state: dict[str, Any],
    freeze_live_preview: bool = False,
) -> dict[str, Any]:
    """Add a response-only live odom preview without changing artifact evidence.

    ``trail`` belongs to the current watch/map process. A terminal response may
    freeze that process-local trail for operator orientation, but the preview is
    never written back to the task, final artifact, or verifier input. A new
    process (including a page reload) starts with an empty trail and therefore
    shows only persisted observed/recovery evidence.
    """

    telemetry = artifacts.get("turtlebot3_live_telemetry")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    raw_position = telemetry.get("raw_odom_position")
    raw_position = raw_position if isinstance(raw_position, dict) else {}
    raw_x = _as_float(raw_position.get("x_m"))
    raw_y = _as_float(raw_position.get("y_m"))
    if raw_x is None or raw_y is None:
        if freeze_live_preview and trail:
            frozen = dict(indoor_map)
            frozen["live_display_points"] = [dict(item) for item in trail]
            live_path_length_m = sum(
                math.hypot(
                    float(end.get("raw_x_m") or 0.0) - float(start.get("raw_x_m") or 0.0),
                    float(end.get("raw_y_m") or 0.0) - float(start.get("raw_y_m") or 0.0),
                )
                for start, end in zip(trail, trail[1:])
            )
            frozen["live_telemetry"] = {
                "telemetry_status": "ended",
                "display_path_length_m": round(live_path_length_m, 6),
                "display_path_length_only": True,
                "display_only": True,
                "evidence_status": "not_evidence",
                "persistence": "process_local_response_overlay_only",
            }
            frozen["live_display_alignment"] = {
                "method": "latest_artifact_pose_plus_live_odom_delta",
                "display_only": True,
                "evidence_status": "not_evidence",
                "persistence": "process_local_response_overlay_only",
                "claim_boundary": (
                    "Live preview ended. This frozen process-local projection "
                    "is not persisted, is not verifier input, and is not "
                    "trajectory evidence."
                ),
            }
            return frozen
        return indoor_map
    observed = indoor_map.get("observed_points")
    observed = observed if isinstance(observed, list) else []
    artifact_count = len(observed)
    if (
        alignment_state.get("artifact_observed_count") != artifact_count
        or "raw_anchor_x_m" not in alignment_state
    ):
        anchor = indoor_map.get("current_pose")
        anchor = anchor if isinstance(anchor, dict) else {}
        if not anchor and observed and isinstance(observed[-1], dict):
            anchor = observed[-1]
        map_x = _as_float(anchor.get("x_m"))
        map_y = _as_float(anchor.get("y_m"))
        if map_x is None or map_y is None:
            planned = indoor_map.get("planned_points")
            planned = planned if isinstance(planned, list) else []
            home = planned[0] if planned and isinstance(planned[0], dict) else {}
            map_x = _as_float(home.get("x_m")) or 0.0
            map_y = _as_float(home.get("y_m")) or 0.0
        alignment_state.update(
            {
                "artifact_observed_count": artifact_count,
                "raw_anchor_x_m": raw_x,
                "raw_anchor_y_m": raw_y,
                "map_anchor_x_m": map_x,
                "map_anchor_y_m": map_y,
            }
        )
    display_x = (
        float(alignment_state["map_anchor_x_m"]) + raw_x - float(alignment_state["raw_anchor_x_m"])
    )
    display_y = (
        float(alignment_state["map_anchor_y_m"]) + raw_y - float(alignment_state["raw_anchor_y_m"])
    )
    point = {
        "x_m": display_x,
        "y_m": display_y,
        "raw_x_m": raw_x,
        "raw_y_m": raw_y,
        "raw_frame_id": str(telemetry.get("frame_id") or "odom"),
        "frame_id": "map",
        "captured_at": telemetry.get("captured_at"),
        "source": "ros2_telemetry_sidecar_live_display",
        "display_only": True,
        "evidence_status": "not_evidence",
        "persistence": "process_local_response_overlay_only",
        "display_alignment_applied": True,
        "display_alignment_method": "latest_artifact_pose_plus_live_odom_delta",
    }
    if not trail or (trail[-1].get("raw_x_m"), trail[-1].get("raw_y_m")) != (raw_x, raw_y):
        trail.append(point)
        if len(trail) > _FLIGHT_MAP_TRAIL_LIMIT:
            del trail[: len(trail) - _FLIGHT_MAP_TRAIL_LIMIT]
    overlaid = dict(indoor_map)
    overlaid["live_display_points"] = [dict(item) for item in trail]
    live_path_length_m = sum(
        math.hypot(
            float(end.get("raw_x_m") or 0.0) - float(start.get("raw_x_m") or 0.0),
            float(end.get("raw_y_m") or 0.0) - float(start.get("raw_y_m") or 0.0),
        )
        for start, end in zip(trail, trail[1:])
    )
    overlaid["live_telemetry"] = {
        **dict(telemetry),
        "telemetry_status": ("ended" if freeze_live_preview else telemetry.get("telemetry_status")),
        "display_path_length_m": round(live_path_length_m, 6),
        "display_path_length_only": True,
        "display_only": True,
        "evidence_status": "not_evidence",
        "persistence": "process_local_response_overlay_only",
    }
    overlaid["live_display_alignment"] = {
        "method": "latest_artifact_pose_plus_live_odom_delta",
        "display_only": True,
        "evidence_status": "not_evidence",
        "persistence": "process_local_response_overlay_only",
        "claim_boundary": (
            "Live odom preview is a process-local projection for operator "
            "orientation. It is not persisted, is not verifier input, and does "
            "not alter stored trajectory evidence or completion claims."
        ),
    }
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    recovery = overlaid.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, dict) else {}
    checkpoint_status = str(checkpoint.get("checkpoint_status") or "")
    candidate_resolution = _turtlebot3_recovery_candidate_resolution_from_artifacts(artifacts)
    runtime_status = (
        "awaiting_operator_approval"
        if checkpoint_status == "awaiting_operator_approval"
        else "approved_recovery_and_route_in_progress"
        if checkpoint_status == "dispatching"
        else "recovery_completed_and_route_completed"
        if checkpoint_status == "consumed" and summary.get("route_completed_after_recovery") is True
        else "recovery_failed"
        if checkpoint_status in {"failed", "dispatch_unknown"}
        else "not_triggered"
    )
    recovery["runtime_status"] = runtime_status
    recovery["selected_action"] = checkpoint.get("selected_action")
    recovery["checkpoint_status"] = checkpoint_status or None
    recovery["route_segment_completion_count"] = summary.get("segment_completion_count")
    recovery["route_segment_planned_count"] = summary.get("planned_segment_count")
    recovery["recovery_completion_claimed"] = summary.get("recovery_completion_claimed")
    recovery["route_resumed_after_recovery"] = summary.get("route_resumed_after_recovery")
    recovery["goal_status"] = summary.get("recovery_goal_status") or recovery.get("goal_status")
    recovery["verification_status"] = summary.get("recovery_verification_status") or recovery.get(
        "verification_status"
    )
    recovery["route_resume_status"] = summary.get("route_resume_status") or recovery.get(
        "route_resume_status"
    )
    selected_candidate = candidate_resolution.get("selected_candidate")
    selected_candidate = selected_candidate if isinstance(selected_candidate, dict) else {}
    recovery["candidate_resolution_status"] = candidate_resolution.get("resolution_status")
    recovery["candidate_id"] = selected_candidate.get("candidate_id")
    recovery["candidate_path_length_m"] = selected_candidate.get("path_length_m")
    overlaid["recovery"] = recovery
    return overlaid


def _mission_indoor_map_model(
    *,
    task_payload: dict[str, Any],
    indoor_map: dict[str, Any],
    live_task_url: str | None,
    poll_interval: float,
) -> dict[str, Any]:
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    robot_profile = _normalize_turtlebot_robot_profile(
        indoor_map.get("robot_profile")
    ) or _turtlebot_robot_profile_from_artifacts(artifacts)
    robot_label = _status_text(
        indoor_map.get("robot_label"),
        _turtlebot_robot_label_from_profile(robot_profile),
    )
    return {
        **indoor_map,
        "schema_version": "missionos_cli_indoor_map.v1",
        "source_schema_version": indoor_map.get("schema_version"),
        "map_kind": "indoor_local_xy",
        "robot_profile": robot_profile or indoor_map.get("robot_profile"),
        "robot_label": robot_label,
        "task_id": _status_text(task.get("task_id")),
        "task_status": _task_status(task_payload),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": {
            "label": "Indoor local XY",
            "url_template": "",
            "attribution": f"MissionOS {robot_label}/Nav2 simulator evidence",
            "attribution_url": "",
        },
        "live": {
            "enabled": bool(live_task_url),
            "task_url": live_task_url or "",
            "poll_interval_ms": max(500, int(float(poll_interval) * 1000)),
            "terminal_statuses": sorted(TERMINAL_TASK_STATUSES),
        },
        "boundaries": [
            *list(indoor_map.get("claim_boundaries") or []),
            "Indoor map display is read-only and is not a verifier, dispatch control, delivery claim, or physical-execution claim.",
        ],
    }


def _mission_map_model(
    *,
    task_payload: dict[str, Any],
    provider: str,
    live_task_url: str | None = None,
    poll_interval: float = MISSION_MAP_POLL_INTERVAL,
) -> dict[str, Any]:
    artifacts = _task_artifacts(task_payload)
    task = _task_record(task_payload)
    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
    if indoor_map:
        return _mission_indoor_map_model(
            task_payload=task_payload,
            indoor_map=indoor_map,
            live_task_url=live_task_url,
            poll_interval=poll_interval,
        )
    route = _mission_map_latlon_from_route(artifacts)
    if route is None:
        raise click.ClickException(
            "task does not include source coordinates; `missionos map` needs "
            "mission_designer_coordinate_pair_route takeoff/dropoff lat/lon"
        )
    takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon = route
    planned_points = _mission_map_planned_points(
        artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    observed_trace = _mission_map_observed_trace(
        artifacts=artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
    )
    observed_segment_details = list(observed_trace["segments"])
    observed_segments = [list(segment.get("points") or []) for segment in observed_segment_details]
    lon_scale = 111320.0 * math.cos(math.radians(takeoff_lat))

    def distance_to(point: dict[str, Any], *, lat: float, lon: float) -> float:
        return math.hypot(
            (float(point["lat"]) - lat) * 111320.0,
            (float(point["lon"]) - lon) * lon_scale,
        )

    for index, segment in enumerate(observed_segment_details):
        points = segment.get("points") or []
        if not points:
            segment["role"] = "observed"
            continue
        first = points[0]
        last = points[-1]
        is_return = (
            index > 0
            and distance_to(last, lat=takeoff_lat, lon=takeoff_lon)
            < distance_to(last, lat=dropoff_lat, lon=dropoff_lon)
            and (
                (_as_int(segment.get("segment_index")) or 0) > 0
                or distance_to(first, lat=dropoff_lat, lon=dropoff_lon)
                < distance_to(first, lat=takeoff_lat, lon=takeoff_lon)
            )
        )
        segment["role"] = (
            "return_to_home"
            if is_return
            else "outbound"
            if index == 0
            else "outbound_after_observation_gap"
        )
    observed_points = [point for segment in observed_segments for point in segment]
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    latest_snapshot_point = (
        _mission_map_sample_latlon(
            snapshot,
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
        )
        if snapshot
        else None
    )
    if latest_snapshot_point is not None:
        lat, lon, source = latest_snapshot_point
        latest = {
            "lat": lat,
            "lon": lon,
            "source": f"{source}_latest_snapshot",
            "phase": _status_text(snapshot.get("phase"), "latest_snapshot"),
            "alt_m": _as_float(
                snapshot.get("relative_alt_m")
                or snapshot.get("altitude_above_home_m")
                or snapshot.get("local_z_m")
                or snapshot.get("z_m")
            ),
            "elapsed_s": snapshot.get("elapsed_seconds")
            or snapshot.get("elapsed_s")
            or snapshot.get("sample_index"),
        }
        if observed_points and (
            abs(observed_points[-1]["lat"] - latest["lat"]) <= 1e-8
            and abs(observed_points[-1]["lon"] - latest["lon"]) <= 1e-8
        ):
            latest = observed_points[-1]
    else:
        latest = None
    compatibility_points = list(observed_points)
    if not compatibility_points:
        compatibility_points = [
            {
                "lat": takeoff_lat,
                "lon": takeoff_lon,
                "source": "route_takeoff",
                "phase": "takeoff",
                "alt_m": 0,
                "elapsed_s": None,
            },
            {
                "lat": dropoff_lat,
                "lon": dropoff_lon,
                "source": "route_dropoff",
                "phase": "dropoff",
                "alt_m": None,
                "elapsed_s": None,
            },
        ]
    provider_config = MISSION_MAP_PROVIDERS[provider]
    obstacles = _mission_map_obstacles(
        artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    avoidances = _mission_map_maneuvers(
        artifacts=artifacts,
        snapshot=snapshot,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
    )
    avoidance = avoidances[-1] if avoidances else {}
    route_north_m, route_east_m = _mission_map_latlon_to_local(
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        lat=dropoff_lat,
        lon=dropoff_lon,
    )
    route_length_m = math.hypot(route_north_m, route_east_m)
    if route_length_m > 1e-6:
        route_unit_north = route_north_m / route_length_m
        route_unit_east = route_east_m / route_length_m

        def route_geometry(point: dict[str, Any]) -> tuple[float, float]:
            north_m, east_m = _mission_map_latlon_to_local(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                lat=float(point["lat"]),
                lon=float(point["lon"]),
            )
            progress_m = north_m * route_unit_north + east_m * route_unit_east
            cross_track_m = abs(north_m * route_unit_east - east_m * route_unit_north)
            return progress_m, cross_track_m

        outbound_points = [
            point
            for detail in observed_segment_details
            if detail.get("role") != "return_to_home"
            for point in detail.get("points") or []
        ]
        for avoidance_index, avoidance in enumerate(avoidances):
            target = avoidance.get("target")
            target = target if isinstance(target, dict) else None
            source_obstacle_name = _status_text(avoidance.get("source_obstacle_name"))
            obstacle = next(
                (
                    item
                    for item in obstacles
                    if source_obstacle_name
                    and _status_text(item.get("name")) == source_obstacle_name
                ),
                None,
            )
            if obstacle is None:
                obstacle = min(
                    obstacles,
                    key=lambda item: math.hypot(
                        (_as_float(item.get("x_m")) or 0.0)
                        - (_as_float((avoidance.get("start") or {}).get("x_m")) or 0.0),
                        (_as_float(item.get("y_m")) or 0.0)
                        - (_as_float((avoidance.get("start") or {}).get("y_m")) or 0.0),
                    ),
                    default=None,
                )
            target_progress_m = route_geometry(target)[0] if target else None
            next_recovery_start = (
                avoidances[avoidance_index + 1].get("start")
                if avoidance_index + 1 < len(avoidances)
                else None
            )
            next_recovery_start_progress_m = (
                route_geometry(next_recovery_start)[0]
                if isinstance(next_recovery_start, dict)
                else None
            )
            obstacle_progress_m = None
            obstacle_half_along_m = 0.0
            if obstacle is not None:
                obstacle_progress_m = (_as_float(obstacle.get("x_m")) or 0.0) * route_unit_north + (
                    _as_float(obstacle.get("y_m")) or 0.0
                ) * route_unit_east
                obstacle_half_along_m = abs(route_unit_north) * (
                    (_as_float(obstacle.get("size_x_m")) or 0.0) / 2.0
                ) + abs(route_unit_east) * ((_as_float(obstacle.get("size_y_m")) or 0.0) / 2.0)
            target_beyond_obstacle = bool(
                target_progress_m is not None
                and obstacle_progress_m is not None
                and target_progress_m > obstacle_progress_m + obstacle_half_along_m
            )
            rejoin_point = None
            if target and outbound_points:
                target_index = min(
                    range(len(outbound_points)),
                    key=lambda index: distance_to(
                        outbound_points[index],
                        lat=float(target["lat"]),
                        lon=float(target["lon"]),
                    ),
                )
                for point in outbound_points[target_index + 1 :]:
                    progress_m, cross_track_m = route_geometry(point)
                    if (
                        target_progress_m is not None
                        and progress_m >= target_progress_m
                        and (
                            next_recovery_start_progress_m is None
                            or progress_m < next_recovery_start_progress_m
                        )
                        and cross_track_m <= 12.0
                    ):
                        rejoin_point = {
                            **point,
                            "route_progress_m": round(progress_m, 3),
                            "cross_track_m": round(cross_track_m, 3),
                        }
                        break
            avoidance["route_rejoin"] = rejoin_point
            avoidance["geometry_status"] = (
                "lateral_bypass_target_beyond_obstacle"
                if target_beyond_obstacle
                else "legacy_target_before_obstacle"
                if target and obstacle_progress_m is not None
                else "geometry_unavailable"
            )
            avoidance["target_beyond_obstacle"] = target_beyond_obstacle
            avoidance["target_route_progress_m"] = (
                round(target_progress_m, 3) if target_progress_m is not None else None
            )
            avoidance["obstacle_route_progress_m"] = (
                round(obstacle_progress_m, 3) if obstacle_progress_m is not None else None
            )
            avoidance["rejoin_observed"] = rejoin_point is not None

    return_points = [
        point
        for detail in observed_segment_details
        if detail.get("role") == "return_to_home"
        for point in detail.get("points") or []
    ]
    for obstacle in obstacles:
        obstacle_x_m = _as_float(obstacle.get("x_m"))
        obstacle_y_m = _as_float(obstacle.get("y_m"))
        half_x_m = (_as_float(obstacle.get("size_x_m")) or 0.0) / 2.0
        half_y_m = (_as_float(obstacle.get("size_y_m")) or 0.0) / 2.0
        obstacle_top_m = _as_float(obstacle.get("top_altitude_m"))
        overflight_clearances: list[float] = []
        if (
            obstacle_x_m is not None
            and obstacle_y_m is not None
            and half_x_m > 0.0
            and half_y_m > 0.0
            and obstacle_top_m is not None
        ):
            for point in return_points:
                altitude_m = _as_float(point.get("alt_m"))
                if altitude_m is None:
                    continue
                point_x_m, point_y_m = _mission_map_latlon_to_local(
                    takeoff_lat=takeoff_lat,
                    takeoff_lon=takeoff_lon,
                    lat=float(point["lat"]),
                    lon=float(point["lon"]),
                )
                if (
                    abs(point_x_m - obstacle_x_m) <= half_x_m
                    and abs(point_y_m - obstacle_y_m) <= half_y_m
                ):
                    overflight_clearances.append(altitude_m - obstacle_top_m)
        if overflight_clearances:
            minimum_clearance_m = min(overflight_clearances)
            obstacle["return_overflight_observed"] = True
            obstacle["return_overflight_sample_count"] = len(overflight_clearances)
            obstacle["return_overflight_min_vertical_clearance_m"] = round(
                minimum_clearance_m, 3
            )
            obstacle["return_overflight_status"] = (
                "observed_above_obstacle"
                if minimum_clearance_m > 0.0
                else "vertical_clearance_not_observed"
            )

    raw_observed_gaps = list(observed_trace["gaps"])
    covered_observed_gap_indexes: set[int] = set()
    display_gaps: list[dict[str, Any]] = []

    def distance_between(left: dict[str, Any], right: dict[str, Any]) -> float:
        return distance_to(left, lat=float(right["lat"]), lon=float(right["lon"]))

    for avoidance in avoidances:
        start = avoidance.get("start")
        target = avoidance.get("target")
        samples = avoidance.get("samples") or []
        if not isinstance(start, dict) or not isinstance(target, dict):
            continue
        last_sample = samples[-1] if samples and isinstance(samples[-1], dict) else target
        for gap_index, gap in enumerate(raw_observed_gaps):
            gap_from = gap.get("from")
            gap_to = gap.get("to")
            if not isinstance(gap_from, dict) or not isinstance(gap_to, dict):
                continue
            if (
                distance_between(gap_from, start) <= 15.0
                and min(
                    distance_between(gap_to, target),
                    distance_between(gap_to, last_sample),
                )
                <= 15.0
            ):
                covered_observed_gap_indexes.add(gap_index)
                start_gap = avoidance.get("observation_start_gap")
                if isinstance(start_gap, dict):
                    display_gaps.append(
                        {**start_gap, "source": "recovery_observation_gap"}
                    )
                end_gap_distance_m = distance_between(last_sample, gap_to)
                if end_gap_distance_m > 10.0:
                    display_gaps.append(
                        {
                            "from": last_sample,
                            "to": gap_to,
                            "reason": "recovery_to_main_telemetry_gap",
                            "distance_m": round(end_gap_distance_m, 3),
                            "elapsed_gap_s": None,
                            "evidence_status": "not_observed_between_endpoints",
                            "source": "recovery_observation_gap",
                        }
                    )
                break
    display_gaps.extend(
        gap
        for gap_index, gap in enumerate(raw_observed_gaps)
        if gap_index not in covered_observed_gap_indexes
    )
    task_status = _task_status(task_payload)
    latest_point = latest or (observed_points[-1] if observed_points else None)
    terminal_marker_label = "current"
    if task_status in TERMINAL_TASK_STATUSES and latest_point is not None:
        if distance_to(latest_point, lat=takeoff_lat, lon=takeoff_lon) <= 15.0:
            terminal_marker_label = (
                "landed at home"
                if _as_bool(snapshot.get("landed")) is True
                else "mission ended at home"
            )
        elif _as_bool(snapshot.get("post_abort_tracking")) is True:
            terminal_marker_label = "last return observation"
        else:
            terminal_marker_label = "last saved observation"
    return {
        "schema_version": "missionos_cli_2d_map.v1",
        "task_id": _status_text(task.get("task_id")),
        "task_status": task_status,
        "task_updated_at": task.get("updated_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": {**provider_config, "key": provider},
        "route": {
            "takeoff": {"lat": takeoff_lat, "lon": takeoff_lon, "label": "H"},
            "dropoff": {"lat": dropoff_lat, "lon": dropoff_lon, "label": "D"},
        },
        "planned_points": planned_points,
        "observed_points": observed_points,
        "observed_segments": observed_segments,
        "observed_segment_details": observed_segment_details,
        "observed_gaps": raw_observed_gaps,
        "display_gaps": display_gaps,
        "observed_trace_source": observed_trace["source"],
        "points": compatibility_points,
        # Current pose is a marker, not permission to draw a line from the
        # latest persisted trajectory point.  It may belong to a later return
        # or post-run observation segment.
        "latest": latest_point,
        "terminal_marker_label": terminal_marker_label,
        "avoidance": avoidance,
        "avoidances": avoidances,
        "obstacles": obstacles,
        "telemetry": _mission_map_telemetry_model(
            snapshot=snapshot,
            artifacts=artifacts,
        ),
        "battery": _mission_map_battery_model(
            snapshot=snapshot,
            artifacts=artifacts,
        ),
        "recovery_provenance": _mission_map_recovery_provenance(artifacts),
        "weather": _mission_map_weather_model(artifacts),
        "live": {
            "enabled": bool(live_task_url),
            "task_url": live_task_url or "",
            "poll_interval_ms": max(500, int(float(poll_interval) * 1000)),
            "terminal_statuses": sorted(TERMINAL_TASK_STATUSES),
        },
        "boundaries": [
            "2D map uses real browser-fetched basemap tiles from the configured provider.",
            "MissionOS overlays source planned route, observed telemetry, operator-approved recovery maneuver traces, and source-backed obstacle markers.",
            "Solid observed paths contain saved telemetry only; dashed gap connectors are display-only and are not observation evidence.",
            "A return path crossing a 2D obstacle footprint is labeled as an overflight only when saved altitude observations prove positive clearance above the obstacle top.",
            "Map display is read-only and is not a verifier, dispatch control, or delivery claim.",
        ],
    }
