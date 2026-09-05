from __future__ import annotations

import pytest

from hashlib import sha256
import json
import math
from pathlib import Path
import shlex
import sys

from src.runtime import turtlebot3_home_mission as turtlebot3_home_mission_runtime
from src.runtime import turtlebot3_nav2_execution as turtlebot3_nav2_execution_runtime
from src.intelligence.turtlebot3_recovery_planner import (
    TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV,
    TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV,
    TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
)
from src.intelligence.turtlebot3_perception_sidecar import (
    TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV,
    TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV,
    TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV,
)
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)
from src.runtime.ros2_nav2_hardware_adapter import Nav2GoalPose
from src.runtime.nvblox_perception_evidence import (
    NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV,
    NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV,
)
from src.runtime.turtlebot3_log_collector import TURTLEBOT3_LOG_BUNDLE_PATHS_ENV
from src.runtime.turtlebot3_home_mission import (
    TURTLEBOT3_CAMERA_PERCEPTION_ENABLED_ENV,
    TURTLEBOT3_CAMERA_PERCEPTION_PIPELINE_SCHEMA_VERSION,
    TURTLEBOT3_RECOVERY_REFLEX_SCHEMA_VERSION,
    TURTLEBOT3_RECOVERY_SHADOW_COMPARISON_SCHEMA_VERSION,
    _recovery_checkpoint_hash,
    approve_turtlebot3_home_mission_plan,
    build_turtlebot3_recovery_checkpoint_revision,
    build_turtlebot3_home_mission_plan,
    infer_turtlebot3_home_mission_kind,
    instruction_requests_turtlebot3_home_mission,
    _execute_turtlebot3_home_mission as run_turtlebot3_home_mission_dispatch,
)
from src.runtime.turtlebot3_telemetry_sidecar import (
    TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV,
)

@pytest.fixture(autouse=True)
def _default_arena_world_profile(monkeypatch):
    """Pin the historical arena world for this module's regression corpus.

    The runtime default is the turtlebot3_house profile; these tests assert
    arena-era routes and geometry, and house-specific tests override the env.
    """

    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "arena")
    # These are Nav2 runtime component tests, with subprocess model/bridge
    # fixtures. The common graph and its inference/approval enforcement are
    # exercised without these stubs in test_turtlebot3_mission_incident.py.
    from src.runtime import turtlebot3_mission_incident
    monkeypatch.setattr(turtlebot3_mission_incident, "judge_turtlebot3_checkpoint",
                        lambda **_: {"decision_status": "awaiting_operator_approval"})
    monkeypatch.setattr(turtlebot3_mission_incident, "turtlebot3_incident_dispatch_reasons",
                        lambda _: [])



_FIXTURE_CAMERA_FRAME_REF = "sha256:" + "c" * 64
_FIXTURE_CAMERA_FRAME_BYTES = b"\x89PNG\r\n\x1a\nfixture-camera-frame"


def _source_backed_recovery_obstacle(
    *,
    x_m: float = -1.15,
    y_m: float = -0.5,
) -> dict[str, object]:
    return {
        "runtime_obstacle_x_m": x_m,
        "runtime_obstacle_y_m": y_m,
        "runtime_obstacle_size_x_m": 0.32,
        "runtime_obstacle_size_y_m": 0.32,
        "runtime_obstacle_z_m": 0.25,
        "runtime_obstacle_collision_size_x_m": 0.32,
        "runtime_obstacle_collision_size_y_m": 0.32,
        "runtime_obstacle_size_z_m": 0.5,
        "runtime_obstacle_frame_id": "map",
        "runtime_obstacle_scene_ref": "fixture_obstacle",
        "runtime_obstacle_geometry_source": "fixture_sdf_collision",
        "runtime_obstacle_source": "fixture_costmap",
    }


def _core_ready_candidate(
    candidate: dict,
    obstacle: dict,
    *,
    path_valid: bool = True,
    maximum_path_cost: int = 20,
    local_current_cost: int = 0,
    local_maximum_path_cost: int = 40,
) -> dict:
    target_x = float(candidate["x_m"])
    target_y = float(candidate["y_m"])
    obstacle_x = float(obstacle["runtime_obstacle_x_m"])
    obstacle_y = float(obstacle["runtime_obstacle_y_m"])
    dx = target_x - obstacle_x
    dy = target_y - obstacle_y
    magnitude = math.hypot(dx, dy) or 1.0
    unit_x = dx / magnitude
    unit_y = dy / magnitude
    start_x = target_x + unit_x * 0.10
    start_y = target_y + unit_y * 0.10
    path_points = [
        {"x_m": round(start_x, 6), "y_m": round(start_y, 6), "frame_id": "map"},
        {"x_m": round(target_x, 6), "y_m": round(target_y, 6), "frame_id": "map"},
    ]
    path_material = [
        [point["x_m"], point["y_m"]] for point in path_points
    ]
    path_length_m = math.hypot(target_x - start_x, target_y - start_y)
    return {
        **candidate,
        "path_valid": path_valid,
        "planner_status": "succeeded",
        "target_cost": 4,
        "maximum_path_cost": maximum_path_cost,
        "local_current_cost": local_current_cost,
        "local_maximum_path_cost": local_maximum_path_cost,
        "path_length_m": round(path_length_m, 6),
        "path_points": path_points,
        "path_frame_id": "map",
        "path_goal_error_m": 0.0,
        "path_goal_tolerance_m": 0.1,
        "path_sha256": sha256(
            json.dumps(path_material, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _core_ready_evaluation(
    *,
    evaluated: list[dict],
    selected: dict | None,
    stamp_ns: int = 1_000_000_000,
    global_hash: str = "global-costmap-hash",
    local_hash: str = "local-costmap-hash",
) -> dict:
    return {
        "evaluation_status": "validated" if selected else "blocked",
        "selected_candidate": selected,
        "candidate_evaluations": evaluated,
        "costmap_snapshot_hash": global_hash,
        "costmap_source": "/global_costmap/get_costmap",
        "global_costmap_snapshot_hash": global_hash,
        "global_costmap_source": "/global_costmap/get_costmap",
        "global_costmap_frame_id": "map",
        "global_costmap_stamp_ns": stamp_ns,
        "global_costmap_age_s": 0.1,
        "local_costmap_snapshot_hash": local_hash,
        "local_costmap_source": "/local_costmap/get_costmap",
        "local_costmap_frame_id": "odom",
        "local_costmap_stamp_ns": stamp_ns,
        "local_costmap_age_s": 0.1,
        "local_frame_transform_verified": True,
        "compute_path_action": "/compute_path_to_pose",
        "blocking_reasons": [] if selected else ["no_valid_recovery_candidate"],
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "command_ack_observed": False,
        "observation_captured_at": "2026-07-24T03:00:00+00:00",
    }


def _write_success_bridge(
    path: Path,
    *,
    obstacle_avoidance_observed: bool = False,
    trajectory_frame_id: str | None = "map",
    sample_collection: str = "trajectory_samples",
    camera_observation: bool = False,
    camera_capture_times_out: bool = False,
) -> None:
    obstacle = "True" if obstacle_avoidance_observed else "False"
    camera_observation_block = (
        (
            "'camera_observation': {\n"
            "            'claim_kind': 'corridor_blocked_by_object',\n"
            f"            'source_frame_ref': '{_FIXTURE_CAMERA_FRAME_REF}',\n"
            "            'confidence': 0.82,\n"
            "        },\n"
        )
        if camera_observation
        else ""
    )
    if camera_capture_times_out:
        capture_branch = (
            "if request.get('action') == 'capture_camera_frame':\n"
            "    print(json.dumps({\n"
            "        'physical_execution_invoked': False,\n"
            "        'raw_velocity_published': False,\n"
            "        'raw_ros_topic_published': False,\n"
            "        'cmd_vel_published_by_missionos': False,\n"
            "        'ack_status': 'timeout',\n"
            "        'ack_source': 'fixture_nav2_camera',\n"
            "        'nav2_status': 'blocked',\n"
            "        'camera_frame_captured': False,\n"
            "        'blocking_reasons': ['camera_frame_not_received_before_timeout'],\n"
            "    }))\n"
            "    raise SystemExit(0)\n"
        )
    else:
        capture_branch = (
            "if request.get('action') == 'capture_camera_frame':\n"
            "    import hashlib\n"
            "    capture_payload = request.get('payload') or {}\n"
            "    frame_path = Path(\n"
            "        capture_payload.get('output_path')\n"
            "        or str(Path(__file__).with_suffix('')) + '_frame.png'\n"
            "    )\n"
            "    frame_bytes = " + repr(_FIXTURE_CAMERA_FRAME_BYTES) + "\n"
            "    frame_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    frame_path.write_bytes(frame_bytes)\n"
            "    now = datetime.now(timezone.utc).isoformat()\n"
            "    print(json.dumps({\n"
            "        'physical_execution_invoked': False,\n"
            "        'raw_velocity_published': False,\n"
            "        'raw_ros_topic_published': False,\n"
            "        'cmd_vel_published_by_missionos': False,\n"
            "        'ack_status': 'accepted',\n"
            "        'ack_source': 'fixture_nav2_camera',\n"
            "        'nav2_status': 'not_applicable',\n"
            "        'camera_frame_captured': True,\n"
            "        'camera_frame_path': str(frame_path),\n"
            "        'camera_frame_sha256': hashlib.sha256(frame_bytes).hexdigest(),\n"
            "        'camera_topic': '/camera/image_raw',\n"
            "        'camera_lidar_observation': {\n"
            "            'camera_observed_at': now,\n"
            "            'camera_received_at': now,\n"
            "            'camera_frame_id': 'camera_rgb_optical_frame',\n"
            "            'camera_width': 640,\n"
            "            'camera_fx': 554.25,\n"
            "            'camera_cx': 320.0,\n"
            "            'lidar_observed_at': now,\n"
            "            'lidar_frame_id': 'base_scan',\n"
            "            'lidar_obstacle_observed': True,\n"
            "            'lidar_horizontal_sector': 'center',\n"
            "            'lidar_candidate_bearing_rad': 0.0,\n"
            "            'target_candidate_id': 'lidar_candidate:fixture',\n"
            "            'lidar_evidence_ref': 'laser_scan:fixture',\n"
            "        },\n"
            "    }))\n"
            "    raise SystemExit(0)\n"
        )
    path.write_text(
        "import json, math, sys\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "request = json.loads(sys.stdin.read())\n"
        + capture_branch +
        "if request.get('action') == 'cancel_goal':\n"
        "    print(json.dumps({\n"
        "        'physical_execution_invoked': False,\n"
        "        'raw_velocity_published': False,\n"
        "        'raw_ros_topic_published': False,\n"
        "        'cmd_vel_published_by_missionos': False,\n"
        "        'ack_status': 'accepted',\n"
        "        'ack_source': 'fixture_nav2_cancel',\n"
        "        'nav2_status': 'canceled',\n"
        "        'cancel_accepted': True,\n"
        "        'stop_observed': True,\n"
        "        'post_cancel_odom_delta_m': 0.0,\n"
        "        'stop_observation_window_s': 2.0,\n"
        "        'stop_observation_source': 'odom:/odom',\n"
        "    }))\n"
        "    raise SystemExit(0)\n"
        "payload = request.get('payload') or {}\n"
        "trajectory_frame_id = "
        + repr(trajectory_frame_id)
        + "\n"
        "sample_collection = "
        + repr(sample_collection)
        + "\n"
        "state_path = Path(__file__).with_suffix('.state.json')\n"
        "calls_path = Path(__file__).with_suffix('.calls.jsonl')\n"
        "with calls_path.open('a', encoding='utf-8') as calls_handle:\n"
        "    calls_handle.write(json.dumps({'label': payload.get('label'), 'x_m': payload.get('x_m'), 'y_m': payload.get('y_m')}) + '\\n')\n"
        "if state_path.exists():\n"
        "    state = json.loads(state_path.read_text())\n"
        "else:\n"
        "    state = {'x_m': -2.0, 'y_m': -0.5}\n"
        "start_x = float(state.get('x_m', -2.0))\n"
        "start_y = float(state.get('y_m', -0.5))\n"
        "goal_x = float(payload.get('x_m') or 0.0)\n"
        "goal_y = float(payload.get('y_m') or 0.0)\n"
        "mid_x = (start_x + goal_x) / 2.0\n"
        "mid_y = (start_y + goal_y) / 2.0\n"
        "if "
        + obstacle
        + " and payload.get('label') == 'turtlebot3_validated_home_patrol_leg':\n"
        "    samples = [\n"
        "        {'x_m': start_x, 'y_m': start_y},\n"
        "        {'x_m': -1.60, 'y_m': -0.90},\n"
        "        {'x_m': -1.15, 'y_m': -0.90},\n"
        "        {'x_m': -0.35, 'y_m': -0.90},\n"
        "        {'x_m': 0.35, 'y_m': -0.55},\n"
        "        {'x_m': goal_x, 'y_m': goal_y},\n"
        "    ]\n"
        "else:\n"
        "    samples = [\n"
        "        {'x_m': start_x, 'y_m': start_y},\n"
        "        {'x_m': mid_x, 'y_m': mid_y},\n"
        "        {'x_m': goal_x, 'y_m': goal_y},\n"
        "    ]\n"
        "for sample_index, sample in enumerate(samples):\n"
        "    sample['sample_index'] = sample_index\n"
        "    if trajectory_frame_id:\n"
        "        sample['frame_id'] = trajectory_frame_id\n"
        "state_path.write_text(json.dumps({'x_m': goal_x, 'y_m': goal_y}))\n"
        "odom_delta_m = max(math.hypot(goal_x - start_x, goal_y - start_y), 0.05)\n"
        "trajectory = {\n"
        "    'trajectory_lateral_deviation_observed': "
        + obstacle
        + ",\n"
        "    'max_lateral_deviation_m': 0.12 if "
        + obstacle
        + " else None,\n"
        "    sample_collection: samples,\n"
        "}\n"
        "assert request.get('physical_execution_invoked') is False\n"
        "assert request.get('raw_velocity_allowed') is False\n"
        "assert request.get('raw_ros_topic_publication_allowed') is False\n"
        "response = {\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'goal_accepted': True,\n"
        "    'goal_x_m': goal_x,\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': True,\n"
        "    'nav2_status': 'succeeded',\n"
        "    'nav2_goal_succeeded': True,\n"
        "    'completion_basis': 'nav2_goal_succeeded',\n"
        "    'state_result': {\n"
        "        'nav2_action_server_available': True,\n"
        "        'nav2_goal_succeeded': True,\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'odom_before_observed': True,\n"
        "        'odom_after_observed': True,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': odom_delta_m,\n"
        "        'completion_basis': 'nav2_goal_succeeded',\n"
        "        'costmap_obstacle_observed': "
        + obstacle
        + ",\n"
        "        'obstacle_avoidance_observed': "
        + obstacle
        + ",\n"
        "        'trajectory_result': trajectory,\n"
        "        " + camera_observation_block +
        "    },\n"
        "    'progress_result': {\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': True,\n"
        "        'nav2_goal_succeeded': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'nav2_status': 'succeeded',\n"
        "        'completion_basis': 'nav2_goal_succeeded',\n"
        "        'costmap_obstacle_observed': "
        + obstacle
        + ",\n"
        "        'obstacle_avoidance_observed': "
        + obstacle
        + ",\n"
        "        'trajectory_result': trajectory,\n"
        "    },\n"
        "}\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )


def _write_nav2_failure_bridge(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('action') == 'cancel_goal':\n"
        "    print(json.dumps({\n"
        "        'physical_execution_invoked': False,\n"
        "        'raw_velocity_published': False,\n"
        "        'raw_ros_topic_published': False,\n"
        "        'cmd_vel_published_by_missionos': False,\n"
        "        'ack_status': 'accepted',\n"
        "        'ack_source': 'fixture_nav2_cancel',\n"
        "        'nav2_status': 'canceled',\n"
        "        'cancel_accepted': True,\n"
        "        'stop_observed': True,\n"
        "        'post_cancel_odom_delta_m': 0.0,\n"
        "        'stop_observation_window_s': 2.0,\n"
        "        'stop_observation_source': 'odom:/odom',\n"
        "    }))\n"
        "    raise SystemExit(0)\n"
        "payload = request.get('payload') or {}\n"
        "assert request.get('physical_execution_invoked') is False\n"
        "response = {\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'goal_x_m': payload.get('x_m'),\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': False,\n"
        "    'nav2_status': 'aborted',\n"
        "    'blocking_reasons': ['nav2_goal_result_not_succeeded'],\n"
        "    'state_result': {\n"
        "        'nav2_action_server_available': True,\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': False,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': 0.0,\n"
        "    },\n"
        "    'progress_result': {\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': False,\n"
        "        'robot_motion_observed': False,\n"
        "        'nav2_status': 'aborted',\n"
        "        'blocking_reasons': ['nav2_goal_result_not_succeeded'],\n"
        "    },\n"
        "}\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )


def _write_intersecting_obstacle_bridge(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('action') == 'cancel_goal':\n"
        "    print(json.dumps({\n"
        "        'physical_execution_invoked': False,\n"
        "        'raw_velocity_published': False,\n"
        "        'raw_ros_topic_published': False,\n"
        "        'cmd_vel_published_by_missionos': False,\n"
        "        'ack_status': 'accepted',\n"
        "        'ack_source': 'fixture_nav2_cancel',\n"
        "        'nav2_status': 'canceled',\n"
        "        'cancel_accepted': True,\n"
        "        'stop_observed': True,\n"
        "        'post_cancel_odom_delta_m': 0.0,\n"
        "        'stop_observation_window_s': 2.0,\n"
        "        'stop_observation_source': 'odom:/odom',\n"
        "    }))\n"
        "    raise SystemExit(0)\n"
        "payload = request.get('payload') or {}\n"
        "trajectory = {\n"
        "    'trajectory_lateral_deviation_observed': True,\n"
        "    'max_lateral_deviation_m': 0.12,\n"
        "    'trajectory_samples': [\n"
        "        {'x_m': -2.0, 'y_m': -0.5, 'sample_index': 0, 'frame_id': 'map'},\n"
        "        {'x_m': -1.15, 'y_m': -0.5, 'sample_index': 1, 'frame_id': 'map'},\n"
        "        {'x_m': payload.get('x_m'), 'y_m': payload.get('y_m') or 0.0, 'sample_index': 2, 'frame_id': 'map'},\n"
        "    ],\n"
        "}\n"
        "print(json.dumps({\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'goal_accepted': True,\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': True,\n"
        "    'nav2_status': 'succeeded',\n"
        "    'nav2_goal_succeeded': True,\n"
        "    'completion_basis': 'nav2_goal_succeeded',\n"
        "    'state_result': {\n"
        "        'nav2_action_server_available': True,\n"
        "        'nav2_goal_succeeded': True,\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'odom_before_observed': True,\n"
        "        'odom_after_observed': True,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': 0.26,\n"
        "        'completion_basis': 'nav2_goal_succeeded',\n"
        "        'costmap_obstacle_observed': True,\n"
        "        'obstacle_avoidance_observed': True,\n"
        "        'trajectory_result': trajectory,\n"
        "    },\n"
        "    'progress_result': {\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': True,\n"
        "        'nav2_goal_succeeded': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'nav2_status': 'succeeded',\n"
        "        'completion_basis': 'nav2_goal_succeeded',\n"
        "        'costmap_obstacle_observed': True,\n"
        "        'obstacle_avoidance_observed': True,\n"
        "        'trajectory_result': trajectory,\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _bridge_command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def test_turtlebot3_recovery_candidate_resolver_uses_plan_only_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV,
        "1",
    )

    def _evaluate(self, *, candidates, obstacle, frame_id="map"):
        del self, frame_id
        selected = _core_ready_candidate(candidates[1], obstacle)
        return _core_ready_evaluation(
            evaluated=[selected],
            selected=selected,
            global_hash="costmap-hash",
        )

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime.Ros2Nav2BridgeCommandClient,
        "evaluate_recovery_candidates",
        _evaluate,
    )
    resolved = turtlebot3_home_mission_runtime._resolve_recovery_candidate(
        _source_backed_recovery_obstacle()
    )

    assert resolved["resolution_status"] == "validated"
    assert resolved["live_costmap_validated"] is True
    assert resolved["dual_costmap_validated"] is True
    assert resolved["selected_candidate"]["candidate_id"] == (
        "obstacle_bypass_north"
    )
    assert resolved["costmap_snapshot_hash"] == "costmap-hash"
    assert resolved["local_costmap_snapshot_hash"] == "local-costmap-hash"
    assert resolved["dispatch_request_sent"] is False
    assert resolved["dispatch_authority_created"] is False


def test_turtlebot3_recovery_candidate_resolver_refreshes_transient_snapshot(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV,
        "1",
    )
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_COUNT_ENV,
        "2",
    )
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_INTERVAL_ENV,
        "0",
    )
    calls = 0

    def _evaluate(self, *, candidates, obstacle, frame_id="map"):
        nonlocal calls
        del self, frame_id
        calls += 1
        if calls == 1:
            failed = _core_ready_candidate(
                candidates[0],
                obstacle,
                path_valid=False,
            )
            failed["blocking_reasons"] = [
                "recovery_current_pose_outside_local_costmap"
            ]
            return _core_ready_evaluation(
                evaluated=[failed],
                selected=None,
            )
        selected = _core_ready_candidate(
            candidates[0],
            obstacle,
            local_current_cost=30,
        )
        return _core_ready_evaluation(
            evaluated=[selected],
            selected=selected,
            global_hash="fresh-global-hash",
            local_hash="fresh-local-hash",
            stamp_ns=2_000_000_000,
        )

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime.Ros2Nav2BridgeCommandClient,
        "evaluate_recovery_candidates",
        _evaluate,
    )

    resolved = turtlebot3_home_mission_runtime._resolve_recovery_candidate(
        _source_backed_recovery_obstacle()
    )

    assert calls == 2
    assert resolved["resolution_status"] == "validated"
    assert resolved["plan_only_evaluation_attempt_count"] == 2
    assert resolved["plan_only_retry_performed"] is True
    assert resolved["dispatch_request_sent"] is False
    assert resolved["dispatch_authority_created"] is False


def test_turtlebot3_recovery_candidate_resolver_uses_second_stable_snapshot(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV,
        "1",
    )
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_COUNT_ENV,
        "2",
    )
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_INTERVAL_ENV,
        "0",
    )
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_PLAN_ONLY_STABILITY_SNAPSHOT_COUNT_ENV,
        "2",
    )
    calls = 0

    def _evaluate(self, *, candidates, obstacle, frame_id="map"):
        nonlocal calls
        del self, frame_id
        selected_index = 0 if calls == 0 else 1
        calls += 1
        selected = _core_ready_candidate(
            candidates[selected_index],
            obstacle,
            local_current_cost=30,
        )
        return _core_ready_evaluation(
            evaluated=[selected],
            selected=selected,
            global_hash=f"global-{calls}",
            local_hash=f"local-{calls}",
            stamp_ns=calls * 1_000_000_000,
        )

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime.Ros2Nav2BridgeCommandClient,
        "evaluate_recovery_candidates",
        _evaluate,
    )

    resolved = turtlebot3_home_mission_runtime._resolve_recovery_candidate(
        _source_backed_recovery_obstacle()
    )

    assert calls == 2
    assert resolved["selected_candidate"]["candidate_id"] == (
        "obstacle_bypass_north"
    )
    assert resolved["global_costmap_snapshot_hash"] == "global-2"
    assert resolved["plan_only_evaluation_attempt_count"] == 2
    assert resolved["plan_only_retry_performed"] is True
    assert resolved["dispatch_authority_created"] is False


