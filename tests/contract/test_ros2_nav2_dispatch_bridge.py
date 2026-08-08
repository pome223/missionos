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
    ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_ENV,
    ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV,
    Ros2Nav2BridgeCommandClient,
    Ros2Nav2BridgeError,
)
from src.runtime import ros2_nav2_dispatch_bridge as dispatch_bridge_runtime
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
        "elif action == 'evaluate_recovery_candidates':\n"
        "    candidate = dict(payload.get('candidates')[0])\n"
        "    candidate.update({\n"
        "        'path_valid': True,\n"
        "        'planner_status': 'succeeded',\n"
        "        'target_cost': 10,\n"
        "        'maximum_path_cost': 20,\n"
        "        'local_current_cost': 0,\n"
        "        'local_maximum_path_cost': 40,\n"
        "        'path_length_m': 1.25,\n"
        "        'path_sha256': 'fixture-path-hash',\n"
        "    })\n"
        "    response.update({\n"
        "        'ack_status': 'not_requested',\n"
        "        'evaluation_status': 'validated',\n"
        "        'selected_candidate': candidate,\n"
        "        'candidate_evaluations': [candidate],\n"
        "        'costmap_snapshot_hash': 'fixture-costmap-hash',\n"
        "        'global_costmap_snapshot_hash': 'fixture-global-costmap-hash',\n"
        "        'local_costmap_snapshot_hash': 'fixture-local-costmap-hash',\n"
        "        'dispatch_request_sent': False,\n"
        "        'dispatch_authority_created': False,\n"
        "        'command_ack_observed': False,\n"
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


def test_ros2_nav2_bridge_evaluates_recovery_candidates_without_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "ros2_nav2_plan_only.py"
    _write_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _command(bridge))

    response = Ros2Nav2BridgeCommandClient().evaluate_recovery_candidates(
        candidates=[
            {
                "candidate_id": "south",
                "x_m": 0.2,
                "y_m": -2.1,
                "yaw_rad": 0.0,
            }
        ],
        obstacle={"x_m": 0.2, "y_m": -1.2},
    )

    assert response["evaluation_status"] == "validated"
    assert response["selected_candidate"]["candidate_id"] == "south"
    assert response["dispatch_request_sent"] is False
    assert response["dispatch_authority_created"] is False
    assert response["command_ack_observed"] is False
    assert response["physical_execution_invoked"] is False


def test_ros2_nav2_recovery_evaluation_timeout_must_be_positive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "ros2_nav2_plan_only.py"
    _write_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _command(bridge))
    monkeypatch.setenv(ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_ENV, "0")

    with pytest.raises(
        Ros2Nav2BridgeError,
        match="ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_S must be positive",
    ):
        Ros2Nav2BridgeCommandClient().evaluate_recovery_candidates(
            candidates=[
                {
                    "candidate_id": "south",
                    "x_m": 0.2,
                    "y_m": -2.1,
                    "yaw_rad": 0.0,
                }
            ],
            obstacle={"x_m": 0.2, "y_m": -1.2},
        )


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


def test_ros2_nav2_bridge_cancels_in_flight_goal_after_result_timeout() -> None:
    source = Path("scripts/ros2_nav2_turtlebot4_bridge.py").read_text(
        encoding="utf-8"
    )

    assert "goal_handle.cancel_goal_async()" in source
    assert '"goal_cancel_requested": False' in source
    assert "nav2_recovery_orbit_detected" in source
    assert "recovery_position_tolerance_reached" in source
    assert "recovery_map_distance_to_goal_m" in source
    assert '"map_pose_confirmation_required": True' in source
    assert 'nav2_status = "position_tolerance_reached"' in source
    assert '"position_tolerance_with_confirmed_cancel"' in source
    assert '"nav2_goal_succeeded": nav2_succeeded' in source
    assert "nav2_recovery_position_tolerance_cancel_unconfirmed" in source
    assert "evaluate_recovery_candidates" in source
    assert "ComputePathToPose" in source
    assert "GetCostmap" in source
    assert '"recommended_arrival_yaw_rad"' in source
    assert 'item.get("selection_priority", 100)' in source
    assert '"goal_cancel_accepted": False' in source
    assert "nav2_goal_cancel_unconfirmed_after_timeout" in source
    assert "nav2_goal_cancel_result_not_observed" in source
    assert "bounded_inflation_escape_valid" in source
    assert "ROS2_NAV2_RECOVERY_TARGET_COST_THRESHOLD" in source
    assert "nav2_compute_path_did_not_reach_candidate_goal" in source
    assert "path_goal_error_m <= path_goal_tolerance_m" in source
    assert "opt_in_simulated_transient_nav2_cancel_after_accept" in source
    assert "sim_fault_cancel_after_accept" in source


