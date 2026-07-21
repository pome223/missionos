"""The indoor map renders the camera+LiDAR observation layer separately.

These lock the display contract: the camera/LiDAR visual observation layer is
embedded and legended distinctly from the harness-placed scene obstacle layer,
and corroborated observations carry a map coordinate while camera-only ones do
not claim a position.
"""

from __future__ import annotations

import json

from missionos_cli.indoor_map_html import _mission_indoor_map_html


def _model_with_visual_observations() -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_indoor_map_model.v1",
        "task_id": "task_tb3_visual_observation",
        "task_status": "completed",
        "map_kind": "indoor_local_xy",
        "robot_label": "TurtleBot3",
        "mission_status": "completed",
        "frame_id": "map",
        "planned_points": [
            {"x_m": -2.0, "y_m": -0.5, "role": "home"},
            {"x_m": -1.4, "y_m": 2.42, "role": "dropoff"},
        ],
        "observed_points": [{"x_m": -2.0, "y_m": -0.5}, {"x_m": -1.4, "y_m": 2.42}],
        "room_boundary": {
            "min_x_m": -2.5,
            "max_x_m": 1.0,
            "min_y_m": -1.0,
            "max_y_m": 3.0,
        },
        "obstacles": [
            {"x_m": -1.8, "y_m": 0.4, "size_x_m": 0.4, "size_y_m": 0.4, "observed": True}
        ],
        "visual_observations": [
            {
                "schema_version": "missionos_visual_observation.v1",
                "observation_id": "visual_observation:corroborated1",
                "semantic_candidate": "unknown_obstacle",
                "claim_kind": "corridor_blocked_by_object",
                "source_frame_ref": f"sha256:{'a' * 64}",
                "camera_confidence": 0.91,
                "map_projection": {
                    "status": "projected",
                    "x_m": -1.75,
                    "y_m": 0.42,
                    "range_m": 0.677,
                },
                "binding_status": "bound",
                "display_status": "camera_lidar_corroborated",
                "evidence_only": True,
                "approval_created": False,
                "dispatch_authority_created": False,
            },
            {
                "schema_version": "missionos_visual_observation.v1",
                "observation_id": "visual_observation:cameraonly1",
                "semantic_candidate": "unknown_obstacle",
                "claim_kind": "corridor_blocked_by_object",
                "source_frame_ref": f"sha256:{'b' * 64}",
                "camera_confidence": 0.62,
                "map_projection": {"status": "unavailable", "x_m": None, "y_m": None},
                "binding_status": "unbound",
                "display_status": "camera_only",
                "evidence_only": True,
                "approval_created": False,
                "dispatch_authority_created": False,
            },
        ],
    }


def test_html_embeds_visual_observation_layer_separate_from_obstacles() -> None:
    model = _model_with_visual_observations()
    html = _mission_indoor_map_html(model)

    start = html.index('type="application/json">') + len('type="application/json">')
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])

    # The two layers stay distinct: harness obstacle vs camera/LiDAR observation.
    assert len(embedded["obstacles"]) == 1
    assert len(embedded["visual_observations"]) == 2
    display_statuses = {
        observation["display_status"] for observation in embedded["visual_observations"]
    }
    assert display_statuses == {"camera_lidar_corroborated", "camera_only"}
    # Corroborated observation keeps a coordinate; camera-only never claims one.
    projected = [
        observation
        for observation in embedded["visual_observations"]
        if observation["display_status"] == "camera_lidar_corroborated"
    ][0]
    assert projected["map_projection"]["status"] == "projected"
    camera_only = [
        observation
        for observation in embedded["visual_observations"]
        if observation["display_status"] == "camera_only"
    ][0]
    assert camera_only["map_projection"]["x_m"] is None


def test_html_declares_visual_observation_layer_and_evidence_boundary() -> None:
    html = _mission_indoor_map_html(_model_with_visual_observations())

    assert "visual-corroborated" in html
    assert "camera+LiDAR corroborated observation" in html
    assert "visualObservationMarkup" in html
    # The rendering path must not treat the layer as authority.
    assert "no approval, dispatch, or delivery claim" in html


def test_html_without_visual_observations_still_renders() -> None:
    model = _model_with_visual_observations()
    del model["visual_observations"]

    html = _mission_indoor_map_html(model)

    assert "visualObservationMarkup" in html


def test_html_distinguishes_2d_clearance_from_3d_collision() -> None:
    model = _model_with_visual_observations()
    model["obstacles"][0].update(
        {
            "trajectory_clearance_observed": True,
            "trajectory_intersects_obstacle": False,
        }
    )
    model["trajectory_clearance_3d"] = {
        "schema_version": "missionos_trajectory_clearance_3d.v2",
        "status": "collision_observed",
        "clearance_observed": False,
        "collision_observed": True,
        "minimum_surface_clearance_m": 0.0,
        "candidate_results": [
            {
                "obstacle_ref": "candidate:test",
                "status": "collision_observed",
            }
        ],
        "unresolved_candidate_refs": [],
        "evidence_only": True,
    }

    html = _mission_indoor_map_html(model)

    start = html.index('type="application/json">') + len('type="application/json">')
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])
    assert embedded["obstacles"][0]["trajectory_clearance_observed"] is True
    assert embedded["trajectory_clearance_3d"]["status"] == "collision_observed"
    assert embedded["trajectory_clearance_3d"]["collision_observed"] is True
    assert "2D centerline clearance" in html
    assert "3D swept-volume clearance" in html
    assert "candidates=${clearance3dCandidates.length}" in html
    assert "unresolved=${clearance3dUnresolved.length}" in html
    assert "clearance3dPillEl.dataset.status = clearance3dStatus" in html
    assert "evidence only" in html