def test_turtlebot3_recovery_candidates_prioritize_route_lateral_bypass() -> None:
    candidates = turtlebot3_home_mission_runtime._deterministic_recovery_candidates(
        {
            "runtime_obstacle_x_m": 0.2,
            "runtime_obstacle_y_m": -1.2,
            "runtime_obstacle_size_x_m": 0.32,
            "runtime_obstacle_size_y_m": 0.32,
        }
    )

    priorities = {item["side"]: item["selection_priority"] for item in candidates}
    assert priorities["right"] == 0
    assert priorities["left"] == 0
    assert priorities["forward"] == 1
    assert priorities["backtrack"] == 2


def test_turtlebot3_recovery_resolution_prefers_direct_validated_bypass(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV,
        "1",
    )

    evaluation_calls: list[list[dict]] = []

    def _evaluate(self, *, candidates, obstacle, frame_id="map"):
        del self, frame_id
        evaluation_calls.append([dict(candidate) for candidate in candidates])
        evaluated = [
            _core_ready_candidate(
                candidate,
                obstacle,
                local_maximum_path_cost=30,
            )
            for candidate in candidates
        ]
        selected = next(
            item for item in evaluated if item.get("sequence_only") is not True
        )
        return _core_ready_evaluation(
            evaluated=evaluated,
            selected=selected,
            global_hash="global-hash",
            local_hash="local-hash",
        )

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime.Ros2Nav2BridgeCommandClient,
        "evaluate_recovery_candidates",
        _evaluate,
    )
    segment_results = [
        {
            "bridge_responses": [
                {
                    "trajectory_result": {
                        "trajectory_samples": [
                            {"x_m": -1.4, "y_m": -0.5, "frame_id": "map"},
                            {"x_m": -0.9, "y_m": -0.7, "frame_id": "map"},
                        ]
                    }
                }
            ]
        }
    ]

    resolved = turtlebot3_home_mission_runtime._resolve_recovery_candidate(
        _source_backed_recovery_obstacle(x_m=0.2, y_m=-1.2),
        segment_results=segment_results,
    )

    assert resolved["dual_costmap_validated"] is True
    assert resolved["bounded_retreat_required"] is False
    assert [item["candidate_id"] for item in resolved["selected_sequence"]] == [
        "obstacle_bypass_south",
    ]
    assert resolved["dispatch_request_sent"] is False
    assert len(evaluation_calls) == 1


def test_turtlebot3_recovery_resolution_rechecks_bypasses_after_retreat(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV,
        "1",
    )

    evaluation_calls: list[list[dict]] = []

    def _evaluate(self, *, candidates, obstacle, frame_id="map"):
        del self, frame_id
        requested = [dict(candidate) for candidate in candidates]
        evaluation_calls.append(requested)
        is_initial = len(requested) > 2
        evaluated = []
        for candidate in requested:
            candidate_id = candidate["candidate_id"]
            is_retreat = candidate_id == "observed_inbound_bounded_retreat"
            is_safe_post_retreat = (
                candidate_id == "obstacle_bypass_south"
                and isinstance(candidate.get("start_pose"), dict)
            )
            path_valid = is_retreat or (not is_initial and is_safe_post_retreat)
            evaluated_candidate = _core_ready_candidate(
                candidate,
                obstacle,
                path_valid=path_valid,
                maximum_path_cost=20 if path_valid else 240,
                local_current_cost=227,
                local_maximum_path_cost=30 if path_valid else 227,
            )
            evaluated.append(evaluated_candidate)
        selected = next(
            (
                item
                for item in evaluated
                if item.get("sequence_only") is not True
                and item.get("path_valid") is True
            ),
            None,
        )
        return _core_ready_evaluation(
            evaluated=evaluated,
            selected=selected,
            global_hash="global-hash",
            local_hash="local-hash",
        )

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime.Ros2Nav2BridgeCommandClient,
        "evaluate_recovery_candidates",
        _evaluate,
    )
    segment_results = [
        {
            "bridge_responses": [
                {
                    "trajectory_result": {
                        "trajectory_samples": [
                            {"x_m": -0.8, "y_m": -0.8, "frame_id": "map"},
                            {"x_m": 0.0, "y_m": -0.8, "frame_id": "map"},
                        ]
                    }
                }
            ]
        }
    ]

    resolved = turtlebot3_home_mission_runtime._resolve_recovery_candidate(
        _source_backed_recovery_obstacle(),
        segment_results=segment_results,
    )

    assert resolved["resolution_status"] == "validated"
    assert resolved["bounded_retreat_required"] is True
    assert [item["candidate_id"] for item in resolved["selected_sequence"]] == [
        "observed_inbound_bounded_retreat",
        "obstacle_bypass_south",
    ]
    assert len(evaluation_calls) == 6
    assert evaluation_calls[1][1]["start_pose"] == {
        "x_m": pytest.approx(-0.45),
        "y_m": pytest.approx(-0.8),
        "yaw_rad": pytest.approx(0.0),
    }


def test_recovery_repair_child_keeps_route_cursor_separate_from_failure_evidence(
    monkeypatch,
) -> None:
    parent = {
        "checkpoint_id": "parent-checkpoint",
        "checkpoint_hash": "parent-hash",
        "next_segment_index": 2,
        "recovery_candidate_binding": {"candidate_id": "failed-south"},
        "failure_reasons": ["nav2_recovery_orbit_detected"],
    }
    selected = {
        "candidate_id": "repair-west",
        "x_m": -1.0,
        "y_m": -1.2,
        "yaw_rad": 0.0,
        "path_sha256": "repair-path",
    }
    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_resolve_recovery_candidate",
        lambda *_args, **_kwargs: {
            "resolution_status": "validated",
            "selected_candidate": selected,
            "selected_sequence": [selected],
            "live_costmap_validated": True,
            "dual_costmap_validated": True,
            "global_costmap_snapshot_hash": "global",
            "local_costmap_snapshot_hash": "local",
        },
    )
    goals = (
        Nav2GoalPose(x_m=0.0, y_m=0.0, label="completed"),
        Nav2GoalPose(x_m=1.0, y_m=0.0, label="remaining"),
    )
    route_results = [{"completion_claimed": True}]
    repair = turtlebot3_home_mission_runtime._build_recovery_repair_child_checkpoint(
        parent_checkpoint=parent,
        proposal={
            "proposal_id": "proposal",
            "robot_profile": "turtlebot3",
            "execution_target": "ros2_nav2_turtlebot3_sim",
        },
        goals=goals,
        segment_results=route_results,
        candidate_observation_results=[
            *route_results,
            {"completion_claimed": False},
        ],
        recovery_proposals=(
            {
                "proposal_id": "recovery-proposal",
                "selected_action": "avoid_obstacle",
                "input_observations": {},
            },
        ),
        recovery_proposal_classifications=(
            {"classification_id": "classification"},
        ),
        recovery_planner_result={},
        obstacle_scenario={},
        motion_context={},
    )

    assert repair is not None
    child, _, _ = repair
    assert child["completed_segment_count"] == 1
    assert child["next_segment_index"] == 2
    assert child["parent_checkpoint_id"] == "parent-checkpoint"
    assert child["checkpoint_status"] == "awaiting_operator_approval"
    assert child["automatic_redispatch_performed"] is False


def _write_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['role_contract']['rules_classify_execution_after_proposal'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'return_home',\n"
        "    'reason': 'LLM judges battery reserve and proposes return_home.',\n"
        "    'input_observations': {\n"
        "        'battery_start_pct': prompt['battery_envelope']['battery_start_pct'],\n"
        "        'minimum_required_pct': prompt['battery_envelope']['minimum_required_pct'],\n"
        "        'distance_to_home_m': prompt['home_distance_envelope']['distance_to_home_m'],\n"
        "        'projected_return_battery_required_pct': prompt['home_distance_envelope']['projected_return_battery_required_pct'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_failure_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "failure = prompt['runtime_failure_context']\n"
        "motion = prompt['runtime_motion_context']\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "assert failure['runtime_failure_observed'] is True\n"
        "assert failure['runtime_failure_source'] == 'ros2_nav2_bridge_segment_result'\n"
        "assert motion['odom_delta_m'] == 0.0\n"
        "assert motion['stalled_after_dispatch'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'return_home',\n"
        "    'reason': 'LLM judges source-backed Nav2 segment failure and odom stall.',\n"
        "    'input_observations': {\n"
        "        'runtime_failure_observed': failure['runtime_failure_observed'],\n"
        "        'failed_segment_index': failure['failed_segment_index'],\n"
        "        'failed_segment_label': failure['failed_segment_label'],\n"
        "        'runtime_failure_source': failure['runtime_failure_source'],\n"
        "        'failed_segment_completion_claimed': failure['failed_segment_completion_claimed'],\n"
        "        'failed_segment_blocking_reason_count': failure['failed_segment_blocking_reason_count'],\n"
        "        'nav2_status': failure['nav2_status'],\n"
        "        'odom_delta_m': motion['odom_delta_m'],\n"
        "        'robot_motion_observed': motion['robot_motion_observed'],\n"
        "        'stalled_after_dispatch': motion['stalled_after_dispatch'],\n"
        "        'motion_observation_source': motion['motion_observation_source'],\n"
        "        'route_progress_delta_m': motion['route_progress_delta_m'],\n"
        "        'distance_to_home_m': prompt['home_distance_envelope']['distance_to_home_m'],\n"
        "        'distance_to_home_source': prompt['home_distance_envelope']['distance_to_home_source'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_ask_human_failure_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "failure = prompt['runtime_failure_context']\n"
        "assert prompt['role_contract']['llm_must_not_approve'] is True\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "assert failure['runtime_failure_observed'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'ask_human',\n"
        "    'reason': 'The new Nav2 failure requires a fresh operator decision.',\n"
        "    'input_observations': {\n"
        "        'runtime_failure_observed': failure['runtime_failure_observed'],\n"
        "        'failed_segment_index': failure['failed_segment_index'],\n"
        "        'failed_segment_label': failure['failed_segment_label'],\n"
        "        'runtime_failure_source': failure['runtime_failure_source'],\n"
        "        'failed_segment_completion_claimed': failure['failed_segment_completion_claimed'],\n"
        "        'failed_segment_blocking_reason_count': failure['failed_segment_blocking_reason_count'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_ask_human_approved_recovery_failure_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "failure = prompt['runtime_failure_context']\n"
        "assert prompt['role_contract']['llm_must_not_approve'] is True\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "assert failure['schema_version'] == 'missionos_turtlebot3_approved_recovery_failure.v1'\n"
        "assert failure['runtime_failure_observed'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'ask_human',\n"
        "    'reason': 'The failed approved recovery requires bounded operator guidance.',\n"
        "    'input_observations': {\n"
        "        'runtime_failure_observed': failure['runtime_failure_observed'],\n"
        "        'failed_recovery_checkpoint_id': failure['failed_recovery_checkpoint_id'],\n"
        "        'failed_recovery_action': failure['failed_recovery_action'],\n"
        "        'failed_recovery_result_count': failure['failed_recovery_result_count'],\n"
        "        'runtime_failure_source': failure['runtime_failure_source'],\n"
        "        'recommended_recovery_action': failure['recommended_recovery_action'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_obstacle_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "assert prompt['obstacle_scenario']['runtime_obstacle_observed'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'avoid_obstacle',\n"
        "    'reason': 'LLM judges runtime obstacle evidence and proposes avoid_obstacle.',\n"
        "    'input_observations': {\n"
        "        'runtime_obstacle_observed': prompt['obstacle_scenario']['runtime_obstacle_observed'],\n"
        "        'costmap_obstacle_observed': prompt['obstacle_scenario']['costmap_obstacle_observed'],\n"
        "        'runtime_obstacle_source': prompt['obstacle_scenario']['runtime_obstacle_source'],\n"
        "        'recommended_recovery_action': prompt['obstacle_scenario']['recommended_recovery_action'],\n"
        "        'recommended_avoidance_target_x_m': prompt['obstacle_scenario']['recommended_avoidance_target_x_m'],\n"
        "        'recommended_avoidance_target_y_m': prompt['obstacle_scenario']['recommended_avoidance_target_y_m'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _recovery_operator_approval(
    checkpoint: dict[str, object],
    *,
    approval_ref: str = "operator_approval:test_interactive_recovery",
) -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_recovery_operator_approval.v1",
        "operator_approved": True,
        "explicit_recovery_dispatch_approval": True,
        "operator_approval_ref": approval_ref,
        "approval_actor": "test_operator",
        "approved_action": checkpoint["selected_action"],
        "approved_parameters": checkpoint["approved_parameters"],
        "recovery_proposal_id": checkpoint["recovery_proposal_id"],
        "recovery_classification_id": checkpoint["recovery_classification_id"],
        "checkpoint_id": checkpoint["checkpoint_id"],
        "checkpoint_hash": checkpoint["checkpoint_hash"],
    }


def _build_awaiting_obstacle_recovery(
    tmp_path: Path,
    monkeypatch,
    *,
    trajectory_frame_id: str | None = "map",
    sample_collection: str = "trajectory_samples",
) -> tuple[dict[str, object], dict[str, object], dict[str, object], Path]:
    # The directional-revision corpus fixes its historical route heading and
    # collision geometry. Production approach clearance is covered separately
    # by test_public_assurance_readiness and the chat arena E2E.
    monkeypatch.setattr(
        turtlebot3_home_mission_runtime, "_TURTLEBOT3_DYNAMIC_OBSTACLE_APPROACH_SEGMENT",
        turtlebot3_home_mission_runtime._TURTLEBOT3_DYNAMIC_OBSTACLE_APPROACH_SEGMENT.model_copy(
            update={"x_m": -1.60, "y_m": -0.85},
        ),
    )
    bridge = tmp_path / "revision_bridge.py"
    planner = tmp_path / "revision_planner.py"
    _write_success_bridge(
        bridge,
        obstacle_avoidance_observed=True,
        trajectory_frame_id=trajectory_frame_id,
        sample_collection=sample_collection,
    )
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    monkeypatch.delenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF",
        raising=False,
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    awaiting = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )
    return proposal, approval, awaiting, bridge


