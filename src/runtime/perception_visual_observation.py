"""Map-facing visual observation layer for corroborated perception claims.

This module turns an evidence-only perception claim plus its camera/LiDAR
observation into a ``visual_observation.v1`` record for the indoor map. It is a
display-evidence projection: it never approves, dispatches, executes, or claims
mission completion, and it refuses to invent a map coordinate when the robot
pose or LiDAR range needed to project the candidate is missing.

The static scene ``obstacles`` layer (the boxes the test harness spawned) is
intentionally kept separate from this layer, which shows only what the robot's
own camera + LiDAR observed and how strongly the two were bound.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field


VISUAL_OBSERVATION_SCHEMA_VERSION = "missionos_visual_observation.v1"

# The default semantic label. A block is honestly an "unknown_obstacle" until a
# visually distinguishable model and a classifier that names it are supplied;
# "humanoid"/"robot_dog" belong to an opt-in scenario, never to a plain box.
DEFAULT_SEMANTIC_CANDIDATE = "unknown_obstacle"

MapProjectionStatus = Literal["projected", "unavailable"]
VisualObservationDisplayStatus = Literal[
    "camera_lidar_corroborated",
    "camera_only",
]
BindingStatus = Literal["bound", "unbound", "mismatched", "stale", "unavailable"]


class VisualObservationMapProjection(BaseModel):
    """Where the LiDAR candidate lands in the map frame, or why it cannot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MapProjectionStatus
    x_m: float | None = None
    y_m: float | None = None
    range_m: float | None = None
    bearing_rad: float | None = None
    # The exact map<-lidar_frame transform used, resolved at the LaserScan
    # timestamp. These are the transform components actually applied, not the
    # robot base pose, so no laser->base_link approximation is involved.
    tf_map_x_m: float | None = None
    tf_map_y_m: float | None = None
    tf_map_yaw_rad: float | None = None
    tf_source_frame_id: str = ""
    tf_lookup_stamp: str = ""
    frame_id: str = "map"
    blocking_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class VisualObservationCollisionVolume(BaseModel):
    """Independent sensor-backed map-frame volume for one visual candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x_m: float
    y_m: float
    z_m: float
    size_x_m: float = Field(gt=0.0)
    size_y_m: float = Field(gt=0.0)
    size_z_m: float = Field(gt=0.0)
    frame_id: Literal["map"] = "map"
    geometry_source: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)


class VisualObservation(BaseModel):
    """One camera candidate placed on the map as observation-only evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_visual_observation.v1"] = VISUAL_OBSERVATION_SCHEMA_VERSION
    observation_id: str
    semantic_candidate: str = DEFAULT_SEMANTIC_CANDIDATE
    claim_kind: str
    source_frame_ref: str
    camera_confidence: float = Field(ge=0.0, le=1.0)
    camera_horizontal_sector: str = "unknown"
    target_center_x_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    map_projection: VisualObservationMapProjection
    collision_volume: VisualObservationCollisionVolume | None = None
    lidar_candidate_ref: str = ""
    corroboration_binding_ref: str = ""
    binding_status: BindingStatus
    display_status: VisualObservationDisplayStatus
    display_layer: Literal["visual_observation"] = "visual_observation"
    limitations: tuple[str, ...] = (
        "visual_observation_is_camera_lidar_evidence_not_scene_ground_truth",
        "geometric_binding_does_not_verify_semantic_object_identity",
    )
    evidence_only: Literal[True] = True
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    completion_claimed: Literal[False] = False


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def project_lidar_candidate_to_map(
    sensor_observation: Mapping[str, Any] | None,
) -> VisualObservationMapProjection:
    """Project the LiDAR candidate into map coordinates, or fail closed.

    Requires an observed LiDAR candidate (bearing + range in the LiDAR frame)
    and the exact ``map <- lidar_frame`` transform resolved at the LaserScan
    timestamp. The bearing/range are measured in the LiDAR frame, so applying
    the ``map <- lidar_frame`` transform places the candidate correctly without
    any laser->base_link approximation. If the LiDAR candidate or that specific
    transform is missing or non-finite, the result is ``unavailable`` and no
    coordinate is produced, so the map never shows a guessed marker.
    """

    sensor = dict(sensor_observation or {})
    reasons: list[str] = []
    if sensor.get("lidar_obstacle_observed") is not True:
        reasons.append("lidar_candidate_not_observed")
    bearing = _finite_float(sensor.get("lidar_candidate_bearing_rad"))
    range_m = _finite_float(sensor.get("lidar_candidate_range_m"))
    if bearing is None:
        reasons.append("lidar_candidate_bearing_missing")
    if range_m is None or range_m <= 0.0:
        reasons.append("lidar_candidate_range_missing")
    tf_x = _finite_float(sensor.get("lidar_map_tf_x_m"))
    tf_y = _finite_float(sensor.get("lidar_map_tf_y_m"))
    tf_yaw = _finite_float(sensor.get("lidar_map_tf_yaw_rad"))
    tf_frame = str(sensor.get("lidar_map_tf_source_frame_id") or "")
    tf_stamp = str(sensor.get("lidar_map_tf_stamp") or "")
    if sensor.get("lidar_map_tf_observed") is not True:
        reasons.append("lidar_map_transform_not_observed")
    if tf_x is None or tf_y is None or tf_yaw is None:
        reasons.append("lidar_map_transform_missing")
    if not tf_frame:
        reasons.append("lidar_map_transform_source_frame_missing")
    if not tf_stamp:
        # A timestamp is required so callers cannot pass a latest-time transform
        # and claim it matched the scan moment.
        reasons.append("lidar_map_transform_stamp_missing")

    if reasons:
        return VisualObservationMapProjection(
            status="unavailable",
            range_m=range_m,
            bearing_rad=bearing,
            tf_map_x_m=tf_x,
            tf_map_y_m=tf_y,
            tf_map_yaw_rad=tf_yaw,
            tf_source_frame_id=tf_frame,
            tf_lookup_stamp=tf_stamp,
            blocking_reasons=tuple(dict.fromkeys(reasons)),
        )

    # Candidate point in the LiDAR frame, then apply the exact map<-lidar_frame
    # transform resolved at the scan timestamp.
    local_x = range_m * math.cos(bearing)
    local_y = range_m * math.sin(bearing)
    map_x = tf_x + local_x * math.cos(tf_yaw) - local_y * math.sin(tf_yaw)
    map_y = tf_y + local_x * math.sin(tf_yaw) + local_y * math.cos(tf_yaw)
    return VisualObservationMapProjection(
        status="projected",
        x_m=round(map_x, 4),
        y_m=round(map_y, 4),
        range_m=range_m,
        bearing_rad=bearing,
        tf_map_x_m=tf_x,
        tf_map_y_m=tf_y,
        tf_map_yaw_rad=tf_yaw,
        tf_source_frame_id=tf_frame,
        tf_lookup_stamp=tf_stamp,
    )