def test_request_scoped_sim_fault_is_explicitly_added_to_goal_payload(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _run_bridge(**kwargs):
        captured.update(kwargs)
        return {
            "ack_status": "accepted",
            "physical_execution_invoked": False,
            "raw_velocity_published": False,
            "raw_ros_topic_published": False,
            "cmd_vel_published_by_missionos": False,
        }

    monkeypatch.setattr(dispatch_bridge_runtime, "_run_bridge", _run_bridge)
    client = Ros2Nav2BridgeCommandClient(
        env_overrides={
            ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV: "1"
        }
    )
    client.send_goal_pose(
        Nav2GoalPose(x_m=1.0, y_m=2.0, yaw_rad=0.0, label="fault-test")
    )

    assert captured["action"] == "send_goal_pose"
    assert captured["payload"]["sim_fault_cancel_after_accept"] is True


def test_recovery_target_cost_threshold_requires_margin_on_both_costmaps() -> None:
    import importlib.util

    path = Path("scripts/ros2_nav2_turtlebot4_bridge.py")
    spec = importlib.util.spec_from_file_location("missionos_nav2_bridge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._recovery_target_costs_safe(
        target_cost=149,
        local_target_cost=107,
        target_cost_threshold=180,
    )
    assert not module._recovery_target_costs_safe(
        target_cost=202,
        local_target_cost=77,
        target_cost_threshold=180,
    )
    assert module._bounded_inflation_escape_valid(
        candidate={
            "candidate_id": "obstacle_bypass_south",
            "selection_role": "route_lateral_bypass",
        },
        path_length_m=0.9,
        local_path_costs=[227, 210, 170, 90],
        local_current_cost=227,
        local_target_cost=90,
        local_cost_threshold=220,
        lethal_cost_threshold=253,
    )
    assert not module._bounded_inflation_escape_valid(
        candidate={
            "candidate_id": "obstacle_bypass_south",
            "selection_role": "route_lateral_bypass",
        },
        path_length_m=0.9,
        local_path_costs=[227, 240, 170, 90],
        local_current_cost=227,
        local_target_cost=90,
        local_cost_threshold=220,
        lethal_cost_threshold=253,
    )
    assert not module._recovery_target_costs_safe(
        target_cost=149,
        local_target_cost=202,
        target_cost_threshold=180,
    )


def test_recovery_costmap_freshness_waits_for_ros_sim_clock() -> None:
    import importlib.util

    path = Path("scripts/ros2_nav2_turtlebot4_bridge.py")
    spec = importlib.util.spec_from_file_location("missionos_nav2_bridge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock_samples = iter((0, 1_950_000_000, 2_010_000_000))
    current_clock_ns = 0
    spin_timeouts: list[float] = []

    def _read_clock_ns() -> int:
        nonlocal current_clock_ns
        current_clock_ns = next(clock_samples)
        return current_clock_ns

    observed_clock_ns = module._wait_for_clock_at_or_after_snapshot(
        read_clock_ns=_read_clock_ns,
        spin_once=spin_timeouts.append,
        maximum_snapshot_stamp_ns=2_000_000_000,
        timeout_s=0.5,
    )

    assert observed_clock_ns == 2_010_000_000
    assert len(spin_timeouts) == 2
    assert module._costmap_age_seconds(
        observed_clock_ns=observed_clock_ns,
        snapshot_stamp_ns=2_000_000_000,
    ) == 0.01


def test_recovery_costmap_freshness_remains_unverified_for_future_stamp() -> None:
    import importlib.util

    path = Path("scripts/ros2_nav2_turtlebot4_bridge.py")
    spec = importlib.util.spec_from_file_location("missionos_nav2_bridge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._costmap_age_seconds(
        observed_clock_ns=1_999_999_999,
        snapshot_stamp_ns=2_000_000_000,
    ) is None


def test_bounded_inflation_escape_only_allows_observed_short_retreat() -> None:
    import importlib.util

    path = Path("scripts/ros2_nav2_turtlebot4_bridge.py")
    spec = importlib.util.spec_from_file_location("missionos_nav2_bridge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    valid = module._bounded_inflation_escape_valid(
        candidate={
            "candidate_id": "observed_inbound_bounded_retreat",
            "retreat_distance_bound_m": 0.45,
        },
        path_length_m=0.44,
        local_path_costs=[227, 227, 210, 120],
        local_current_cost=227,
        local_target_cost=120,
        local_cost_threshold=220,
        lethal_cost_threshold=253,
    )
    arbitrary_bypass = module._bounded_inflation_escape_valid(
        candidate={
            "candidate_id": "obstacle_bypass_north",
            "retreat_distance_bound_m": 0.45,
        },
        path_length_m=0.44,
        local_path_costs=[227, 210, 120],
        local_current_cost=227,
        local_target_cost=120,
        local_cost_threshold=220,
        lethal_cost_threshold=253,
    )
    deeper_inflation = module._bounded_inflation_escape_valid(
        candidate={
            "candidate_id": "observed_inbound_bounded_retreat",
            "retreat_distance_bound_m": 0.45,
        },
        path_length_m=0.44,
        local_path_costs=[227, 240, 120],
        local_current_cost=227,
        local_target_cost=120,
        local_cost_threshold=220,
        lethal_cost_threshold=253,
    )
    excessive_detour = module._bounded_inflation_escape_valid(
        candidate={
            "candidate_id": "observed_inbound_bounded_retreat",
            "retreat_distance_bound_m": 0.45,
        },
        path_length_m=0.61,
        local_path_costs=[227, 210, 120],
        local_current_cost=227,
        local_target_cost=120,
        local_cost_threshold=220,
        lethal_cost_threshold=253,
    )

    assert valid is True
    assert arbitrary_bypass is False
    assert deeper_inflation is False
    assert excessive_detour is False


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


def test_ros2_nav2_turtlebot4_bridge_capture_camera_frame_is_gate_controlled(
    monkeypatch,
) -> None:
    monkeypatch.delenv("RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT4_BRIDGE", raising=False)
    request = {
        "schema_version": "missionos_ros2_nav2_bridge_request.v1",
        "action": "capture_camera_frame",
        "payload": {},
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


def test_ros2_nav2_turtlebot4_bridge_capture_camera_frame_reports_missing_deps(
    monkeypatch,
) -> None:
    """With the gate open but no ROS2 environment, capture blocks cleanly.

    This dev machine has no rclpy/sensor_msgs installed, which exercises the
    real dependency-missing path this bridge falls back to in that case —
    the same path a CI runner without ROS2 would hit.
    """

    monkeypatch.setenv("RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT4_BRIDGE", "1")
    request = {
        "schema_version": "missionos_ros2_nav2_bridge_request.v1",
        "action": "capture_camera_frame",
        "payload": {},
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
    assert "ros2_nav2_python_dependencies_missing" in response["blocking_reasons"]
    assert response["physical_execution_invoked"] is False
    assert response["raw_velocity_published"] is False