def _write_guardrail_fault_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['obstacle_scenario']['runtime_obstacle_observed'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'avoid_obstacle',\n"
        "    'reason': 'Malformed fixture claims dispatch authority.',\n"
        "    'dispatch_request_sent': True,\n"
        "    'input_observations': {\n"
        "        'runtime_obstacle_observed': prompt['obstacle_scenario']['runtime_obstacle_observed'],\n"
        "        'fabricated_distance_to_home_m': 123.456,\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_sidecar_jsonl(path: Path, *, moved: bool) -> None:
    end_x = 0.35 if moved else 0.0
    path.write_text(
        "\n".join(
            json.dumps(row, sort_keys=True)
            for row in [
                {
                    "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                    "sample_kind": "odom",
                    "captured_at": "2026-07-02T00:00:00+00:00",
                    "topic": "/odom",
                    "position": {"x_m": 0.0, "y_m": 0.0},
                },
                {
                    "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                    "sample_kind": "scan",
                    "captured_at": "2026-07-02T00:00:01+00:00",
                    "topic": "/scan",
                    "min_range_m": 0.45,
                },
                {
                    "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                    "sample_kind": "odom",
                    "captured_at": "2026-07-02T00:00:02+00:00",
                    "topic": "/odom",
                    "position": {"x_m": end_x, "y_m": 0.0},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_process_logs(tmp_path: Path) -> dict[str, str]:
    paths = {
        "gazebo": tmp_path / "gazebo.log",
        "nav2": tmp_path / "nav2.log",
        "relay": tmp_path / "relay.log",
        "telemetry_sidecar": tmp_path / "telemetry_sidecar.log",
    }
    paths["gazebo"].write_text("Gazebo started\nworld loaded\n", encoding="utf-8")
    paths["nav2"].write_text(
        "Nav2 active\n"
        "[controller_server]: Received a goal, begin computing control effort.\n"
        "[bt_navigator]: Navigation succeeded\n",
        encoding="utf-8",
    )
    paths["relay"].write_text(
        "relay active\nvelocity_generated_by_missionos=false\n",
        encoding="utf-8",
    )
    paths["telemetry_sidecar"].write_text(
        "telemetry sidecar active\nread_only=true\n",
        encoding="utf-8",
    )
    return {name: str(path) for name, path in paths.items()}


def _write_nav2_failure_process_logs(tmp_path: Path) -> dict[str, str]:
    paths = {
        "gazebo": tmp_path / "gazebo.log",
        "nav2": tmp_path / "nav2.log",
        "relay": tmp_path / "relay.log",
        "telemetry_sidecar": tmp_path / "telemetry_sidecar.log",
    }
    paths["gazebo"].write_text("Gazebo started\nworld loaded\n", encoding="utf-8")
    paths["nav2"].write_text(
        "Nav2 active\n"
        "[controller_server]: Received a goal, begin computing control effort.\n"
        "[controller_server]: Failed to make progress\n"
        "[controller_server]: [follow_path] [ActionServer] Aborting handle.\n"
        "[local_costmap.local_costmap]: Received request to clear entirely the local_costmap\n",
        encoding="utf-8",
    )
    paths["relay"].write_text(
        "relay active\nvelocity_generated_by_missionos=false\n",
        encoding="utf-8",
    )
    paths["telemetry_sidecar"].write_text(
        "telemetry sidecar active\nread_only=true\n",
        encoding="utf-8",
    )
    return {name: str(path) for name, path in paths.items()}


def test_turtlebot3_home_mission_classifier_keeps_questions_non_dispatchable() -> None:
    assert instruction_requests_turtlebot3_home_mission("TurtleBot3で家を一周して")
    assert instruction_requests_turtlebot3_home_mission("家の中を掃除して")
    assert instruction_requests_turtlebot3_home_mission("家の中で障害物を避けて")
    assert instruction_requests_turtlebot3_home_mission("家の中で屋内配送して")
    assert instruction_requests_turtlebot3_home_mission("TurtleBot3でバッテリー不足を判断して")
    assert infer_turtlebot3_home_mission_kind("家の中で障害物を避けながら屋内配送して") == (
        "indoor_delivery_route_leg"
    )
    assert infer_turtlebot3_home_mission_kind("家の中で障害物を避けて") == (
        "obstacle_avoidance_patrol_leg"
    )
    assert infer_turtlebot3_home_mission_kind("家の中を掃除して") == (
        "cleaning_inspection_leg"
    )
    assert infer_turtlebot3_home_mission_kind("荷物を運んで") == (
        "payload_transport_rehearsal_leg"
    )
    assert not instruction_requests_turtlebot3_home_mission("今日の天気を教えて")


def test_turtlebot3_plan_blocks_cleaning_payload_loop_and_physical_claims() -> None:
    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )

    proposal = result["scenario_proposal"]
    summary = result["summary"]

    assert proposal["mission_kind"] == "indoor_patrol_leg"
    assert proposal["requires_operator_approval"] is True
    assert proposal["physical_execution_invoked"] is False
    assert proposal["autonomy_envelope"]["operator_approved"] is False
    assert proposal["autonomy_envelope"]["llm_recovery_proposals_allowed"] is True
    assert proposal["autonomy_envelope"]["proposal_first_classification"] is True
    assert proposal["recovery_proposals"] == []
    assert "whole_home_loop_completion" in proposal["blocked_claims"]
    assert "cleaning_completion" in proposal["blocked_claims"]
    assert "payload_delivery_completion" in proposal["blocked_claims"]
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False


def test_turtlebot3_plan_adds_obstacle_and_battery_judgment_points() -> None:
    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けて。バッテリー80%",
    )

    proposal = result["scenario_proposal"]
    summary = result["summary"]
    points = proposal["ai_judgment_points"]

    assert proposal["mission_kind"] == "obstacle_avoidance_patrol_leg"
    assert proposal["obstacle_scenario"]["obstacle_challenge_requested"] is True
    assert proposal["battery_envelope"]["battery_start_pct"] == 80.0
    assert proposal["battery_envelope"]["dispatch_allowed"] is True
    assert proposal["home_distance_envelope"]["distance_to_home_source_backed"] is True
    assert proposal["autonomy_envelope"]["blocked_actions"] == [
        "raw_velocity",
        "unbounded_move",
        "physical_execution",
        "payload_delivery_completion",
    ]
    assert [point["judgment_kind"] for point in points] == [
        "battery_envelope",
        "obstacle_avoidance",
    ]
    assert points[0]["decision"] == "allow"
    assert points[1]["decision"] == "observe_required"
    assert summary["ai_judgment_points"][1]["required_runtime_observation"] == (
        "obstacle_avoidance_observed"
    )
    assert summary["dispatch_request_sent"] is False


def test_turtlebot3_approval_classifies_low_battery_return_home_recovery() -> None:
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
    )
    proposal = plan["scenario_proposal"]
    assert proposal["recovery_proposals"][0]["proposal_source"] == (
        "deterministic_fallback"
    )
    assert proposal["recovery_proposals"][0]["selected_action"] == "return_home"
    assert proposal["recovery_proposal_classifications"][0]["proposal_allowed"] is True
    assert proposal["recovery_proposal_classifications"][0]["execution_class"] == (
        "requires_human_approval"
    )

    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    approved = approval["turtlebot3_home_mission_approval"]
    classification = approved["recovery_proposal_classifications"][0]
    assert approved["autonomy_envelope"]["operator_approved"] is True
    assert approved["llm_recovery_proposals_allowed"] is True
    assert approved["proposal_first_classification"] is True
    assert classification["proposal_allowed"] is True
    assert classification["execution_class"] == "requires_human_approval"
    assert classification["execution_permitted_by_envelope"] is False
    assert classification["dispatch_authority_created"] is False
    assert classification["physical_execution_invoked"] is False


def test_turtlebot3_plan_uses_llm_recovery_planner_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    planner = tmp_path / "recovery_planner.py"
    _write_recovery_planner(planner)
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中に配送して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    recovery = proposal["recovery_proposals"][0]
    classification = proposal["recovery_proposal_classifications"][0]
    assert proposal["home_distance_envelope"]["distance_to_home_m"] == 2.599
    assert proposal["recovery_planner_result"]["planner_status"] == (
        "proposal_guardrail_passed"
    )
    assert recovery["proposal_source"] == "llm"
    assert recovery["llm_judgment_recorded"] is True
    assert recovery["selected_action"] == "return_home"
    assert recovery["input_observations"]["distance_to_home_m"] == 2.599
    assert recovery["llm_invocation_evidence"]["provider"] == "command_override"
    assert classification["proposal_allowed"] is True
    assert classification["execution_class"] == "requires_human_approval"
    assert classification["dispatch_authority_created"] is False


def test_turtlebot3_plan_uses_ollama_recovery_planner_when_configured(
    monkeypatch,
) -> None:
    from src.intelligence import turtlebot3_recovery_planner

    def fake_ollama_response(
        *,
        prompt_text: str,
        model_id: str,
        timeout_seconds: int,
    ) -> str:
        prompt = json.loads(prompt_text)
        assert model_id == "ollama_chat/gemma4:26b"
        assert timeout_seconds == 240
        return json.dumps(
            {
                "selected_action": "return_home",
                "reason": "Gemma judges battery reserve and proposes return_home.",
                "input_observations": {
                    "battery_start_pct": prompt["battery_envelope"][
                        "battery_start_pct"
                    ],
                    "planned_route_distance_m": prompt["battery_envelope"][
                        "planned_route_distance_m"
                    ],
                    "estimated_consumption_pct": prompt["battery_envelope"][
                        "estimated_consumption_pct"
                    ],
                    "minimum_required_pct": prompt["battery_envelope"][
                        "minimum_required_pct"
                    ],
                    "distance_to_home_m": prompt["home_distance_envelope"][
                        "distance_to_home_m"
                    ],
                    "distance_to_home_source": prompt["home_distance_envelope"][
                        "distance_to_home_source"
                    ],
                },
            }
        )

    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, "1")
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("MISSIONOS_OLLAMA_MODEL", "gemma4:26b")
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS", "240")
    monkeypatch.setattr(
        turtlebot3_recovery_planner,
        "_invoke_ollama_response_text",
        fake_ollama_response,
    )

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中に配送して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    recovery = proposal["recovery_proposals"][0]
    planner_result = proposal["recovery_planner_result"]
    assert planner_result["planner_status"] == "proposal_guardrail_passed"
    assert planner_result["llm_invocation_evidence"]["provider"] == "ollama_native"
    assert planner_result["llm_invocation_evidence"]["invocation_kind"] == (
        "ollama_chat_json"
    )
    assert recovery["proposal_source"] == "llm"
    assert recovery["llm_judgment_recorded"] is True
    assert recovery["selected_action"] == "return_home"
    assert 8.4 < recovery["input_observations"]["planned_route_distance_m"] < 9.0
    assert 17.0 < recovery["input_observations"]["estimated_consumption_pct"] < 18.0


def test_turtlebot4_plan_and_approval_keep_profile_identity() -> None:
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot4で家の中を一周して",
    )

    proposal = plan["scenario_proposal"]
    assert proposal["robot_profile"] == "turtlebot4"
    assert proposal["robot_model"] == "turtlebot4_lite"
    assert proposal["execution_target"] == "ros2_nav2_turtlebot4_sim"
    assert proposal["proposal_id"].startswith("turtlebot4_home_")
    assert "TurtleBot4 indoor patrol" in proposal["mission_objective"]

    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )
    request = approval["turtlebot3_bounded_nav2_request"]
    summary = approval["summary"]
    assert request["robot_profile"] == "turtlebot4"
    assert request["robot_model"] == "turtlebot4_lite"
    assert request["execution_target"] == "ros2_nav2_turtlebot4_sim"
    assert summary["robot_profile"] == "turtlebot4"
    assert summary["dispatch_request_sent"] is False
    assert summary["physical_execution_invoked"] is False

    floor_plan = turtlebot3_home_mission_runtime._turtlebot3_home_floor_plan(
        "turtlebot4"
    )
    assert floor_plan["floor_plan_id"] == "missionos_turtlebot4_indoor_fixture.v1"
    assert "display-only" in floor_plan["claim_boundary"]


def test_turtlebot3_execution_blocks_without_bridge_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, raising=False)
    monkeypatch.delenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, raising=False)
    monkeypatch.setenv(NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV, "1")
    monkeypatch.delenv(NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "blocked"
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert "ros2_nav2_bridge_command_missing" in summary["blocking_reasons"]
    assert f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV}_not_enabled" in summary[
        "blocking_reasons"
    ]
    assert summary["nvblox_perception_evidence_status"] == "not_configured"
    assert summary["nvblox_perception_evidence_available"] is False
    assert "nvblox_perception_evidence_not_configured" in summary["blocking_reasons"]


def test_nova_carter_plan_records_isaac_runtime_profile(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="Nova CarterでIsaac Sim内の短いNav2ルートを走って",
    )

    proposal = plan["scenario_proposal"]
    summary = plan["summary"]
    assert proposal["robot_profile"] == "nova_carter"
    assert proposal["robot_label"] == "Nova Carter"
    assert proposal["robot_model"] == "nova_carter"
    assert proposal["execution_target"] == "isaac_ros_nav2_nova_carter_sim"
    assert proposal["runtime_substrate"] == "NVIDIA Isaac Sim + Isaac ROS/Nav2"
    assert proposal["mission_objective"] == (
        "Nova Carter bounded waypoint move via Nav2 simulation"
    )
    assert summary["robot_profile"] == "nova_carter"
    assert summary["execution_target"] == "isaac_ros_nav2_nova_carter_sim"
    assert summary["physical_execution_invoked"] is False
    assert summary["mission_delivery_completion_claimed"] is False


def test_nova_carter_execution_blocks_when_isaac_runtime_is_not_configured(
    monkeypatch,
) -> None:
    monkeypatch.delenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, raising=False)
    monkeypatch.delenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="Nova CarterでIsaac Sim内の短いNav2ルートを走って",
        robot_profile="nova_carter",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    indoor_map = result["turtlebot3_indoor_map_model"]
    assert summary["status"] == "blocked"
    assert summary["robot_profile"] == "nova_carter"
    assert summary["execution_target"] == "isaac_ros_nav2_nova_carter_sim"
    assert summary["runtime_configuration_status"] == "not_configured"
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert "isaac_sim_nova_carter_bridge_command_missing" in summary[
        "blocking_reasons"
    ]
    assert "isaac_sim_nova_carter_runtime_not_enabled" in summary[
        "blocking_reasons"
    ]
    assert execution["runtime_substrate"] == "NVIDIA Isaac Sim + Isaac ROS/Nav2"
    assert indoor_map["robot_label"] == "Nova Carter"
    assert indoor_map["physical_execution_invoked"] is False


def test_turtlebot3_low_battery_judgment_blocks_before_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge_should_not_run.py"
    bridge.write_text("raise SystemExit('bridge should not run')\n", encoding="utf-8")
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "blocked"
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["recovery_action_suggested"] == "return_home"
    assert summary["recovery_execution_permitted_by_envelope"] is False
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_proposal_classifications"][0]["proposal_allowed"] is True
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "requires_human_approval"
    )
    assert "battery_below_minimum_required" in summary["blocking_reasons"]