def _binding_status(binding: Mapping[str, Any] | None) -> BindingStatus:
    """Reduce the corroboration binding to one map-facing status."""

    if not binding:
        return "unavailable"
    temporal = str(binding.get("temporal_status") or "unavailable")
    spatial = str(binding.get("spatial_status") or "unavailable")
    target = str(binding.get("target_identity_status") or "unavailable")
    if temporal == "bound" and spatial == "bound" and target == "bound":
        return "bound"
    for status in (spatial, target, temporal):
        if status in {"mismatched", "stale"}:
            return status  # type: ignore[return-value]
    return "unbound"


def _sensor_collision_volume(
    sensor: Mapping[str, Any],
) -> VisualObservationCollisionVolume | None:
    """Accept a complete sensor-provided volume without inventing dimensions."""

    payload = sensor.get("collision_volume")
    if not isinstance(payload, Mapping):
        return None
    try:
        return VisualObservationCollisionVolume.model_validate(payload)
    except (TypeError, ValueError):
        return None


def build_visual_observation(
    *,
    claim: Mapping[str, Any],
    sensor_observation: Mapping[str, Any] | None,
    semantic_candidate: str = DEFAULT_SEMANTIC_CANDIDATE,
) -> VisualObservation | None:
    """Build a map visual observation from one corroborated perception claim.

    Returns ``None`` for claims that carry no obstacle-bearing camera geometry
    (e.g. ``path_clear``), so the map layer only ever shows candidate objects.
    """

    claim_kind = str(claim.get("claim_kind") or "").strip()
    source_frame_ref = str(claim.get("source_frame_ref") or "").strip()
    confidence = _finite_float(claim.get("confidence"))
    if not claim_kind or not source_frame_ref or confidence is None:
        return None
    if claim_kind == "path_clear":
        return None

    binding = claim.get("corroboration_binding")
    binding = binding if isinstance(binding, Mapping) else None
    sensor = dict(sensor_observation or {})
    projection = project_lidar_candidate_to_map(sensor)
    collision_volume = _sensor_collision_volume(sensor)
    binding_status = _binding_status(binding)
    display_status: VisualObservationDisplayStatus = (
        "camera_lidar_corroborated"
        if binding_status == "bound" and projection.status == "projected"
        else "camera_only"
    )

    camera_sector = str(
        (binding or {}).get("camera_horizontal_sector")
        or claim.get("camera_horizontal_sector")
        or sensor.get("camera_horizontal_sector")
        or "unknown"
    )
    target_center = _finite_float((binding or {}).get("target_center_x_normalized"))
    if target_center is not None and not (0.0 <= target_center <= 1.0):
        target_center = None
    lidar_candidate_ref = str(
        sensor.get("target_candidate_id")
        or sensor.get("lidar_evidence_ref")
        or (binding or {}).get("target_candidate_id")
        or ""
    )
    binding_ref = str((binding or {}).get("binding_id") or "")

    identity_payload = {
        "source_frame_ref": source_frame_ref,
        "claim_kind": claim_kind,
        "binding_ref": binding_ref,
        "lidar_candidate_ref": lidar_candidate_ref,
        "map_x_m": projection.x_m,
        "map_y_m": projection.y_m,
    }
    return VisualObservation(
        observation_id=f"visual_observation:{_canonical_sha256(identity_payload)[:16]}",
        semantic_candidate=semantic_candidate or DEFAULT_SEMANTIC_CANDIDATE,
        claim_kind=claim_kind,
        source_frame_ref=source_frame_ref,
        camera_confidence=max(0.0, min(1.0, confidence)),
        camera_horizontal_sector=camera_sector,
        target_center_x_normalized=target_center,
        map_projection=projection,
        collision_volume=collision_volume,
        lidar_candidate_ref=lidar_candidate_ref,
        corroboration_binding_ref=binding_ref,
        binding_status=binding_status,
        display_status=display_status,
    )


