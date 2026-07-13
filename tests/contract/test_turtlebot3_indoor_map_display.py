from __future__ import annotations

import pytest

from pathlib import Path
import shlex
import sys

from rich.console import Console

from missionos_cli import cli as missionos_cli
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)
from src.runtime.turtlebot3_home_mission import (
    _trajectory_samples_from_response,
    approve_turtlebot3_home_mission_plan,
    build_turtlebot3_home_mission_plan,
    run_turtlebot3_home_mission_dispatch,
)

@pytest.fixture(autouse=True)
def _default_arena_world_profile(monkeypatch):
    """Pin the historical arena world for this module's regression corpus.

    The runtime default is the turtlebot3_house profile; these tests assert
    arena-era routes and geometry, and house-specific tests override the env.
    """

    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "arena")



def _write_indoor_map_bridge(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, math, sys",
                "from pathlib import Path",
                "request = json.loads(sys.stdin.read())",
                "payload = request.get('payload') or {}",
                "state_path = Path(__file__).with_suffix('.state.json')",
                "if state_path.exists():",
                "    state = json.loads(state_path.read_text())",
                "else:",
                "    state = {'x_m': -2.0, 'y_m': -0.5}",
                "start_x = float(state.get('x_m', -2.0))",
                "start_y = float(state.get('y_m', -0.5))",
                "goal_x = float(payload.get('x_m') or 0.0)",
                "goal_y = float(payload.get('y_m') or 0.0)",
                "mid_x = (start_x + goal_x) / 2.0",
                "mid_y = (start_y + goal_y) / 2.0",
                "samples = [",
                "    {'x_m': start_x, 'y_m': start_y, 'sample_index': 0},",
                "    {'x_m': mid_x, 'y_m': mid_y, 'sample_index': 1},",
                "    {'x_m': goal_x, 'y_m': goal_y, 'sample_index': 2},",
                "]",
                "state_path.write_text(json.dumps({'x_m': goal_x, 'y_m': goal_y}))",
                "odom_delta_m = max(math.hypot(goal_x - start_x, goal_y - start_y), 0.05)",
                "trajectory = {",
                "    'trajectory_lateral_deviation_observed': True,",
                "    'max_lateral_deviation_m': 0.12,",
                "    'trajectory_samples': samples,",
                "}",
                "print(json.dumps({",
                "    'physical_execution_invoked': False,",
                "    'raw_velocity_published': False,",
                "    'raw_ros_topic_published': False,",
                "    'cmd_vel_published_by_missionos': False,",
                "    'ack_status': 'accepted',",
                "    'ack_source': 'fixture_nav2_navigate_to_pose',",
                "    'runtime_progress_observed': True,",
                "    'completion_observed': True,",
                "    'nav2_status': 'succeeded',",
                "    'state_result': {",
                "        'nav2_action_server_available': True,",
                "        'pose_observed': True,",
                "        'robot_motion_observed': True,",
                "        'odom_topic': '/odom',",
                "        'odom_delta_m': odom_delta_m,",
                "        'costmap_obstacle_observed': True,",
                "        'obstacle_avoidance_observed': True,",
                "        'trajectory_result': trajectory,",
                "    },",
                "    'progress_result': {",
                "        'runtime_progress_observed': True,",
                "        'completion_observed': True,",
                "        'robot_motion_observed': True,",
                "        'nav2_status': 'succeeded',",
                "        'costmap_obstacle_observed': True,",
                "        'obstacle_avoidance_observed': True,",
                "        'trajectory_result': trajectory,",
                "    },",
                "}))",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_odom_origin_indoor_map_bridge(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                "from pathlib import Path",
                "request = json.loads(sys.stdin.read())",
                "payload = request.get('payload') or {}",
                "counter_path = Path(__file__).with_suffix('.count')",
                "count = int(counter_path.read_text() or '0') if counter_path.exists() else 0",
                "counter_path.write_text(str(count + 1))",
                "if count == 0:",
                "    samples = [",
                "        {'x_m': 0.0, 'y_m': 0.0, 'sample_index': 0},",
                "        {'x_m': 0.85, 'y_m': -0.35, 'sample_index': 1},",
                "        {'x_m': 0.85, 'y_m': -0.35, 'sample_index': 2},",
                "    ]",
                "else:",
                "    samples = [",
                "        {'x_m': 0.85, 'y_m': -0.35, 'sample_index': 0},",
                "        {'x_m': 2.35, 'y_m': 0.22, 'sample_index': 1},",
                "        {'x_m': 2.55, 'y_m': 0.5, 'sample_index': 2},",
                "    ]",
                "trajectory = {",
                "    'trajectory_lateral_deviation_observed': True,",
                "    'max_lateral_deviation_m': 0.12,",
                "    'trajectory_samples': samples,",
                "}",
                "print(json.dumps({",
                "    'physical_execution_invoked': False,",
                "    'raw_velocity_published': False,",
                "    'raw_ros_topic_published': False,",
                "    'cmd_vel_published_by_missionos': False,",
                "    'ack_status': 'accepted',",
                "    'ack_source': 'fixture_nav2_navigate_to_pose',",
                "    'runtime_progress_observed': True,",
                "    'completion_observed': True,",
                "    'nav2_status': 'succeeded',",
                "    'state_result': {",
                "        'nav2_action_server_available': True,",
                "        'pose_observed': True,",
                "        'robot_motion_observed': True,",
                "        'odom_topic': '/odom',",
                "        'odom_delta_m': 0.26,",
                "        'costmap_obstacle_observed': True,",
                "        'obstacle_avoidance_observed': True,",
                "        'trajectory_result': trajectory,",
                "    },",
                "    'progress_result': {",
                "        'runtime_progress_observed': True,",
                "        'completion_observed': True,",
                "        'robot_motion_observed': True,",
                "        'nav2_status': 'succeeded',",
                "        'costmap_obstacle_observed': True,",
                "        'obstacle_avoidance_observed': True,",
                "        'trajectory_result': trajectory,",
                "    },",
                "}))",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_mixed_odom_map_indoor_map_bridge(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                "request = json.loads(sys.stdin.read())",
                "payload = request.get('payload') or {}",
                "goal_x = float(payload.get('x_m') or 0.0)",
                "goal_y = float(payload.get('y_m') or 0.0)",
                "samples = [",
                "    {'frame_id': 'odom', 'x_m': 0.0, 'y_m': 0.0, 'sample_index': 0},",
                "    {'frame_id': 'map', 'x_m': goal_x - 0.2, 'y_m': goal_y - 0.1, 'sample_index': 1},",
                "    {'frame_id': 'map', 'x_m': goal_x, 'y_m': goal_y, 'sample_index': 2},",
                "]",
                "trajectory = {",
                "    'trajectory_lateral_deviation_observed': True,",
                "    'max_lateral_deviation_m': 0.12,",
                "    'trajectory_samples': samples,",
                "}",
                "print(json.dumps({",
                "    'physical_execution_invoked': False,",
                "    'raw_velocity_published': False,",
                "    'raw_ros_topic_published': False,",
                "    'cmd_vel_published_by_missionos': False,",
                "    'ack_status': 'accepted',",
                "    'ack_source': 'fixture_nav2_navigate_to_pose',",
                "    'runtime_progress_observed': True,",
                "    'completion_observed': True,",
                "    'nav2_status': 'succeeded',",
                "    'state_result': {",
                "        'nav2_action_server_available': True,",
                "        'pose_observed': True,",
                "        'robot_motion_observed': True,",
                "        'odom_topic': '/odom',",
                "        'odom_delta_m': 0.26,",
                "        'costmap_obstacle_observed': True,",
                "        'obstacle_avoidance_observed': True,",
                "        'trajectory_result': trajectory,",
                "    },",
                "    'progress_result': {",
                "        'runtime_progress_observed': True,",
                "        'completion_observed': True,",
                "        'robot_motion_observed': True,",
                "        'nav2_status': 'succeeded',",
                "        'costmap_obstacle_observed': True,",
                "        'obstacle_avoidance_observed': True,",
                "        'trajectory_result': trajectory,",
                "    },",
                "}))",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _bridge_command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def test_turtlebot3_execution_artifact_feeds_indoor_watch_and_map(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_indoor_map_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    indoor = result["turtlebot3_indoor_map_model"]
    assert indoor["map_kind"] == "indoor_local_xy"
    assert indoor["execution_mode"] == "sim"
    assert indoor["physical_execution_invoked"] is False
    assert len(indoor["planned_points"]) == 11
    assert len(indoor["observed_points"]) >= 3
    assert len(indoor["obstacles"]) == 3
    assert indoor["obstacles"][0]["observed"] is True
    assert [obstacle["label"] for obstacle in indoor["obstacles"]] == [
        "closed door",
        "person",
        "dog",
    ]
    assert indoor["floor_plan"]["floor_plan_id"] == "turtlebot3_simulated_home_loop.v1"
    assert [item["label"] for item in indoor["floor_plan"]["furniture"]] == [
        "sofa",
        "table",
        "bookshelf",
        "counter",
    ]
    assert indoor["room_boundary"]["source"] == (
        "missionos_static_simulated_home_floor_plan_bounds"
    )

    task_payload = {
        "task_id": "task_turtlebot3_indoor_map",
        "status": "completed",
        "artifacts": result,
    }
    model = missionos_cli._mission_map_model(
        task_payload=task_payload,
        provider="osm",
        live_task_url=None,
    )
    assert model["map_kind"] == "indoor_local_xy"
    assert model["provider"]["label"] == "Indoor local XY"
    assert model["task_id"] == "task_turtlebot3_indoor_map"
    assert len(model["planned_points"]) == 11
    assert len(model["observed_points"]) >= 3

    html = missionos_cli._mission_map_html(model)
    assert "MissionOS Indoor Map" in html
    assert "TurtleBot3/Nav2 simulator local-XY evidence" in html
    assert "persisted observed trajectory" in html
    assert "🐢" in html
    assert "pathGroupsBySegment" in html
    assert "avoid obstacle" in html
    assert "sofa" in html
    assert "table" in html
    assert "bookshelf" in html
    assert "furniture" in html
    assert "furniture (sim pillars)" not in html
    assert "physical" in html
    assert "live_display_points" in html
    assert "live /odom preview — display-only, not evidence" in html
    assert "live preview ended — not evidence" in html
    assert "persisted observed trajectory (final evidence)" in html
    assert "Green is not persisted and is never verifier input" in html
    assert ".live-path-ended" in html
    assert "stroke-dasharray: 7 8" in html
    assert html.index("${liveMarkup}") < html.index("${observedMarkup}")
    assert "recovery phase" in html

    console = Console(record=True, color_system=None, width=120)
    console.print(
        missionos_cli._render_turtlebot3_indoor_map(
            indoor_map=indoor,
            status="completed",
            task_id="task_turtlebot3_indoor_map",
        )
    )
    rendered = console.export_text()
    assert "MissionOS Indoor Map" in rendered
    assert "right=+map x top=+map y" in rendered
    assert "🐢" in rendered
    assert "floor_plan=turtlebot3_simulated_home_loop.v1" in rendered
    assert "S/T/B/C" in rendered
    assert "observed_source=ros2_nav2_bridge_trajectory_samples" in rendered
    assert "physical_execution_invoked=false" in rendered


def test_watch_overlays_live_odom_without_rewriting_observed_evidence() -> None:
    indoor = {
        "frame_id": "map",
        "mission_kind": "indoor_delivery_route_leg",
        "planned_points": [
            {"x_m": -2.0, "y_m": -0.5, "role": "home"},
            {"x_m": -1.4, "y_m": 2.42, "role": "dropoff"},
        ],
        "observed_points": [{"x_m": -1.0, "y_m": 0.5}],
        "current_pose": {"x_m": -1.0, "y_m": 0.5},
        "recovery": {"triggered": True},
    }
    artifacts = {
        "summary": {
            "runtime_recovery_triggered": True,
            "route_completed_after_recovery": False,
            "segment_completion_count": 3,
            "planned_segment_count": 6,
            "recovery_completion_claimed": False,
            "route_resumed_after_recovery": False,
        },
        "turtlebot3_recovery_checkpoint": {
            "checkpoint_status": "dispatching",
            "selected_action": "avoid_obstacle",
        },
        "turtlebot3_live_telemetry": {
            "telemetry_status": "observed",
            "captured_at": "2026-07-12T01:00:00+00:00",
            "frame_id": "odom",
            "raw_odom_position": {"x_m": 0.2, "y_m": 0.3},
            "twist": {"linear_x_mps": 0.1},
            "display_only": True,
        },
    }
    trail: list[dict] = []
    alignment: dict = {}

    first = missionos_cli._overlay_turtlebot3_live_telemetry(
        indoor,
        artifacts=artifacts,
        trail=trail,
        alignment_state=alignment,
    )
    artifacts["turtlebot3_live_telemetry"]["raw_odom_position"] = {
        "x_m": 0.4,
        "y_m": 0.6,
    }
    second = missionos_cli._overlay_turtlebot3_live_telemetry(
        indoor,
        artifacts=artifacts,
        trail=trail,
        alignment_state=alignment,
    )

    assert indoor["observed_points"] == [{"x_m": -1.0, "y_m": 0.5}]
    assert first["live_display_points"][-1]["x_m"] == pytest.approx(-1.0)
    assert second["live_display_points"][-1]["x_m"] == pytest.approx(-0.8)
    assert second["live_display_points"][-1]["y_m"] == pytest.approx(0.8)
    assert second["live_display_points"][-1]["raw_x_m"] == 0.4
    assert second["live_display_points"][-1]["display_only"] is True
    assert second["live_display_points"][-1]["evidence_status"] == "not_evidence"
    assert second["live_telemetry"]["persistence"] == (
        "process_local_response_overlay_only"
    )
    assert second["recovery"]["runtime_status"] == (
        "approved_recovery_and_route_in_progress"
    )

    console = Console(record=True, color_system=None, width=120)
    console.print(
        missionos_cli._render_turtlebot3_indoor_map(
            indoor_map=second,
            status="running",
            task_id="task_live_watch",
        )
    )
    rendered = console.export_text()
    assert "live_preview=observed (display-only, not evidence)" in rendered
    assert "live_samples=2" in rendered
    assert "recovery_phase=approved_recovery_and_route_in_progress" in rendered
    assert "recovery_action=avoid_obstacle" in rendered
    assert "route_segments=3/6" in rendered
    assert "recovery_complete=False" in rendered


def test_terminal_live_preview_freezes_only_in_current_process() -> None:
    indoor = {
        "observed_points": [
            {"x_m": -1.0, "y_m": 0.5, "source": "ros2_nav2_bridge"},
            {"x_m": -0.5, "y_m": 0.8, "source": "ros2_nav2_bridge"},
        ],
        "current_pose": {"x_m": -0.5, "y_m": 0.8},
        "recovery": {
            "observed_points": [{"x_m": -0.8, "y_m": 0.4}],
            "target": {"x_m": -0.7, "y_m": 0.3},
        },
    }
    terminal_artifacts = {"summary": {"completion_claimed": True}}
    process_local_trail = [
        {
            "x_m": -0.9,
            "y_m": 0.55,
            "raw_x_m": 0.0,
            "raw_y_m": 0.0,
            "display_only": True,
            "evidence_status": "not_evidence",
        },
        {
            "x_m": -0.6,
            "y_m": 0.75,
            "raw_x_m": 0.4,
            "raw_y_m": 0.2,
            "display_only": True,
            "evidence_status": "not_evidence",
        },
    ]

    frozen = missionos_cli._overlay_turtlebot3_live_telemetry(
        indoor,
        artifacts=terminal_artifacts,
        trail=process_local_trail,
        alignment_state={},
        freeze_live_preview=True,
    )

    assert frozen is not indoor
    assert frozen["live_display_points"] == process_local_trail
    assert frozen["live_telemetry"]["telemetry_status"] == "ended"
    assert frozen["live_telemetry"]["evidence_status"] == "not_evidence"
    assert frozen["live_telemetry"]["display_path_length_m"] == pytest.approx(
        0.447214
    )
    assert "live_display_points" not in indoor
    assert "live_telemetry" not in terminal_artifacts

    reloaded = missionos_cli._overlay_turtlebot3_live_telemetry(
        indoor,
        artifacts=terminal_artifacts,
        trail=[],
        alignment_state={},
        freeze_live_preview=True,
    )
    assert reloaded is indoor
    assert "live_display_points" not in reloaded
    assert reloaded["observed_points"] == indoor["observed_points"]
    assert reloaded["recovery"] == indoor["recovery"]


def test_turtlebot3_indoor_map_display_aligns_odom_origin_to_planned_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_odom_origin_indoor_map_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    indoor = result["turtlebot3_indoor_map_model"]
    alignment = indoor["display_alignment"]
    observed = indoor["observed_points"]
    planned = indoor["planned_points"]
    assert alignment["applied"] is True
    assert alignment["method"] == "first_observed_pose_to_planned_home"
    assert alignment["dx_m"] == -2.0
    assert alignment["dy_m"] == -0.5
    assert observed[0]["raw_x_m"] == 0.0
    assert observed[0]["raw_y_m"] == 0.0
    assert observed[0]["x_m"] == planned[0]["x_m"]
    assert observed[0]["y_m"] == planned[0]["y_m"]
    assert observed[-1]["display_alignment_applied"] is True
    assert abs(observed[-1]["x_m"] - planned[-1]["x_m"]) < 1e-6
    assert abs(observed[-1]["y_m"] - planned[-1]["y_m"]) < 1e-6
    assert indoor["room_boundary"]["source"] == (
        "missionos_static_simulated_home_floor_plan_bounds"
    )
    assert "Raw bridge trajectory samples remain" in alignment["claim_boundary"]


def test_turtlebot3_indoor_map_prefers_map_frame_samples_over_mixed_odom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_mixed_odom_map_indoor_map_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    indoor = result["turtlebot3_indoor_map_model"]
    observed = indoor["observed_points"]
    alignment = indoor["display_alignment"]
    planned = indoor["planned_points"]
    assert alignment["applied"] is False
    assert alignment["method"] == "map_frame_samples"
    assert {point["frame_id"] for point in observed} == {"map"}
    assert all("raw_x_m" not in point for point in observed)
    assert abs(observed[-1]["x_m"] - planned[-1]["x_m"]) < 1e-6
    assert abs(observed[-1]["y_m"] - planned[-1]["y_m"]) < 1e-6


def test_mission_map_recovers_saved_mixed_frame_display_from_raw_fields() -> None:
    payload = {
        "task_id": "task_saved_mixed_frame_map",
        "status": "completed",
        "artifacts": {
            "turtlebot3_indoor_map_model": {
                "schema_version": "missionos_turtlebot3_indoor_map.v1",
                "map_kind": "indoor_local_xy",
                "planned_points": [
                    {"label": "H home", "phase": "home", "x_m": -2.0, "y_m": -0.5},
                    {
                        "label": "D dropoff",
                        "phase": "dropoff",
                        "x_m": -1.4,
                        "y_m": 2.42,
                    },
                ],
                "observed_points": [
                    {
                        "phase": "segment_6",
                        "segment_ref": "segment_6",
                        "sample_index": 0,
                        "x_m": -2.75,
                        "y_m": -2.81,
                        "raw_x_m": -0.75,
                        "raw_y_m": -2.31,
                        "display_alignment_applied": True,
                    },
                    {
                        "phase": "segment_0",
                        "segment_ref": "segment_6",
                        "sample_index": 2282,
                        "x_m": -3.38,
                        "y_m": 1.89,
                        "raw_x_m": -1.38,
                        "raw_y_m": 2.39,
                        "display_alignment_applied": True,
                    },
                ],
                "display_alignment": {
                    "applied": True,
                    "method": "first_observed_pose_to_planned_home",
                    "dx_m": -2.0,
                    "dy_m": -0.5,
                },
                "claim_boundaries": [
                    "Indoor map display is read-only and not runtime evidence."
                ],
            }
        },
    }

    model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )

    alignment = model["display_alignment"]
    observed = model["observed_points"]
    current_pose = model["current_pose"]
    assert alignment["applied"] is False
    assert alignment["method"] == "map_frame_samples_recovered_from_raw_fields"
    assert alignment["repaired_display_only"] is True
    assert [point["sample_index"] for point in observed] == [2282]
    assert observed[-1]["x_m"] == -1.38
    assert observed[-1]["y_m"] == 2.39
    assert observed[-1]["frame_id"] == "map"
    assert observed[-1]["display_alignment_applied"] is False
    assert current_pose["x_m"] == -1.38
    assert current_pose["y_m"] == 2.39


def test_trajectory_samples_filter_odom_when_map_frame_samples_are_present() -> None:
    response = {
        "trajectory_result": {
            "trajectory_samples": [
                {"frame_id": "odom", "x_m": 0.0, "y_m": 0.0, "sample_index": 0},
                {"frame_id": "map", "x_m": -1.4, "y_m": 2.42, "sample_index": 1},
            ],
        }
    }

    samples = _trajectory_samples_from_response(response)

    assert samples == [
        {
            "x_m": -1.4,
            "y_m": 2.42,
            "frame_id": "map",
            "source": "ros2_nav2_bridge.trajectory_samples",
            "sample_index": 1,
            "elapsed_s": None,
            "trajectory_sample_collection": "trajectory_samples",
            "trajectory_container_index": 0,
            "observed_trajectory_evidence_eligible": True,
            "observation_provenance": "bridge_observed_trajectory_sample",
        }
    ]
