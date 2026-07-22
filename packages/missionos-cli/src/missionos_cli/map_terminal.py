"""Read-only terminal renderers for MissionOS flight and indoor maps."""

from __future__ import annotations

from typing import Any
import math

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from .job_status import (
    _as_float,
    _as_int,
    _auto_process_status_text,
    _fmt_metres,
    _format_percent,
    _operator_recovery_dispatch_hint,
    _operator_recovery_dispatch_status_text,
    _runtime_recovery_agent_action,
    _runtime_recovery_agent_parameters,
    _status_text,
    _terrain_profile_samples_for_watch,
)
from .map_model import (
    _dropoff_ned_from_route,
    _mission_map_battery_model,
    _mission_map_latlon_from_route,
    _mission_map_latlon_to_local,
    _mission_map_planned_points,
    _mission_obstacle_records_from_artifacts,
    _operator_recovery_local_maneuver_model,
    _project_flight_points,
)


FLIGHT_MAP_WIDTH = 64
FLIGHT_MAP_HEIGHT = 24
FLIGHT_PROFILE_HEIGHT = 9

def _watch_altitude_status(snapshot: dict[str, Any]) -> str:
    """Summarize altitude without implying terrain data exists when it does not."""
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    clearance = _as_float(snapshot.get("terrain_clearance_m"))
    target = _as_float(snapshot.get("terrain_clearance_target_m"))
    status = _status_text(snapshot.get("terrain_clearance_status"))
    if terrain is None and clearance is None and target is None:
        return (
            f"alt(home)={_fmt_metres(alt_home)}  "
            "terrain_elev(AMSL)=not_configured  AGL=-  target=-  "
            "drone_amsl=-"
        )
    amsl = terrain + clearance if terrain is not None and clearance is not None else None
    return (
        f"alt(home)={_fmt_metres(alt_home)}  "
        f"terrain_elev(AMSL)={_fmt_metres(terrain)}  "
        f"AGL={_fmt_metres(clearance)}  "
        f"target={_fmt_metres(target)} ({status})  "
        f"drone_amsl={_fmt_metres(amsl)}"
    )


def _watch_process_status(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
) -> str | None:
    process_status = _auto_process_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    if process_status:
        return process_status.removeprefix("Process: ")
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    if monitor_stop != "-":
        return f"terminal_receipt=pending; stop={monitor_stop}"
    return None


def _interpolate_watch_profile_value(
    samples: list[dict[str, float]],
    *,
    fraction: float,
    key: str,
) -> float | None:
    points = [
        (sample["fraction"], sample[key])
        for sample in samples
        if sample.get(key) is not None
    ]
    if not points:
        return None
    if fraction <= points[0][0]:
        return points[0][1]
    if fraction >= points[-1][0]:
        return points[-1][1]
    for (left_fraction, left_value), (right_fraction, right_value) in zip(
        points,
        points[1:],
        strict=False,
    ):
        if left_fraction <= fraction <= right_fraction:
            span = max(right_fraction - left_fraction, 1e-9)
            ratio = (fraction - left_fraction) / span
            return left_value + (right_value - left_value) * ratio
    return points[-1][1]