def test_turtlebot3_execution_claims_only_sim_action_with_motion_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    evidence = result["ros2_nav2_hardware_adapter_evidence"]
    review = result["mission_episode_review"]
    segment = execution["segment_results"][0]
    predicate = segment["mission_contract_predicate_evaluation"]
    assert summary["status"] == "completed", json.dumps(summary, indent=2)
    assert summary["dispatch_request_sent"] is True
    assert summary["completion_claimed"] is True
    assert summary["completion_scope"] == "sim_action"
    assert summary["mission_episode_review_status"] == "passed"
    assert summary["mission_episode_review_passed"] is True
    assert result["mission_episode_review_ref"].startswith("mission_episode_review:")
    assert execution["mission_episode_review_ref"] == result["mission_episode_review_ref"]
    assert review["status"] == "passed"
    assert review["source_completion_scope"] == "sim_action"
    assert summary["robot_motion_observed"] is True
    assert 2.4 < summary["odom_delta_m"] < 2.8
    assert summary["physical_execution_invoked"] is False
    assert summary["llm_recovery_proposals_allowed"] is True
    assert summary["proposal_first_classification"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["whole_home_loop_completion_claimed"] is False
    assert execution["cleaning_completion_claimed"] is False
    assert execution["payload_delivery_completion_claimed"] is False
    assert evidence["completion_scope"] == "sim_action"
    assert "sim_action_completion_not_physical" in evidence["unproven_claims"]
    assert "mission_contract_frozen_before_dispatch" not in segment
    assert segment["dispatch_started_at"]
    assert segment["result_observed_at"]
    assert segment["adapter_completion_claimed"] is True
    assert predicate["status"] == "satisfied"
    assert predicate["evaluated_outcome_claim"] is True
    assert predicate["dispatch_authority_created"] is False
    assert predicate["runtime_effect_requested"] is False
    assert predicate["operational_closure_created"] is False
    assert predicate["physical_execution_invoked"] is False


def test_turtlebot3_execution_attaches_process_log_bundle_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(
        TURTLEBOT3_LOG_BUNDLE_PATHS_ENV,
        json.dumps(_write_process_logs(tmp_path)),
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    evidence = result["ros2_nav2_hardware_adapter_evidence"]
    bundle = result["log_bundle_artifacts"]["turtlebot3_log_bundle"]
    assert summary["status"] == "completed"
    assert summary["raw_logs_ref"].startswith("turtlebot3_process_log_bundle:")
    assert summary["raw_logs_ref"] == evidence["raw_logs_ref"]
    assert summary["log_bundle_status"] == "ready"
    assert summary["log_bundle_observed_source_count"] == 4
    assert summary["nav2_log_diagnostics_status"] == "ready"
    assert summary["nav2_log_observed_patterns"] == ["nav2_goal_received"]
    assert summary["nav2_log_failure_hypotheses"] == []
    assert bundle["bundle_status"] == "ready"
    assert bundle["raw_logs_ref"] == summary["raw_logs_ref"]
    assert bundle["raw_logs_included"] is False
    assert bundle["physical_execution_invoked"] is False
    assert (
        result["log_bundle_artifacts"]["nav2_log_diagnostics"][
            "physical_execution_invoked"
        ]
        is False
    )
    assert execution["raw_logs_ref"] == summary["raw_logs_ref"]


def test_turtlebot3_execution_uses_sidecar_motion_when_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    sidecar = tmp_path / "telemetry.jsonl"
    _write_success_bridge(bridge)
    _write_sidecar_jsonl(sidecar, moved=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV, str(sidecar))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    artifacts = result["telemetry_sidecar_artifacts"]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["robot_motion_observed"] is True
    assert summary["odom_delta_m"] == 0.35
    assert summary["robot_motion_observation_source"] == (
        "ros2_telemetry_sidecar_jsonl"
    )
    assert summary["telemetry_sidecar_required"] is True
    assert summary["telemetry_sidecar_motion_correlation_confirmed"] is True
    assert artifacts["telemetry_sidecar_status"] == "ready"
    assert artifacts["turtlebot3_telemetry_window"]["odom_motion_observed"] is True
    assert artifacts["turtlebot3_state_correlation"][
        "motion_correlation_confirmed"
    ] is True
    assert execution["telemetry_sidecar_artifacts"]["raw_logs_ref"].startswith(
        "turtlebot3_telemetry_sidecar_jsonl:"
    )
    assert summary["physical_execution_invoked"] is False


def test_turtlebot3_execution_does_not_complete_when_sidecar_motion_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    sidecar = tmp_path / "telemetry.jsonl"
    _write_success_bridge(bridge)
    _write_sidecar_jsonl(sidecar, moved=False)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV, str(sidecar))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    artifacts = result["telemetry_sidecar_artifacts"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["status"] == "blocked"
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["robot_motion_observed"] is False
    assert summary["robot_motion_observation_source"] == (
        "ros2_telemetry_sidecar_jsonl"
    )
    assert summary["telemetry_sidecar_required"] is True
    assert summary["telemetry_sidecar_motion_correlation_confirmed"] is False
    assert "telemetry_sidecar_motion_correlation_not_confirmed" in summary[
        "blocking_reasons"
    ]
    assert "sidecar_motion_not_observed" in summary["blocking_reasons"]
    assert artifacts["telemetry_sidecar_status"] == "blocked"
    assert artifacts["turtlebot3_state_correlation"]["correlation_status"] == (
        "blocked"
    )


def test_turtlebot3_obstacle_mission_needs_avoidance_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=False)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["obstacle_challenge_required"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert "obstacle_avoidance_not_observed" in summary["blocking_reasons"]


def test_turtlebot3_obstacle_mission_does_not_promote_nvblox_costmap_to_avoidance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    nvblox = tmp_path / "nvblox_perception.json"
    _write_success_bridge(bridge, obstacle_avoidance_observed=False)
    nvblox.write_text(
        json.dumps(
            {
                "perception_source": "isaac_ros_nvblox",
                "depth_input_observed": True,
                "pose_input_observed": True,
                "scene_reconstruction_observed": True,
                "nav2_costmap_updated_from_perception": True,
                "dynamic_obstacle_observed": True,
                "perception_artifact_refs": [
                    "output/nvblox/perception_evidence.json",
                    "output/nvblox/costmap.json",
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV, "1")
    monkeypatch.setenv(NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV, str(nvblox))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["nvblox_perception_evidence_status"] == "available"
    assert summary["nvblox_perception_evidence_available"] is True
    assert summary["perception_source"] == "isaac_ros_nvblox"
    assert summary["depth_input_observed"] is True
    assert summary["pose_input_observed"] is True
    assert summary["scene_reconstruction_observed"] is True
    assert summary["nav2_costmap_updated_from_perception"] is True
    assert summary["costmap_obstacle_observed"] is True
    assert summary["bridge_obstacle_avoidance_observed"] is False
    assert summary["obstacle_avoidance_observed"] is False
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert "obstacle_avoidance_not_observed" in summary["blocking_reasons"]
    assert "cannot claim obstacle avoidance" in summary[
        "nvblox_perception_claim_boundary"
    ]
    assert (
        execution["nvblox_perception_evidence"][
            "obstacle_avoidance_completion_claimed"
        ]
        is False
    )


def test_turtlebot3_obstacle_mission_claims_completion_with_avoidance_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["completion_scope"] == "sim_action"
    assert summary["obstacle_detected"] is True
    assert summary["obstacle_avoidance_observed"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is True
    assert summary["trajectory_lateral_deviation_observed"] is True
    assert summary["max_lateral_deviation_m"] == 0.12
    assert summary["obstacle_trajectory_clearance_observed"] is True
    assert summary["obstacle_trajectory_intersects_obstacle"] is False
    assert summary["obstacle_trajectory_3d_clearance_status"] == "verified_clear"
    assert summary["obstacle_trajectory_3d_clearance_observed"] is True
    assert summary["obstacle_trajectory_3d_collision_observed"] is False
    clearance_3d = summary["obstacle_trajectory_3d_clearance"]
    assert clearance_3d["robot_collision_envelope"]["radius_m"] == 0.19
    assert clearance_3d["trajectory_segment_count"] > 0
    assert len(clearance_3d["candidate_results"]) == 3
    assert {
        candidate["semantic_candidate"]
        for candidate in clearance_3d["candidate_results"]
    } == {"closed door", "humanoid", "robot dog"}
    assert all(
        candidate["status"] == "verified_clear"
        for candidate in clearance_3d["candidate_results"]
    )
    assert clearance_3d["approval_created"] is False
    assert clearance_3d["dispatch_authority_created"] is False
    assert clearance_3d["completion_claimed"] is False
    assert result["turtlebot3_indoor_map_model"]["trajectory_clearance_3d"] == (
        clearance_3d
    )


def test_turtlebot3_obstacle_mission_requires_raw_map_frame_trajectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(
        bridge,
        obstacle_avoidance_observed=True,
        trajectory_frame_id=None,
    )
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    indoor_map = result["turtlebot3_indoor_map_model"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["bridge_obstacle_avoidance_observed"] is True
    assert summary["obstacle_trajectory_clearance_observed"] is False
    assert summary["obstacle_trajectory_intersects_obstacle"] is False
    assert summary["obstacle_trajectory_geometry_status"] == (
        "raw_map_frame_trajectory_unavailable"
    )
    assert summary["obstacle_trajectory_3d_clearance_status"] == "unavailable"
    assert summary["obstacle_trajectory_3d_clearance_observed"] is False
    assert summary["obstacle_trajectory_raw_map_frame_sample_count"] == 0
    assert summary["obstacle_trajectory_non_map_sample_count_excluded"] > 0
    assert summary["obstacle_trajectory_display_alignment_used"] is False
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert summary["completion_claimed"] is False
    assert summary["status"] == "blocked"
    assert (
        "obstacle_trajectory_raw_map_frame_evidence_unavailable"
        in summary["blocking_reasons"]
    )
    assert indoor_map["display_alignment"]["method"] == (
        "first_observed_pose_to_planned_home"
    )


def test_obstacle_geometry_never_uses_display_aligned_points_as_evidence() -> None:
    obstacle_x_m, obstacle_y_m = (
        turtlebot3_home_mission_runtime._profile_delivery_obstacle_xy()
    )
    planned_points = [
        {
            "x_m": -2.0,
            "y_m": -0.5,
            "frame_id": "map",
            "role": "home",
            "label": "simulated_home_origin",
        }
    ]
    observed_points = [
        {
            "x_m": 0.0,
            "y_m": 0.0,
            "frame_id": "odom",
            "segment_ref": "mixed_frame_segment",
            "trajectory_sample_collection": "trajectory_samples",
            "observed_trajectory_evidence_eligible": True,
        },
        {
            "x_m": obstacle_x_m,
            "y_m": obstacle_y_m,
            "frame_id": "map",
            "segment_ref": "mixed_frame_segment",
            "trajectory_sample_collection": "trajectory_samples",
            "observed_trajectory_evidence_eligible": True,
        },
    ]
    display_alignment = (
        turtlebot3_home_mission_runtime._observed_display_alignment(
            planned_points=planned_points,
            observed_points=observed_points,
            recovery_points=[],
        )
    )
    display_points = turtlebot3_home_mission_runtime._apply_observed_display_alignment(
        observed_points,
        alignment=display_alignment,
    )

    raw_geometry = turtlebot3_home_mission_runtime._obstacle_trajectory_geometry(
        obstacle_required=True,
        obstacle={"costmap_obstacle_observed": True},
        observed_points=observed_points,
        recovery_points=[],
    )
    display_geometry = turtlebot3_home_mission_runtime._obstacle_trajectory_geometry(
        obstacle_required=True,
        obstacle={"costmap_obstacle_observed": True},
        observed_points=display_points,
        recovery_points=[],
    )

    assert display_alignment["applied"] is True
    assert raw_geometry["obstacle_trajectory_intersects_obstacle"] is True
    assert raw_geometry["obstacle_trajectory_clearance_observed"] is False
    assert raw_geometry["obstacle_trajectory_raw_map_frame_sample_count"] == 1
    assert raw_geometry["obstacle_trajectory_non_map_sample_count_excluded"] == 1
    assert raw_geometry["obstacle_trajectory_display_alignment_used"] is False
    assert display_geometry["obstacle_trajectory_clearance_observed"] is False
    assert display_geometry[
        "obstacle_trajectory_display_aligned_sample_count_excluded"
    ] == len(display_points)


def test_turtlebot3_obstacle_mission_blocks_when_trajectory_crosses_obstacle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_intersecting_obstacle_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["status"] == "blocked"
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["bridge_obstacle_avoidance_observed"] is True
    assert summary["obstacle_avoidance_observed"] is False
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert summary["obstacle_trajectory_clearance_observed"] is False
    assert summary["obstacle_trajectory_intersects_obstacle"] is True
    assert summary["obstacle_intersection_point_count"] > 0
    assert "obstacle_trajectory_intersects_obstacle" in summary["blocking_reasons"]


def test_turtlebot3_delivery_route_claims_dropoff_arrival_not_payload_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
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

    proposal = plan["scenario_proposal"]
    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert proposal["mission_kind"] == "indoor_delivery_route_leg"
    assert len(proposal["planned_segments"]) == 10
    assert 8.4 < proposal["planned_route_distance_m"] < 9.0
    assert proposal["indoor_delivery_route"]["route_layout"] == (
        "simulated_home_loop_with_closed_door_humanoid_and_robot_dog_detours"
    )
    assert [
        blocker["label"]
        for blocker in proposal["indoor_delivery_route"]["simulated_blockers"]
    ] == ["closed door", "humanoid", "robot dog"]
    assert proposal["obstacle_scenario"]["obstacle_challenge_requested"] is True
    assert proposal["indoor_delivery_route"]["route_requested"] is True
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["planned_segment_count"] == 10
    assert 8.4 < summary["planned_route_distance_m"] < 9.0
    assert summary["segment_dispatch_count"] == 10
    assert summary["segment_completion_count"] == 10
    assert summary["multi_segment_mission_claimed"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is True
    assert summary["indoor_delivery_route_completion_claimed"] is True
    assert summary["dropoff_arrival_claimed"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["planned_route_distance_m"] == summary["planned_route_distance_m"]
    assert execution["payload_delivery_completion_claimed"] is False


def test_turtlebot3_mid_mission_low_battery_dispatches_return_home_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で障害物を避けながら屋内配送して。"
            "途中でバッテリーが足りなくなったら帰還"
        ),
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert summary["status"] == "pending"
    assert summary["completion_claimed"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is False
    assert summary["dropoff_arrival_claimed"] is False
    assert summary["runtime_recovery_triggered"] is True
    assert summary["route_interrupted_for_recovery"] is True
    assert summary["planned_segment_count"] == 10
    assert 8.4 < summary["planned_route_distance_m"] < 9.0
    assert summary["segment_dispatch_count"] == 1
    assert summary["segment_completion_count"] == 1
    assert summary["recovery_action_suggested"] == "return_home"
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_completion_claimed"] is False
    assert summary["turtlebot3_recovery_checkpoint"]["selected_action"] == "return_home"
    assert summary["physical_execution_invoked"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["recovery_completion_claimed"] is False


def test_turtlebot3_mid_mission_obstacle_recovery_resumes_delivery_with_llm_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    assert result["summary"]["recovery_dispatch_request_sent"] is False
    checkpoint = result["turtlebot3_recovery_checkpoint"]
    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal, approval=approval["turtlebot3_home_mission_approval"],
        resume_execution=result["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )
    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert proposal["mission_kind"] == "indoor_delivery_route_leg"
    assert proposal["obstacle_scenario"]["runtime_obstacle_recovery_requested"] is True
    assert proposal["planned_segments"][0]["label"] == (
        "simulated_dynamic_obstacle_approach"
    )
    assert proposal["planned_segments"][0]["x_m"] == pytest.approx(-1.90)
    assert math.hypot(
        proposal["planned_segments"][0]["x_m"]
        - turtlebot3_home_mission_runtime._TURTLEBOT3_DELIVERY_OBSTACLE_X_M,
        proposal["planned_segments"][0]["y_m"]
        - turtlebot3_home_mission_runtime._TURTLEBOT3_DELIVERY_OBSTACLE_Y_M,
    ) > 0.5
    assert proposal["planned_segments"][1]["x_m"] == pytest.approx(-0.55)
    assert proposal["planned_segments"][1]["y_m"] == pytest.approx(-1.55)
    assert len(proposal["planned_segments"]) == 10
    assert 8.4 < proposal["planned_route_distance_m"] < 9.0
    assert proposal["autonomy_envelope"]["preapproved_recovery_actions"] == [
        "hold",
    ]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["runtime_recovery_triggered"] is True
    assert summary["runtime_recovery_action_kind"] == "avoid_obstacle"
    assert summary["route_interrupted_for_recovery"] is True
    assert summary["route_resumed_after_recovery"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["recovery_action_suggested"] == "avoid_obstacle"
    assert summary["recovery_proposals"][0]["proposal_source"] == "llm"
    assert summary["recovery_proposals"][0]["llm_judgment_recorded"] is True
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "requires_human_approval"
    )
    reflex = summary["recovery_planner_result"]["recovery_reflex"]
    assert reflex["trigger"] == "runtime_obstacle_observed"
    assert reflex["robot_motion_state"] == "moving"
    assert reflex["stop_dispatch_required"] is True
    assert reflex["stop_dispatch_performed"] is True
    stop_dispatch = summary["recovery_planner_result"]["harness_stop_dispatch"]
    assert stop_dispatch["authority_source"] == "emergency_harness"
    assert stop_dispatch["harness_action"] == "hold"
    assert stop_dispatch["bridge_action"] == "cancel_goal"
    assert stop_dispatch["recorded_reason"] == (
        "reflex_first_recovery_entry:runtime_obstacle_observed"
    )
    assert stop_dispatch["stop_confirmed"] is True
    assert stop_dispatch["bridge_receipt"]["ack_status"] == "accepted"
    assert stop_dispatch["physical_execution_invoked"] is False
    assert stop_dispatch["progress_counted"] is False
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert 8.4 < summary["planned_route_distance_m"] < 9.0
    assert summary["segment_dispatch_count"] == summary["planned_segment_count"]
    assert summary["segment_completion_count"] == summary["planned_segment_count"]
    assert summary["obstacle_avoidance_completion_claimed"] is True
    assert summary["obstacle_trajectory_clearance_observed"] is True
    assert summary["obstacle_trajectory_intersects_obstacle"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert execution["runtime_recovery_obstacle_scenario"][
        "runtime_obstacle_observed"
    ] is True
    assert execution["recovery_segment_result"]["goal_pose"]["label"] == (
        "runtime_recovery_avoid_obstacle_waypoint"
    )


def test_recovery_shadow_comparison_records_agreement_with_llm_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    planner = tmp_path / "recovery_planner.py"
    _write_recovery_planner(planner)
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中に配送して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    shadow = proposal["recovery_planner_result"]["shadow_comparison"]
    assert shadow["schema_version"] == (
        TURTLEBOT3_RECOVERY_SHADOW_COMPARISON_SCHEMA_VERSION
    )
    assert shadow["deterministic_action"] == "return_home"
    assert shadow["deterministic_trigger"] == "battery_envelope_below_reserve"
    assert shadow["llm_action"] == "return_home"
    assert shadow["llm_proposal_available"] is True
    assert shadow["agreement"] is True
    assert shadow["measurement_only"] is True
    assert shadow["approval_created"] is False
    assert shadow["dispatch_authority_created"] is False
    assert shadow["physical_execution_invoked"] is False
    assert shadow["progress_counted"] is False


def test_recovery_shadow_comparison_records_disagreement_without_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    planner = tmp_path / "hold_recovery_planner.py"
    planner.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "print(json.dumps({\n"
        "    'selected_action': 'hold',\n"
        "    'reason': 'LLM prefers holding in place over returning home.',\n"
        "    'input_observations': {\n"
        "        'battery_start_pct': prompt['battery_envelope']['battery_start_pct'],\n"
        "        'minimum_required_pct': prompt['battery_envelope']['minimum_required_pct'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中に配送して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    shadow = proposal["recovery_planner_result"]["shadow_comparison"]
    assert proposal["recovery_proposals"][0]["selected_action"] == "hold"
    assert shadow["deterministic_action"] == "return_home"
    assert shadow["llm_action"] == "hold"
    assert shadow["llm_proposal_available"] is True
    assert shadow["agreement"] is False
    assert shadow["measurement_only"] is True
    assert shadow["dispatch_authority_created"] is False


def test_recovery_shadow_comparison_marks_llm_unavailable_on_fallback(
    monkeypatch,
) -> None:
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    shadow = proposal["recovery_planner_result"]["shadow_comparison"]
    assert proposal["recovery_proposals"][0]["proposal_source"] == (
        "deterministic_fallback"
    )
    assert shadow["deterministic_action"] == "return_home"
    assert shadow["llm_action"] == ""
    assert shadow["llm_proposal_available"] is False
    assert shadow["agreement"] is None
    assert shadow["planner_status"] == "not_configured"
    assert shadow["measurement_only"] is True


def test_recovery_entry_records_reflex_phase_before_deliberation(
    monkeypatch,
) -> None:
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    reflex = proposal["recovery_planner_result"]["recovery_reflex"]
    assert reflex["schema_version"] == TURTLEBOT3_RECOVERY_REFLEX_SCHEMA_VERSION
    assert reflex["trigger"] == "battery_envelope_below_reserve"
    assert reflex["reflex_action"] == "hold"
    assert reflex["robot_motion_state"] == "pre_dispatch"
    assert reflex["stop_dispatch_required"] is False
    assert reflex["stop_dispatch_performed"] is False
    assert reflex["entered_deliberation"] is True
    assert reflex["approval_created"] is False
    assert reflex["dispatch_authority_created"] is False
    assert reflex["physical_execution_invoked"] is False
    harness = proposal["autonomy_envelope"]["emergency_harness"]
    assert harness["reflex_first_recovery_entry"] is True


def _write_perception_claim_citing_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "claims = prompt['perception_claims']\n"
        "assert len(claims) == 1\n"
        "claim = claims[0]\n"
        "assert claim['claim_kind'] == 'corridor_blocked_by_object'\n"
        "assert claim['corroborated_by'][0] == "
        "'lidar_costmap:nav2_costmap_obstacle_observed'\n"
        "print(json.dumps({\n"
        "    'selected_action': 'avoid_obstacle',\n"
        "    'reason': 'Camera claim is corroborated by the Nav2 costmap; going around it.',\n"
        "    'input_observations': {\n"
        "        'runtime_obstacle_observed': prompt['obstacle_scenario']['runtime_obstacle_observed'],\n"
        "        'costmap_obstacle_observed': prompt['obstacle_scenario']['costmap_obstacle_observed'],\n"
        "    },\n"
        "    'cited_perception_claim_ids': [claim['claim_id']],\n"
        "}))\n",
        encoding="utf-8",
    )


def test_runtime_obstacle_recovery_wires_camera_perception_claim_into_planner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(
        bridge,
        obstacle_avoidance_observed=True,
        camera_observation=True,
    )
    _write_perception_claim_citing_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "pending"
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["runtime_recovery_action_kind"] == "avoid_obstacle"
    assert summary["recovery_proposals"][0]["proposal_source"] == "deterministic_fallback"
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == "requires_human_approval"
    claims = summary["recovery_planner_result"]["perception_claims"]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["claim_kind"] == "corridor_blocked_by_object"
    assert claim["source_frame_ref"] == _FIXTURE_CAMERA_FRAME_REF
    assert claim["confidence"] == 0.82
    assert claim["corroborated_by"] == ["lidar_costmap:nav2_costmap_obstacle_observed"]
    assert claim["evidence_only"] is True
    assert claim["dispatch_authority_created"] is False
    assert claim["corroboration_binding"]["progressive_action_supported"] is False
    validated_proposal = summary["recovery_planner_result"]["guardrail"][
        "validated_proposal"
    ]
    assert validated_proposal == {}
    support = summary["recovery_planner_result"]["guardrail"][
        "perception_claim_support"
    ]
    assert support["selected_action_is_conservative"] is False
    assert support["checks"]["perception_claim_support_respected"] is False


def _write_classifying_perception_sidecar(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['task'] == "
        "'classify_camera_frame_for_recovery_perception_claim'\n"
        "assert 'image_base64' in prompt\n"
        "print(json.dumps({\n"
        "    'claim_kind': 'corridor_blocked_by_object',\n"
        "    'confidence': 0.82,\n"
        "    'horizontal_sector': 'center',\n"
        "    'target_center_x_normalized': 0.5,\n"
        "}))\n",
        encoding="utf-8",
    )


def test_camera_perception_pipeline_captures_classifies_and_feeds_planner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The full loop: bridge capture -> VLM sidecar -> claim -> LLM prompt.

    All under pseudo data (fixture-written PNG, fake sidecar). This proves
    regression wiring only; the fake sidecar cannot satisfy live-VLM binding.
    """

    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    sidecar = tmp_path / "perception_sidecar.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_perception_claim_citing_recovery_planner(planner)
    _write_classifying_perception_sidecar(sidecar)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(TURTLEBOT3_CAMERA_PERCEPTION_ENABLED_ENV, "1")
    monkeypatch.setenv(
        TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _bridge_command(sidecar)
    )
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert summary["status"] == "pending"
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["runtime_recovery_action_kind"] == "avoid_obstacle"
    assert summary["recovery_proposals"][0]["proposal_source"] == "deterministic_fallback"

    pipeline = execution["runtime_recovery_obstacle_scenario"][
        "camera_perception_pipeline"
    ]
    assert pipeline["schema_version"] == (
        TURTLEBOT3_CAMERA_PERCEPTION_PIPELINE_SCHEMA_VERSION
    )
    assert pipeline["pipeline_status"] == "classified"
    assert pipeline["claim_produced"] is True
    assert pipeline["capture"]["camera_frame_captured"] is True
    expected_frame_sha = sha256(_FIXTURE_CAMERA_FRAME_BYTES).hexdigest()
    assert pipeline["capture"]["camera_frame_sha256"] == expected_frame_sha
    assert pipeline["sidecar_status"] == "classified"
    assert pipeline["dispatch_authority_created"] is False
    assert pipeline["physical_execution_invoked"] is False

    claims = summary["recovery_planner_result"]["perception_claims"]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["claim_kind"] == "corridor_blocked_by_object"
    assert claim["source_frame_ref"] == f"sha256:{expected_frame_sha}"
    assert claim["corroborated_by"] == [
        "lidar_costmap:nav2_costmap_obstacle_observed",
        "range_sensor:ros2_laser_scan_angular_candidate",
    ]
    binding = claim["corroboration_binding"]
    assert binding["temporal_status"] == "bound"
    assert binding["vlm_temporal_status"] == "bound"
    assert binding["spatial_status"] == "bound"
    assert binding["target_identity_status"] == "bound"
    assert binding["live_vlm_invocation_observed"] is False
    assert binding["progressive_action_supported"] is False
    validated_proposal = summary["recovery_planner_result"]["guardrail"][
        "validated_proposal"
    ]
    assert validated_proposal == {}


def test_camera_perception_pipeline_disabled_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_CAMERA_PERCEPTION_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert summary["status"] == "pending"
    assert summary["recovery_dispatch_request_sent"] is False
    pipeline = execution["runtime_recovery_obstacle_scenario"][
        "camera_perception_pipeline"
    ]
    assert pipeline["pipeline_status"] == "not_enabled"
    assert pipeline["claim_produced"] is False
    assert summary["recovery_planner_result"]["perception_claims"] == []


def test_camera_perception_pipeline_fails_open_when_camera_topic_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A profile without a camera topic records timeout and proceeds safely."""

    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(
        bridge,
        obstacle_avoidance_observed=True,
        camera_capture_times_out=True,
    )
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(TURTLEBOT3_CAMERA_PERCEPTION_ENABLED_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert summary["status"] == "pending"
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["runtime_recovery_action_kind"] == "avoid_obstacle"
    pipeline = execution["runtime_recovery_obstacle_scenario"][
        "camera_perception_pipeline"
    ]
    assert pipeline["pipeline_status"] == "capture_blocked"
    assert pipeline["claim_produced"] is False
    assert pipeline["capture"]["camera_frame_captured"] is False
    assert "camera_frame_not_received_before_timeout" in (
        pipeline["capture"]["blocking_reasons"]
    )
    assert summary["recovery_planner_result"]["perception_claims"] == []


def test_turtlebot3_obstacle_recovery_env_values_cannot_mint_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL", "1")
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF",
        "operator_approval:test_fresh_recovery",
    )
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_ACTOR",
        "codex_e2e_operator",
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    checkpoint = result["turtlebot3_recovery_checkpoint"]
    assert proposal["autonomy_envelope"]["preapproved_recovery_actions"] == []
    assert set(
        proposal["autonomy_envelope"]["requires_human_approval_for"]
    ) >= {"avoid_obstacle", "return_home", "hold"}
    assert summary["status"] == "pending"
    assert summary["runtime_recovery_triggered"] is True
    assert summary["recovery_action_suggested"] == "avoid_obstacle"
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "requires_human_approval"
    )
    assert summary["recovery_proposal_classifications"][0][
        "execution_permitted_by_envelope"
    ] is False
    assert summary["recovery_proposal_classifications"][0][
        "requires_new_human_approval"
    ] is True
    assert summary["recovery_execution_permitted_by_operator_approval"] is False
    assert summary["recovery_dispatch_authority_source"] is None
    assert summary["fresh_recovery_operator_approval_count"] == 0
    assert summary["fresh_recovery_operator_approvals"] == []
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_completion_claimed"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert checkpoint["checkpoint_status"] == "awaiting_operator_approval"
    assert checkpoint["dispatch_authority_created"] is False


def test_turtlebot3_recovery_checkpoint_resumes_without_replaying_completed_segment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    monkeypatch.delenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF", raising=False
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]

    awaiting = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )

    checkpoint = awaiting["turtlebot3_recovery_checkpoint"]
    assert awaiting["summary"]["status"] == "pending"
    assert awaiting["summary"]["recovery_dispatch_request_sent"] is False
    assert awaiting["summary"]["recovery_goal_status"] == "not_dispatched"
    assert awaiting["summary"]["recovery_verification_status"] == "pending"
    assert awaiting["summary"]["route_resume_status"] == "not_resumed"
    assert checkpoint["schema_version"] == "turtlebot3_recovery_checkpoint.v1"
    assert checkpoint["checkpoint_status"] == "awaiting_operator_approval"
    assert checkpoint["selected_action"] == "avoid_obstacle"
    assert checkpoint["approved_parameters"]["target_x_m"] == pytest.approx(-1.15)
    assert checkpoint["approved_parameters"]["target_y_m"] == pytest.approx(-1.41)
    assert checkpoint["approved_parameters"]["target_yaw_rad"] == pytest.approx(0.0)
    assert checkpoint["approved_parameters"]["obstacle_avoidance_required"] is True
    assert checkpoint["recovery_candidate_binding"] == {
        "candidate_id": "obstacle_bypass_south",
        "path_sha256": None,
        "costmap_snapshot_hash": None,
        "recommended_arrival_yaw_rad": 0.0,
        "live_costmap_validated": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    assert checkpoint["completed_segment_count"] == 1
    assert checkpoint["next_segment_index"] == 2
    assert checkpoint["remaining_segment_count"] == 9
    assert checkpoint["dispatch_authority_created"] is False
    contract_bundle = checkpoint["recovery_contract_bundle"]
    assert contract_bundle["recovery_intent"]["strategy"] == "local_avoidance"
    assert contract_bundle["intent_compilation"]["meaning_preserved"] is True
    assert contract_bundle["dispatch_authority_created"] is False
    assert awaiting["summary"]["turtlebot3_recovery_checkpoint"] == checkpoint
    assert (
        awaiting["turtlebot3_home_mission_execution"][
            "turtlebot3_recovery_checkpoint"
        ]
        == checkpoint
    )

    recovery_partials: list[dict] = []
    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting,
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
        progress_callback=recovery_partials.append,
    )

    summary = resumed["summary"]
    consumed = resumed["turtlebot3_recovery_checkpoint"]
    calls_path = bridge.with_suffix(".calls.jsonl")
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    first_segment_label = proposal["planned_segments"][0]["label"]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert summary["route_resumed_after_recovery"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["recovery_goal_status"] == "succeeded"
    assert summary["recovery_goal_succeeded_observed"] is True
    assert summary["recovery_verification_status"] == "verified"
    assert summary["route_resume_status"] == "resumed"
    resumed_partial = next(
        partial["summary"]
        for partial in recovery_partials
        if partial["summary"].get("route_resume_status") == "resumed"
    )
    assert resumed_partial["recovery_dispatch_request_sent"] is True
    assert resumed_partial["recovery_completion_claimed"] is True
    assert resumed_partial["odom_delta_m"] > summary["runtime_recovery_motion_context"][
        "odom_delta_m"
    ]
    map_recovery = summary["turtlebot3_indoor_map_model"]["recovery"]
    assert map_recovery["goal_status"] == "succeeded"
    assert map_recovery["goal_succeeded_observed"] is True
    assert map_recovery["verification_status"] == "verified"
    assert map_recovery["route_resume_status"] == "resumed"
    assert summary["segment_completion_count"] == summary["planned_segment_count"]
    assert consumed["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert consumed["checkpoint_hash"] == checkpoint["checkpoint_hash"]
    assert consumed["checkpoint_status"] == "consumed"
    assert consumed["claimed_by_approval_ref"] == (
        "operator_approval:test_interactive_recovery"
    )
    assert consumed["consumed_by_approval_ref"] == (
        "operator_approval:test_interactive_recovery"
    )
    assert consumed["consumed_at"] > consumed["claimed_at"]
    assert _recovery_checkpoint_hash(consumed) == consumed["checkpoint_hash"]
    outcome_verification = resumed["turtlebot3_home_mission_execution"][
        "recovery_closed_loop_cycles"
    ][0]["outcome_verification"]
    assert outcome_verification["verification_status"] == "verified"
    assert outcome_verification["authority_bound"] is True
    assert outcome_verification["recovery_success_verified"] is True
    assert outcome_verification["delivery_completion_claimed"] is False
    assert calls[0]["label"] == first_segment_label
    assert calls[1]["label"] == "runtime_recovery_avoid_obstacle_waypoint"
    assert sum(call["label"] == first_segment_label for call in calls) == 1
    assert len(calls) == len(proposal["planned_segments"]) + 1


def test_failed_recovery_history_remains_visible_on_repair_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal

    def fail_recovery(**kwargs):
        result = original_dispatch(**kwargs)
        if "recovery_avoid_obstacle" in kwargs["action_ref_suffix"]:
            result["completion_claimed"] = False
            result["blocking_reasons"] = ["nav2_recovery_orbit_detected"]
        return result

    repair_candidate = {
        "candidate_id": "repair-alternative",
        "x_m": -1.0,
        "y_m": -1.2,
        "yaw_rad": 0.0,
        "path_sha256": "repair-path",
    }
    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        fail_recovery,
    )
    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_resolve_recovery_candidate",
        lambda *_args, **_kwargs: {
            "resolution_status": "validated",
            "selected_candidate": repair_candidate,
            "selected_sequence": [repair_candidate],
            "live_costmap_validated": True,
            "dual_costmap_validated": True,
            "global_costmap_snapshot_hash": "global",
            "local_costmap_snapshot_hash": "local",
        },
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting,
        recovery_operator_approval=_recovery_operator_approval(
            awaiting["turtlebot3_recovery_checkpoint"]
        ),
    )

    execution = result["turtlebot3_home_mission_execution"]
    history = execution["recovery_attempt_history"]
    assert history
    assert history[-1]["blocking_reasons"] == [
        "nav2_recovery_orbit_detected"
    ]
    indoor_map = execution["turtlebot3_indoor_map_model"]
    recovery_points = indoor_map["recovery"]["observed_points"]
    assert recovery_points
    assert indoor_map["current_pose"] == recovery_points[-1]
    checkpoint = result["turtlebot3_recovery_checkpoint"]
    assert checkpoint["checkpoint_status"] == "awaiting_operator_approval"
    assert checkpoint["requires_new_human_approval"] is True
    assert result["summary"]["status"] == "pending"


def test_failed_approved_recovery_gets_fresh_ask_human_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = awaiting["turtlebot3_recovery_checkpoint"]
    planner = tmp_path / "ask_human_after_recovery_failure.py"
    _write_ask_human_approved_recovery_failure_planner(planner)
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(planner),
    )
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal

    def fail_recovery(**kwargs):
        result = original_dispatch(**kwargs)
        if "recovery_avoid_obstacle" in kwargs["action_ref_suffix"]:
            result["completion_claimed"] = False
            result["blocking_reasons"] = ["nav2_goal_result_not_succeeded"]
        return result

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        fail_recovery,
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting,
        recovery_operator_approval=_recovery_operator_approval(parent),
    )

    child = result["turtlebot3_recovery_checkpoint"]
    execution = result["turtlebot3_home_mission_execution"]
    assert result["summary"]["status"] == "pending"
    assert child["checkpoint_status"] == "awaiting_operator_approval"
    assert child["checkpoint_id"] != parent["checkpoint_id"]
    assert child["parent_checkpoint_id"] == parent["checkpoint_id"]
    assert child["selected_action"] == "ask_human"
    assert child["operator_guidance_required"] is True
    assert child["requires_new_human_approval"] is True
    assert child["automatic_redispatch_performed"] is False
    assert execution["recovery_proposals"][0]["selected_action"] == "ask_human"
    assert execution["recovery_planner_result"]["proposal_source"] == "llm"
    assert execution["recovery_closed_loop_cycles"][0][
        "reobservation_status"
    ] == "failed"
    assert execution["recovery_closed_loop_cycles"][0][
        "checkpoint_id"
    ] == parent["checkpoint_id"]


@pytest.mark.parametrize(
    ("instruction", "direction"),
    [
        ("右に大きく回って障害物を避けて", "right"),
        ("右に回避して", "right"),
        ("Take a wide turn around it on the right", "right"),
    ],
)
def test_turtlebot3_recovery_revision_builds_source_bound_directional_waypoints(
    tmp_path: Path,
    monkeypatch,
    instruction: str,
    direction: str,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = awaiting["turtlebot3_recovery_checkpoint"]

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction=instruction,
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "proposed"
    assert revision["blocking_reasons"] == []
    assert revision["operator_approval_created"] is False
    assert revision["dispatch_authority_created"] is False
    child = revision["turtlebot3_recovery_checkpoint"]
    assert child["checkpoint_status"] == "awaiting_operator_approval"
    assert child["checkpoint_id"] != parent["checkpoint_id"]
    assert child["parent_checkpoint_id"] == parent["checkpoint_id"]
    assert child["parent_checkpoint_hash"] == parent["checkpoint_hash"]
    assert revision["parent_recovery_context"]["recovery_proposals"][0][
        "proposal_id"
    ] == parent["recovery_proposal_id"]
    assert child["selected_action"] == "avoid_obstacle"
    assert len(child["recovery_goal_poses"]) == 2
    assert len(child["approved_parameters"]["recovery_waypoints"]) == 2
    geometry = child["recovery_revision_geometry"]
    assert geometry["requested_direction"] == direction
    assert geometry["direction_reference"] == (
        "planned_travel_direction_a_to_b_in_map_frame"
    )
    assert geometry["wide_bbox_clearance_m"] == 0.55
    assert geometry["floor_plan_id"] == "turtlebot3_simulated_home_loop.v1"
    assert len(geometry["floor_plan_geometry_sha256"]) == 64
    assert geometry["path_feasibility_claimed"] is False
    assert len(child["planned_segments_sha256"]) == 64
    assert _recovery_checkpoint_hash(child) == child["checkpoint_hash"]
    superseded = revision["superseded_checkpoint"]
    assert superseded["checkpoint_status"] == "superseded"
    assert superseded["superseded_by_checkpoint_id"] == child["checkpoint_id"]
    assert superseded["superseded_by_checkpoint_hash"] == child["checkpoint_hash"]
    assert _recovery_checkpoint_hash(superseded) == parent["checkpoint_hash"]
    assert revision["summary"]["recovery_dispatch_request_sent"] is False
    assert revision["summary"]["physical_execution_invoked"] is False


@pytest.mark.parametrize(
    "instruction",
    [
        "左に大きく旋回してかわして",
        "左を回って",
        "Go around it via the left side",
    ],
)
def test_turtlebot3_recovery_revision_blocks_direction_with_static_collision(
    tmp_path: Path,
    monkeypatch,
    instruction: str,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = awaiting["turtlebot3_recovery_checkpoint"]

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction=instruction,
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "blocked"
    assert len(revision["blocking_reasons"]) == 1
    assert revision["blocking_reasons"][0] in {
        "operator_recovery_revision_detour_distance_exceeds_bound",
        "operator_recovery_revision_waypoint_static_collision_clearance_insufficient",
    }
    assert revision["parent_checkpoint_id"] == parent["checkpoint_id"]
    assert revision["superseded_checkpoint"] == {}
    assert revision["turtlebot3_recovery_checkpoint"] == {}
    assert revision["operator_approval_created"] is False
    assert revision["dispatch_authority_created"] is False


def test_turtlebot3_recovery_revision_binds_dual_costmap_plan_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV,
        "1",
    )

    def evaluate(self, *, candidates, obstacle, frame_id="map"):
        del self, obstacle, frame_id
        evaluated = [
            {
                **candidate,
                "path_valid": True,
                "planner_status": "succeeded",
                "maximum_path_cost": 10,
                "local_current_cost": 20,
                "local_maximum_path_cost": 40,
                "path_length_m": 1.0 + index,
                "path_sha256": f"revision-path-{index}",
            }
            for index, candidate in enumerate(candidates)
        ]
        return {
            "evaluation_status": "validated",
            "selected_candidate": evaluated[0],
            "candidate_evaluations": evaluated,
            "costmap_snapshot_hash": "global-hash",
            "global_costmap_snapshot_hash": "global-hash",
            "local_costmap_snapshot_hash": "local-hash",
            "global_costmap_source": "/global_costmap/get_costmap",
            "local_costmap_source": "/local_costmap/get_costmap",
            "compute_path_action": "/compute_path_to_pose",
            "dispatch_request_sent": False,
        }

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime.Ros2Nav2BridgeCommandClient,
        "evaluate_recovery_candidates",
        evaluate,
    )

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="障害物を大きく右に回って避けて",
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "proposed"
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    binding = checkpoint["recovery_candidate_binding"]
    assert binding["dual_costmap_validated"] is True
    assert binding["global_costmap_snapshot_hash"] == "global-hash"
    assert binding["local_costmap_snapshot_hash"] == "local-hash"
    assert binding["path_sha256_sequence"] == [
        "revision-path-0",
        "revision-path-1",
    ]
    assert revision["summary"]["recovery_candidate_resolution"][
        "dual_costmap_validated"
    ] is True
    assert revision["recovery_planner_result"]["proposal_source"] == "operator"
    assert revision["dispatch_authority_created"] is False


def test_turtlebot3_recovery_revision_uses_latest_reobservation_and_live_clearance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    execution = awaiting["turtlebot3_home_mission_execution"]
    execution["approved_recovery_segment_results"] = [
        {
            "segment_ref": "recovery_avoid_obstacle_waypoint_2",
            "completion_claimed": True,
            "bridge_responses": [
                {
                    "trajectory_result": {
                        "trajectory_samples": [
                            {
                                "x_m": -1.48,
                                "y_m": 0.49,
                                "frame_id": "map",
                                "sample_index": 42,
                            }
                        ]
                    }
                }
            ],
        }
    ]
    monkeypatch.setenv(
        turtlebot3_home_mission_runtime.TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_ENV,
        "1.10",
    )

    geometry, reasons = (
        turtlebot3_home_mission_runtime._build_directional_recovery_revision_geometry(
            direction="left",
            proposal=proposal,
            execution=execution,
            checkpoint=awaiting["turtlebot3_recovery_checkpoint"],
        )
    )

    assert reasons == []
    assert geometry["anchor_pose"] == {
        "x_m": -1.48,
        "y_m": 0.49,
        "source": "latest_approved_recovery_raw_map_frame_observation",
        "segment_ref": "recovery_avoid_obstacle_waypoint_2",
        "sample_index": 42,
        "frame_id": "map",
        "completion_claimed": True,
    }
    assert geometry["route_anchor_pose"]["source"] == (
        "last_completed_nav2_segment_goal"
    )
    assert geometry["geometry_strategy"] == (
        "reobserved_anchor_via_side_shoulder_to_route_rejoin"
    )
    assert geometry["wide_bbox_clearance_m"] == 0.55
    assert geometry["computed_detour_distance_m"] < 3.5
    assert [
        (item["x_m"], item["y_m"])
        for item in geometry["recovery_goal_poses"]
    ] == [
        (-1.114466, 0.403995),
        (0.178162, -0.457757),
    ]


@pytest.mark.parametrize(
    "instruction",
    [
        "高度を高く取って上を通って",
        "左に避けながら高度を上げて",
        "climb over it on the right",
    ],
)
def test_turtlebot3_recovery_revision_rejects_altitude_without_superseding(
    tmp_path: Path,
    monkeypatch,
    instruction: str,
) -> None:
    proposal, _approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = json.loads(json.dumps(awaiting["turtlebot3_recovery_checkpoint"]))
    calls_before = bridge.with_suffix(".calls.jsonl").read_text().splitlines()

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction=instruction,
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "unsupported"
    assert revision["blocking_reasons"] == [
        "operator_recovery_revision_unsupported_for_ground_robot"
    ]
    assert revision["superseded_checkpoint"] == {}
    assert revision["turtlebot3_recovery_checkpoint"] == {}
    assert revision["turtlebot3_home_mission_execution"] == {}
    assert revision["operator_approval_created"] is False
    assert revision["dispatch_authority_created"] is False
    assert awaiting["turtlebot3_recovery_checkpoint"] == parent
    assert bridge.with_suffix(".calls.jsonl").read_text().splitlines() == calls_before


@pytest.mark.parametrize(
    "instruction",
    [
        "左には曲がらないで",
        "右へ行かないで",
        "引き返さないで",
        "Don't turn left around it",
        "Do not go right",
        "Don't return home",
        "Never turn back",
        "左以外を通って",
        "without turning left",
        "帰還不要",
        "avoid return home",
        "左を通らずに回避して",
        "左を通るのは避けて",
        "右に行く案は却下",
        "帰還は却下",
        "帰還は禁止",
        "return home is forbidden",
        "anything but return home",
        "do anything except return home",
        "go left, actually no",
        "左に曲がると危険",
        "turn left is dangerous",
        "左に大きく旋回すると危険",
        "右に迂回する案は不採用",
        "左に旋回する案を拒否",
        "右に行くのはダメ",
        "turn left is rejected",
        "go right is unsafe",
        "reject turning left",
        "cancel going right",
        "左に旋回する案は無し",
        "右に進むのはNG",
        "左に旋回は不可",
        "return home? absolutely not",
        "return home? no way",
        "go left? absolutely not",
    ],
)
def test_turtlebot3_recovery_revision_rejects_negated_intent_without_child(
    tmp_path: Path,
    monkeypatch,
    instruction: str,
) -> None:
    proposal, _approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = json.loads(json.dumps(awaiting["turtlebot3_recovery_checkpoint"]))
    execution_before = json.loads(
        json.dumps(awaiting["turtlebot3_home_mission_execution"])
    )
    calls_before = bridge.with_suffix(".calls.jsonl").read_text().splitlines()

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction=instruction,
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "unsupported"
    assert revision["blocking_reasons"] == [
        "operator_recovery_revision_negated_intent_not_executable"
    ]
    assert revision["superseded_checkpoint"] == {}
    assert revision["turtlebot3_recovery_checkpoint"] == {}
    assert revision["turtlebot3_home_mission_execution"] == {}
    assert revision["recovery_proposal"] == {}
    assert revision["recovery_proposal_classification"] == {}
    assert revision["operator_approval_created"] is False
    assert revision["dispatch_authority_created"] is False
    assert awaiting["turtlebot3_recovery_checkpoint"] == parent
    assert awaiting["turtlebot3_home_mission_execution"] == execution_before
    assert bridge.with_suffix(".calls.jsonl").read_text().splitlines() == calls_before


@pytest.mark.parametrize(
    "instruction",
    [
        "左側に障害物がある",
        "左は危険です",
        "左にある障害物を避けて",
        "avoid the obstacle on the left",
        "go around the obstacle on the left",
        "左に行けますか？",
        "左に旋回する案を見て",
        "we should return home",
    ],
)
def test_turtlebot3_recovery_revision_rejects_side_observation_without_motion(
    tmp_path: Path,
    monkeypatch,
    instruction: str,
) -> None:
    proposal, _approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = json.loads(json.dumps(awaiting["turtlebot3_recovery_checkpoint"]))
    calls_before = bridge.with_suffix(".calls.jsonl").read_text().splitlines()

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction=instruction,
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "unsupported"
    assert revision["blocking_reasons"] == [
        "operator_recovery_revision_intent_not_supported"
    ]
    assert revision["superseded_checkpoint"] == {}
    assert revision["turtlebot3_recovery_checkpoint"] == {}
    assert revision["dispatch_authority_created"] is False
    assert awaiting["turtlebot3_recovery_checkpoint"] == parent
    assert bridge.with_suffix(".calls.jsonl").read_text().splitlines() == calls_before


def test_turtlebot3_recovery_revision_builds_fresh_approval_return_home_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = awaiting["turtlebot3_recovery_checkpoint"]

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="その案ではなく、出発地点へ引き返して",
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "proposed"
    child = revision["turtlebot3_recovery_checkpoint"]
    assert child["selected_action"] == "return_home"
    assert len(child["recovery_goal_poses"]) == 1
    assert child["recovery_goal_poses"][0]["label"] == "simulated_home_origin"
    assert child["approved_parameters"] == {
        "target_x_m": -2.0,
        "target_y_m": -0.5,
        "return_home_required": True,
    }
    classification = revision["recovery_proposal_classification"]
    assert classification["execution_class"] == "requires_human_approval"
    assert classification["requires_new_human_approval"] is True
    assert classification["execution_permitted_by_envelope"] is False
    assert child["parent_checkpoint_id"] == parent["checkpoint_id"]
    assert revision["summary"]["route_resumed_after_recovery"] is False
    assert revision["summary"]["mission_delivery_completion_claimed"] is False


def test_turtlebot3_recovery_revision_can_explicitly_return_home_then_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    parent = awaiting["turtlebot3_recovery_checkpoint"]

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="出発地点に一旦戻ってから配送を再開して",
        proposal=proposal,
        resume_execution=awaiting,
    )

    assert revision["revision_status"] == "proposed"
    child = revision["turtlebot3_recovery_checkpoint"]
    assert child["selected_action"] == "return_home"
    assert child["revision_intent"] == "return_home_then_resume"
    assert child["approved_parameters"] == {
        "target_x_m": -2.0,
        "target_y_m": -0.5,
        "return_home_required": True,
        "resume_route_after_recovery": True,
    }
    assert child["parent_checkpoint_id"] == parent["checkpoint_id"]
    assert child["checkpoint_id"] != parent["checkpoint_id"]
    assert revision["operator_approval_created"] is False
    assert revision["dispatch_authority_created"] is False


def test_turtlebot3_recovery_revision_can_retry_exact_failed_segment_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revised_source = json.loads(json.dumps(awaiting))
    execution = revised_source["turtlebot3_home_mission_execution"]
    failure = {
        "segment_ref": "segment_2",
        "completion_claimed": False,
        "dispatch_request_sent": True,
        "blocking_reasons": ["fixture_transient_cancel"],
        "bridge_responses": [],
    }
    execution["route_failure_observation_results"] = [failure]
    checkpoint = revised_source["turtlebot3_recovery_checkpoint"]
    checkpoint["resume_state_hash"] = (
        turtlebot3_home_mission_runtime._recovery_resume_state_hash(execution)
    )
    checkpoint["checkpoint_hash"] = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint['checkpoint_hash'][:12]}"
    )
    execution["turtlebot3_recovery_checkpoint"] = dict(checkpoint)

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="停止を確認した。同じ配送区間を一度だけ再試行して",
        proposal=proposal,
        resume_execution=revised_source,
    )

    assert revision["revision_status"] == "proposed"
    child = revision["turtlebot3_recovery_checkpoint"]
    assert child["selected_action"] == "reroute"
    assert child["revision_intent"] == "retry_failed_segment"
    assert child["approved_parameters"] == {
        "target_x_m": proposal["planned_segments"][1]["x_m"],
        "target_y_m": proposal["planned_segments"][1]["y_m"],
        "retry_failed_segment_required": True,
        "retry_count": 1,
    }
    assert len(child["recovery_goal_poses"]) == 1
    assert child["recovery_revision_geometry"]["failed_segment_ref"] == "segment_2"
    assert revision["operator_approval_created"] is False
    assert revision["dispatch_authority_created"] is False


def test_turtlebot3_directional_recovery_revision_dispatches_both_waypoints_before_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    first_segment_label = proposal["planned_segments"][0]["label"]
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく旋回してかわして",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal

    def dispatch_with_raw_map_frame_recovery_samples(**kwargs):
        result = original_dispatch(**kwargs)
        if kwargs["goal"].label.startswith("operator_revision_right_wide"):
            for response in result["bridge_responses"]:
                for container_key in ("state_result", "progress_result"):
                    container = response.get(container_key) or {}
                    trajectory = container.get("trajectory_result") or {}
                    for sample in trajectory.get("trajectory_samples") or []:
                        sample["frame_id"] = "map"
        return result

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        dispatch_with_raw_map_frame_recovery_samples,
    )

    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    calls = [
        json.loads(line)
        for line in bridge.with_suffix(".calls.jsonl").read_text().splitlines()
    ]
    recovery_labels = [goal["label"] for goal in checkpoint["recovery_goal_poses"]]
    assert calls[1]["label"] == recovery_labels[0]
    assert calls[2]["label"] == recovery_labels[1]
    assert sum(call["label"] == first_segment_label for call in calls) == 1
    assert len(calls) == len(proposal["planned_segments"]) + 2
    execution = resumed["turtlebot3_home_mission_execution"]
    approved_results = execution["approved_recovery_segment_results"]
    assert [item["goal_pose"]["label"] for item in approved_results] == recovery_labels
    assert all(item["completion_claimed"] is True for item in approved_results)
    assert execution["recovery_goal_sequence_completed"] is True
    assert execution["recovery_completion_claimed"] is True
    assert execution["route_resumed_after_recovery"] is True
    assert execution["turtlebot3_recovery_checkpoint"]["checkpoint_status"] == (
        "consumed"
    )
    side_observation = execution["recovery_requested_side_observation"]
    assert side_observation["requested_side_observed"] is True
    assert side_observation["approved_recovery_segment_count"] == 2
    assert len(side_observation["segment_observations"]) == 2
    assert side_observation["display_alignment_used"] is False
    map_recovery = execution["turtlebot3_indoor_map_model"]["recovery"]
    assert [item["label"] for item in map_recovery["approved_targets"]] == (
        recovery_labels
    )
    assert {
        point["segment_ref"] for point in map_recovery["observed_points"]
    } >= {
        "recovery_avoid_obstacle_waypoint_1",
        "recovery_avoid_obstacle_waypoint_2",
    }


def test_turtlebot3_obstacle_detected_then_request_enables_runtime_recovery() -> None:
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送してください。障害物を検出したら"
            "Recovery Agentが回避案を提案し、人間の承認後に再開してください。"
        ),
    )

    scenario = plan["scenario_proposal"]["obstacle_scenario"]
    assert scenario["obstacle_challenge_requested"] is True
    assert scenario["runtime_obstacle_recovery_requested"] is True
    assert scenario["runtime_obstacle_recovery_trigger_after_segment_index"] == 1


