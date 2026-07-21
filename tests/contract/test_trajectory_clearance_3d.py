from __future__ import annotations

from src.runtime.trajectory_clearance_3d import (
    assess_ground_robot_trajectory_clearance_3d,
)


ROBOT_ENVELOPE = {
    "radius_m": 0.19,
    "z_min_m": -0.01,
    "z_max_m": 0.14,
    "frame_id": "base_footprint",
    "geometry_source": "turtlebot3_waffle_pi_model_sdf_collision_envelope",
}
OBSTACLE = {
    "obstacle_ref": "missionos_closed_door_blocker",
    "x_m": 0.0,
    "y_m": 0.0,
    "z_m": 0.25,
    "size_x_m": 0.32,
    "size_y_m": 0.32,
    "size_z_m": 0.5,
    "frame_id": "map",
    "geometry_source": "opt_in_gazebo_sdf_collision",
}


def _stream(y_m: float) -> list[dict[str, object]]:
    return [
        {"x_m": -1.0, "y_m": y_m, "frame_id": "map"},
        {"x_m": 1.0, "y_m": y_m, "frame_id": "map"},
    ]


def test_swept_robot_envelope_detects_collision_when_centerline_misses_box() -> None:
    result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.30)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[OBSTACLE],
    )

    # The point/centreline is 0.14 m outside the 0.16 m box half-width, but the
    # 0.19 m robot radius overlaps it. This is the bug a point-only check misses.
    assert result.status == "collision_observed"
    assert result.collision_observed is True
    assert result.clearance_observed is False
    assert result.minimum_surface_clearance_m == 0.0


def test_swept_robot_envelope_verifies_clear_path_with_surface_clearance() -> None:
    result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.60)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[OBSTACLE],
    )

    assert result.status == "verified_clear"
    assert result.clearance_observed is True
    assert result.collision_observed is False
    assert result.minimum_surface_clearance_m == 0.25
    assert result.candidate_results[0].obstacle_ref == OBSTACLE["obstacle_ref"]
    assert result.candidate_results[0].status == "verified_clear"
    assert result.candidate_results[0].minimum_surface_clearance_m == 0.25


def test_vertical_separation_is_counted_as_3d_clearance() -> None:
    overhead = {**OBSTACLE, "z_m": 1.0, "size_z_m": 0.2}
    result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.0)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[overhead],
    )

    assert result.status == "verified_clear"
    assert result.minimum_surface_clearance_m == 0.76


def test_missing_volume_evidence_fails_closed_without_clearance_claim() -> None:
    result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.60)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[],
    )

    assert result.status == "unavailable"
    assert result.clearance_observed is False
    assert "source_backed_obstacle_volumes_missing" in result.blocking_reasons
    assert result.approval_created is False
    assert result.dispatch_authority_created is False
    assert result.physical_execution_invoked is False
    assert result.completion_claimed is False


def test_non_map_or_single_point_trajectory_is_not_3d_verification() -> None:
    result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[
            [{"x_m": 0.0, "y_m": 0.0, "frame_id": "odom"}],
        ],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[OBSTACLE],
    )

    assert result.status == "unavailable"
    assert "map_frame_observed_trajectory_segments_missing" in result.blocking_reasons


def test_semantic_label_does_not_change_candidate_geometry_result() -> None:
    humanoid = {**OBSTACLE, "semantic_candidate": "humanoid"}
    unknown = {**OBSTACLE, "semantic_candidate": "unknown_obstacle"}

    humanoid_result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.60)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[humanoid],
    )
    unknown_result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.60)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[unknown],
    )

    assert humanoid_result.minimum_surface_clearance_m == (
        unknown_result.minimum_surface_clearance_m
    )
    assert humanoid_result.candidate_results[0].semantic_candidate == "humanoid"
    assert unknown_result.candidate_results[0].semantic_candidate == "unknown_obstacle"


def test_unresolved_candidate_fails_closed_even_with_other_clear_volume() -> None:
    result = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[_stream(0.60)],
        robot_collision_envelope=ROBOT_ENVELOPE,
        obstacle_volumes=[OBSTACLE],
        unresolved_candidate_refs=["visual_observation:unknown"],
    )

    assert result.status == "unavailable"
    assert result.clearance_observed is False
    assert result.collision_observed is False
    assert result.unresolved_candidate_refs == ("visual_observation:unknown",)
    assert result.candidate_results[-1].status == "unavailable"
    assert "obstacle_candidate_collision_volume_unavailable" in result.blocking_reasons
