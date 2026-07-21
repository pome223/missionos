"""Contract tests for the map visual observation layer (issue #83 follow-up).

These lock the fail-closed projection rule and the evidence-only boundary: a
camera candidate becomes a corroborated map marker only when an independent
LiDAR candidate and the robot's map pose are both present, and no map coordinate
is ever invented from a camera claim alone.
"""

from __future__ import annotations

import math

from src.runtime.perception_visual_observation import (
    DEFAULT_SEMANTIC_CANDIDATE,
    VISUAL_OBSERVATION_SCHEMA_VERSION,
    build_visual_observation,
    build_visual_observations,
    project_lidar_candidate_to_map,
    visual_observation_collision_candidates,
)


FRAME_REF = f"sha256:{'a' * 64}"


def _bound_binding() -> dict[str, object]:
    return {
        "binding_id": "perception_binding:abc123",
        "temporal_status": "bound",
        "spatial_status": "bound",
        "target_identity_status": "bound",
        "camera_horizontal_sector": "center",
        "target_center_x_normalized": 0.5,
        "target_candidate_id": "lidar_candidate:deadbeef",
    }


def _sensor(*, projectable: bool = True) -> dict[str, object]:
    sensor: dict[str, object] = {
        "lidar_obstacle_observed": True,
        "lidar_horizontal_sector": "center",
        "lidar_candidate_bearing_rad": 0.0,
        "lidar_candidate_range_m": 0.677,
        "target_candidate_id": "lidar_candidate:deadbeef",
        "lidar_evidence_ref": "laser_scan:deadbeef",
    }
    if projectable:
        sensor.update(
            {
                "lidar_map_tf_observed": True,
                "lidar_map_tf_x_m": 1.0,
                "lidar_map_tf_y_m": 2.0,
                "lidar_map_tf_yaw_rad": 0.0,
                "lidar_map_tf_source_frame_id": "base_scan",
                "lidar_map_tf_stamp": "2026-07-21T12:00:00+00:00",
            }
        )
    return sensor


def _claim(**overrides: object) -> dict[str, object]:
    claim = {
        "claim_kind": "corridor_blocked_by_object",
        "source_frame_ref": FRAME_REF,
        "confidence": 0.91,
        "corroboration_binding": _bound_binding(),
    }
    claim.update(overrides)
    return claim


def test_projection_places_forward_candidate_ahead_of_lidar() -> None:
    projection = project_lidar_candidate_to_map(_sensor())

    assert projection.status == "projected"
    # lidar_frame at map (1,2) with 0 yaw, candidate 0.677m dead ahead -> +x.
    assert projection.x_m == round(1.0 + 0.677, 4)
    assert projection.y_m == 2.0
    assert projection.tf_source_frame_id == "base_scan"
    assert projection.tf_lookup_stamp == "2026-07-21T12:00:00+00:00"


def test_projection_respects_lidar_frame_yaw() -> None:
    sensor = _sensor()
    sensor["lidar_map_tf_yaw_rad"] = math.pi / 2  # lidar frame facing +y
    projection = project_lidar_candidate_to_map(sensor)

    assert projection.status == "projected"
    assert projection.x_m == 1.0
    assert projection.y_m == round(2.0 + 0.677, 4)


def test_projection_unavailable_without_lidar_map_transform() -> None:
    projection = project_lidar_candidate_to_map(_sensor(projectable=False))

    assert projection.status == "unavailable"
    assert projection.x_m is None and projection.y_m is None
    assert "lidar_map_transform_not_observed" in projection.blocking_reasons


def test_projection_unavailable_without_scan_timestamp() -> None:
    sensor = _sensor()
    # A transform without the scan stamp must not be accepted as scan-time-bound.
    sensor["lidar_map_tf_stamp"] = ""
    projection = project_lidar_candidate_to_map(sensor)

    assert projection.status == "unavailable"
    assert "lidar_map_transform_stamp_missing" in projection.blocking_reasons


def test_projection_unavailable_without_lidar_candidate() -> None:
    sensor = _sensor()
    sensor["lidar_obstacle_observed"] = False
    sensor["lidar_candidate_range_m"] = None

    projection = project_lidar_candidate_to_map(sensor)

    assert projection.status == "unavailable"
    assert projection.x_m is None and projection.y_m is None