def build_visual_observations(
    *,
    claims: Any,
    sensor_observation: Mapping[str, Any] | None,
    semantic_candidate: str = DEFAULT_SEMANTIC_CANDIDATE,
) -> list[dict[str, Any]]:
    """Build the map ``visual_observations`` list from perception claims."""

    observations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for claim in claims or ():
        if not isinstance(claim, Mapping):
            continue
        observation = build_visual_observation(
            claim=claim,
            sensor_observation=sensor_observation,
            semantic_candidate=semantic_candidate,
        )
        if observation is None or observation.observation_id in seen_ids:
            continue
        seen_ids.add(observation.observation_id)
        observations.append(observation.model_dump(mode="json"))
    return observations


def visual_observation_collision_candidates(
    observations: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split visual candidates into resolved volumes and fail-closed refs.

    Semantic labels are copied for display only. Geometry eligibility depends
    solely on a complete source-backed collision volume.
    """

    volumes: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for payload in observations or ():
        if not isinstance(payload, Mapping):
            continue
        try:
            observation = VisualObservation.model_validate(payload)
        except (TypeError, ValueError):
            continue
        if observation.collision_volume is None:
            # A camera-only observation is not localized enough to decide
            # whether the observed trajectory could intersect it. A projected
            # candidate is route-addressable, so missing XYZ extents must fail
            # closed instead of being approximated as a point obstacle.
            if observation.map_projection.status == "projected":
                unresolved.append(observation.observation_id)
            continue
        volumes.append(
            {
                "obstacle_ref": observation.observation_id,
                **observation.collision_volume.model_dump(mode="json"),
                "semantic_candidate": observation.semantic_candidate,
            }
        )
    return volumes, list(dict.fromkeys(unresolved))


__all__ = [
    "DEFAULT_SEMANTIC_CANDIDATE",
    "VISUAL_OBSERVATION_SCHEMA_VERSION",
    "VisualObservation",
    "VisualObservationCollisionVolume",
    "VisualObservationMapProjection",
    "build_visual_observation",
    "build_visual_observations",
    "project_lidar_candidate_to_map",
    "visual_observation_collision_candidates",
]