def _render_elevation_profile(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
    width: int = FLIGHT_MAP_WIDTH,
    height: int = FLIGHT_PROFILE_HEIGHT,
) -> Panel | None:
    samples, planned_route_m = _terrain_profile_samples_for_watch(artifacts)
    if not samples:
        return None

    terrain_values = [
        _interpolate_watch_profile_value(
            samples,
            fraction=col / max(width - 1, 1),
            key="terrain_elevation_m",
        )
        for col in range(width)
    ]
    target_values = [
        _interpolate_watch_profile_value(
            samples,
            fraction=col / max(width - 1, 1),
            key="target_amsl_m",
        )
        for col in range(width)
    ]
    progress_m = _as_float(snapshot.get("progress_m"))
    progress_fraction = (
        min(1.0, max(0.0, progress_m / planned_route_m))
        if progress_m is not None and planned_route_m
        else _as_float(snapshot.get("route_completion_fraction"))
    )
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    clearance = _as_float(snapshot.get("terrain_clearance_m"))
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    first_terrain = samples[0]["terrain_elevation_m"]
    current_amsl = (
        terrain + clearance
        if terrain is not None and clearance is not None
        else first_terrain + alt_home
        if alt_home is not None
        else None
    )

    plotted_values = [
        value
        for value in [*terrain_values, *target_values, current_amsl]
        if value is not None
    ]
    if not plotted_values:
        return None
    vmin = min(plotted_values)
    vmax = max(plotted_values)
    if math.isclose(vmin, vmax):
        vmin -= 1.0
        vmax += 1.0
    pad = max((vmax - vmin) * 0.08, 1.0)
    vmin -= pad
    vmax += pad

    def row_for(value: float) -> int:
        ratio = (value - vmin) / max(vmax - vmin, 1e-9)
        return min(max(round((height - 1) * (1.0 - ratio)), 0), height - 1)

    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * width for _ in range(height)
    ]
    for col, value in enumerate(terrain_values):
        if value is not None:
            grid[row_for(value)][col] = ("▁", "green")
    for col, value in enumerate(target_values):
        if value is not None:
            row = row_for(value)
            if grid[row][col][0] == " ":
                grid[row][col] = ("·", "cyan")
    if progress_fraction is not None and current_amsl is not None:
        col = min(max(round(progress_fraction * (width - 1)), 0), width - 1)
        grid[row_for(current_amsl)][col] = ("◆", "bold red")

    body = Text()
    for row in range(height):
        for col in range(width):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != height - 1:
            body.append("\n")

    footer = (
        f"progress={_fmt_metres(progress_m)} / {_fmt_metres(planned_route_m)}  "
        f"terrain={_fmt_metres(terrain)} AMSL  AGL={_fmt_metres(clearance)}  "
        f"drone={_fmt_metres(current_amsl)} AMSL"
    )
    body.append(f"\n{footer}", style="dim")
    body.append("\n")
    body.append("▁=terrain AMSL  ·=target altitude  ◆=drone AMSL", style="dim")
    return Panel(
        body,
        title="Altitude Profile (horizontal=route progress / vertical=AMSL)",
        border_style="magenta",
    )