def test_turtlebot3_return_home_revision_consumes_checkpoint_without_resuming_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="出発地点へ引き返して",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]

    returned = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    calls = [
        json.loads(line)
        for line in bridge.with_suffix(".calls.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 2
    assert calls[1]["label"] == "simulated_home_origin"
    assert all(
        call["label"] != proposal["planned_segments"][1]["label"] for call in calls
    )
    summary = returned["summary"]
    execution = returned["turtlebot3_home_mission_execution"]
    assert summary["status"] == "recovered"
    assert summary["recovery_completion_claimed"] is True
    assert summary["route_resumed_after_recovery"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["completion_claimed"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["segment_completion_count"] == 1
    assert execution["turtlebot3_recovery_checkpoint"]["checkpoint_status"] == (
        "consumed"
    )
    assert len(execution["approved_recovery_segment_results"]) == 1
    current_pose = execution["turtlebot3_indoor_map_model"]["current_pose"]
    assert current_pose["x_m"] == pytest.approx(-2.0)
    assert current_pose["y_m"] == pytest.approx(-0.5)
    assert current_pose["segment_ref"] == "recovery_return_home"


def test_turtlebot3_explicit_return_home_then_resume_reaches_delivery_goal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="出発地点に一旦戻ってから配送を再開して",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    original_geometry = (
        turtlebot3_home_mission_runtime._obstacle_trajectory_geometry
    )

    def verified_post_return_route_geometry(**kwargs):
        return {
            **original_geometry(**kwargs),
            "obstacle_trajectory_clearance_observed": True,
            "obstacle_trajectory_intersects_obstacle": False,
            "obstacle_intersection_point_count": 0,
            "obstacle_intersection_segment_count": 0,
        }

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_obstacle_trajectory_geometry",
        verified_post_return_route_geometry,
    )

    completed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    calls = [
        json.loads(line)
        for line in bridge.with_suffix(".calls.jsonl").read_text().splitlines()
    ]
    assert calls[1]["label"] == "simulated_home_origin"
    assert [call["label"] for call in calls[2:]] == [
        segment["label"] for segment in proposal["planned_segments"][1:]
    ]
    summary = completed["summary"]
    assert summary["status"] == "completed", json.dumps(summary, indent=2)
    assert summary["route_resumed_after_recovery"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["completion_claimed"] is True
    assert summary["segment_completion_count"] == len(
        proposal["planned_segments"]
    )
    assert summary["mission_delivery_completion_claimed"] is False


def test_turtlebot3_requested_side_observation_requires_abreast_trajectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく避けて",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    geometry = checkpoint["recovery_revision_geometry"]
    obstacle = geometry["obstacle"]
    direction = geometry["route_direction_unit"]
    left_normal = geometry["left_normal_unit"]
    parallel_support = (
        abs(direction["x"]) * obstacle["size_x_m"] / 2.0
        + abs(direction["y"]) * obstacle["size_y_m"] / 2.0
    )
    requested_offset = (
        abs(left_normal["x"]) * obstacle["size_x_m"] / 2.0
        + abs(left_normal["y"]) * obstacle["size_y_m"] / 2.0
        + geometry["wide_bbox_clearance_m"]
    )

    def point(longitudinal: float, lateral: float) -> dict[str, float | str]:
        return {
            "x_m": obstacle["x_m"]
            + longitudinal * direction["x"]
            + lateral * left_normal["x"],
            "y_m": obstacle["y_m"]
            + longitudinal * direction["y"]
            + lateral * left_normal["y"],
            "frame_id": "map",
        }

    # The path crosses the obstacle's longitudinal window on the opposite
    # (left) side, then reaches the requested right side only after it has
    # already passed the obstacle. A global max-offset check would false-pass.
    samples = [
        point(-(parallel_support + 0.3), requested_offset),
        point(parallel_support + 0.3, requested_offset),
        point(parallel_support + 0.6, -(requested_offset + 0.1)),
    ]
    later_requested_side_samples = [
        point(-(parallel_support + 0.3), -(requested_offset + 0.1)),
        point(parallel_support + 0.3, -(requested_offset + 0.1)),
    ]
    observation = (
        turtlebot3_home_mission_runtime._recovery_requested_side_observation(
            checkpoint=checkpoint,
            approved_recovery_results=[
                {
                    "segment_ref": "adversarial_opposite_side_path",
                    "bridge_responses": [
                        {"trajectory_result": {"trajectory_samples": samples}}
                    ],
                },
                {
                    "segment_ref": "later_requested_side_path",
                    "bridge_responses": [
                        {
                            "trajectory_result": {
                                "trajectory_samples": later_requested_side_samples
                            }
                        }
                    ],
                },
            ],
        )
    )

    assert observation["requested_direction"] == "right"
    assert observation["raw_map_frame_sample_count"] == 5
    assert len(observation["segment_observations"]) == 2
    assert observation["segment_observations"][1]["requested_side_observed"] is True
    assert observation["requested_side_observed"] is False
    assert observation["observation_status"] == "not_observed"
    assert observation["display_alignment_used"] is False


def test_turtlebot3_requested_side_observation_rejects_non_finite_samples(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく避けて",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]

    observation = (
        turtlebot3_home_mission_runtime._recovery_requested_side_observation(
            checkpoint=checkpoint,
            approved_recovery_results=[
                {
                    "segment_ref": "non_finite_map_samples",
                    "bridge_responses": [
                        {
                            "trajectory_result": {
                                "trajectory_samples": [
                                    {
                                        "x_m": -1.0,
                                        "y_m": float("inf"),
                                        "frame_id": "map",
                                        "sample_index": 0,
                                    },
                                    {
                                        "x_m": 1.0,
                                        "y_m": float("inf"),
                                        "frame_id": "map",
                                        "sample_index": 1,
                                    },
                                ]
                            }
                        }
                    ],
                }
            ],
        )
    )

    assert observation["raw_map_frame_sample_count"] == 0
    assert observation["requested_side_observed"] is False
    assert observation["observation_status"] == (
        "raw_map_frame_trajectory_unavailable"
    )


def test_turtlebot3_directional_revision_does_not_claim_mission_without_raw_side_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
        trajectory_frame_id=None,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく旋回してかわして",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]

    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    summary = resumed["summary"]
    side_observation = summary["recovery_requested_side_observation"]
    calls = [
        json.loads(line)
        for line in bridge.with_suffix(".calls.jsonl").read_text().splitlines()
    ]
    assert side_observation["requested_side_observed"] is False
    assert side_observation["display_alignment_used"] is False
    assert len(calls) == 3
    assert [call["label"] for call in calls[1:]] == [
        goal["label"] for goal in checkpoint["recovery_goal_poses"]
    ]
    assert summary["status"] == "pending"
    assert summary["recovery_goal_sequence_completed"] is True
    assert summary["recovery_completion_claimed"] is False
    assert summary["route_resumed_after_recovery"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert summary["completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    followup_checkpoint = resumed["turtlebot3_recovery_checkpoint"]
    assert followup_checkpoint["checkpoint_status"] == "awaiting_operator_approval"
    assert followup_checkpoint["parent_checkpoint_id"] == checkpoint["checkpoint_id"]
    failed_checkpoint = resumed["turtlebot3_recovery_repair_parent_checkpoint"]
    assert failed_checkpoint["checkpoint_status"] == "failed"
    assert failed_checkpoint["failure_reasons"] == [
        "requested_recovery_side_not_observed_in_raw_map_frame"
    ]
    map_recovery = resumed["turtlebot3_indoor_map_model"]["recovery"]
    assert map_recovery["goal_sequence_completed"] is True
    assert map_recovery["completion_claimed"] is False
    assert followup_checkpoint["automatic_redispatch_performed"] is False


def test_turtlebot3_directional_revision_rejects_path_samples_as_runtime_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
        sample_collection="path_samples",
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく旋回してかわして",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]

    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    calls = bridge.with_suffix(".calls.jsonl").read_text().splitlines()
    summary = resumed["summary"]
    side_observation = summary["recovery_requested_side_observation"]
    assert len(calls) == 3
    assert side_observation["raw_map_frame_sample_count"] == 0
    assert side_observation["path_sample_count_excluded"] > 0
    assert side_observation["requested_side_observed"] is False
    assert side_observation["observation_status"] == (
        "raw_map_frame_trajectory_unavailable"
    )
    assert summary["obstacle_trajectory_raw_map_frame_sample_count"] == 0
    assert summary["obstacle_trajectory_path_sample_count_excluded"] > 0
    assert summary["obstacle_trajectory_clearance_observed"] is False
    assert summary["recovery_goal_sequence_completed"] is True
    assert summary["recovery_completion_claimed"] is False
    assert summary["route_resumed_after_recovery"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["completion_claimed"] is False
    followup_checkpoint = resumed["turtlebot3_recovery_checkpoint"]
    assert followup_checkpoint["checkpoint_status"] == "awaiting_operator_approval"
    assert followup_checkpoint["parent_checkpoint_id"] == checkpoint["checkpoint_id"]
    failed_checkpoint = resumed["turtlebot3_recovery_repair_parent_checkpoint"]
    assert failed_checkpoint["checkpoint_status"] == "failed"
    assert failed_checkpoint["failure_reasons"] == [
        "requested_recovery_side_not_observed_in_raw_map_frame"
    ]


@pytest.mark.parametrize("splice_mode", ["collection", "response", "container"])
def test_turtlebot3_directional_revision_does_not_splice_observed_streams(
    tmp_path: Path,
    monkeypatch,
    splice_mode: str,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
        sample_collection="path_samples",
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく旋回してかわして",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    geometry = checkpoint["recovery_revision_geometry"]
    obstacle = geometry["obstacle"]
    direction = geometry["route_direction_unit"]
    left_normal = geometry["left_normal_unit"]
    parallel_support = (
        abs(direction["x"]) * obstacle["size_x_m"] / 2.0
        + abs(direction["y"]) * obstacle["size_y_m"] / 2.0
    )
    requested_offset = -(
        abs(left_normal["x"]) * obstacle["size_x_m"] / 2.0
        + abs(left_normal["y"]) * obstacle["size_y_m"] / 2.0
        + geometry["wide_bbox_clearance_m"]
        + 0.1
    )

    def point(longitudinal: float) -> dict[str, float | str | int]:
        return {
            "x_m": obstacle["x_m"]
            + longitudinal * direction["x"]
            + requested_offset * left_normal["x"],
            "y_m": obstacle["y_m"]
            + longitudinal * direction["y"]
            + requested_offset * left_normal["y"],
            "frame_id": "map",
            "sample_index": 0,
        }

    before = point(-(parallel_support + 0.3))
    after = point(parallel_support + 0.3)
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal

    def dispatch_with_split_observed_collections(**kwargs):
        result = original_dispatch(**kwargs)
        if kwargs["goal"].label.startswith("operator_revision_right_wide"):
            if splice_mode == "collection":
                result["bridge_responses"] = [
                    {
                        "trajectory_result": {
                            "trajectory_samples": [dict(before)],
                            "pose_samples": [dict(after)],
                        }
                    }
                ]
            elif splice_mode == "response":
                result["bridge_responses"] = [
                    {
                        "trajectory_result": {
                            "trajectory_samples": [dict(before)]
                        }
                    },
                    {
                        "trajectory_result": {
                            "trajectory_samples": [dict(after)]
                        }
                    },
                ]
            else:
                result["bridge_responses"] = [
                    {
                        "state_result": {"trajectory_samples": [dict(before)]},
                        "progress_result": {"trajectory_samples": [dict(after)]},
                    }
                ]
        return result

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        dispatch_with_split_observed_collections,
    )

    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    summary = resumed["summary"]
    side_observation = summary["recovery_requested_side_observation"]
    assert len(bridge.with_suffix(".calls.jsonl").read_text().splitlines()) == 3
    assert side_observation["raw_map_frame_sample_count"] > 0
    assert side_observation["observed_trajectory_stream_count"] >= 2
    assert side_observation["requested_side_observed"] is False
    assert all(
        item["full_longitudinal_crossing_observed"] is False
        for item in side_observation["segment_observations"]
    )
    assert summary["obstacle_trajectory_observed_stream_count"] >= 2
    assert summary["obstacle_trajectory_observed_segment_count"] == 0
    assert summary["obstacle_trajectory_geometry_status"] == (
        "raw_map_frame_trajectory_insufficient"
    )
    assert summary["obstacle_trajectory_clearance_observed"] is False
    assert summary["recovery_goal_sequence_completed"] is True
    assert summary["recovery_completion_claimed"] is False
    assert summary["route_resumed_after_recovery"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["completion_claimed"] is False


def test_turtlebot3_recovery_revision_fails_closed_without_source_geometry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, _approval, awaiting, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    malformed = json.loads(json.dumps(awaiting))
    execution = malformed["turtlebot3_home_mission_execution"]
    scenario = execution["runtime_recovery_obstacle_scenario"]
    scenario.pop("runtime_obstacle_size_x_m", None)
    scenario.pop("runtime_obstacle_size_y_m", None)
    execution["turtlebot3_indoor_map_model"]["obstacles"] = []
    checkpoint = malformed["turtlebot3_recovery_checkpoint"]
    checkpoint["resume_state_hash"] = turtlebot3_home_mission_runtime._recovery_resume_state_hash(
        execution
    )
    checkpoint["checkpoint_hash"] = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint['checkpoint_hash'][:12]}"
    )
    execution["turtlebot3_recovery_checkpoint"] = dict(checkpoint)

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく避けて",
        proposal=proposal,
        resume_execution=malformed,
    )

    assert revision["revision_status"] == "blocked"
    assert "operator_recovery_revision_source_obstacle_geometry_missing" in revision[
        "blocking_reasons"
    ]
    assert revision["turtlebot3_recovery_checkpoint"] == {}
    assert revision["dispatch_authority_created"] is False


def test_turtlebot3_recovery_revision_rechecks_floor_plan_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく避けて",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    tampered_execution = json.loads(
        json.dumps(revision["turtlebot3_home_mission_execution"])
    )
    tampered_execution["turtlebot3_indoor_map_model"]["floor_plan"].setdefault(
        "walls", []
    ).append(
        {
            "x_m": 0.0,
            "y_m": 0.0,
            "size_x_m": 0.5,
            "size_y_m": 0.5,
        }
    )
    calls_before = bridge.with_suffix(".calls.jsonl").read_text().splitlines()

    blocked = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=tampered_execution,
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    assert "turtlebot3_recovery_revision_floor_plan_geometry_changed" in blocked[
        "summary"
    ]["blocking_reasons"]
    assert blocked["summary"]["recovery_dispatch_request_sent"] is False
    assert blocked["summary"]["completion_claimed"] is False
    assert bridge.with_suffix(".calls.jsonl").read_text().splitlines() == calls_before


def test_turtlebot3_recovery_revision_rejects_changed_planned_route_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval, awaiting, bridge = _build_awaiting_obstacle_recovery(
        tmp_path,
        monkeypatch,
    )
    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="右に大きく避けて",
        proposal=proposal,
        resume_execution=awaiting,
    )
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    changed_proposal = json.loads(json.dumps(proposal))
    changed_proposal["planned_segments"][1]["x_m"] += 0.2
    calls_before = bridge.with_suffix(".calls.jsonl").read_text().splitlines()

    blocked = run_turtlebot3_home_mission_dispatch(
        proposal=changed_proposal,
        approval=approval,
        resume_execution=revision["turtlebot3_home_mission_execution"],
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    summary = blocked["summary"]
    assert "turtlebot3_recovery_checkpoint_planned_segments_mismatch" in summary[
        "blocking_reasons"
    ]
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["route_resumed_after_recovery"] is False
    assert summary["completion_claimed"] is False
    assert bridge.with_suffix(".calls.jsonl").read_text().splitlines() == calls_before


def test_turtlebot3_recovery_checkpoint_rejects_binding_mismatches_without_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    monkeypatch.delenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF", raising=False
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    awaiting = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )
    checkpoint = awaiting["turtlebot3_recovery_checkpoint"]
    binding_cases = []
    parameter_mismatch = _recovery_operator_approval(checkpoint)
    parameter_mismatch["approved_parameters"] = {
        **checkpoint["approved_parameters"],
        "target_x_m": 0.8,
    }
    binding_cases.append(
        (
            parameter_mismatch,
            "turtlebot3_recovery_operator_approval_parameters_mismatch",
        )
    )
    action_mismatch = _recovery_operator_approval(checkpoint)
    action_mismatch["approved_action"] = "hold"
    binding_cases.append(
        (
            action_mismatch,
            "turtlebot3_recovery_operator_approval_approved_action_mismatch",
        )
    )
    proposal_mismatch = _recovery_operator_approval(checkpoint)
    proposal_mismatch["recovery_proposal_id"] = "recovery_proposal:other"
    binding_cases.append(
        (
            proposal_mismatch,
            "turtlebot3_recovery_operator_approval_recovery_proposal_id_mismatch",
        )
    )
    checkpoint_mismatch = _recovery_operator_approval(checkpoint)
    checkpoint_mismatch["checkpoint_hash"] = "0" * 64
    binding_cases.append(
        (
            checkpoint_mismatch,
            "turtlebot3_recovery_operator_approval_checkpoint_hash_mismatch",
        )
    )

    for mismatched_approval, expected_reason in binding_cases:
        blocked = run_turtlebot3_home_mission_dispatch(
            proposal=proposal,
            approval=approval,
            resume_execution=awaiting,
            recovery_operator_approval=mismatched_approval,
        )
        assert blocked["summary"]["status"] == "blocked"
        assert blocked["summary"]["completion_claimed"] is False
        assert blocked["summary"]["recovery_dispatch_request_sent"] is False
        assert (
            blocked["turtlebot3_recovery_checkpoint"]["checkpoint_status"]
            == "failed"
        )
        assert expected_reason in blocked["summary"]["blocking_reasons"]

    malformed_resume = json.loads(json.dumps(awaiting))
    malformed_resume["turtlebot3_recovery_checkpoint"][
        "next_segment_index"
    ] = "bad"
    malformed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=malformed_resume,
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )
    assert malformed["summary"]["status"] == "blocked"
    assert (
        "turtlebot3_recovery_checkpoint_segment_cursor_invalid"
        in malformed["summary"]["blocking_reasons"]
    )

    calls_path = bridge.with_suffix(".calls.jsonl")
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len(calls) == 1


def test_turtlebot3_recovery_checkpoint_fails_closed_when_bridge_is_lost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    awaiting = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )
    checkpoint = awaiting["turtlebot3_recovery_checkpoint"]
    calls_path = bridge.with_suffix(".calls.jsonl")
    calls_before_resume = calls_path.read_text().splitlines()

    monkeypatch.delenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV)
    blocked = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting,
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    assert blocked["summary"]["status"] == "blocked"
    assert blocked["summary"]["completion_claimed"] is False
    assert blocked["summary"]["recovery_dispatch_request_sent"] is False
    assert blocked["summary"]["route_resumed_after_recovery"] is False
    assert blocked["summary"]["physical_execution_invoked"] is False
    assert (
        "RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_not_enabled"
        in blocked["summary"]["blocking_reasons"]
    )
    assert calls_path.read_text().splitlines() == calls_before_resume


def test_turtlebot3_resume_preserves_approved_recovery_when_later_route_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    obstacle_planner = tmp_path / "obstacle_recovery_planner.py"
    failure_planner = tmp_path / "failure_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(obstacle_planner)
    _write_failure_recovery_planner(failure_planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(obstacle_planner),
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    awaiting = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )
    checkpoint = awaiting["turtlebot3_recovery_checkpoint"]
    failed_label = proposal["planned_segments"][1]["label"]
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal
    failure_injected = False

    def dispatch_with_later_failure(**kwargs):
        nonlocal failure_injected
        result = original_dispatch(**kwargs)
        if kwargs["goal"].label == failed_label and not failure_injected:
            failure_injected = True
            result = {
                **result,
                "completion_claimed": False,
                "blocking_reasons": ["fixture_remaining_segment_failure"],
                "robot_motion_observed": False,
                "odom_delta_m": 0.0,
            }
        return result

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        dispatch_with_later_failure,
    )
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(failure_planner),
    )

    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting,
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    summary = resumed["summary"]
    execution = resumed["turtlebot3_home_mission_execution"]
    approved_recovery = execution["recovery_segment_result"]
    followups = execution["subsequent_recovery_segment_results"]
    assert failure_injected is True
    assert summary["completion_claimed"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert summary["subsequent_recovery_dispatch_request_sent"] is False
    assert summary["subsequent_recovery_completion_claimed"] is False
    assert approved_recovery["goal_pose"]["label"] == (
        "runtime_recovery_avoid_obstacle_waypoint"
    )
    assert approved_recovery["adapter_evidence"]["operator_approval_ref"] == (
        "operator_approval:test_interactive_recovery"
    )
    assert followups == []
    assert resumed["ros2_nav2_recovery_adapter_evidence"][
        "operator_approval_ref"
    ] == "operator_approval:test_interactive_recovery"
    assert len(
        resumed["ros2_nav2_subsequent_recovery_adapter_evidence_segments"]
    ) == 0
    indoor_map = execution["turtlebot3_indoor_map_model"]
    assert indoor_map["recovery"]["subsequent_targets"] == []
    assert indoor_map["recovery"]["subsequent_completion_claimed"] is False
    subsequent_observed_points = indoor_map["recovery"][
        "subsequent_observed_points"
    ]
    assert not subsequent_observed_points


def test_turtlebot3_later_route_failure_proposes_fresh_followup_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    obstacle_planner = tmp_path / "obstacle_recovery_planner.py"
    failure_planner = tmp_path / "failure_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(obstacle_planner)
    _write_failure_recovery_planner(failure_planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(obstacle_planner),
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL", "1")
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    awaiting = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )
    parent = awaiting["turtlebot3_recovery_checkpoint"]
    failed_label = proposal["planned_segments"][1]["label"]
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal

    def fail_first_resumed_segment(**kwargs):
        result = original_dispatch(**kwargs)
        if kwargs["goal"].label == failed_label:
            return {
                **result,
                "completion_claimed": False,
                "blocking_reasons": ["fixture_remaining_segment_failure"],
                "adapter_evidence": {
                    **result["adapter_evidence"],
                    "completion_claimed": False,
                    "blocking_reasons": [
                        "fixture_remaining_segment_failure"
                    ],
                },
            }
        return result

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        fail_first_resumed_segment,
    )
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(failure_planner),
    )

    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting,
        recovery_operator_approval=_recovery_operator_approval(parent),
    )

    summary = resumed["summary"]
    child = resumed["turtlebot3_recovery_checkpoint"]
    consumed_parent = resumed[
        "turtlebot3_recovery_followup_parent_checkpoint"
    ]
    assert summary["status"] == "pending"
    assert summary["recovery_completion_claimed"] is True
    assert summary["route_resumed_after_recovery"] is True
    assert summary["route_completed_after_recovery"] is False
    assert "fixture_remaining_segment_failure" in summary["blocking_reasons"]
    assert summary["recovery_candidate_resolution"] == {}
    assert consumed_parent["checkpoint_id"] == parent["checkpoint_id"]
    assert consumed_parent["checkpoint_status"] == "consumed"
    assert child["checkpoint_status"] == "awaiting_operator_approval"
    assert child["checkpoint_id"] != parent["checkpoint_id"]
    assert child["parent_checkpoint_id"] == parent["checkpoint_id"]
    assert child["selected_action"] == "return_home"
    assert child["requires_new_human_approval"] is True
    assert child["automatic_redispatch_performed"] is False
    assert child["dispatch_authority_created"] is False
    assert "claimed_at" not in child
    assert "claimed_by_approval_ref" not in child
    assert "recovery_candidate_binding" not in child


