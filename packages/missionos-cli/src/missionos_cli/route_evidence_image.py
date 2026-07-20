"""Deterministic terminal route-evidence image derived from map artifacts.

The SVG is a read-only visualization.  It never creates approval, dispatch,
completion, delivery, or physical-execution authority.  Every solid route is
derived from persisted observations already present in the map model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import Any
import json
import math


ROUTE_EVIDENCE_IMAGE_SCHEMA_VERSION = "missionos_route_evidence_image.v1"
ROUTE_EVIDENCE_MANIFEST_SCHEMA_VERSION = "missionos_route_evidence_manifest.v1"

_WIDTH = 1600
_HEIGHT = 900
_LEFT = 110
_RIGHT = 70
_TOP = 190
_BOTTOM = 130

_COLORS = {
    "background": "#07101f",
    "panel": "#0d1728",
    "grid": "#243247",
    "text": "#f8fafc",
    "muted": "#a9b7ca",
    "planned": "#facc15",
    "outbound": "#38bdf8",
    "return": "#22d3ee",
    "recovery": "#fb923c",
    "rejoin": "#a78bfa",
    "obstacle": "#f43f5e",
    "dropoff": "#22c55e",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_slug(value: Any) -> str:
    text = str(value or "task")
    return (
        "".join(
            character if character.isalnum() or character in "_.-" else "-" for character in text
        )
        or "task"
    )


def _latlon(point: Any) -> tuple[float, float] | None:
    point = _mapping(point)
    lat = _number(point.get("lat"))
    lon = _number(point.get("lon"))
    if lat is None or lon is None:
        return None
    return lat, lon


def _route_projection(model: dict[str, Any]) -> tuple[Any, float]:
    route = _mapping(model.get("route"))
    takeoff = _latlon(route.get("takeoff"))
    dropoff = _latlon(route.get("dropoff"))
    if takeoff is None or dropoff is None:
        raise ValueError("route evidence needs source takeoff and dropoff coordinates")
    takeoff_lat, takeoff_lon = takeoff
    dropoff_lat, dropoff_lon = dropoff
    lon_scale = 111320.0 * math.cos(math.radians(takeoff_lat))
    route_north = (dropoff_lat - takeoff_lat) * 111320.0
    route_east = (dropoff_lon - takeoff_lon) * lon_scale
    route_length = math.hypot(route_north, route_east)
    if route_length <= 1e-6:
        raise ValueError("route evidence needs distinct takeoff and dropoff coordinates")
    unit_north = route_north / route_length
    unit_east = route_east / route_length

    def project(point: Any) -> tuple[float, float] | None:
        coordinates = _latlon(point)
        if coordinates is None:
            return None
        lat, lon = coordinates
        north = (lat - takeoff_lat) * 111320.0
        east = (lon - takeoff_lon) * lon_scale
        progress = north * unit_north + east * unit_east
        signed_cross_track = north * unit_east - east * unit_north
        return progress, signed_cross_track

    return project, route_length


def _observed_details(model: dict[str, Any]) -> list[dict[str, Any]]:
    details = model.get("observed_segment_details")
    if isinstance(details, list):
        return [dict(item) for item in details if isinstance(item, dict)]
    segments = model.get("observed_segments")
    if not isinstance(segments, list):
        return []
    return [
        {
            "role": "outbound" if index == 0 else "observed",
            "points": segment,
        }
        for index, segment in enumerate(segments)
        if isinstance(segment, list)
    ]


def _projected_points(points: Any, project: Any) -> list[tuple[float, float]]:
    if not isinstance(points, list):
        return []
    projected: list[tuple[float, float]] = []
    for point in points:
        coordinates = project(point)
        if coordinates is not None:
            projected.append(coordinates)
    return projected


def _canonical_source_payload(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_EVIDENCE_IMAGE_SCHEMA_VERSION,
        "task_id": model.get("task_id"),
        "task_status": model.get("task_status"),
        "task_updated_at": model.get("task_updated_at"),
        "source_schema_version": model.get("schema_version"),
        "route": model.get("route"),
        "planned_points": model.get("planned_points"),
        "observed_segment_details": model.get("observed_segment_details"),
        "observed_gaps": model.get("observed_gaps"),
        "observed_trace_source": model.get("observed_trace_source"),
        "avoidance": model.get("avoidance"),
        "avoidances": model.get("avoidances"),
        "obstacles": model.get("obstacles"),
        "battery": model.get("battery"),
        "recovery_provenance": model.get("recovery_provenance"),
        "terminal_marker_label": model.get("terminal_marker_label"),
        "boundaries": model.get("boundaries"),
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def mission_route_evidence_source_sha256(model: dict[str, Any]) -> str:
    return sha256(_canonical_json(_canonical_source_payload(model))).hexdigest()


def _path(points: list[tuple[float, float]], sx: Any, sy: Any) -> str:
    return " ".join(f"{sx(progress):.1f},{sy(cross_track):.1f}" for progress, cross_track in points)


def _nice_step(span: float, *, target_ticks: int = 8) -> float:
    raw = max(span / max(target_ticks, 1), 1e-6)
    exponent = 10 ** math.floor(math.log10(raw))
    normalized = raw / exponent
    multiplier = (
        1.0
        if normalized <= 1.0
        else 2.0
        if normalized <= 2.0
        else 5.0
        if normalized <= 5.0
        else 10.0
    )
    return multiplier * exponent


def mission_route_evidence_svg(model: dict[str, Any]) -> str:
    """Render a source-backed route-profile SVG for a terminal flight task."""

    if model.get("map_kind") == "indoor_local_xy":
        raise ValueError("route evidence SVG currently supports flight route maps only")
    project, route_length = _route_projection(model)
    segment_details = _observed_details(model)
    projected_segments = [
        {
            "role": str(detail.get("role") or "observed"),
            "points": _projected_points(detail.get("points"), project),
        }
        for detail in segment_details
    ]
    projected_segments = [segment for segment in projected_segments if segment["points"]]
    raw_avoidances = model.get("avoidances")
    avoidances = (
        [_mapping(item) for item in raw_avoidances if isinstance(item, dict)]
        if isinstance(raw_avoidances, list)
        else []
    )
    if not avoidances and isinstance(model.get("avoidance"), dict):
        avoidances = [_mapping(model.get("avoidance"))]
    recovery_point_sets: list[list[tuple[float, float]]] = []
    for item in avoidances:
        source_points = [
            *([item.get("start")] if isinstance(item.get("start"), dict) else []),
            *(
                [point for point in item.get("samples") or [] if isinstance(point, dict)]
                if isinstance(item.get("samples"), list)
                else []
            ),
            *([item.get("target")] if isinstance(item.get("target"), dict) else []),
        ]
        recovery_point_sets.append(
            [coordinates for point in source_points if (coordinates := project(point)) is not None]
        )
    recovery_points = [point for point_set in recovery_point_sets for point in point_set]
    obstacles = [dict(item) for item in model.get("obstacles") or [] if isinstance(item, dict)]
    obstacle_polygons = [
        _projected_points(obstacle.get("footprint"), project) for obstacle in obstacles
    ]
    obstacle_polygons = [polygon for polygon in obstacle_polygons if len(polygon) >= 3]
    takeoff = project(_mapping(model.get("route")).get("takeoff"))
    dropoff = project(_mapping(model.get("route")).get("dropoff"))
    if takeoff is None or dropoff is None:
        raise ValueError("route evidence needs projectable route endpoints")
    rejoins = [project(item.get("route_rejoin")) for item in avoidances]
    all_points = [takeoff, dropoff]
    all_points.extend(point for segment in projected_segments for point in segment["points"])
    all_points.extend(recovery_points)
    all_points.extend(point for polygon in obstacle_polygons for point in polygon)
    all_points.extend(rejoin for rejoin in rejoins if rejoin is not None)
    progresses = [point[0] for point in all_points]
    cross_tracks = [point[1] for point in all_points]
    progress_margin = max(12.0, route_length * 0.025)
    progress_min = min(min(progresses), 0.0) - progress_margin
    progress_max = max(max(progresses), route_length) + progress_margin
    cross_min = min(min(cross_tracks), -10.0)
    cross_max = max(max(cross_tracks), 10.0)
    cross_margin = max(12.0, (cross_max - cross_min) * 0.18)
    cross_min -= cross_margin
    cross_max += cross_margin

    def sx(progress: float) -> float:
        return _LEFT + (progress - progress_min) / (progress_max - progress_min) * (
            _WIDTH - _LEFT - _RIGHT
        )

    def sy(cross_track: float) -> float:
        return _TOP + (cross_max - cross_track) / (cross_max - cross_min) * (
            _HEIGHT - _TOP - _BOTTOM
        )

    source_hash = mission_route_evidence_source_sha256(model)
    task_id = str(model.get("task_id") or "task")
    task_status = str(model.get("task_status") or "unknown")
    terminal_label = str(model.get("terminal_marker_label") or "mission ended")
    provenance = _mapping(model.get("recovery_provenance"))
    battery = _mapping(model.get("battery"))
    battery_percent = _number(battery.get("display_percent"))
    model_id = str(provenance.get("model_id") or provenance.get("origin_kind") or "-")
    recovery_status = " | ".join(
        str(item.get("resume_auto_status") or item.get("status") or "-") for item in avoidances
    ) or str(provenance.get("resume_status") or "-")
    resume_seq = provenance.get("resume_mission_current_seq")
    if resume_seq is None:
        resume_seq = provenance.get("resume_mission_seq_after_obstacle")
    resume_seq_text = f"waypoint {resume_seq}" if resume_seq is not None else "-"
    geometry_status = (
        " | ".join(str(item.get("geometry_status") or "-") for item in avoidances) or "-"
    )

    svg: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}">',
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="{_COLORS["background"]}"/>',
        (
            '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",'
            f'"Noto Sans JP",sans-serif;fill:{_COLORS["text"]}}}'
            f".muted{{fill:{_COLORS['muted']}}}.label{{fill:{_COLORS['text']};stroke:none;font-weight:700}}"
            ".small{font-size:17px}.med{font-size:21px}.big{font-size:32px;font-weight:800}</style>"
        ),
        (
            "<defs>"
            f'<marker id="arrow-outbound" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="{_COLORS["outbound"]}"/></marker>'
            f'<marker id="arrow-recovery" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="{_COLORS["recovery"]}"/></marker>'
            f'<marker id="arrow-return" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="{_COLORS["return"]}"/></marker>'
            "</defs>"
        ),
        '<text x="42" y="48" class="big">MissionOS E2E Route Evidence / 実行後の航跡証拠</text>',
        '<text x="42" y="82" class="muted med">横軸＝経路の進行距離　縦軸＝元経路からの横ずれ。実線は保存済み観測だけです。</text>',
    ]

    legend = [
        (_COLORS["planned"], "planned route / 計画", True),
        (_COLORS["outbound"], "saved outbound / 往路観測", False),
        (_COLORS["recovery"], "approved Recovery / 回避観測", False),
        (_COLORS["return"], "saved return / 帰投観測", True),
        (_COLORS["obstacle"], "collision obstacle / 障害物", False),
    ]
    legend_x = 42.0
    for color, label, dashed in legend:
        svg.append(
            f'<line x1="{legend_x:.1f}" y1="119" x2="{legend_x + 28:.1f}" y2="119" stroke="{color}" stroke-width="5"'
            + (' stroke-dasharray="10 7"' if dashed else "")
            + "/>"
        )
        svg.append(f'<text x="{legend_x + 36:.1f}" y="126" class="small">{escape(label)}</text>')
        legend_x += 278.0

    plot_width = _WIDTH - _LEFT - _RIGHT
    plot_height = _HEIGHT - _TOP - _BOTTOM
    svg.append(
        f'<rect x="{_LEFT}" y="{_TOP}" width="{plot_width}" height="{plot_height}" rx="12" fill="{_COLORS["panel"]}" stroke="#334155"/>'
    )
    progress_step = _nice_step(progress_max - progress_min)
    progress_tick = math.ceil(progress_min / progress_step) * progress_step
    while progress_tick <= progress_max + 1e-6:
        x = sx(progress_tick)
        svg.append(
            f'<line x1="{x:.1f}" y1="{_TOP}" x2="{x:.1f}" y2="{_HEIGHT - _BOTTOM}" stroke="{_COLORS["grid"]}"/>'
        )
        svg.append(
            f'<text x="{x:.1f}" y="{_HEIGHT - _BOTTOM + 30}" text-anchor="middle" class="muted small">{progress_tick:.0f}m</text>'
        )
        progress_tick += progress_step
    cross_step = _nice_step(cross_max - cross_min, target_ticks=6)
    cross_tick = math.ceil(cross_min / cross_step) * cross_step
    while cross_tick <= cross_max + 1e-6:
        y = sy(cross_tick)
        svg.append(
            f'<line x1="{_LEFT}" y1="{y:.1f}" x2="{_WIDTH - _RIGHT}" y2="{y:.1f}" stroke="{_COLORS["grid"]}"/>'
        )
        svg.append(
            f'<text x="{_LEFT - 16}" y="{y + 6:.1f}" text-anchor="end" class="muted small">{cross_tick:+.0f}m</text>'
        )
        cross_tick += cross_step

    svg.append(
        f'<line x1="{sx(0.0):.1f}" y1="{sy(0.0):.1f}" x2="{sx(route_length):.1f}" y2="{sy(0.0):.1f}" stroke="{_COLORS["planned"]}" stroke-width="4" stroke-dasharray="13 9" opacity=".9"/>'
    )
    for polygon in obstacle_polygons:
        svg.append(
            f'<polygon points="{_path(polygon, sx, sy)}" fill="{_COLORS["obstacle"]}" fill-opacity=".3" stroke="{_COLORS["obstacle"]}" stroke-width="3"/>'
        )
    for segment in projected_segments:
        role = segment["role"]
        is_return = role == "return_to_home"
        color = _COLORS["return"] if is_return else _COLORS["outbound"]
        marker = "arrow-return" if is_return else "arrow-outbound"
        dash = ' stroke-dasharray="11 7"' if is_return else ""
        path = _path(segment["points"], sx, sy)
        svg.append(
            f'<polyline points="{path}" fill="none" stroke="#020617" stroke-width="11" stroke-linecap="round" stroke-linejoin="round" opacity=".65"/>'
        )
        svg.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#{marker})"{dash}/>'
        )
    for recovery_points_for_attempt in recovery_point_sets:
        if len(recovery_points_for_attempt) < 2:
            continue
        recovery_path = _path(recovery_points_for_attempt, sx, sy)
        svg.append(
            f'<polyline points="{recovery_path}" fill="none" stroke="#020617" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" opacity=".7"/>'
        )
        svg.append(
            f'<polyline points="{recovery_path}" fill="none" stroke="{_COLORS["recovery"]}" stroke-width="6" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#arrow-recovery)"/>'
        )

    markers: list[tuple[str, tuple[float, float] | None, str, float, float]] = [
        ("1 Start", takeoff, _COLORS["outbound"], 12.0, -18.0),
        ("5 Dropoff", dropoff, _COLORS["dropoff"], -120.0, -18.0),
    ]
    for index, recovery_points_for_attempt in enumerate(recovery_point_sets, start=1):
        if not recovery_points_for_attempt:
            continue
        recovery_label = "2 Recovery" if len(recovery_point_sets) == 1 else f"R{index} Recovery"
        bypass_label = "3 Bypass" if len(recovery_point_sets) == 1 else f"R{index} Bypass"
        rejoin_label = "4 Rejoin" if len(recovery_point_sets) == 1 else f"R{index} Rejoin"
        markers.extend(
            [
                (
                    recovery_label,
                    recovery_points_for_attempt[0],
                    _COLORS["recovery"],
                    12.0,
                    -18.0,
                ),
                (
                    bypass_label,
                    recovery_points_for_attempt[-1],
                    _COLORS["recovery"],
                    12.0,
                    27.0,
                ),
                (
                    rejoin_label,
                    rejoins[index - 1],
                    _COLORS["rejoin"],
                    12.0,
                    27.0,
                ),
            ]
        )
    if terminal_label != "current":
        markers.append(("6 Home", takeoff, _COLORS["return"], 12.0, 30.0))
    for label, point, color, dx, dy in markers:
        if point is None:
            continue
        x, y = sx(point[0]), sy(point[1])
        svg.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="{color}" stroke="white" stroke-width="2"/>'
        )
        svg.append(
            f'<text x="{x + dx:.1f}" y="{y + dy:.1f}" class="label med">{escape(label)}</text>'
        )
    for index, obstacle in enumerate(obstacles):
        center = project(obstacle)
        if center is None:
            continue
        width = _number(obstacle.get("size_x_m"))
        depth = _number(obstacle.get("size_y_m"))
        height = _number(obstacle.get("size_z_m"))
        size = "×".join(f"{value:.0f}" for value in (width, depth, height) if value is not None)
        suffix = f" {size}m" if size else ""
        x, y = sx(center[0]), sy(center[1])
        svg.append(
            f'<text x="{x + 12:.1f}" y="{y - 18:.1f}" class="label med">O{index + 1} obstacle{escape(suffix)}</text>'
        )

    footer_y = _HEIGHT - 82
    svg.append(
        f'<rect x="42" y="{footer_y - 34}" width="1516" height="76" rx="10" fill="{_COLORS["panel"]}" stroke="#334155"/>'
    )
    footer = [
        ("task", task_id),
        ("task status", task_status),
        ("Recovery model", model_id),
        ("Recoveries", f"{len(avoidances)} · {recovery_status}"),
        ("route resume", resume_seq_text),
        ("battery", f"{battery_percent:.1f}%" if battery_percent is not None else "-"),
    ]
    footer_x = 62.0
    for label, value in footer:
        svg.append(
            f'<text x="{footer_x:.1f}" y="{footer_y - 7}" class="muted small">{escape(label)}</text>'
        )
        svg.append(
            f'<text x="{footer_x:.1f}" y="{footer_y + 23}" class="med" font-weight="700">{escape(str(value))}</text>'
        )
        footer_x += 245.0
    svg.append(
        f'<text x="42" y="166" class="muted small">source={escape(str(model.get("observed_trace_source") or "-"))} · geometry={escape(geometry_status)} · source_sha256={source_hash[:16]}… · display-only summary, not verifier authority</text>'
    )
    svg.append("</svg>")
    return "\n".join(svg)


def write_mission_route_evidence_artifacts(
    *,
    model: dict[str, Any],
    output_dir: Path,
    stem: str | None = None,
) -> dict[str, Any]:
    """Persist a terminal SVG and a hash-bound manifest beside it."""

    task_id = _safe_slug(model.get("task_id"))
    artifact_stem = stem or f"{task_id}_e2e_route_evidence"
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / f"{artifact_stem}.svg"
    manifest_path = output_dir / f"{artifact_stem}.json"
    svg = mission_route_evidence_svg(model)
    svg_bytes = svg.encode("utf-8")
    source_sha = mission_route_evidence_source_sha256(model)
    svg_sha = sha256(svg_bytes).hexdigest()
    svg_path.write_bytes(svg_bytes)
    manifest = {
        "schema_version": ROUTE_EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "task_id": model.get("task_id"),
        "task_status": model.get("task_status"),
        "task_updated_at": model.get("task_updated_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "image_path": str(svg_path),
        "image_format": "svg",
        "source_map_schema_version": model.get("schema_version"),
        "source_payload_sha256": source_sha,
        "svg_sha256": svg_sha,
        "observed_trace_source": model.get("observed_trace_source"),
        "display_only": True,
        "verifier_input": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "physical_execution_claimed": False,
        "claim_boundary": (
            "This image summarizes persisted route observations. The source task "
            "artifacts remain authoritative for approval, dispatch, verification, "
            "completion, delivery, and physical-execution claims."
        ),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "svg_path": svg_path,
        "manifest_path": manifest_path,
        "svg_bytes": svg_bytes,
        "source_payload_sha256": source_sha,
        "svg_sha256": svg_sha,
    }