def _watch_planned_route_points(artifacts: dict[str, Any]) -> list[tuple[float, float]]:
    route = _mission_map_latlon_from_route(artifacts)
    if route is None:
        dropoff = _dropoff_ned_from_route(artifacts)
        return [(0.0, 0.0), dropoff] if dropoff is not None else [(0.0, 0.0)]
    takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon = route
    planned_points = _mission_map_planned_points(
        artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    local_points: list[tuple[float, float]] = []
    for point in planned_points:
        lat = _as_float(point.get("lat"))
        lon = _as_float(point.get("lon"))
        if lat is None or lon is None:
            continue
        local_points.append(
            _mission_map_latlon_to_local(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                lat=lat,
                lon=lon,
            )
        )
    return local_points


def _watch_overlay_status_text(
    *,
    planned_points: list[tuple[float, float]],
    obstacle_records: list[dict[str, Any]],
    maneuver: dict[str, Any],
) -> str | None:
    parts: list[str] = []
    if planned_points:
        parts.append(f"planned={len(planned_points)}pts")
    if obstacle_records:
        spawned = [record.get("spawned") for record in obstacle_records]
        if any(value is True for value in spawned):
            spawn_status = "spawned"
        elif all(value is False for value in spawned):
            spawn_status = "not_spawned"
        else:
            spawn_status = "unknown"
        parts.append(f"obstacles={len(obstacle_records)}({spawn_status})")
    if maneuver:
        samples = maneuver.get("samples")
        samples_count = len(samples) if isinstance(samples, list) else 0
        parts.append(
            "avoid="
            f"{_status_text(maneuver.get('status'))}"
            f"/target={_status_text(maneuver.get('target_reached'))}"
            f"/resume={_status_text(maneuver.get('resume_auto_status'))}"
            f"/samples={samples_count}"
        )
    return "overlay: " + " · ".join(parts) if parts else None


def _indoor_xy_points(records: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not isinstance(records, list):
        return points
    for record in records:
        if not isinstance(record, dict):
            continue
        x_m = _as_float(record.get("x_m"))
        y_m = _as_float(record.get("y_m"))
        if x_m is None or y_m is None:
            continue
        points.append((x_m, y_m))
    return points


def _project_indoor_xy_points(
    points: list[tuple[float, float]],
    *,
    width: int,
    height: int,
) -> list[tuple[int, int]]:
    """Project ROS local-XY points onto a terminal grid.

    Unlike the PX4/NED map, TurtleBot3 indoor maps use x to the right and y up.
    Terminal rows are roughly twice as tall as columns, so vertical scale uses the
    same compensation as the flight map projection.
    """
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    span_x = max(xmax - xmin, 1e-6)
    span_y = max(ymax - ymin, 1e-6)
    scale = max(span_x / max(width - 1, 1), span_y / max((height - 1) * 2, 1)) or 1.0
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    projected: list[tuple[int, int]] = []
    for x_m, y_m in points:
        col = round((width - 1) / 2.0 + (x_m - cx) / scale)
        row = round((height - 1) / 2.0 - (y_m - cy) / (scale * 2.0))
        col = min(max(col, 0), width - 1)
        row = min(max(row, 0), height - 1)
        projected.append((row, col))
    return projected


TURTLEBOT3_MAP_ICON = "🐢"


def _render_turtlebot3_indoor_map(
    *,
    indoor_map: dict[str, Any],
    status: str,
    task_id: str,
) -> Group:
    robot_label = _status_text(indoor_map.get("robot_label"), "TurtleBot3")
    planned_records = indoor_map.get("planned_points")
    planned_records = planned_records if isinstance(planned_records, list) else []
    observed_records = indoor_map.get("observed_points")
    observed_records = observed_records if isinstance(observed_records, list) else []
    obstacle_records = indoor_map.get("obstacles")
    obstacle_records = obstacle_records if isinstance(obstacle_records, list) else []
    floor_plan = indoor_map.get("floor_plan")
    floor_plan = floor_plan if isinstance(floor_plan, dict) else {}
    furniture_records = floor_plan.get("furniture")
    furniture_records = furniture_records if isinstance(furniture_records, list) else []
    recovery = indoor_map.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    recovery_records = recovery.get("observed_points")
    recovery_records = recovery_records if isinstance(recovery_records, list) else []
    recovery_target = recovery.get("target")
    recovery_target_records = [recovery_target] if isinstance(recovery_target, dict) else []
    live_records = indoor_map.get("live_display_points")
    live_records = live_records if isinstance(live_records, list) else []
    live_telemetry = indoor_map.get("live_telemetry")
    live_telemetry = live_telemetry if isinstance(live_telemetry, dict) else {}
    live_preview_ended = live_telemetry.get("telemetry_status") == "ended"
    current_pose = indoor_map.get("current_pose")
    current_pose_records = [current_pose] if isinstance(current_pose, dict) else []

    planned_points = _indoor_xy_points(planned_records)
    observed_points = _indoor_xy_points(observed_records)
    obstacle_points = _indoor_xy_points(obstacle_records)
    furniture_points = _indoor_xy_points(furniture_records)
    recovery_points = _indoor_xy_points(recovery_records)
    recovery_target_points = _indoor_xy_points(recovery_target_records)
    live_points = _indoor_xy_points(live_records)
    current_pose_points = _indoor_xy_points(current_pose_records)
    obstacle_record = (
        obstacle_records[0]
        if obstacle_records and isinstance(obstacle_records[0], dict)
        else {}
    )
    anchors = [
        *planned_points,
        *observed_points,
        *furniture_points,
        *obstacle_points,
        *recovery_points,
        *recovery_target_points,
        *live_points,
        *current_pose_points,
    ]
    if not anchors:
        anchors = [(0.0, 0.0)]
    projected = _project_indoor_xy_points(
        anchors,
        width=FLIGHT_MAP_WIDTH,
        height=FLIGHT_MAP_HEIGHT,
    )
    sections: dict[str, tuple[int, int]] = {}
    cursor = 0
    for name, points in (
        ("planned", planned_points),
        ("observed", observed_points),
        ("furniture", furniture_points),
        ("obstacles", obstacle_points),
        ("recovery", recovery_points),
        ("recovery_target", recovery_target_points),
        ("live", live_points),
        ("current_pose", current_pose_points),
    ):
        sections[name] = (cursor, cursor + len(points))
        cursor += len(points)

    def section(name: str) -> list[tuple[int, int]]:
        start, end = sections.get(name, (0, 0))
        return projected[start:end]

    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * FLIGHT_MAP_WIDTH for _ in range(FLIGHT_MAP_HEIGHT)
    ]
    for row, col in section("planned"):
        grid[row][col] = ("p", "cyan")
    for index, (row, col) in enumerate(section("observed")):
        style = "green" if index >= max(0, len(observed_points) - 12) else "grey42"
        grid[row][col] = ("·", style)
    for (row, col), record in zip(section("furniture"), furniture_records, strict=False):
        label = str(record.get("label") or record.get("kind") or "f").lower()
        char = "F"
        if "sofa" in label:
            char = "S"
        elif "table" in label:
            char = "T"
        elif "book" in label:
            char = "B"
        elif "counter" in label:
            char = "C"
        grid[row][col] = (char, "bold white")
    for row, col in section("recovery"):
        grid[row][col] = ("r", "bright_magenta")
    for row, col in section("obstacles"):
        grid[row][col] = ("O", "bold red")
    for row, col in section("recovery_target"):
        grid[row][col] = ("R", "bold magenta")
    for row, col in section("live"):
        grid[row][col] = (
            "·",
            "dim green" if live_preview_ended else "bright_green",
        )
    if planned_points:
        home_row, home_col = section("planned")[0]
        grid[home_row][home_col] = ("H", "bold blue")
        drop_row, drop_col = section("planned")[-1]
        grid[drop_row][drop_col] = ("D", "bold yellow")
    if live_points and not live_preview_ended:
        robot_row, robot_col = section("live")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold bright_green")
    elif current_pose_points:
        robot_row, robot_col = section("current_pose")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold green")
    elif recovery_points:
        robot_row, robot_col = section("recovery")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold green")
    elif observed_points:
        robot_row, robot_col = section("observed")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold green")

    body = Text()
    for row in range(FLIGHT_MAP_HEIGHT):
        for col in range(FLIGHT_MAP_WIDTH):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != FLIGHT_MAP_HEIGHT - 1:
            body.append("\n")

    motion = indoor_map.get("motion")
    motion = motion if isinstance(motion, dict) else {}
    room = indoor_map.get("room_boundary")
    room = room if isinstance(room, dict) else {}
    alignment = indoor_map.get("display_alignment")
    alignment = alignment if isinstance(alignment, dict) else {}
    clearance_3d = indoor_map.get("trajectory_clearance_3d")
    clearance_3d = clearance_3d if isinstance(clearance_3d, dict) else {}
    live_twist = live_telemetry.get("twist")
    live_twist = live_twist if isinstance(live_twist, dict) else {}
    recovery_phase = _status_text(recovery.get("runtime_status"))
    hud = Text.from_markup(
        f"[bold]task[/bold]={task_id}  [bold]status[/bold]={status}  "
        f"[bold]mission[/bold]={_status_text(indoor_map.get('mission_kind'))}\n"
        f"frame={_status_text(indoor_map.get('frame_id'), 'map')}  "
        f"planned={len(planned_points)}pts  observed={len(observed_points)}pts  "
        f"recovery_observed={len(recovery_points)}pts  "
        f"obstacles={len(obstacle_points)}  furniture={len(furniture_points)}  "
        f"recovery={_status_text(recovery.get('triggered'))}\n"
        f"recovery_phase={recovery_phase or '-'}  "
        f"recovery_action={_status_text(recovery.get('selected_action')) or '-'}\n"
        f"recovery_goal={_status_text(recovery.get('goal_status')) or '-'}  "
        f"verification={_status_text(recovery.get('verification_status')) or '-'}  "
        f"resume_status={_status_text(recovery.get('route_resume_status')) or '-'}\n"
        f"route_segments={_status_text(recovery.get('route_segment_completion_count')) or '-'}/"
        f"{_status_text(recovery.get('route_segment_planned_count')) or '-'}  "
        f"recovery_complete={_status_text(recovery.get('recovery_completion_claimed')) or '-'}  "
        f"route_resumed={_status_text(recovery.get('route_resumed_after_recovery')) or '-'}\n"
        f"live_preview={_status_text(live_telemetry.get('telemetry_status')) or '-'} "
        "(display-only, not evidence)  "
        f"live_samples={len(live_points)}  "
        f"live_path={_fmt_metres(live_telemetry.get('display_path_length_m'))}  "
        f"linear_x={_status_text(live_twist.get('linear_x_mps')) or '-'}m/s  "
        f"captured_at={_status_text(live_telemetry.get('captured_at')) or '-'}\n"
        f"motion={_status_text(motion.get('robot_motion_observed'))}  "
        f"odom={_fmt_metres(motion.get('odom_delta_m'))}  "
        f"observed_source={_status_text(indoor_map.get('observed_pose_source'))}\n"
        f"centerline_2d_clearance={_status_text(obstacle_record.get('trajectory_clearance_observed'))}  "
        f"centerline_2d_intersects={_status_text(obstacle_record.get('trajectory_intersects_obstacle'))}\n"
        f"clearance_3d_status={_status_text(clearance_3d.get('status'), 'unavailable')}  "
        f"clearance_3d_clear={_status_text(clearance_3d.get('clearance_observed'))}  "
        f"clearance_3d_collision={_status_text(clearance_3d.get('collision_observed'))}  "
        f"clearance_3d_min={_fmt_metres(clearance_3d.get('minimum_surface_clearance_m'))}\n"
        f"display_alignment={_status_text(alignment.get('method'))}  "
        f"applied={_status_text(alignment.get('applied'))}  "
        f"dx={_fmt_metres(alignment.get('dx_m'))}  "
        f"dy={_fmt_metres(alignment.get('dy_m'))}\n"
        f"room={_status_text(room.get('source'))}; physical_execution_invoked=false\n"
        f"floor_plan={_status_text(floor_plan.get('floor_plan_id'))}; "
        "furniture is display-only unless separately spawned\n"
        f"[blue]H[/blue]=home  [yellow]D[/yellow]=dropoff  [bright_green]{TURTLEBOT3_MAP_ICON}[/bright_green]=live preview marker  "
        "[cyan]p[/cyan]=plan  [green]·[/green]=persisted observed odom  "
        "[dim green]·[/dim green]=live odom preview (not evidence)  "
        "[white]S/T/B/C[/white]=sofa/table/bookshelf/counter  "
        "[bright_magenta]r/R[/bright_magenta]=recovery path/target  [red]O[/red]=obstacle"
    )
    return Group(
        Panel(
            body,
            title=(
                f"MissionOS Indoor Map ({robot_label}/Nav2 sim · "
                "right=+map x top=+map y)"
            ),
            border_style="cyan",
        ),
        hud,
    )


def _render_flight_map(
    *,
    trail: list[tuple[float, float]],
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
    status: str,
    task_id: str,
) -> Group:
    dropoff = _dropoff_ned_from_route(artifacts)
    planned_points = _watch_planned_route_points(artifacts)
    obstacle_records = _mission_obstacle_records_from_artifacts(artifacts)
    obstacle_points = [
        (float(record["x_m"]), float(record["y_m"]))
        for record in obstacle_records
        if record.get("x_m") is not None and record.get("y_m") is not None
    ]
    maneuver = _operator_recovery_local_maneuver_model(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    maneuver_samples = [
        (float(sample["x_m"]), float(sample["y_m"]))
        for sample in maneuver.get("samples") or []
        if sample.get("x_m") is not None and sample.get("y_m") is not None
    ]
    maneuver_target = maneuver.get("target") if isinstance(maneuver, dict) else None
    maneuver_target_point = (
        (float(maneuver_target["x_m"]), float(maneuver_target["y_m"]))
        if isinstance(maneuver_target, dict)
        and maneuver_target.get("x_m") is not None
        and maneuver_target.get("y_m") is not None
        else None
    )
    anchors: list[tuple[float, float]] = []
    sections: dict[str, tuple[int, int]] = {}

    def add_section(name: str, points: list[tuple[float, float]]) -> None:
        start = len(anchors)
        anchors.extend(points)
        sections[name] = (start, len(anchors))

    add_section("planned", planned_points)
    add_section("trail", list(trail))
    add_section("home", [(0.0, 0.0)])
    blocked_dropoff = False
    if dropoff is not None:
        add_section("dropoff", [dropoff])
    add_section("obstacles", obstacle_points)
    add_section("maneuver_samples", maneuver_samples)
    if maneuver_target_point is not None:
        add_section("maneuver_target", [maneuver_target_point])
    projected = _project_flight_points(
        anchors, width=FLIGHT_MAP_WIDTH, height=FLIGHT_MAP_HEIGHT
    )

    def projected_section(name: str) -> list[tuple[int, int]]:
        start, end = sections.get(name, (0, 0))
        return projected[start:end]

    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * FLIGHT_MAP_WIDTH for _ in range(FLIGHT_MAP_HEIGHT)
    ]
    for row, col in projected_section("planned"):
        grid[row][col] = ("p", "cyan")
    n_trail = len(trail)
    for idx, (row, col) in enumerate(projected_section("trail")):
        # Older path dim, recent path brighter green.
        style = "green" if idx >= n_trail - 12 else "grey42"
        grid[row][col] = ("·", style)
    for row, col in projected_section("maneuver_samples"):
        grid[row][col] = ("a", "bright_yellow")
    for row, col in projected_section("obstacles"):
        grid[row][col] = ("O", "bold red")
    for row, col in projected_section("maneuver_target"):
        grid[row][col] = ("A", "bold yellow")
    home_row, home_col = projected_section("home")[0]
    grid[home_row][home_col] = ("H", "bold blue")
    if dropoff is not None:
        d_row, d_col = projected_section("dropoff")[0]
        blocked_dropoff = any(
            math.hypot(point[0] - dropoff[0], point[1] - dropoff[1])
            <= max(
                3.0,
                (_as_float(record.get("size_x_m")) or 0.0) / 2.0,
                (_as_float(record.get("size_y_m")) or 0.0) / 2.0,
            )
            for point, record in zip(
                obstacle_points, obstacle_records, strict=False
            )
        )
        grid[d_row][d_col] = (
            "X" if blocked_dropoff else "D",
            "bold red" if blocked_dropoff else "bold yellow",
        )
    if n_trail:
        dr, dc = projected_section("trail")[-1]
        grid[dr][dc] = (
            "!"
            if blocked_dropoff
            and dropoff is not None
            and (dr, dc) == (d_row, d_col)
            else "◆",
            "bold red",
        )

    body = Text()
    for row in range(FLIGHT_MAP_HEIGHT):
        for col in range(FLIGHT_MAP_WIDTH):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != FLIGHT_MAP_HEIGHT - 1:
            body.append("\n")

    battery_model = _mission_map_battery_model(
        snapshot=snapshot,
        artifacts=artifacts,
    )
    battery = _format_percent(battery_model.get("display_percent"))
    battery_detail = (
        f"source={_status_text(battery_model.get('source'), 'unknown')}  "
        f"status={_status_text(battery_model.get('status'))}  "
        f"sample={_status_text(battery_model.get('sample_index'))}  "
        f"observed_at={_status_text(battery_model.get('observed_at'))}"
    )
    if battery_model.get("reset_detected") is True:
        battery_detail += (
            "  reported="
            f"{_format_percent(battery_model.get('reported_percent'))} rejected_reset="
            f"+{round(float(battery_model.get('reset_delta_percent') or 0.0), 1)}pp"
        )
    reached = _status_text(_as_int(snapshot.get("mission_reached_seq")))
    total = _status_text(_as_int(snapshot.get("waypoint_total")))
    home_dist = snapshot.get("distance_to_home_m")
    title = "MissionOS Live Map (SITL · top=North right=East)"
    process_status = _watch_process_status(artifacts=artifacts, snapshot=snapshot)
    process_line = f"{process_status}\n" if process_status else ""
    monitor_ended = snapshot.get("monitor_window_ended") is True or (
        snapshot.get("snapshot_status") == "monitor_window_ended"
    )
    recovery_hint = _operator_recovery_dispatch_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
        compact=True,
    )
    if recovery_hint is None and status == "running" and not monitor_ended:
        recovery_hint = _operator_recovery_dispatch_hint(
            task_id=task_id,
            action=_runtime_recovery_agent_action(artifacts),
            parameters=_runtime_recovery_agent_parameters(artifacts),
            compact=True,
        )
    recovery_line = f"{recovery_hint}\n" if recovery_hint else ""
    overlay_status = _watch_overlay_status_text(
        planned_points=planned_points,
        obstacle_records=obstacle_records,
        maneuver=maneuver,
    )
    overlay_line = f"{overlay_status}\n" if overlay_status else ""
    hud = Text.from_markup(
        f"[bold]task[/bold]={task_id}  [bold]status[/bold]={status}\n"
        f"{process_line}"
        f"{recovery_line}"
        f"{overlay_line}"
        f"{_watch_altitude_status(snapshot)}\n"
        f"battery={battery}  {battery_detail}\n"
        f"wp={reached}/{total}  home_dist={_fmt_metres(home_dist)}\n"
        "[blue]H[/blue]=home  [yellow]D[/yellow]=dropoff  [red]◆[/red]=drone  "
        "[red]X/![/red]=blocked dropoff / drone at blocked dropoff  "
        "[cyan]p[/cyan]=initial plan  [green]·[/green]=observed  "
        "[bright_yellow]a/A[/bright_yellow]=avoid path/target  [red]O[/red]=obstacle"
    )
    profile = _render_elevation_profile(snapshot=snapshot, artifacts=artifacts)
    if profile is not None:
        return Group(Panel(body, title=title, border_style="cyan"), profile, hud)
    return Group(Panel(body, title=title, border_style="cyan"), hud)