def test_turtlebot3_two_recovery_cycles_retry_failed_segment_and_reach_goal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    obstacle_planner = tmp_path / "obstacle_recovery_planner.py"
    ask_human_planner = tmp_path / "ask_human_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(obstacle_planner)
    _write_ask_human_failure_recovery_planner(ask_human_planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(obstacle_planner),
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL", "1")
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。障害物を検出したらRecovery Agentが"
            "提案し、人間の承認後に回避して、再観測してから続けて"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    awaiting_first = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )
    first_checkpoint = awaiting_first["turtlebot3_recovery_checkpoint"]
    failed_label = proposal["planned_segments"][1]["label"]
    original_dispatch = turtlebot3_home_mission_runtime._dispatch_nav2_goal
    failure_injected = False

    def fail_first_attempt_at_second_segment(**kwargs):
        nonlocal failure_injected
        result = original_dispatch(**kwargs)
        if kwargs["goal"].label == failed_label and not failure_injected:
            failure_injected = True
            return {
                **result,
                "completion_claimed": False,
                "blocking_reasons": ["fixture_post_recovery_segment_failure"],
                "adapter_evidence": {
                    **result["adapter_evidence"],
                    "completion_claimed": False,
                    "blocking_reasons": [
                        "fixture_post_recovery_segment_failure"
                    ],
                },
            }
        return result

    monkeypatch.setattr(
        turtlebot3_home_mission_runtime,
        "_dispatch_nav2_goal",
        fail_first_attempt_at_second_segment,
    )
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
        _bridge_command(ask_human_planner),
    )
    awaiting_second = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=awaiting_first,
        recovery_operator_approval=_recovery_operator_approval(
            first_checkpoint,
            approval_ref="operator_approval:first_recovery",
        ),
    )

    second_proposal_checkpoint = awaiting_second[
        "turtlebot3_recovery_checkpoint"
    ]
    assert awaiting_second["summary"]["status"] == "pending"
    assert second_proposal_checkpoint["selected_action"] == "ask_human"
    assert second_proposal_checkpoint["operator_guidance_required"] is True
    assert second_proposal_checkpoint["checkpoint_id"] != first_checkpoint[
        "checkpoint_id"
    ]
    assert second_proposal_checkpoint["completed_segment_count"] == 1
    assert second_proposal_checkpoint["next_segment_index"] == 2
    assert len(
        awaiting_second["turtlebot3_home_mission_execution"][
            "route_failure_observation_results"
        ]
    ) == 1

    revision = build_turtlebot3_recovery_checkpoint_revision(
        operator_instruction="停止を確認した。同じ配送区間を一度だけ再試行して",
        proposal=proposal,
        resume_execution=awaiting_second,
    )
    assert revision["revision_status"] == "proposed"
    second_checkpoint = revision["turtlebot3_recovery_checkpoint"]
    assert second_checkpoint["selected_action"] == "reroute"
    assert second_checkpoint["approved_parameters"] == {
        "target_x_m": proposal["planned_segments"][1]["x_m"],
        "target_y_m": proposal["planned_segments"][1]["y_m"],
        "retry_failed_segment_required": True,
        "retry_count": 1,
    }
    assert second_checkpoint["recovery_revision_geometry"][
        "failed_segment_ref"
    ] == "segment_2"
    assert second_checkpoint["checkpoint_id"] not in {
        first_checkpoint["checkpoint_id"],
        second_proposal_checkpoint["checkpoint_id"],
    }

    completed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=revision,
        recovery_operator_approval=_recovery_operator_approval(
            second_checkpoint,
            approval_ref="operator_approval:second_recovery",
        ),
    )

    summary = completed["summary"]
    execution = completed["turtlebot3_home_mission_execution"]
    assert failure_injected is True
    assert summary["status"] == "completed", json.dumps(summary, indent=2)
    assert summary["completion_claimed"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["segment_completion_count"] == len(
        proposal["planned_segments"]
    )
    assert len(execution["segment_results"]) == len(proposal["planned_segments"])
    assert len(execution["route_failure_observation_results"]) == 1
    assert summary["recovery_closed_loop_verified_cycle_count"] == 2
    assert summary["form3_closed_loop_status"] == "verified"
    assert summary["form3_closed_loop_claimed"] is True
    cycles = summary["recovery_closed_loop_cycles"]
    assert [cycle["checkpoint_id"] for cycle in cycles] == [
        first_checkpoint["checkpoint_id"],
        second_checkpoint["checkpoint_id"],
    ]
    assert len({cycle["operator_approval_ref"] for cycle in cycles}) == 2
    assert all(cycle["reobservation_status"] == "verified" for cycle in cycles)
    assert cycles[1]["selected_action"] == "reroute"


def test_turtlebot3_mid_mission_obstacle_recovery_uses_fallback_when_llm_guardrail_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "guardrail_fault_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_guardrail_fault_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    planner_result = summary["recovery_planner_result"]
    guardrail = planner_result["guardrail"]
    assert planner_result["planner_status"] == "guardrail_blocked"
    assert (
        "raw_llm_output_forbidden_authority_key:dispatch_request_sent"
        in planner_result["blocking_reasons"]
    )
    assert (
        "unsupported_observation_claim:fabricated_distance_to_home_m"
        in planner_result["blocking_reasons"]
    )
    assert guardrail["checks"]["forbidden_authority_keys_absent"] is False
    assert summary["recovery_proposals"][0]["proposal_source"] == (
        "deterministic_fallback"
    )
    assert summary["recovery_proposals"][0]["llm_judgment_recorded"] is False
    assert summary["recovery_action_suggested"] == "avoid_obstacle"
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_completion_claimed"] is False
    assert summary["route_resumed_after_recovery"] is False
    assert summary["route_completed_after_recovery"] is False
    assert summary["completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False


def test_turtlebot3_mid_mission_nav2_failure_convenes_recovery_and_stays_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "planner.py"
    _write_nav2_failure_bridge(bridge)
    _write_failure_recovery_planner(planner)
    process_logs = _write_nav2_failure_process_logs(tmp_path)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_LOG_BUNDLE_PATHS_ENV, json.dumps(process_logs))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して。",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "pending"
    assert summary["dispatch_request_sent"] is True
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    # An unplanned segment failure now convenes the recovery machinery:
    # The planner proposes return_home and waits for fresh action approval.
    assert summary["runtime_recovery_triggered"] is True
    assert summary["runtime_failure_recovery_triggered"] is True
    failure_context = summary["runtime_failure_context"]
    assert failure_context["failed_segment_index"] == 1
    assert failure_context["source"] == "ros2_nav2_bridge_segment_result"
    assert failure_context["runtime_failure_observed"] is True
    assert "recommended_recovery_action" not in failure_context
    assert failure_context["nav2_status"] == "aborted"
    assert failure_context["goal_cancel_result_observed"] is False
    motion_context = summary["runtime_recovery_motion_context"]
    assert motion_context["odom_delta_m"] == 0.0
    assert motion_context["stalled_after_dispatch"] is True
    assert motion_context["motion_observation_source"] == "ros2_nav2_bridge_receipt"
    reflex = summary["recovery_planner_result"]["recovery_reflex"]
    assert reflex["trigger"] == "runtime_segment_failure"
    assert reflex["robot_motion_state"] == "stationary"
    assert reflex["motion_observation_source"] == "ros2_nav2_bridge_receipt"
    assert reflex["stop_dispatch_required"] is False
    assert reflex["stop_dispatch_performed"] is False
    assert reflex["entered_deliberation"] is True
    assert summary["recovery_planner_result"]["harness_stop_dispatch"] == {}
    assert summary["recovery_proposals"]
    assert summary["recovery_proposals"][0]["proposal_source"] == "llm"
    assert summary["recovery_proposals"][0]["llm_judgment_recorded"] is True
    assert (
        summary["recovery_proposals"][0]["input_observations"][
            "runtime_failure_source"
        ]
        == "ros2_nav2_bridge_segment_result"
    )
    assert (
        summary["recovery_proposals"][0]["input_observations"]["odom_delta_m"]
        == 0.0
    )
    assert (
        summary["recovery_proposals"][0]["input_observations"][
            "stalled_after_dispatch"
        ]
        is True
    )
    assert summary["recovery_planner_status"] == "proposal_guardrail_passed"
    assert summary["runtime_recovery_action_kind"] == "return_home"
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["segment_dispatch_count"] == 1
    assert summary["segment_completion_count"] == 0
    assert "nav2_goal_result_not_succeeded" in summary["blocking_reasons"]
    assert summary["nav2_log_diagnostics_status"] == "ready"
    assert "controller_failed_to_make_progress" in summary["nav2_log_observed_patterns"]
    assert "follow_path_action_aborted" in summary["nav2_log_observed_patterns"]
    assert "costmap_clear_recovery_observed" in summary["nav2_log_observed_patterns"]
    assert (
        "recovery_goal_stalled_after_costmap_clear"
        in summary["nav2_log_failure_hypotheses"]
    )


def test_turtlebot3_failure_return_home_waits_for_fresh_recovery_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "planner.py"
    _write_nav2_failure_bridge(bridge)
    _write_failure_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL", "1")
    monkeypatch.delenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF",
        raising=False,
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して。",
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
    )

    summary = result["summary"]
    checkpoint = result["turtlebot3_recovery_checkpoint"]
    assert proposal["autonomy_envelope"]["preapproved_recovery_actions"] == []
    assert "return_home" in proposal["autonomy_envelope"][
        "requires_human_approval_for"
    ]
    classification = summary["recovery_proposal_classifications"][0]
    assert classification["execution_class"] == "requires_human_approval"
    assert classification["requires_new_human_approval"] is True
    assert classification["execution_permitted_by_envelope"] is False
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_completion_claimed"] is False
    assert summary["fresh_recovery_operator_approval_count"] == 0
    assert summary["recovery_dispatch_authority_source"] is None
    assert checkpoint["checkpoint_status"] == "awaiting_operator_approval"
    assert checkpoint["selected_action"] == "return_home"
    assert checkpoint["approved_parameters"] == {
        "target_x_m": -2.0,
        "target_y_m": -0.5,
        "return_home_required": True,
    }
    assert checkpoint["recovery_goal_poses"][0]["label"] == "simulated_home_origin"
    assert checkpoint["dispatch_authority_created"] is False
    assert summary["segment_dispatch_count"] == 1

    _write_success_bridge(bridge)
    resumed = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval,
        resume_execution=result,
        recovery_operator_approval=_recovery_operator_approval(checkpoint),
    )

    consumed = resumed["turtlebot3_recovery_checkpoint"]
    resumed_summary = resumed["summary"]
    calls = [
        json.loads(line)
        for line in bridge.with_suffix(".calls.jsonl").read_text().splitlines()
    ]
    assert consumed["checkpoint_status"] == "consumed"
    assert consumed["consumed_by_approval_ref"] == (
        "operator_approval:test_interactive_recovery"
    )
    assert resumed_summary["recovery_dispatch_authority_source"] == (
        "fresh_operator_approval"
    )
    assert resumed_summary["fresh_recovery_operator_approval_count"] == 1
    assert resumed_summary["recovery_dispatch_request_sent"] is True
    assert resumed_summary["recovery_completion_claimed"] is True
    assert resumed_summary["route_resumed_after_recovery"] is False
    assert resumed_summary["mission_delivery_completion_claimed"] is False
    assert len(calls) == 1
    assert calls[0]["label"] == "simulated_home_origin"


