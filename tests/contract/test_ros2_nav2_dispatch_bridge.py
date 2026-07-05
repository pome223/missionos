from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from src.runtime.hardware_adapter_contract import HardwareExecutionMode
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
    Ros2Nav2BridgeCommandClient,
    Ros2Nav2BridgeError,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
)


def _write_bridge(
    path: Path,
    *,
    physical_claim: bool = False,
    raw_topic_claim: bool = False,
    robot_motion_observed: bool = True,
) -> None:
    physical = "True" if physical_claim else "False"
    raw_topic = "True" if raw_topic_claim else "False"
    robot_motion = "True" if robot_motion_observed else "False"
    odom_delta_m = "0.25" if robot_motion_observed else "0.0"
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('physical_execution_invoked') is True:\n"
        "    raise SystemExit(3)\n"
        "if request.get('raw_velocity_allowed') is not False:\n"
        "    raise SystemExit(4)\n"
        "if request.get('raw_ros_topic_publication_allowed') is not False:\n"
        "    raise SystemExit(5)\n"
        "action = request.get('action')\n"
        "payload = request.get('payload') or {}\n"
        "response = {\n"
        "    'physical_execution_invoked': "
        + physical
        + ",\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': "
        + raw_topic
        + ",\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "}\n"
        "if action == 'send_goal_pose':\n"
        "    response.update({\n"
        "        'ack_status': 'accepted',\n"
        "        'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "        'goal_x_m': payload.get('x_m'),\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': True,\n"
        "        'nav2_status': 'succeeded',\n"
        "        'state_result': {\n"
        "            'nav2_action_server_available': True,\n"
        "            'pose_observed': True,\n"
        "            'robot_motion_observed': "
        + robot_motion
        + ",\n"
        "            'odom_delta_m': "
        + odom_delta_m
        + ",\n"
        "        },\n"
        "        'progress_result': {\n"
        "            'runtime_progress_observed': True,\n"
        "            'completion_observed': True,\n"
        "            'robot_motion_observed': "
        + robot_motion
        + ",\n"
        "            'nav2_status': 'succeeded',\n"
        "        },\n"
        "    })\n"
        "else:\n"
        "    response.update({'ack_status': 'accepted'})\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )


def _command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def test_ros2_nav2_bridge_dispatches_bounded_sim_goal_without_physical_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "ros2_nav2_bridge.py"
    _write_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _command(bridge))

    client = Ros2Nav2BridgeCommandClient()
    adapter = Ros2Nav2HardwareAdapter(
        config=Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref="missionos_plan_turtlebot4_bounded_nav2_goal",
            goal_pose=Nav2GoalPose(x_m=0.25, y_m=0.0),
            execution_mode=HardwareExecutionMode.SIM,
            operator_approval_ref="approval_nav2_turtlebot4_bridge_001",
            approval_actor="operator-001",
            approval_timestamp=datetime.now(timezone.utc),
        ),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()
    bridge_responses = client.collect_responses()

    assert evidence.dispatch_request_sent is True
    assert evidence.command_ack_observed is True
    assert evidence.runtime_progress_observed is True
    assert evidence.completion_claimed is True
    assert evidence.completion_scope == "sim_action"
    assert evidence.physical_execution_invoked is False
    assert "sim_action_completion_not_physical" in evidence.unproven_claims
    assert bridge_responses[0]["action"] == "send_goal_pose"
    assert bridge_responses[0]["ack_source"] == "fixture_nav2_navigate_to_pose"
    assert bridge_responses[0]["goal_x_m"] == 0.25


def test_ros2_nav2_bridge_rejects_physical_execution_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "ros2_nav2_physical.py"
    _write_bridge(bridge, physical_claim=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _command(bridge))

    client = Ros2Nav2BridgeCommandClient()

    with pytest.raises(Ros2Nav2BridgeError):
        client.send_goal_pose(Nav2GoalPose(x_m=0.25, y_m=0.0))


def test_ros2_nav2_bridge_rejects_raw_topic_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "ros2_nav2_raw_topic.py"
    _write_bridge(bridge, raw_topic_claim=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _command(bridge))

    client = Ros2Nav2BridgeCommandClient()

    with pytest.raises(Ros2Nav2BridgeError):
        client.send_goal_pose(Nav2GoalPose(x_m=0.25, y_m=0.0))


def test_ros2_nav2_bridge_does_not_claim_completion_without_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "ros2_nav2_no_motion.py"
    _write_bridge(bridge, robot_motion_observed=False)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _command(bridge))

    client = Ros2Nav2BridgeCommandClient()
    adapter = Ros2Nav2HardwareAdapter(
        config=Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref="missionos_plan_turtlebot3_no_motion",
            goal_pose=Nav2GoalPose(x_m=0.25, y_m=0.0),
            execution_mode=HardwareExecutionMode.SIM,
            operator_approval_ref="approval_nav2_turtlebot3_no_motion_001",
            approval_actor="operator-001",
            approval_timestamp=datetime.now(timezone.utc),
        ),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_request_sent is True
    assert evidence.command_ack_observed is True
    assert evidence.runtime_progress_observed is True
    assert evidence.completion_claimed is False
    assert evidence.completion_scope == "none"
    assert "nav2_completion_without_robot_motion_observed" in evidence.blocking_reasons
    assert "nav2_completion_without_robot_motion_not_claimed" in (
        evidence.unproven_claims
    )


