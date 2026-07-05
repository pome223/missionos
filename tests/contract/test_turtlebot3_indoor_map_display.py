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
    assert "observed odom" in html
    assert "🐢" in html
    assert "pathGroupsBySegment" in html
    assert "avoid obstacle" in html
    assert "sofa" in html
    assert "table" in html
    assert "bookshelf" in html
    assert "furniture" in html
    assert "physical" in html

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
        }
    ]