def test_turtlebot3_house_profile_routes_through_front_door(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )

    proposal = result["scenario_proposal"]
    segments = proposal["planned_segments"]
    labels = [segment["label"] for segment in segments]
    assert len(segments) == 7
    assert "simulated_front_door_passage_checkpoint" in labels
    assert labels[-1] == "simulated_living_room_dropoff_waypoint"
    dropoff = segments[-1]
    assert dropoff["x_m"] == pytest.approx(-4.0)
    assert dropoff["y_m"] == pytest.approx(1.15)
    planned_clearance_3d = (
        turtlebot3_home_mission_runtime.assess_ground_robot_trajectory_clearance_3d(
            trajectory_streams=[
                [
                    {
                        "frame_id": "map",
                        "x_m": -2.0,
                        "y_m": -0.5,
                    },
                    *segments,
                ]
            ],
            robot_collision_envelope=(
                turtlebot3_home_mission_runtime._TURTLEBOT3_STOCK_COLLISION_ENVELOPE
            ),
            obstacle_volumes=(
                turtlebot3_home_mission_runtime._turtlebot3_obstacle_collision_volumes(
                    turtlebot3_home_mission_runtime._TURTLEBOT3_HOUSE_LAYOUT_OBSTACLES
                )
            ),
        )
    )
    assert planned_clearance_3d.status == "verified_clear"
    assert planned_clearance_3d.minimum_surface_clearance_m is not None
    assert planned_clearance_3d.minimum_surface_clearance_m >= 0.19
    assert len(planned_clearance_3d.candidate_results) == 3
    assert all(
        candidate.status == "verified_clear"
        for candidate in planned_clearance_3d.candidate_results
    )
    dropoff_point = (dropoff["x_m"], dropoff["y_m"])
    static_clearance, static_reasons = (
        turtlebot3_home_mission_runtime._revision_floor_plan_waypoint_clearance(
            dropoff_point,
            turtlebot3_home_mission_runtime._turtlebot3_home_floor_plan(),
        )
    )
    assert static_reasons == []
    assert static_clearance is not None
    assert static_clearance >= 1.05
    for obstacle in turtlebot3_home_mission_runtime._TURTLEBOT3_HOUSE_LAYOUT_OBSTACLES:
        half_x = obstacle["size_x_m"] / 2.0
        half_y = obstacle["size_y_m"] / 2.0
        obstacle_clearance = turtlebot3_home_mission_runtime._point_rect_clearance_m(
            dropoff_point,
            (
                obstacle["x_m"] - half_x,
                obstacle["x_m"] + half_x,
                obstacle["y_m"] - half_y,
                obstacle["y_m"] + half_y,
            ),
        )
        assert obstacle_clearance >= 2.0
    assert 11.5 < proposal["planned_route_distance_m"] < 11.8
    route = proposal["indoor_delivery_route"]
    assert route["planned_room_sequence"][0] == "home"
    assert "front door" in route["planned_room_sequence"]

    from src.runtime.turtlebot3_home_mission import _turtlebot3_home_floor_plan

    floor_plan = _turtlebot3_home_floor_plan()
    assert floor_plan["floor_plan_id"] == "turtlebot3_house.v1"
    assert floor_plan["source"] == "turtlebot3_house_model_sdf_collision"
    assert len(floor_plan["walls"]) >= 30
    assert floor_plan["pillars"] == []
    furniture_labels = {item["label"] for item in floor_plan["furniture"]}
    assert {"table", "bookshelf", "mailbox"} <= furniture_labels
    rooms = floor_plan["rooms"]
    for index, room_a in enumerate(rooms):
        for room_b in rooms[index + 1:]:
            overlap_x = min(room_a["max_x_m"], room_b["max_x_m"]) - max(
                room_a["min_x_m"], room_b["min_x_m"]
            )
            overlap_y = min(room_a["max_y_m"], room_b["max_y_m"]) - max(
                room_a["min_y_m"], room_b["min_y_m"]
            )
            assert not (overlap_x > 1e-9 and overlap_y > 1e-9), (
                room_a["room_id"],
                room_b["room_id"],
            )

    obstacle_scenario = proposal["obstacle_scenario"]
    assert obstacle_scenario["physical_execution_invoked"] is False