def test_ros2_nav2_turtlebot4_bridge_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv("RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT4_BRIDGE", raising=False)
    request = {
        "schema_version": "missionos_ros2_nav2_bridge_request.v1",
        "action": "send_goal_pose",
        "payload": Nav2GoalPose(x_m=0.25, y_m=0.0).model_dump(mode="json"),
        "execution_mode": "sim",
        "physical_execution_invoked": False,
        "raw_velocity_allowed": False,
        "raw_ros_topic_publication_allowed": False,
    }

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot4_bridge.py"),
        input=json.dumps(request, ensure_ascii=True, sort_keys=True),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ack_status"] == "rejected"
    assert "ros2_nav2_turtlebot4_bridge_opt_in_not_enabled" in (
        response["blocking_reasons"]
    )
    assert response["physical_execution_invoked"] is False
    assert response["raw_velocity_published"] is False
    assert response["raw_ros_topic_published"] is False


def test_ros2_nav2_turtlebot3_bridge_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv("RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE", raising=False)
    request = {
        "schema_version": "missionos_ros2_nav2_bridge_request.v1",
        "action": "send_goal_pose",
        "payload": Nav2GoalPose(x_m=0.25, y_m=0.0).model_dump(mode="json"),
        "execution_mode": "sim",
        "physical_execution_invoked": False,
        "raw_velocity_allowed": False,
        "raw_ros_topic_publication_allowed": False,
    }

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot3_bridge.py"),
        input=json.dumps(request, ensure_ascii=True, sort_keys=True),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ack_status"] == "rejected"
    assert "ros2_nav2_turtlebot3_bridge_opt_in_not_enabled" in (
        response["blocking_reasons"]
    )
    assert response["physical_execution_invoked"] is False
    assert response["raw_velocity_published"] is False
    assert response["raw_ros_topic_published"] is False


def test_ros2_nav2_tf_readiness_probe_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv(
        "RUN_MISSIONOS_ROS2_NAV2_TF_READINESS_PROBE",
        raising=False,
    )

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot4_tf_readiness_probe.py"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ran"] is False
    assert response["dispatch_request_sent"] is False
    assert response["physical_execution_invoked"] is False
    assert response["raw_ros_topic_published"] is False


def test_ros2_nav2_odom_tf_republisher_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv(
        "RUN_MISSIONOS_ROS2_NAV2_ODOM_TF_REPUBLISHER",
        raising=False,
    )

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot4_odom_tf_republisher.py"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ran"] is False
    assert response["dispatch_request_sent"] is False
    assert response["physical_execution_invoked"] is False
    assert response["raw_control_topic_published"] is False


def test_ros2_nav2_nav_velocity_relay_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv(
        "RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY",
        raising=False,
    )

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot4_nav_velocity_relay.py"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ran"] is False
    assert response["dispatch_request_sent"] is False
    assert response["physical_execution_invoked"] is False
    assert response["cmd_vel_published_by_missionos"] is False
    assert response["velocity_generated_by_missionos"] is False
    assert response["target_control_topic_published"] is False


def test_ros2_nav2_turtlebot3_nav_velocity_relay_is_gate_controlled(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        "RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY",
        raising=False,
    )

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot3_nav_velocity_relay.py"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ran"] is False
    assert response["shim"] == "ros2_nav2_turtlebot3_nav_velocity_relay"
    assert response["dispatch_request_sent"] is False
    assert response["physical_execution_invoked"] is False
    assert response["cmd_vel_published_by_missionos"] is False
    assert response["velocity_generated_by_missionos"] is False
    assert response["target_control_topic_published"] is False


def test_ros2_nav2_controller_motion_probe_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv(
        "RUN_MISSIONOS_ROS2_NAV2_CONTROLLER_MOTION_PROBE",
        raising=False,
    )

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot4_controller_motion_probe.py"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ran"] is False
    assert response["missionos_dispatch_path"] is False
    assert response["dispatch_request_sent"] is False
    assert response["completion_claimed"] is False
    assert response["physical_execution_invoked"] is False
    assert response["diagnostic_raw_velocity_published"] is False
    assert response["target_control_topic_published"] is False


def test_ros2_nav2_turtlebot3_motion_probe_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv(
        "RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_MOTION_PROBE",
        raising=False,
    )

    completed = subprocess.run(
        (sys.executable, "scripts/ros2_nav2_turtlebot3_motion_probe.py"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ran"] is False
    assert response["missionos_dispatch_path"] is False
    assert response["dispatch_request_sent"] is False
    assert response["completion_claimed"] is False
    assert response["physical_execution_invoked"] is False
    assert response["diagnostic_raw_velocity_published"] is False
    assert response["target_control_topic_published"] is False