def test_corroborated_observation_is_teal_layer_with_coordinate() -> None:
    observation = build_visual_observation(
        claim=_claim(),
        sensor_observation=_sensor(),
    )

    assert observation is not None
    assert observation.schema_version == VISUAL_OBSERVATION_SCHEMA_VERSION
    assert observation.semantic_candidate == DEFAULT_SEMANTIC_CANDIDATE
    assert observation.binding_status == "bound"
    assert observation.display_status == "camera_lidar_corroborated"
    assert observation.map_projection.status == "projected"
    assert observation.evidence_only is True
    assert observation.approval_created is False
    assert observation.dispatch_authority_created is False


def test_camera_only_when_pose_missing_stays_unprojected() -> None:
    observation = build_visual_observation(
        claim=_claim(),
        sensor_observation=_sensor(projectable=False),
    )

    assert observation is not None
    # Binding may be bound, but without a projectable pose the map cannot place
    # a corroborated coordinate, so it must fall back to the camera-only layer.
    assert observation.display_status == "camera_only"
    assert observation.map_projection.status == "unavailable"
    assert observation.map_projection.x_m is None


def test_camera_only_when_binding_not_bound() -> None:
    binding = _bound_binding()
    binding["spatial_status"] = "mismatched"
    observation = build_visual_observation(
        claim=_claim(corroboration_binding=binding),
        sensor_observation=_sensor(),
    )

    assert observation is not None
    assert observation.binding_status == "mismatched"
    assert observation.display_status == "camera_only"


def test_path_clear_claim_produces_no_marker() -> None:
    observation = build_visual_observation(
        claim=_claim(claim_kind="path_clear"),
        sensor_observation=_sensor(),
    )

    assert observation is None


def test_malformed_claim_produces_no_marker() -> None:
    assert (
        build_visual_observation(
            claim={"claim_kind": "", "source_frame_ref": "", "confidence": None},
            sensor_observation=_sensor(),
        )
        is None
    )


def test_build_list_dedupes_and_drops_non_markers() -> None:
    observations = build_visual_observations(
        claims=[
            _claim(),
            _claim(),  # identical -> deduped
            _claim(claim_kind="path_clear"),  # dropped
            "not-a-mapping",  # ignored
        ],
        sensor_observation=_sensor(),
    )

    assert len(observations) == 1
    assert observations[0]["display_status"] == "camera_lidar_corroborated"
    assert observations[0]["evidence_only"] is True


def test_source_backed_collision_volume_is_preserved_for_generic_3d() -> None:
    sensor = _sensor()
    sensor["collision_volume"] = {
        "x_m": 1.677,
        "y_m": 2.0,
        "z_m": 0.4,
        "size_x_m": 0.5,
        "size_y_m": 0.3,
        "size_z_m": 0.8,
        "frame_id": "map",
        "geometry_source": "depth_cluster_aabb",
        "evidence_ref": "depth_cluster:deadbeef",
    }
    observation = build_visual_observation(
        claim=_claim(),
        sensor_observation=sensor,
        semantic_candidate="robot_dog",
    )

    assert observation is not None
    volumes, unresolved = visual_observation_collision_candidates(
        [observation.model_dump(mode="json")]
    )
    assert unresolved == []
    assert volumes[0]["obstacle_ref"] == observation.observation_id
    assert volumes[0]["semantic_candidate"] == "robot_dog"
    assert volumes[0]["geometry_source"] == "depth_cluster_aabb"


def test_visual_candidate_without_volume_is_unresolved_not_guessed() -> None:
    observation = build_visual_observation(
        claim=_claim(),
        sensor_observation=_sensor(),
    )

    assert observation is not None
    volumes, unresolved = visual_observation_collision_candidates(
        [observation.model_dump(mode="json")]
    )
    assert volumes == []
    assert unresolved == [observation.observation_id]


def test_camera_only_candidate_is_not_claimed_as_route_addressable() -> None:
    observation = build_visual_observation(
        claim=_claim(),
        sensor_observation=_sensor(projectable=False),
    )

    assert observation is not None
    volumes, unresolved = visual_observation_collision_candidates(
        [observation.model_dump(mode="json")]
    )
    assert volumes == []
    assert unresolved == []