def test_turtlebot3_world_profile_defaults_to_house_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )

    from src.runtime.turtlebot3_home_mission import _turtlebot3_home_floor_plan

    proposal = result["scenario_proposal"]
    assert _turtlebot3_home_floor_plan()["floor_plan_id"] == "turtlebot3_house.v1"
    assert len(proposal["planned_segments"]) == 7


def test_turtlebot3_arena_profile_stays_available_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "arena")

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )

    from src.runtime.turtlebot3_home_mission import _turtlebot3_home_floor_plan

    proposal = result["scenario_proposal"]
    assert _turtlebot3_home_floor_plan()["floor_plan_id"] == (
        "turtlebot3_simulated_home_loop.v1"
    )
    assert len(proposal["planned_segments"]) == 10


def test_turtlebot3_house_delivery_to_named_bedroom(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="亀さん、bedroomへ荷物を届けて",
    )

    proposal = result["scenario_proposal"]
    labels = [segment["label"] for segment in proposal["planned_segments"]]
    assert labels[-1] == "simulated_bedroom_dropoff_waypoint"
    assert "simulated_front_door_passage_checkpoint" in labels
    assert "simulated_study_door_checkpoint" in labels
    assert "simulated_bedroom_door_checkpoint" in labels
    route = proposal["indoor_delivery_route"]
    assert route["destination_room_id"] == "bedroom"
    assert route["destination_room_label"] == "Bedroom"
    assert "bedroom" in route["planned_room_sequence"]


def test_turtlebot3_house_delivery_room_terms_japanese(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    for text, expected in (
        ("寝室まで配送して", "bedroom"),
        ("書斎に届けて", "study"),
        ("ダイニングへ配送して", "dining"),
        ("パントリーに配送して", "pantry"),
        ("ラウンジまで届けて", "lounge"),
        ("屋内配送して", "living"),
    ):
        result = build_turtlebot3_home_mission_plan(
            operator_instruction=f"TurtleBot3で{text}",
        )
        route = result["scenario_proposal"]["indoor_delivery_route"]
        assert route["destination_room_id"] == expected, text


def test_turtlebot3_dispatch_emits_claim_safe_live_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    partials: list[dict] = []
    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
        progress_callback=partials.append,
    )

    assert result["summary"]["status"] == "completed"
    segment_count = len(plan["scenario_proposal"]["planned_segments"])
    assert len(partials) == segment_count
    dispatch_counts = [
        partial["summary"]["segment_dispatch_count"] for partial in partials
    ]
    assert dispatch_counts == sorted(dispatch_counts)
    assert dispatch_counts[-1] == segment_count
    transition_records = result["summary"][
        "segment_transition_authority_records"
    ]
    assert len(transition_records) == segment_count
    assert result["summary"]["segment_transition_authority_count"] == (
        segment_count
    )
    assert result["summary"]["segment_transition_authorized_count"] == (
        segment_count
    )
    assert [
        record["segment_ref"] for record in transition_records
    ] == [f"segment_{index}" for index in range(1, segment_count + 1)]
    assert all(
        record["transition_status"] == "authorized"
        and record["dispatch_authority_source"]
        == "preexisting_route_approval"
        and record["approval_created"] is False
        and record["dispatch_authority_created"] is False
        and record["runtime_effect_requested"] is False
        for record in transition_records
    )
    assert transition_records[0]["previous_segment_ref"] is None
    assert transition_records[0]["previous_predicate_satisfied"] is None
    assert all(
        record["previous_predicate_satisfied"] is True
        for record in transition_records[1:]
    )
    for partial in partials:
        summary = partial["summary"]
        assert summary["status"] == "running"
        assert summary["completion_claimed"] is False
        assert summary["physical_execution_invoked"] is False
        assert summary["mission_delivery_completion_claimed"] is False
        model = summary["turtlebot3_indoor_map_model"]
        assert model["mission_status"] == "running"
        assert model["physical_execution_invoked"] is False


def test_turtlebot3_display_sanitize_drops_frame_leaks_and_spikes() -> None:
    from src.runtime.turtlebot3_home_mission import (
        _sanitize_observed_display_points,
    )

    points = [
        {"x_m": 5.0, "y_m": 5.0, "frame_id": "odom"},
        {"x_m": -2.0, "y_m": -0.5, "frame_id": "map"},
        {"x_m": -1.9, "y_m": -0.5, "frame_id": "map"},
        {"x_m": 3.0, "y_m": 2.0, "frame_id": "map"},
        {"x_m": -1.8, "y_m": -0.45, "frame_id": "map"},
        {"x_m": -0.9, "y_m": -0.4, "frame_id": "map"},
        {"x_m": -0.85, "y_m": -0.35, "frame_id": "map"},
    ]

    kept, meta = _sanitize_observed_display_points(points)

    xs = [point["x_m"] for point in kept]
    assert 5.0 not in xs
    assert 3.0 not in xs
    assert meta["odom_fallback_dropped"] == 1
    assert meta["spike_dropped"] == 1
    sustained = [
        {"x_m": -2.0, "y_m": -0.5, "frame_id": "map"},
        {"x_m": -0.9, "y_m": -0.4, "frame_id": "map"},
        {"x_m": -0.88, "y_m": -0.38, "frame_id": "map"},
    ]
    kept_sustained, _ = _sanitize_observed_display_points(sustained)
    assert len(kept_sustained) == 3


def test_harness_stop_ack_without_stop_observation_is_not_confirmed(
    monkeypatch,
) -> None:
    """ACK is not a stop: cancel_accepted alone must not confirm the stop."""

    class _AckOnlyClient:
        def cancel_goal(self):
            return {
                "ack_status": "accepted",
                "ack_source": "stub_bridge",
                "nav2_status": "canceled",
                "blocking_reasons": [],
                "cancel_accepted": True,
                "stop_observed": False,
                "post_cancel_odom_delta_m": 0.41,
                "stop_observation_window_s": 2.0,
                "stop_observation_source": "odom:/odom",
            }

    monkeypatch.setattr(
        turtlebot3_nav2_execution_runtime,
        "Ros2Nav2BridgeCommandClient",
        _AckOnlyClient,
    )
    record = turtlebot3_home_mission_runtime._dispatch_harness_stop(
        reflex={"trigger": "runtime_obstacle_observed"},
        proposal={},
    )
    assert record["cancel_accepted"] is True
    assert record["stop_observed"] is False
    assert record["stop_confirmed"] is False
    assert record["bridge_receipt"]["post_cancel_odom_delta_m"] == 0.41
