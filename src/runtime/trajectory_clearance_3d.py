"""Backend-neutral swept-volume clearance evidence for ground robots.

This module evaluates an observed map-frame trajectory against source-backed
axis-aligned obstacle volumes.  It treats the robot as a vertical collision
cylinder, not a point.  The result is evidence only: it cannot approve,
dispatch, execute, or complete a mission.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


TRAJECTORY_CLEARANCE_3D_SCHEMA_VERSION = "missionos_trajectory_clearance_3d.v2"


class RobotCollisionEnvelope(BaseModel):
    """Conservative vertical-cylinder collision envelope in the base frame."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    radius_m: float = Field(gt=0.0)
    z_min_m: float
    z_max_m: float
    frame_id: str = "base_footprint"
    geometry_source: str


class ObstacleCollisionVolume(BaseModel):
    """One source-backed map-frame axis-aligned obstacle volume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    obstacle_ref: str
    x_m: float
    y_m: float
    z_m: float
    size_x_m: float = Field(gt=0.0)
    size_y_m: float = Field(gt=0.0)
    size_z_m: float = Field(gt=0.0)
    frame_id: str = "map"
    geometry_source: str
    semantic_candidate: str | None = None
    evidence_ref: str | None = None


class ObstacleClearance3DResult(BaseModel):
    """Geometry-only result for one obstacle candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    obstacle_ref: str
    semantic_candidate: str | None = None
    status: Literal["verified_clear", "collision_observed", "unavailable"]
    collision_observed: bool = False
    minimum_surface_clearance_m: float | None = None
    geometry_source: str | None = None
    evidence_ref: str | None = None
    blocking_reasons: tuple[str, ...] = ()


class TrajectoryClearance3D(BaseModel):
    """Result of a swept collision-envelope check against obstacle volumes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_trajectory_clearance_3d.v2"] = (
        TRAJECTORY_CLEARANCE_3D_SCHEMA_VERSION
    )
    status: Literal["verified_clear", "collision_observed", "unavailable"]
    clearance_observed: bool = False
    collision_observed: bool = False
    minimum_surface_clearance_m: float | None = None
    robot_collision_envelope: RobotCollisionEnvelope | None = None
    obstacle_volumes: tuple[ObstacleCollisionVolume, ...] = ()
    candidate_results: tuple[ObstacleClearance3DResult, ...] = ()
    unresolved_candidate_refs: tuple[str, ...] = ()
    trajectory_frame_id: str | None = None
    trajectory_stream_count: int = 0
    trajectory_segment_count: int = 0
    blocking_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_only: Literal[True] = True
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    completion_claimed: Literal[False] = False


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point_inside_rect(point: tuple[float, float], rect: tuple[float, float, float, float]) -> bool:
    min_x, max_x, min_y, max_y = rect
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


def _orientation(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> float:
    return (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])


def _on_segment(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    return min(a[0], c[0]) <= b[0] <= max(a[0], c[0]) and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)
    eps = 1e-9
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if abs(o1) <= eps and _on_segment(a1, b1, a2):
        return True
    if abs(o2) <= eps and _on_segment(a1, b2, a2):
        return True
    if abs(o3) <= eps and _on_segment(b1, a1, b2):
        return True
    return abs(o4) <= eps and _on_segment(b1, a2, b2)


def _rect_edges(
    rect: tuple[float, float, float, float],
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    min_x, max_x, min_y, max_y = rect
    return (
        ((min_x, min_y), (max_x, min_y)),
        ((max_x, min_y), (max_x, max_y)),
        ((max_x, max_y), (min_x, max_y)),
        ((min_x, max_y), (min_x, min_y)),
    )


def _segment_intersects_rect(
    start: tuple[float, float],
    end: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    return (
        _point_inside_rect(start, rect)
        or _point_inside_rect(end, rect)
        or any(
            _segments_intersect(start, end, edge_start, edge_end)
            for edge_start, edge_end in _rect_edges(rect)
        )
    )


def _point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    amount = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq,
        ),
    )
    closest = (start[0] + amount * dx, start[1] + amount * dy)
    return math.hypot(point[0] - closest[0], point[1] - closest[1])


def _segment_rect_distance(
    start: tuple[float, float],
    end: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> float:
    if _segment_intersects_rect(start, end, rect):
        return 0.0
    distances: list[float] = []
    for edge_start, edge_end in _rect_edges(rect):
        distances.extend(
            (
                _point_segment_distance(start, edge_start, edge_end),
                _point_segment_distance(end, edge_start, edge_end),
                _point_segment_distance(edge_start, start, end),
                _point_segment_distance(edge_end, start, end),
            )
        )
    return min(distances)


def _parse_envelope(payload: Mapping[str, Any] | None) -> RobotCollisionEnvelope | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        envelope = RobotCollisionEnvelope.model_validate(payload)
    except (TypeError, ValueError):
        return None
    return envelope if envelope.z_max_m > envelope.z_min_m else None


def _parse_volumes(payloads: Sequence[Mapping[str, Any]]) -> tuple[ObstacleCollisionVolume, ...]:
    volumes: list[ObstacleCollisionVolume] = []
    for payload in payloads:
        try:
            volume = ObstacleCollisionVolume.model_validate(payload)
        except (TypeError, ValueError):
            continue
        if volume.frame_id == "map":
            volumes.append(volume)
    return tuple(volumes)


def _trajectory_streams(
    payloads: Sequence[Sequence[Mapping[str, Any]]],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    streams: list[tuple[tuple[float, float], ...]] = []
    for payload_stream in payloads:
        points: list[tuple[float, float]] = []
        for payload in payload_stream:
            if str(payload.get("frame_id") or "") != "map":
                continue
            x_m = _finite(payload.get("x_m"))
            y_m = _finite(payload.get("y_m"))
            if x_m is not None and y_m is not None:
                points.append((x_m, y_m))
        if len(points) >= 2:
            streams.append(tuple(points))
    return tuple(streams)


def assess_ground_robot_trajectory_clearance_3d(
    *,
    trajectory_streams: Sequence[Sequence[Mapping[str, Any]]],
    robot_collision_envelope: Mapping[str, Any] | None,
    obstacle_volumes: Sequence[Mapping[str, Any]],
    unresolved_candidate_refs: Sequence[str] = (),
    base_z_m: float = 0.0,
) -> TrajectoryClearance3D:
    """Evaluate a ground-robot swept cylinder against map-frame AABBs.

    The fixed ``base_z_m`` is valid only when supplied by the robot/runtime
    profile for a planar map. It is recorded as a limitation rather than
    inferred from missing trajectory z samples.
    """

    envelope = _parse_envelope(robot_collision_envelope)
    volumes = _parse_volumes(obstacle_volumes)
    streams = _trajectory_streams(trajectory_streams)
    unresolved_refs = tuple(
        dict.fromkeys(str(ref).strip() for ref in unresolved_candidate_refs if str(ref).strip())
    )
    reasons: list[str] = []
    if envelope is None:
        reasons.append("robot_collision_envelope_missing")
    if not volumes:
        reasons.append("source_backed_obstacle_volumes_missing")
    if not streams:
        reasons.append("map_frame_observed_trajectory_segments_missing")
    if unresolved_refs:
        reasons.append("obstacle_candidate_collision_volume_unavailable")
    fatal_reasons = [
        reason for reason in reasons if reason != "obstacle_candidate_collision_volume_unavailable"
    ]
    if fatal_reasons:
        return TrajectoryClearance3D(
            status="unavailable",
            robot_collision_envelope=envelope,
            obstacle_volumes=volumes,
            candidate_results=tuple(
                ObstacleClearance3DResult(
                    obstacle_ref=ref,
                    status="unavailable",
                    blocking_reasons=("collision_volume_unavailable",),
                )
                for ref in unresolved_refs
            ),
            unresolved_candidate_refs=unresolved_refs,
            trajectory_frame_id="map" if streams else None,
            trajectory_stream_count=len(streams),
            trajectory_segment_count=sum(len(stream) - 1 for stream in streams),
            blocking_reasons=tuple(reasons),
            limitations=("planar_base_z_from_robot_runtime_profile",),
        )

    robot_min_z = base_z_m + envelope.z_min_m
    robot_max_z = base_z_m + envelope.z_max_m
    collision = False
    minimum_clearance: float | None = None
    candidate_results: list[ObstacleClearance3DResult] = []
    for volume in volumes:
        raw_rect = (
            volume.x_m - volume.size_x_m / 2.0,
            volume.x_m + volume.size_x_m / 2.0,
            volume.y_m - volume.size_y_m / 2.0,
            volume.y_m + volume.size_y_m / 2.0,
        )
        expanded_rect = (
            raw_rect[0] - envelope.radius_m,
            raw_rect[1] + envelope.radius_m,
            raw_rect[2] - envelope.radius_m,
            raw_rect[3] + envelope.radius_m,
        )
        obstacle_min_z = volume.z_m - volume.size_z_m / 2.0
        obstacle_max_z = volume.z_m + volume.size_z_m / 2.0
        vertical_gap = max(
            obstacle_min_z - robot_max_z,
            robot_min_z - obstacle_max_z,
            0.0,
        )
        z_overlaps = vertical_gap <= 0.0
        candidate_collision = False
        candidate_minimum_clearance: float | None = None
        for stream in streams:
            for start, end in zip(stream, stream[1:], strict=False):
                planar_surface_gap = max(
                    _segment_rect_distance(start, end, raw_rect) - envelope.radius_m,
                    0.0,
                )
                surface_clearance = math.hypot(planar_surface_gap, vertical_gap)
                minimum_clearance = (
                    surface_clearance
                    if minimum_clearance is None
                    else min(minimum_clearance, surface_clearance)
                )
                candidate_minimum_clearance = (
                    surface_clearance
                    if candidate_minimum_clearance is None
                    else min(candidate_minimum_clearance, surface_clearance)
                )
                if z_overlaps and _segment_intersects_rect(start, end, expanded_rect):
                    collision = True
                    candidate_collision = True
                    minimum_clearance = 0.0
                    candidate_minimum_clearance = 0.0
        candidate_results.append(
            ObstacleClearance3DResult(
                obstacle_ref=volume.obstacle_ref,
                semantic_candidate=volume.semantic_candidate,
                status=("collision_observed" if candidate_collision else "verified_clear"),
                collision_observed=candidate_collision,
                minimum_surface_clearance_m=(
                    round(candidate_minimum_clearance, 6)
                    if candidate_minimum_clearance is not None
                    else None
                ),
                geometry_source=volume.geometry_source,
                evidence_ref=volume.evidence_ref,
            )
        )

    candidate_results.extend(
        ObstacleClearance3DResult(
            obstacle_ref=ref,
            status="unavailable",
            blocking_reasons=("collision_volume_unavailable",),
        )
        for ref in unresolved_refs
    )
    status: Literal["verified_clear", "collision_observed", "unavailable"] = (
        "collision_observed"
        if collision
        else "unavailable"
        if unresolved_refs
        else "verified_clear"
    )

    return TrajectoryClearance3D(
        status=status,
        clearance_observed=status == "verified_clear",
        collision_observed=collision,
        minimum_surface_clearance_m=(
            round(minimum_clearance, 6) if minimum_clearance is not None else None
        ),
        robot_collision_envelope=envelope,
        obstacle_volumes=volumes,
        candidate_results=tuple(candidate_results),
        unresolved_candidate_refs=unresolved_refs,
        trajectory_frame_id="map",
        trajectory_stream_count=len(streams),
        trajectory_segment_count=sum(len(stream) - 1 for stream in streams),
        blocking_reasons=(
            ("obstacle_candidate_collision_volume_unavailable",)
            if unresolved_refs and not collision
            else ()
        ),
        limitations=("planar_base_z_from_robot_runtime_profile",),
    )


__all__ = [
    "ObstacleCollisionVolume",
    "ObstacleClearance3DResult",
    "RobotCollisionEnvelope",
    "TRAJECTORY_CLEARANCE_3D_SCHEMA_VERSION",
    "TrajectoryClearance3D",
    "assess_ground_robot_trajectory_clearance_3d",
]
