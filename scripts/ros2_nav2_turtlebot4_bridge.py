#!/usr/bin/env python3
"""Opt-in ROS2 action bridge for TurtleBot4/Nav2 simulation.

This script is meant to run inside a ROS2 Humble + TurtleBot4 simulator
environment. It accepts a bounded MissionOS JSON request on stdin and sends a
Nav2 ``NavigateToPose`` action goal. It never publishes raw ``cmd_vel`` or
claims physical execution.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import sys
import time
from typing import Any


ROS2_NAV2_TURTLEBOT4_BRIDGE_ENV = "RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT4_BRIDGE"
ROS2_NAV2_TURTLEBOT3_BRIDGE_ENV = "RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE"
ROS2_NAV2_BRIDGE_PROFILE_ENV = "MISSIONOS_ROS2_NAV2_BRIDGE_PROFILE"
ROS2_NAV2_ACTION_NAME_ENV = "ROS2_NAV2_ACTION_NAME"
ROS2_NAV2_ODOM_TOPIC_ENV = "ROS2_NAV2_ODOM_TOPIC"
ROS2_NAV2_ACTION_SERVER_TIMEOUT_ENV = "ROS2_NAV2_ACTION_SERVER_TIMEOUT_S"
ROS2_NAV2_GOAL_RESULT_TIMEOUT_ENV = "ROS2_NAV2_GOAL_RESULT_TIMEOUT_S"
ROS2_NAV2_POST_RESULT_SETTLE_ENV = "ROS2_NAV2_POST_RESULT_SETTLE_S"
ROS2_NAV2_ODOM_TIMEOUT_ENV = "ROS2_NAV2_ODOM_TIMEOUT_S"
ROS2_NAV2_MOTION_THRESHOLD_ENV = "ROS2_NAV2_MOTION_THRESHOLD_M"
ROS2_NAV2_INITIALPOSE_ENABLE_ENV = "ROS2_NAV2_INITIALPOSE_ENABLE"
ROS2_NAV2_INITIALPOSE_TOPIC_ENV = "ROS2_NAV2_INITIALPOSE_TOPIC"
ROS2_NAV2_INITIALPOSE_FRAME_ID_ENV = "ROS2_NAV2_INITIALPOSE_FRAME_ID"
ROS2_NAV2_INITIALPOSE_X_M_ENV = "ROS2_NAV2_INITIALPOSE_X_M"
ROS2_NAV2_INITIALPOSE_Y_M_ENV = "ROS2_NAV2_INITIALPOSE_Y_M"
ROS2_NAV2_INITIALPOSE_YAW_RAD_ENV = "ROS2_NAV2_INITIALPOSE_YAW_RAD"
ROS2_NAV2_INITIALPOSE_TIMEOUT_ENV = "ROS2_NAV2_INITIALPOSE_TIMEOUT_S"
ROS2_NAV2_INITIALPOSE_PUBLISHES_ENV = "ROS2_NAV2_INITIALPOSE_PUBLISHES"
ROS2_NAV2_TF_READY_TIMEOUT_ENV = "ROS2_NAV2_TF_READY_TIMEOUT_S"
ROS2_NAV2_TF_READY_TARGET_FRAME_ENV = "ROS2_NAV2_TF_READY_TARGET_FRAME"
ROS2_NAV2_TF_READY_SOURCE_FRAME_ENV = "ROS2_NAV2_TF_READY_SOURCE_FRAME"
ROS2_NAV2_REQUIRE_TF_READY_ENV = "ROS2_NAV2_REQUIRE_TF_READY"
ROS2_NAV2_LIFECYCLE_READY_TIMEOUT_ENV = "ROS2_NAV2_LIFECYCLE_READY_TIMEOUT_S"
ROS2_NAV2_LIFECYCLE_NODES_ENV = "ROS2_NAV2_LIFECYCLE_NODES"
ROS2_NAV2_REQUIRE_LIFECYCLE_READY_ENV = "ROS2_NAV2_REQUIRE_LIFECYCLE_READY"
ROS2_NAV2_VELOCITY_OBSERVE_TOPICS_ENV = "ROS2_NAV2_VELOCITY_OBSERVE_TOPICS"
ROS2_NAV2_VELOCITY_OBSERVE_ENABLE_ENV = "ROS2_NAV2_VELOCITY_OBSERVE_ENABLE"
ROS2_NAV2_VELOCITY_THRESHOLD_ENV = "ROS2_NAV2_VELOCITY_THRESHOLD"
ROS2_NAV2_OBSTACLE_OBSERVE_ENABLE_ENV = "ROS2_NAV2_OBSTACLE_OBSERVE_ENABLE"
ROS2_NAV2_OBSTACLE_COSTMAP_TOPICS_ENV = "ROS2_NAV2_OBSTACLE_COSTMAP_TOPICS"
ROS2_NAV2_OBSTACLE_COST_THRESHOLD_ENV = "ROS2_NAV2_OBSTACLE_COST_THRESHOLD"
ROS2_NAV2_TRAJECTORY_OBSERVE_ENABLE_ENV = "ROS2_NAV2_TRAJECTORY_OBSERVE_ENABLE"
ROS2_NAV2_TRAJECTORY_LATERAL_DEVIATION_THRESHOLD_ENV = (
    "ROS2_NAV2_TRAJECTORY_LATERAL_DEVIATION_THRESHOLD_M"
)
ROS2_NAV2_COMPUTE_PATH_ACTION_ENV = "ROS2_NAV2_COMPUTE_PATH_ACTION"
ROS2_NAV2_GLOBAL_COSTMAP_SERVICE_ENV = "ROS2_NAV2_GLOBAL_COSTMAP_SERVICE"
ROS2_NAV2_LOCAL_COSTMAP_SERVICE_ENV = "ROS2_NAV2_LOCAL_COSTMAP_SERVICE"
ROS2_NAV2_RECOVERY_ORBIT_GUARD_ENABLE_ENV = (
    "ROS2_NAV2_RECOVERY_ORBIT_GUARD_ENABLE"
)
ROS2_NAV2_RECOVERY_ORBIT_MIN_DURATION_ENV = (
    "ROS2_NAV2_RECOVERY_ORBIT_MIN_DURATION_S"
)
ROS2_NAV2_RECOVERY_ORBIT_NO_PROGRESS_ENV = (
    "ROS2_NAV2_RECOVERY_ORBIT_NO_PROGRESS_S"
)
ROS2_NAV2_RECOVERY_ORBIT_MIN_PATH_ENV = (
    "ROS2_NAV2_RECOVERY_ORBIT_MIN_PATH_M"
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ALLOWED_ACTIONS = {
    "send_goal_pose",
    "cancel_goal",
    "read_state",
    "read_progress",
    "evaluate_recovery_candidates",
}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _base_response(**values: Any) -> dict[str, Any]:
    response = {
        "physical_execution_invoked": False,
        "raw_velocity_invoked": False,
        "raw_velocity_published": False,
        "raw_ros_topic_published": False,
        "cmd_vel_published_by_missionos": False,
    }
    response.update(values)
    return response


def _bridge_profile() -> str:
    raw = os.environ.get(ROS2_NAV2_BRIDGE_PROFILE_ENV, "").strip().lower()
    if raw in {"turtlebot3", "turtlebot4"}:
        return raw
    if _truthy_env(ROS2_NAV2_TURTLEBOT3_BRIDGE_ENV):
        return "turtlebot3"
    return "turtlebot4"


def _bridge_source() -> str:
    return f"ros2_nav2_{_bridge_profile()}_bridge"


def _bridge_enabled() -> bool:
    if _bridge_profile() == "turtlebot3":
        return _truthy_env(ROS2_NAV2_TURTLEBOT3_BRIDGE_ENV)
    return _truthy_env(ROS2_NAV2_TURTLEBOT4_BRIDGE_ENV)


def _blocked_response(*, blocking_reasons: list[str], ack_status: str = "rejected") -> dict[str, Any]:
    return _base_response(
        ack_status=ack_status,
        ack_source=_bridge_source(),
        runtime_progress_observed=False,
        completion_observed=False,
        nav2_status="blocked",
        state_result={
            "nav2_action_server_available": False,
            "pose_observed": False,
            "robot_motion_observed": False,
        },
        progress_result={
            "runtime_progress_observed": False,
            "completion_observed": False,
            "robot_motion_observed": False,
            "nav2_status": "blocked",
        },
        blocking_reasons=blocking_reasons,
    )


def _validate_request(request: dict[str, Any]) -> dict[str, Any] | None:
    if request.get("physical_execution_invoked") is True:
        return _blocked_response(
            blocking_reasons=["request_attempted_physical_execution_claim"],
        )
    if request.get("raw_velocity_allowed") is not False:
        return _blocked_response(blocking_reasons=["raw_velocity_not_allowed"])
    if request.get("raw_ros_topic_publication_allowed") is not False:
        return _blocked_response(
            blocking_reasons=["raw_ros_topic_publication_not_allowed"],
        )
    action = str(request.get("action") or "")
    if action not in _ALLOWED_ACTIONS:
        return _blocked_response(blocking_reasons=["unsupported_nav2_bridge_action"])
    return None


def _status_name(status: int, goal_status: Any) -> str:
    mapping = {
        goal_status.STATUS_UNKNOWN: "unknown",
        goal_status.STATUS_ACCEPTED: "accepted",
        goal_status.STATUS_EXECUTING: "executing",
        goal_status.STATUS_CANCELING: "canceling",
        goal_status.STATUS_SUCCEEDED: "succeeded",
        goal_status.STATUS_CANCELED: "canceled",
        goal_status.STATUS_ABORTED: "aborted",
    }
    return str(mapping.get(status, f"status_{status}"))


def _yaw_to_quaternion(yaw_rad: float) -> tuple[float, float, float, float]:
    half = yaw_rad / 2.0
    return (0.0, 0.0, math.sin(half), math.cos(half))


def _evaluate_recovery_candidates(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate bounded recovery targets without creating motion authority."""

    try:
        import rclpy
        from action_msgs.msg import GoalStatus
        from geometry_msgs.msg import PoseStamped
        from nav2_msgs.action import ComputePathToPose
        from nav2_msgs.srv import GetCostmap
        from rclpy.action import ActionClient
        from rclpy.time import Time
        from tf2_ros import Buffer, TransformListener
    except Exception as exc:
        return _blocked_response(
            blocking_reasons=["ros2_nav2_planning_dependencies_missing"],
        ) | {
            "schema_version": "missionos_nav2_recovery_candidate_evaluation.v1",
            "evaluation_status": "blocked",
            "dispatch_request_sent": False,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }

    raw_candidates = payload.get("candidates")
    raw_candidates = raw_candidates if isinstance(raw_candidates, list) else []
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates[:8]:
        if not isinstance(raw, dict):
            continue
        try:
            x_m = float(raw["x_m"])
            y_m = float(raw["y_m"])
            yaw_rad = float(raw.get("yaw_rad") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (x_m, y_m, yaw_rad)):
            continue
        candidate_id = str(raw.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        candidates.append(
            {
                **raw,
                "candidate_id": candidate_id,
                "x_m": x_m,
                "y_m": y_m,
                "yaw_rad": yaw_rad,
            }
        )
    if not candidates:
        return _blocked_response(
            blocking_reasons=["recovery_candidate_list_empty"],
        ) | {
            "schema_version": "missionos_nav2_recovery_candidate_evaluation.v1",
            "evaluation_status": "blocked",
            "dispatch_request_sent": False,
        }

    action_name = os.environ.get(
        ROS2_NAV2_COMPUTE_PATH_ACTION_ENV,
        "/compute_path_to_pose",
    )
    costmap_service_name = os.environ.get(
        ROS2_NAV2_GLOBAL_COSTMAP_SERVICE_ENV,
        "/global_costmap/get_costmap",
    )
    local_costmap_service_name = os.environ.get(
        ROS2_NAV2_LOCAL_COSTMAP_SERVICE_ENV,
        "/local_costmap/get_costmap",
    )
    server_timeout_s = _float_env(ROS2_NAV2_ACTION_SERVER_TIMEOUT_ENV, 10.0)
    frame_id = str(payload.get("frame_id") or "map")
    lethal_cost_threshold = _int_env("ROS2_NAV2_RECOVERY_LETHAL_COST_THRESHOLD", 253)
    local_cost_threshold = _int_env(
        "ROS2_NAV2_RECOVERY_LOCAL_COST_THRESHOLD",
        220,
    )

    rclpy.init(args=None)
    node = rclpy.create_node(
        f"missionos_ros2_nav2_{_bridge_profile()}_recovery_resolver"
    )
    try:
        def _read_costmap(service_name: str, label: str) -> dict[str, Any] | None:
            client = node.create_client(GetCostmap, service_name)
            if not client.wait_for_service(timeout_sec=server_timeout_s):
                return None
            future = client.call_async(GetCostmap.Request())
            rclpy.spin_until_future_complete(
                node,
                future,
                timeout_sec=server_timeout_s,
            )
            response = future.result() if future.done() else None
            if response is None:
                return None
            costmap = response.map
            metadata = costmap.metadata
            width = int(metadata.size_x)
            height = int(metadata.size_y)
            resolution = float(metadata.resolution)
            origin_x = float(metadata.origin.position.x)
            origin_y = float(metadata.origin.position.y)
            costs = bytes(int(value) & 0xFF for value in costmap.data)
            header = {
                "label": label,
                "frame_id": str(costmap.header.frame_id),
                "stamp_sec": int(costmap.header.stamp.sec),
                "stamp_nanosec": int(costmap.header.stamp.nanosec),
                "width": width,
                "height": height,
                "resolution": resolution,
                "origin_x": origin_x,
                "origin_y": origin_y,
            }
            snapshot_hash = hashlib.sha256(
                json.dumps(
                    header,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\0"
                + costs
            ).hexdigest()

            def _cost_at(x_m: float, y_m: float) -> int | None:
                if resolution <= 0:
                    return None
                cell_x = math.floor((x_m - origin_x) / resolution)
                cell_y = math.floor((y_m - origin_y) / resolution)
                if (
                    cell_x < 0
                    or cell_y < 0
                    or cell_x >= width
                    or cell_y >= height
                ):
                    return None
                index = cell_y * width + cell_x
                return int(costs[index]) if 0 <= index < len(costs) else None

            return {
                **header,
                "snapshot_hash": snapshot_hash,
                "service": service_name,
                "cost_at": _cost_at,
            }

        global_costmap = _read_costmap(costmap_service_name, "global")
        if global_costmap is None:
            return _blocked_response(
                blocking_reasons=["nav2_global_costmap_unavailable"],
            ) | {
                "schema_version": (
                    "missionos_nav2_recovery_candidate_evaluation.v1"
                ),
                "evaluation_status": "blocked",
                "dispatch_request_sent": False,
                "costmap_service": costmap_service_name,
            }
        local_costmap = _read_costmap(local_costmap_service_name, "local")
        if local_costmap is None:
            return _blocked_response(
                blocking_reasons=["nav2_local_costmap_unavailable"],
            ) | {
                "schema_version": (
                    "missionos_nav2_recovery_candidate_evaluation.v1"
                ),
                "evaluation_status": "blocked",
                "dispatch_request_sent": False,
                "global_costmap_snapshot_hash": global_costmap[
                    "snapshot_hash"
                ],
                "local_costmap_service": local_costmap_service_name,
            }
        costmap_snapshot_hash = str(global_costmap["snapshot_hash"])
        _cost_at = global_costmap["cost_at"]

        local_tf_buffer = Buffer()
        local_tf_listener = TransformListener(local_tf_buffer, node)  # noqa: F841
        local_frame = str(local_costmap["frame_id"] or frame_id)
        local_transform = None
        if local_frame != frame_id:
            transform_deadline = time.monotonic() + server_timeout_s
            while time.monotonic() < transform_deadline:
                try:
                    local_transform = local_tf_buffer.lookup_transform(
                        local_frame,
                        frame_id,
                        Time(),
                    )
                    break
                except Exception:
                    rclpy.spin_once(node, timeout_sec=0.1)
            if local_transform is None:
                return _blocked_response(
                    blocking_reasons=[
                        "nav2_local_costmap_frame_transform_unavailable"
                    ],
                ) | {
                    "schema_version": (
                        "missionos_nav2_recovery_candidate_evaluation.v1"
                    ),
                    "evaluation_status": "blocked",
                    "dispatch_request_sent": False,
                    "global_costmap_snapshot_hash": global_costmap[
                        "snapshot_hash"
                    ],
                    "local_costmap_snapshot_hash": local_costmap[
                        "snapshot_hash"
                    ],
                    "local_costmap_frame_id": local_frame,
                }

        def _to_local_frame(x_m: float, y_m: float) -> tuple[float, float] | None:
            if local_frame == frame_id:
                return (x_m, y_m)
            if local_transform is None:
                return None
            transform = local_transform
            translation = transform.transform.translation
            rotation = transform.transform.rotation
            yaw = math.atan2(
                2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
                1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
            )
            cos_yaw = math.cos(yaw)
            sin_yaw = math.sin(yaw)
            return (
                float(translation.x) + x_m * cos_yaw - y_m * sin_yaw,
                float(translation.y) + x_m * sin_yaw + y_m * cos_yaw,
            )

        planner_client = ActionClient(node, ComputePathToPose, action_name)
        if not planner_client.wait_for_server(timeout_sec=server_timeout_s):
            return _blocked_response(
                blocking_reasons=["nav2_compute_path_action_server_unavailable"],
            ) | {
                "schema_version": (
                    "missionos_nav2_recovery_candidate_evaluation.v1"
                ),
                "evaluation_status": "blocked",
                "dispatch_request_sent": False,
                "costmap_snapshot_hash": costmap_snapshot_hash,
                "compute_path_action": action_name,
            }

        evaluations: list[dict[str, Any]] = []
        for candidate in candidates:
            goal = ComputePathToPose.Goal()
            pose = PoseStamped()
            pose.header.frame_id = frame_id
            pose.header.stamp.sec = 0
            pose.header.stamp.nanosec = 0
            pose.pose.position.x = candidate["x_m"]
            pose.pose.position.y = candidate["y_m"]
            qx, qy, qz, qw = _yaw_to_quaternion(candidate["yaw_rad"])
            pose.pose.orientation.x = qx
            pose.pose.orientation.y = qy
            pose.pose.orientation.z = qz
            pose.pose.orientation.w = qw
            goal.goal = pose
            goal.use_start = False
            goal.planner_id = ""
            send_future = planner_client.send_goal_async(goal)
            rclpy.spin_until_future_complete(
                node,
                send_future,
                timeout_sec=server_timeout_s,
            )
            goal_handle = send_future.result() if send_future.done() else None
            if goal_handle is None or not goal_handle.accepted:
                evaluations.append(
                    {
                        **candidate,
                        "path_valid": False,
                        "planner_status": "goal_rejected",
                        "blocking_reasons": ["nav2_compute_path_goal_rejected"],
                    }
                )
                continue
            result_future = goal_handle.get_result_async()
            rclpy.spin_until_future_complete(
                node,
                result_future,
                timeout_sec=server_timeout_s,
            )
            wrapped_result = (
                result_future.result() if result_future.done() else None
            )
            if wrapped_result is None:
                evaluations.append(
                    {
                        **candidate,
                        "path_valid": False,
                        "planner_status": "timeout",
                        "blocking_reasons": ["nav2_compute_path_result_timeout"],
                    }
                )
                continue
            planner_status = _status_name(
                int(wrapped_result.status),
                GoalStatus,
            )
            path = wrapped_result.result.path
            path_points = [
                (
                    float(stamped.pose.position.x),
                    float(stamped.pose.position.y),
                )
                for stamped in path.poses
            ]
            path_length_m = sum(
                math.hypot(end_x - start_x, end_y - start_y)
                for (start_x, start_y), (end_x, end_y) in zip(
                    path_points,
                    path_points[1:],
                )
            )
            arrival_yaw_rad = candidate["yaw_rad"]
            if len(path_points) >= 2:
                end_x, end_y = path_points[-1]
                for prior_x, prior_y in reversed(path_points[:-1]):
                    delta_x = end_x - prior_x
                    delta_y = end_y - prior_y
                    if math.hypot(delta_x, delta_y) >= 0.05:
                        arrival_yaw_rad = math.atan2(delta_y, delta_x)
                        break
            path_costs = [
                cost
                for point in path_points
                if (cost := _cost_at(*point)) is not None
            ]
            target_cost = _cost_at(candidate["x_m"], candidate["y_m"])
            maximum_path_cost = max(path_costs) if path_costs else None
            local_path_points = [
                local_point
                for point in path_points
                if (local_point := _to_local_frame(*point)) is not None
            ]
            local_path_costs = [
                cost
                for point in local_path_points
                if (cost := local_costmap["cost_at"](*point)) is not None
            ]
            local_current_cost = (
                local_costmap["cost_at"](*local_path_points[0])
                if local_path_points
                else None
            )
            local_target_point = _to_local_frame(
                candidate["x_m"],
                candidate["y_m"],
            )
            local_target_cost = (
                local_costmap["cost_at"](*local_target_point)
                if local_target_point is not None
                else None
            )
            local_maximum_path_cost = (
                max(local_path_costs) if local_path_costs else None
            )
            path_sha256 = hashlib.sha256(
                json.dumps(
                    [[round(x_m, 6), round(y_m, 6)] for x_m, y_m in path_points],
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            path_valid = (
                planner_status == "succeeded"
                and bool(path_points)
                and target_cost is not None
                and target_cost < lethal_cost_threshold
                and maximum_path_cost is not None
                and maximum_path_cost < lethal_cost_threshold
                and local_current_cost is not None
                and local_current_cost < lethal_cost_threshold
                and len(local_path_costs) >= 2
                and local_maximum_path_cost is not None
                and local_maximum_path_cost < local_cost_threshold
            )
            evaluation_reasons: list[str] = []
            if planner_status != "succeeded":
                evaluation_reasons.append("nav2_compute_path_not_succeeded")
            if not path_points:
                evaluation_reasons.append("nav2_compute_path_empty")
            if target_cost is None:
                evaluation_reasons.append("recovery_target_outside_costmap")
            elif target_cost >= lethal_cost_threshold:
                evaluation_reasons.append("recovery_target_cost_lethal")
            if maximum_path_cost is None:
                evaluation_reasons.append("recovery_path_cost_unavailable")
            elif maximum_path_cost >= lethal_cost_threshold:
                evaluation_reasons.append("recovery_path_crosses_lethal_cost")
            if local_current_cost is None:
                evaluation_reasons.append(
                    "recovery_current_pose_outside_local_costmap"
                )
            elif local_current_cost >= lethal_cost_threshold:
                evaluation_reasons.append("recovery_current_pose_local_cost_lethal")
            if local_maximum_path_cost is None:
                evaluation_reasons.append("recovery_local_path_cost_unavailable")
            elif len(local_path_costs) < 2:
                evaluation_reasons.append(
                    "recovery_local_path_prefix_insufficient"
                )
            elif local_maximum_path_cost >= local_cost_threshold:
                evaluation_reasons.append(
                    "recovery_local_path_exceeds_controller_cost_threshold"
                )
            evaluations.append(
                {
                    **candidate,
                    "path_valid": path_valid,
                    "planner_status": planner_status,
                    "target_cost": target_cost,
                    "maximum_path_cost": maximum_path_cost,
                    "local_current_cost": local_current_cost,
                    "local_target_cost": local_target_cost,
                    "local_maximum_path_cost": local_maximum_path_cost,
                    "local_path_pose_count": len(local_path_costs),
                    "local_cost_threshold": local_cost_threshold,
                    "path_pose_count": len(path_points),
                    "path_length_m": round(path_length_m, 6),
                    "path_sha256": path_sha256,
                    "recommended_arrival_yaw_rad": round(arrival_yaw_rad, 6),
                    "arrival_heading_source": "compute_path_final_tangent",
                    "blocking_reasons": evaluation_reasons,
                }
            )

        valid = [
            item
            for item in evaluations
            if item.get("path_valid") is True
            and item.get("sequence_only") is not True
        ]
        valid.sort(
            key=lambda item: (
                int(item.get("local_maximum_path_cost") or 0),
                int(item.get("selection_priority", 100)),
                int(item.get("maximum_path_cost") or 0),
                float(item.get("path_length_m") or math.inf),
                str(item.get("candidate_id") or ""),
            )
        )
        selected = valid[0] if valid else None
        blocking_reasons = [] if selected else ["no_valid_recovery_candidate"]
        return _base_response(
            schema_version="missionos_nav2_recovery_candidate_evaluation.v1",
            evaluation_status="validated" if selected else "blocked",
            candidate_evaluations=evaluations,
            selected_candidate=selected,
            costmap_snapshot_hash=costmap_snapshot_hash,
            costmap_source=costmap_service_name,
            global_costmap_snapshot_hash=global_costmap["snapshot_hash"],
            global_costmap_source=costmap_service_name,
            local_costmap_snapshot_hash=local_costmap["snapshot_hash"],
            local_costmap_source=local_costmap_service_name,
            local_costmap_frame_id=local_costmap["frame_id"],
            local_cost_threshold=local_cost_threshold,
            compute_path_action=action_name,
            dispatch_request_sent=False,
            dispatch_authority_created=False,
            command_ack_observed=False,
            completion_claimed=False,
            runtime_progress_observed=False,
            completion_observed=False,
            nav2_status="not_requested",
            blocking_reasons=blocking_reasons,
        )
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _publish_initial_pose(
    *,
    node: Any,
    pose_type: Any,
    topic: str,
    frame_id: str,
    x_m: float,
    y_m: float,
    yaw_rad: float,
    timeout_s: float,
    publishes: int,
) -> dict[str, Any]:
    publisher = node.create_publisher(pose_type, topic, 10)
    deadline = time.monotonic() + max(timeout_s, 0.0)
    try:
        while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            import rclpy

            rclpy.spin_once(node, timeout_sec=0.1)

        qx, qy, qz, qw = _yaw_to_quaternion(yaw_rad)
        covariance = [0.0] * 36
        covariance[0] = 0.25
        covariance[7] = 0.25
        covariance[35] = 0.06853892326654787
        message = pose_type()
        message.header.frame_id = frame_id
        # Use latest transform time. The simulator runs on ROS time, while this
        # bridge node may still use wall time.
        message.header.stamp.sec = 0
        message.header.stamp.nanosec = 0
        message.pose.pose.position.x = x_m
        message.pose.pose.position.y = y_m
        message.pose.pose.position.z = 0.0
        message.pose.pose.orientation.x = qx
        message.pose.pose.orientation.y = qy
        message.pose.pose.orientation.z = qz
        message.pose.pose.orientation.w = qw
        message.pose.covariance = covariance

        publish_count = max(publishes, 1)
        for _ in range(publish_count):
            publisher.publish(message)
            import rclpy

            rclpy.spin_once(node, timeout_sec=0.1)
        return {
            "initialpose_enabled": True,
            "initialpose_topic": topic,
            "initialpose_frame_id": frame_id,
            "initialpose_stamp_zero": True,
            "initialpose_subscription_observed": publisher.get_subscription_count() > 0,
            "initialpose_publish_count": publish_count,
        }
    finally:
        node.destroy_publisher(publisher)


def _wait_for_transform(
    *,
    node: Any,
    buffer_type: Any,
    listener_type: Any,
    time_type: Any,
    target_frame: str,
    source_frame: str,
    timeout_s: float,
) -> dict[str, Any]:
    buffer = buffer_type()
    listeners = [listener_type(buffer, node)]
    listener_profiles = ["default"]
    try:
        from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

        dynamic_qos = QoSProfile(depth=100)
        dynamic_qos.reliability = QoSReliabilityPolicy.RELIABLE
        dynamic_qos.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
        static_qos = QoSProfile(depth=100)
        static_qos.reliability = QoSReliabilityPolicy.RELIABLE
        static_qos.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
        listeners.append(
            listener_type(
                buffer,
                node,
                qos=dynamic_qos,
                static_qos=static_qos,
            )
        )
        listener_profiles.append("transient_local")
    except TypeError:
        pass
    deadline = time.monotonic() + max(timeout_s, 0.0)
    ready = False
    error: str | None = None
    while time.monotonic() < deadline:
        import rclpy

        rclpy.spin_once(node, timeout_sec=0.1)
        try:
            ready = bool(buffer.can_transform(target_frame, source_frame, time_type()))
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
            ready = False
        if ready:
            error = None
            break
    return {
        "tf_ready": ready,
        "tf_target_frame": target_frame,
        "tf_source_frame": source_frame,
        "tf_wait_timeout_s": timeout_s,
        "tf_error": error,
        "tf_listener_profiles": listener_profiles,
    }


def _lifecycle_node_names() -> tuple[str, ...]:
    raw = os.environ.get(
        ROS2_NAV2_LIFECYCLE_NODES_ENV,
        "amcl,map_server,controller_server,planner_server,bt_navigator",
    )
    return tuple(name.strip().strip("/") for name in raw.split(",") if name.strip())


def _wait_for_lifecycle_nodes(
    *,
    node: Any,
    get_state_type: Any,
    node_names: tuple[str, ...],
    timeout_s: float,
) -> dict[str, Any]:
    import rclpy

    deadline = time.monotonic() + max(timeout_s, 0.0)
    clients = {
        name: node.create_client(get_state_type, f"/{name}/get_state")
        for name in node_names
    }
    states: dict[str, str] = {name: "not_checked" for name in node_names}
    try:
        while time.monotonic() < deadline:
            all_active = True
            for name, client in clients.items():
                if not client.service_is_ready() and not client.wait_for_service(
                    timeout_sec=0.1
                ):
                    states[name] = "service_unavailable"
                    all_active = False
                    continue
                future = client.call_async(get_state_type.Request())
                call_deadline = time.monotonic() + min(1.0, max(0.1, timeout_s))
                while (
                    rclpy.ok()
                    and not future.done()
                    and time.monotonic() < call_deadline
                ):
                    rclpy.spin_once(node, timeout_sec=0.05)
                if not future.done():
                    states[name] = "state_timeout"
                    all_active = False
                    continue
                response = future.result()
                label = str(response.current_state.label or "").lower()
                states[name] = label or f"id_{response.current_state.id}"
                if label != "active":
                    all_active = False
            if all_active:
                break
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        for client in clients.values():
            node.destroy_client(client)
    return {
        "lifecycle_ready": bool(node_names) and all(
            state == "active" for state in states.values()
        ),
        "lifecycle_wait_timeout_s": timeout_s,
        "lifecycle_nodes": list(node_names),
        "lifecycle_states": states,
    }


def _observe_odom_pose(
    *,
    node: Any,
    odometry_type: Any,
    topic: str,
    timeout_s: float,
) -> tuple[float, float] | None:
    from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

    observed: list[tuple[float, float]] = []

    def _handler(message: Any) -> None:
        pose = message.pose.pose
        observed.append((float(pose.position.x), float(pose.position.y)))

    qos = QoSProfile(depth=10)
    qos.reliability = QoSReliabilityPolicy.RELIABLE
    qos.durability = QoSDurabilityPolicy.VOLATILE
    subscription = node.create_subscription(odometry_type, topic, _handler, qos)
    deadline = time.monotonic() + max(timeout_s, 0.0)
    try:
        while not observed and time.monotonic() < deadline:
            import rclpy

            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_subscription(subscription)
    return observed[-1] if observed else None


def _motion_delta_m(
    before: tuple[float, float] | None,
    after: tuple[float, float] | None,
) -> float | None:
    if before is None or after is None:
        return None
    return math.hypot(after[0] - before[0], after[1] - before[1])


def _velocity_observe_topics() -> tuple[str, ...]:
    raw = os.environ.get(
        ROS2_NAV2_VELOCITY_OBSERVE_TOPICS_ENV,
        "/cmd_vel_nav,/cmd_vel,/diffdrive_controller/cmd_vel_unstamped",
    )
    return tuple(topic.strip() for topic in raw.split(",") if topic.strip())


def _start_velocity_observers(
    *,
    node: Any,
    twist_type: Any,
    topics: tuple[str, ...],
    threshold: float,
) -> tuple[list[Any], dict[str, dict[str, Any]]]:
    from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

    observations: dict[str, dict[str, Any]] = {
        topic: {
            "message_count": 0,
            "nonzero_count": 0,
            "max_abs_linear_x": 0.0,
            "max_abs_angular_z": 0.0,
        }
        for topic in topics
    }

    def _handler(topic: str) -> Any:
        def _inner(message: Any) -> None:
            linear_x = abs(float(message.linear.x))
            angular_z = abs(float(message.angular.z))
            data = observations[topic]
            data["message_count"] += 1
            data["max_abs_linear_x"] = max(data["max_abs_linear_x"], linear_x)
            data["max_abs_angular_z"] = max(data["max_abs_angular_z"], angular_z)
            if linear_x >= threshold or angular_z >= threshold:
                data["nonzero_count"] += 1

        return _inner

    volatile_qos = QoSProfile(depth=20)
    volatile_qos.reliability = QoSReliabilityPolicy.RELIABLE
    volatile_qos.durability = QoSDurabilityPolicy.VOLATILE

    subscriptions: list[Any] = []
    for topic in topics:
        subscriptions.append(
            node.create_subscription(
                twist_type,
                topic,
                _handler(topic),
                volatile_qos,
            )
        )
    return subscriptions, observations


def _finish_velocity_observers(
    *,
    node: Any,
    subscriptions: list[Any],
    observations: dict[str, dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    for subscription in subscriptions:
        node.destroy_subscription(subscription)
    total_messages = sum(int(value["message_count"]) for value in observations.values())
    total_nonzero = sum(int(value["nonzero_count"]) for value in observations.values())
    return {
        "velocity_observe_enabled": True,
        "velocity_threshold": threshold,
        "velocity_message_observed": total_messages > 0,
        "nonzero_velocity_observed": total_nonzero > 0,
        "topics": observations,
    }


def _obstacle_costmap_topics() -> tuple[str, ...]:
    raw = os.environ.get(
        ROS2_NAV2_OBSTACLE_COSTMAP_TOPICS_ENV,
        "/local_costmap/costmap,/global_costmap/costmap",
    )
    return tuple(topic.strip() for topic in raw.split(",") if topic.strip())


def _start_costmap_observers(
    *,
    node: Any,
    occupancy_grid_type: Any,
    topics: tuple[str, ...],
    threshold: int,
) -> tuple[list[Any], dict[str, dict[str, Any]]]:
    from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

    observations: dict[str, dict[str, Any]] = {
        topic: {
            "message_count": 0,
            "occupied_cell_count": 0,
            "max_occupied_cell_count": 0,
            "max_cost": None,
            "obstacle_observed": False,
        }
        for topic in topics
    }

    def _handler(topic: str) -> Any:
        def _inner(message: Any) -> None:
            data = observations[topic]
            costs = [int(value) for value in message.data if int(value) >= 0]
            occupied_count = sum(1 for value in costs if value >= threshold)
            max_cost = max(costs) if costs else None
            data["message_count"] += 1
            data["occupied_cell_count"] = occupied_count
            data["max_occupied_cell_count"] = max(
                int(data["max_occupied_cell_count"]),
                occupied_count,
            )
            if max_cost is not None:
                prior = data["max_cost"]
                data["max_cost"] = max(max_cost, int(prior)) if prior is not None else max_cost
            if occupied_count > 0:
                data["obstacle_observed"] = True

        return _inner

    qos = QoSProfile(depth=5)
    qos.reliability = QoSReliabilityPolicy.RELIABLE
    qos.durability = QoSDurabilityPolicy.VOLATILE

    subscriptions: list[Any] = []
    for topic in topics:
        subscriptions.append(
            node.create_subscription(
                occupancy_grid_type,
                topic,
                _handler(topic),
                qos,
            )
        )
    return subscriptions, observations


def _finish_costmap_observers(
    *,
    node: Any,
    subscriptions: list[Any],
    observations: dict[str, dict[str, Any]],
    threshold: int,
) -> dict[str, Any]:
    for subscription in subscriptions:
        node.destroy_subscription(subscription)
    total_messages = sum(int(value["message_count"]) for value in observations.values())
    observed_topics = [
        topic for topic, value in observations.items() if value["obstacle_observed"]
    ]
    return {
        "obstacle_observe_enabled": True,
        "cost_threshold": threshold,
        "costmap_message_observed": total_messages > 0,
        "costmap_obstacle_observed": bool(observed_topics),
        "observed_topics": observed_topics,
        "topics": observations,
    }


def _start_trajectory_observer(
    *,
    node: Any,
    odometry_type: Any,
    topic: str,
    tf_buffer: Any | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Record trajectory samples, preferring the AMCL-corrected map frame.

    Raw wheel odometry accumulates yaw error during Nav2 spin recoveries, so
    an odom-frame trail can rotate away from the real path even on a
    successful run. When a tf buffer is supplied, each sample is transformed
    with the latest map->odom correction; samples fall back to the odom frame
    when the transform is unavailable.
    """

    from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

    samples: list[dict[str, Any]] = []

    def _map_transform(frame_id: str) -> tuple[float, float, float] | None:
        if tf_buffer is None:
            return None
        try:
            from rclpy.time import Time

            transform = tf_buffer.lookup_transform("map", frame_id, Time())
        except Exception:
            return None
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        yaw = math.atan2(
            2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
            1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
        )
        return (float(translation.x), float(translation.y), yaw)

    def _handler(message: Any) -> None:
        pose = message.pose.pose
        x_m = float(pose.position.x)
        y_m = float(pose.position.y)
        frame_id = str(message.header.frame_id or "odom")
        transform = _map_transform(frame_id)
        if transform is None:
            samples.append(
                {"x_m": x_m, "y_m": y_m, "frame_id": frame_id, "odom_x_m": x_m, "odom_y_m": y_m}
            )
            return
        tx, ty, yaw = transform
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)
        samples.append(
            {
                "x_m": tx + x_m * cos_yaw - y_m * sin_yaw,
                "y_m": ty + x_m * sin_yaw + y_m * cos_yaw,
                "frame_id": "map",
                "odom_x_m": x_m,
                "odom_y_m": y_m,
            }
        )

    qos = QoSProfile(depth=100)
    qos.reliability = QoSReliabilityPolicy.RELIABLE
    qos.durability = QoSDurabilityPolicy.VOLATILE
    subscription = node.create_subscription(odometry_type, topic, _handler, qos)
    return subscription, samples


def _point_line_distance_m(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    x0, y0 = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(x0 - x1, y0 - y1)
    t = max(0.0, min(1.0, ((x0 - x1) * dx + (y0 - y1) * dy) / length_sq))
    projection = (x1 + t * dx, y1 + t * dy)
    return math.hypot(x0 - projection[0], y0 - projection[1])


def _finish_trajectory_observer(
    *,
    node: Any,
    subscription: Any | None,
    samples: list[dict[str, Any]],
    start: tuple[float, float] | None,
    end: tuple[float, float] | None,
    threshold_m: float,
) -> dict[str, Any]:
    if subscription is not None:
        node.destroy_subscription(subscription)
    max_deviation = None
    odom_points = [
        (float(sample["odom_x_m"]), float(sample["odom_y_m"])) for sample in samples
    ]
    if start is not None and end is not None and odom_points:
        # Deviation is measured against the odom-frame start/end readbacks, so
        # keep the comparison in the odom frame regardless of display frame.
        max_deviation = max(
            _point_line_distance_m(point, start, end) for point in odom_points
        )
    map_sample_count = sum(1 for sample in samples if sample["frame_id"] == "map")
    return {
        "trajectory_observe_enabled": True,
        "trajectory_sample_count": len(samples),
        "trajectory_frame": (
            "map" if samples and map_sample_count == len(samples) else
            "mixed" if map_sample_count else "odom_local"
        ),
        "trajectory_map_sample_count": map_sample_count,
        "trajectory_samples": [
            {
                "x_m": sample["x_m"],
                "y_m": sample["y_m"],
                "frame_id": sample["frame_id"],
                "sample_index": index,
            }
            for index, sample in enumerate(samples)
        ],
        "lateral_deviation_threshold_m": threshold_m,
        "max_lateral_deviation_m": max_deviation,
        "trajectory_lateral_deviation_observed": (
            max_deviation is not None and max_deviation >= threshold_m
        ),
        "trajectory_start_observed": start is not None,
        "trajectory_end_observed": end is not None,
    }


def _send_goal_pose(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        import rclpy
        from action_msgs.msg import GoalStatus
        from geometry_msgs.msg import PoseWithCovarianceStamped
        from geometry_msgs.msg import PoseStamped
        from geometry_msgs.msg import Twist
        from lifecycle_msgs.srv import GetState
        from nav2_msgs.action import NavigateToPose
        from nav_msgs.msg import Odometry
        from nav_msgs.msg import OccupancyGrid
        from rclpy.action import ActionClient
        from rclpy.time import Time
        from tf2_ros import Buffer, TransformListener
    except Exception as exc:
        return _blocked_response(
            blocking_reasons=["ros2_nav2_python_dependencies_missing"],
            ack_status="rejected",
        ) | {"error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    action_name = os.environ.get(ROS2_NAV2_ACTION_NAME_ENV, "/navigate_to_pose")
    odom_topic = os.environ.get(ROS2_NAV2_ODOM_TOPIC_ENV, "/odom")
    server_timeout_s = _float_env(ROS2_NAV2_ACTION_SERVER_TIMEOUT_ENV, 10.0)
    goal_timeout_s = _float_env(ROS2_NAV2_GOAL_RESULT_TIMEOUT_ENV, 45.0)
    post_result_settle_s = _float_env(ROS2_NAV2_POST_RESULT_SETTLE_ENV, 0.0)
    odom_timeout_s = _float_env(ROS2_NAV2_ODOM_TIMEOUT_ENV, 2.0)
    motion_threshold_m = _float_env(ROS2_NAV2_MOTION_THRESHOLD_ENV, 0.03)
    initialpose_result = {"initialpose_enabled": False}
    tf_ready_timeout_s = _float_env(ROS2_NAV2_TF_READY_TIMEOUT_ENV, 0.0)
    tf_ready_result = {
        "tf_ready": False,
        "tf_wait_timeout_s": tf_ready_timeout_s,
    }
    lifecycle_ready_timeout_s = _float_env(ROS2_NAV2_LIFECYCLE_READY_TIMEOUT_ENV, 0.0)
    lifecycle_result = {
        "lifecycle_ready": False,
        "lifecycle_wait_timeout_s": lifecycle_ready_timeout_s,
    }
    velocity_result = {"velocity_observe_enabled": False}
    obstacle_result = {"obstacle_observe_enabled": False}
    trajectory_result = {"trajectory_observe_enabled": False}

    rclpy.init(args=None)
    node = rclpy.create_node(f"missionos_ros2_nav2_{_bridge_profile()}_bridge")
    feedback_count = 0
    last_distance_remaining_m: float | None = None
    odom_before: tuple[float, float] | None = None
    odom_after: tuple[float, float] | None = None
    try:
        client = ActionClient(node, NavigateToPose, action_name)
        if not client.wait_for_server(timeout_sec=server_timeout_s):
            return _blocked_response(
                blocking_reasons=["nav2_navigate_to_pose_action_server_unavailable"],
                ack_status="timeout",
            ) | {
                "action_name": action_name,
                "odom_topic": odom_topic,
                "odom_before_observed": odom_before is not None,
        }

        goal_frame_id = str(payload.get("frame_id") or "map")
        if _truthy_env(ROS2_NAV2_INITIALPOSE_ENABLE_ENV):
            initialpose_result = _publish_initial_pose(
                node=node,
                pose_type=PoseWithCovarianceStamped,
                topic=os.environ.get(ROS2_NAV2_INITIALPOSE_TOPIC_ENV, "/initialpose"),
                frame_id=os.environ.get(ROS2_NAV2_INITIALPOSE_FRAME_ID_ENV, "map"),
                x_m=_float_env(ROS2_NAV2_INITIALPOSE_X_M_ENV, 0.0),
                y_m=_float_env(ROS2_NAV2_INITIALPOSE_Y_M_ENV, 0.0),
                yaw_rad=_float_env(ROS2_NAV2_INITIALPOSE_YAW_RAD_ENV, 0.0),
                timeout_s=_float_env(ROS2_NAV2_INITIALPOSE_TIMEOUT_ENV, 8.0),
                publishes=_int_env(ROS2_NAV2_INITIALPOSE_PUBLISHES_ENV, 5),
            )

        if tf_ready_timeout_s > 0:
            tf_ready_result = _wait_for_transform(
                node=node,
                buffer_type=Buffer,
                listener_type=TransformListener,
                time_type=Time,
                target_frame=os.environ.get(
                    ROS2_NAV2_TF_READY_TARGET_FRAME_ENV,
                    goal_frame_id,
                ),
                source_frame=os.environ.get(
                    ROS2_NAV2_TF_READY_SOURCE_FRAME_ENV,
                    "base_link",
                ),
                timeout_s=tf_ready_timeout_s,
            )
        if _truthy_env(ROS2_NAV2_REQUIRE_TF_READY_ENV) and not tf_ready_result.get(
            "tf_ready"
        ):
            readiness = {
                **lifecycle_result,
                **initialpose_result,
                **tf_ready_result,
                "nav2_action_server_available": True,
            }
            return _base_response(
                ack_status="rejected",
                ack_source=_bridge_source(),
                runtime_progress_observed=False,
                completion_observed=False,
                nav2_status="tf_not_ready",
                state_result={
                    "nav2_action_server_available": True,
                    "pose_observed": False,
                    "robot_motion_observed": False,
                    "odom_topic": odom_topic,
                    "odom_before_observed": False,
                    "odom_after_observed": False,
                    "odom_delta_m": None,
                    "readiness_result": readiness,
                },
                progress_result={
                    "runtime_progress_observed": False,
                    "completion_observed": False,
                    "robot_motion_observed": False,
                    "nav2_status": "tf_not_ready",
                    "readiness_result": readiness,
                },
                blocking_reasons=["nav2_tf_ready_timeout"],
                action_name=action_name,
                goal_accepted=False,
                readiness_result=readiness,
            )

        if lifecycle_ready_timeout_s > 0:
            lifecycle_result = _wait_for_lifecycle_nodes(
                node=node,
                get_state_type=GetState,
                node_names=_lifecycle_node_names(),
                timeout_s=lifecycle_ready_timeout_s,
            )
        if _truthy_env(ROS2_NAV2_REQUIRE_LIFECYCLE_READY_ENV) and not lifecycle_result.get(
            "lifecycle_ready"
        ):
            readiness = {
                **lifecycle_result,
                **initialpose_result,
                **tf_ready_result,
                "nav2_action_server_available": True,
            }
            return _base_response(
                ack_status="rejected",
                ack_source=_bridge_source(),
                runtime_progress_observed=False,
                completion_observed=False,
                nav2_status="lifecycle_not_ready",
                state_result={
                    "nav2_action_server_available": True,
                    "pose_observed": False,
                    "robot_motion_observed": False,
                    "odom_topic": odom_topic,
                    "odom_before_observed": False,
                    "odom_after_observed": False,
                    "odom_delta_m": None,
                    "readiness_result": readiness,
                },
                progress_result={
                    "runtime_progress_observed": False,
                    "completion_observed": False,
                    "robot_motion_observed": False,
                    "nav2_status": "lifecycle_not_ready",
                    "readiness_result": readiness,
                },
                blocking_reasons=["nav2_lifecycle_not_ready"],
                action_name=action_name,
                goal_accepted=False,
                readiness_result=readiness,
            )

        odom_before = _observe_odom_pose(
            node=node,
            odometry_type=Odometry,
            topic=odom_topic,
            timeout_s=odom_timeout_s,
        )
        goal = NavigateToPose.Goal()
        pose = PoseStamped()
        pose.header.frame_id = goal_frame_id
        pose.header.stamp.sec = 0
        pose.header.stamp.nanosec = 0
        pose.pose.position.x = float(payload["x_m"])
        pose.pose.position.y = float(payload["y_m"])
        qx, qy, qz, qw = _yaw_to_quaternion(float(payload.get("yaw_rad") or 0.0))
        pose.pose.orientation.x = qx
        pose.pose.orientation.y = qy
        pose.pose.orientation.z = qz
        pose.pose.orientation.w = qw
        goal.pose = pose

        feedback_best_distance_m: float | None = None
        feedback_last_improvement_at = time.monotonic()
        path_length_at_last_improvement_m = 0.0
        trajectory_path_length_m = 0.0

        def _feedback_handler(message: Any) -> None:
            nonlocal feedback_best_distance_m
            nonlocal feedback_count
            nonlocal feedback_last_improvement_at
            nonlocal last_distance_remaining_m
            nonlocal path_length_at_last_improvement_m
            feedback_count += 1
            distance = getattr(message.feedback, "distance_remaining", None)
            if distance is not None:
                last_distance_remaining_m = float(distance)
                if (
                    feedback_best_distance_m is None
                    or last_distance_remaining_m
                    <= feedback_best_distance_m - 0.1
                ):
                    feedback_best_distance_m = last_distance_remaining_m
                    feedback_last_improvement_at = time.monotonic()
                    path_length_at_last_improvement_m = trajectory_path_length_m

        send_future = client.send_goal_async(goal, feedback_callback=_feedback_handler)
        rclpy.spin_until_future_complete(node, send_future, timeout_sec=server_timeout_s)
        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            state_result = {
                "nav2_action_server_available": True,
                "pose_observed": odom_before is not None,
                "robot_motion_observed": False,
                "odom_topic": odom_topic,
                "odom_before_observed": odom_before is not None,
                "odom_after_observed": False,
                "odom_delta_m": None,
                "readiness_result": {
                    **lifecycle_result,
                    **initialpose_result,
                    **tf_ready_result,
                    "goal_stamp_zero": True,
                },
            }
            progress_result = {
                "runtime_progress_observed": False,
                "completion_observed": False,
                "robot_motion_observed": False,
                "nav2_status": "goal_rejected",
                "feedback_count": feedback_count,
                "last_distance_remaining_m": last_distance_remaining_m,
                "readiness_result": {
                    **lifecycle_result,
                    **initialpose_result,
                    **tf_ready_result,
                    "goal_stamp_zero": True,
                },
            }
            return _base_response(
                ack_status="rejected",
                ack_source=_bridge_source(),
                runtime_progress_observed=False,
                completion_observed=False,
                nav2_status="goal_rejected",
                state_result=state_result,
                progress_result=progress_result,
                blocking_reasons=["nav2_goal_rejected"],
                action_name=action_name,
                goal_accepted=False,
                odom_before_observed=odom_before is not None,
                readiness_result={
                    **lifecycle_result,
                    **initialpose_result,
                    **tf_ready_result,
                    "goal_stamp_zero": True,
                },
            )

        result_future = goal_handle.get_result_async()
        velocity_subscriptions: list[Any] = []
        if _truthy_env(ROS2_NAV2_VELOCITY_OBSERVE_ENABLE_ENV):
            velocity_subscriptions, velocity_observations = _start_velocity_observers(
                node=node,
                twist_type=Twist,
                topics=_velocity_observe_topics(),
                threshold=_float_env(ROS2_NAV2_VELOCITY_THRESHOLD_ENV, 0.001),
            )
        else:
            velocity_observations = {}
        obstacle_subscriptions: list[Any] = []
        if _truthy_env(ROS2_NAV2_OBSTACLE_OBSERVE_ENABLE_ENV):
            obstacle_subscriptions, obstacle_observations = _start_costmap_observers(
                node=node,
                occupancy_grid_type=OccupancyGrid,
                topics=_obstacle_costmap_topics(),
                threshold=_int_env(ROS2_NAV2_OBSTACLE_COST_THRESHOLD_ENV, 65),
            )
        else:
            obstacle_observations = {}
        trajectory_subscription: Any | None = None
        trajectory_samples: list[dict[str, Any]] = []
        trajectory_tf_buffer = None
        trajectory_tf_listener = None
        if _truthy_env(ROS2_NAV2_TRAJECTORY_OBSERVE_ENABLE_ENV) or _truthy_env(
            ROS2_NAV2_OBSTACLE_OBSERVE_ENABLE_ENV
        ):
            try:
                trajectory_tf_buffer = Buffer()
                # Held for the goal's lifetime so the buffer keeps receiving
                # map->odom corrections while samples arrive.
                trajectory_tf_listener = TransformListener(  # noqa: F841
                    trajectory_tf_buffer, node
                )
            except Exception:
                trajectory_tf_buffer = None
            trajectory_subscription, trajectory_samples = _start_trajectory_observer(
                node=node,
                odometry_type=Odometry,
                topic=odom_topic,
                tf_buffer=trajectory_tf_buffer,
            )
        deadline = time.monotonic() + max(goal_timeout_s, 0.0)
        goal_started_at = time.monotonic()
        goal_result_timed_out = False
        goal_orbit_detected = False
        recovery_position_tolerance_reached = False
        recovery_map_distance_to_goal_m: float | None = None
        trajectory_processed_count = 0
        recovery_orbit_guard_enabled = (
            "recovery" in str(payload.get("label") or "").lower()
            and _truthy_env(ROS2_NAV2_RECOVERY_ORBIT_GUARD_ENABLE_ENV)
        )
        orbit_min_duration_s = _float_env(
            ROS2_NAV2_RECOVERY_ORBIT_MIN_DURATION_ENV,
            25.0,
        )
        orbit_no_progress_s = _float_env(
            ROS2_NAV2_RECOVERY_ORBIT_NO_PROGRESS_ENV,
            18.0,
        )
        orbit_min_path_m = _float_env(
            ROS2_NAV2_RECOVERY_ORBIT_MIN_PATH_ENV,
            1.0,
        )
        recovery_position_tolerance_m = min(
            max(float(payload.get("tolerance_m") or 0.25), 0.05),
            0.5,
        )
        goal_cancel_result = {
            "goal_cancel_requested": False,
            "goal_cancel_response_received": False,
            "goal_cancel_accepted": False,
            "goal_cancel_result_observed": False,
        }
        try:
            while rclpy.ok() and not result_future.done() and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=0.1)
                while trajectory_processed_count < len(trajectory_samples):
                    index = trajectory_processed_count
                    trajectory_processed_count += 1
                    if index == 0:
                        continue
                    previous = trajectory_samples[index - 1]
                    current = trajectory_samples[index]
                    if current.get("frame_id") == goal_frame_id:
                        recovery_map_distance_to_goal_m = math.hypot(
                            float(current["x_m"]) - float(payload["x_m"]),
                            float(current["y_m"]) - float(payload["y_m"]),
                        )
                    if previous.get("frame_id") != current.get("frame_id"):
                        continue
                    trajectory_path_length_m += math.hypot(
                        float(current["x_m"]) - float(previous["x_m"]),
                        float(current["y_m"]) - float(previous["y_m"]),
                    )
                now_monotonic = time.monotonic()
                if (
                    recovery_orbit_guard_enabled
                    and last_distance_remaining_m is not None
                    and last_distance_remaining_m <= recovery_position_tolerance_m
                    and recovery_map_distance_to_goal_m is not None
                    and recovery_map_distance_to_goal_m
                    <= recovery_position_tolerance_m
                ):
                    recovery_position_tolerance_reached = True
                    break
                if (
                    recovery_orbit_guard_enabled
                    and now_monotonic - goal_started_at >= orbit_min_duration_s
                    and now_monotonic - feedback_last_improvement_at
                    >= orbit_no_progress_s
                    and trajectory_path_length_m
                    - path_length_at_last_improvement_m
                    >= orbit_min_path_m
                ):
                    goal_orbit_detected = True
                    break
            goal_result_timed_out = (
                not result_future.done()
                and not goal_orbit_detected
                and not recovery_position_tolerance_reached
            )
            if (
                goal_result_timed_out
                or goal_orbit_detected
                or recovery_position_tolerance_reached
            ):
                goal_cancel_result["goal_cancel_requested"] = True
                cancel_future = goal_handle.cancel_goal_async()
                rclpy.spin_until_future_complete(
                    node,
                    cancel_future,
                    timeout_sec=server_timeout_s,
                )
                cancel_response = (
                    cancel_future.result() if cancel_future.done() else None
                )
                goal_cancel_result["goal_cancel_response_received"] = (
                    cancel_response is not None
                )
                goals_canceling = getattr(cancel_response, "goals_canceling", ())
                goal_cancel_result["goal_cancel_accepted"] = bool(goals_canceling)
                if goal_cancel_result["goal_cancel_accepted"]:
                    cancel_deadline = time.monotonic() + max(server_timeout_s, 0.0)
                    while (
                        rclpy.ok()
                        and not result_future.done()
                        and time.monotonic() < cancel_deadline
                    ):
                        rclpy.spin_once(node, timeout_sec=0.1)
                    goal_cancel_result["goal_cancel_result_observed"] = (
                        result_future.done()
                    )
            if result_future.done():
                result = result_future.result()
                status = int(result.status)
                nav2_status = _status_name(status, GoalStatus)
            else:
                status = GoalStatus.STATUS_EXECUTING
                nav2_status = "executing"
            settle_deadline = time.monotonic() + max(post_result_settle_s, 0.0)
            while rclpy.ok() and time.monotonic() < settle_deadline:
                rclpy.spin_once(node, timeout_sec=0.1)
        finally:
            if velocity_subscriptions:
                velocity_result = _finish_velocity_observers(
                    node=node,
                    subscriptions=velocity_subscriptions,
                    observations=velocity_observations,
                    threshold=_float_env(ROS2_NAV2_VELOCITY_THRESHOLD_ENV, 0.001),
                )
            if obstacle_subscriptions:
                obstacle_result = _finish_costmap_observers(
                    node=node,
                    subscriptions=obstacle_subscriptions,
                    observations=obstacle_observations,
                    threshold=_int_env(ROS2_NAV2_OBSTACLE_COST_THRESHOLD_ENV, 65),
                )

        odom_after = _observe_odom_pose(
            node=node,
            odometry_type=Odometry,
            topic=odom_topic,
            timeout_s=odom_timeout_s,
        )
        if trajectory_subscription is not None:
            trajectory_result = _finish_trajectory_observer(
                node=node,
                subscription=trajectory_subscription,
                samples=trajectory_samples,
                start=odom_before,
                end=odom_after,
                threshold_m=_float_env(
                    ROS2_NAV2_TRAJECTORY_LATERAL_DEVIATION_THRESHOLD_ENV,
                    0.04,
                ),
            )
        odom_delta_m = _motion_delta_m(odom_before, odom_after)
        robot_motion_observed = (
            odom_delta_m is not None and odom_delta_m >= motion_threshold_m
        )
        nav2_succeeded = status == GoalStatus.STATUS_SUCCEEDED
        position_tolerance_completion_observed = (
            recovery_position_tolerance_reached
            and goal_cancel_result["goal_cancel_accepted"] is True
            and goal_cancel_result["goal_cancel_result_observed"] is True
        )
        if position_tolerance_completion_observed:
            nav2_status = "position_tolerance_reached"
        obstacle_avoidance_observed = (
            (nav2_succeeded or position_tolerance_completion_observed)
            and robot_motion_observed
            and obstacle_result.get("costmap_obstacle_observed") is True
            and trajectory_result.get("trajectory_lateral_deviation_observed") is True
        )
        completion_observed = (
            nav2_succeeded or position_tolerance_completion_observed
        ) and robot_motion_observed
        runtime_progress_observed = (
            feedback_count > 0 or robot_motion_observed or nav2_succeeded
        )
        blocking_reasons: list[str] = []
        if goal_result_timed_out:
            blocking_reasons.append("nav2_goal_result_timeout")
            if goal_cancel_result["goal_cancel_accepted"] is not True:
                blocking_reasons.append("nav2_goal_cancel_unconfirmed_after_timeout")
            elif goal_cancel_result["goal_cancel_result_observed"] is not True:
                blocking_reasons.append("nav2_goal_cancel_result_not_observed")
        if goal_orbit_detected:
            blocking_reasons.append("nav2_recovery_orbit_detected")
            if goal_cancel_result["goal_cancel_accepted"] is not True:
                blocking_reasons.append(
                    "nav2_goal_cancel_unconfirmed_after_orbit_detection"
                )
            elif goal_cancel_result["goal_cancel_result_observed"] is not True:
                blocking_reasons.append(
                    "nav2_goal_cancel_result_not_observed_after_orbit_detection"
                )
        if nav2_succeeded and not robot_motion_observed:
            blocking_reasons.append("nav2_succeeded_without_robot_motion_observed")
        if (
            not nav2_succeeded
            and not position_tolerance_completion_observed
            and result_future.done()
        ):
            blocking_reasons.append("nav2_goal_result_not_succeeded")
        if (
            recovery_position_tolerance_reached
            and not position_tolerance_completion_observed
            and not nav2_succeeded
        ):
            blocking_reasons.append(
                "nav2_recovery_position_tolerance_cancel_unconfirmed"
            )
        if (
            velocity_result.get("nonzero_velocity_observed") is False
            and result_future.done() is False
        ):
            blocking_reasons.append("nav2_velocity_not_observed")
        if (
            velocity_result.get("nonzero_velocity_observed") is True
            and not robot_motion_observed
        ):
            blocking_reasons.append("nav2_velocity_observed_without_odom_motion")

        state_result = {
            "nav2_action_server_available": True,
            "pose_observed": odom_after is not None,
            "odom_topic": odom_topic,
            "odom_before_observed": odom_before is not None,
            "odom_after_observed": odom_after is not None,
            "odom_delta_m": odom_delta_m,
            "robot_motion_observed": robot_motion_observed,
            "costmap_obstacle_observed": obstacle_result.get(
                "costmap_obstacle_observed"
            )
            is True,
            "obstacle_avoidance_observed": obstacle_avoidance_observed,
            "velocity_result": velocity_result,
            "obstacle_result": obstacle_result,
            "trajectory_result": trajectory_result,
            "post_result_settle_s": post_result_settle_s,
            "goal_cancel_result": goal_cancel_result,
            "recovery_orbit_guard": {
                "enabled": recovery_orbit_guard_enabled,
                "orbit_detected": goal_orbit_detected,
                "trajectory_path_length_m": trajectory_path_length_m,
                "path_since_last_goal_progress_m": (
                    trajectory_path_length_m - path_length_at_last_improvement_m
                ),
                "best_distance_remaining_m": feedback_best_distance_m,
                "no_progress_duration_s": max(
                    time.monotonic() - feedback_last_improvement_at,
                    0.0,
                ),
            },
            "recovery_position_tolerance": {
                "enabled": recovery_orbit_guard_enabled,
                "tolerance_m": recovery_position_tolerance_m,
                "position_tolerance_reached": recovery_position_tolerance_reached,
                "map_distance_to_goal_m": recovery_map_distance_to_goal_m,
                "map_pose_confirmation_required": True,
                "controlled_cancel_observed": (
                    position_tolerance_completion_observed
                ),
            },
            "readiness_result": {
                **lifecycle_result,
                **initialpose_result,
                **tf_ready_result,
                "goal_stamp_zero": True,
            },
        }
        progress_result = {
            "runtime_progress_observed": runtime_progress_observed,
            "completion_observed": completion_observed,
            "robot_motion_observed": robot_motion_observed,
            "costmap_obstacle_observed": obstacle_result.get(
                "costmap_obstacle_observed"
            )
            is True,
            "obstacle_avoidance_observed": obstacle_avoidance_observed,
            "nav2_status": nav2_status,
            "feedback_count": feedback_count,
            "last_distance_remaining_m": last_distance_remaining_m,
            "velocity_result": velocity_result,
            "obstacle_result": obstacle_result,
            "trajectory_result": trajectory_result,
            "post_result_settle_s": post_result_settle_s,
            "goal_cancel_result": goal_cancel_result,
            "recovery_orbit_guard": state_result["recovery_orbit_guard"],
            "recovery_position_tolerance": state_result[
                "recovery_position_tolerance"
            ],
            "readiness_result": {
                **lifecycle_result,
                **initialpose_result,
                **tf_ready_result,
                "goal_stamp_zero": True,
            },
        }
        return _base_response(
            ack_status="accepted",
            ack_source=action_name,
            action_name=action_name,
            odom_topic=odom_topic,
            goal_accepted=True,
            runtime_progress_observed=runtime_progress_observed,
            completion_observed=completion_observed,
            nav2_status=nav2_status,
            state_result=state_result,
            progress_result=progress_result,
            blocking_reasons=blocking_reasons,
            costmap_obstacle_observed=obstacle_result.get(
                "costmap_obstacle_observed"
            )
            is True,
            obstacle_avoidance_observed=obstacle_avoidance_observed,
            obstacle_result=obstacle_result,
            trajectory_result=trajectory_result,
            velocity_result=velocity_result,
            post_result_settle_s=post_result_settle_s,
            goal_cancel_result=goal_cancel_result,
            recovery_orbit_guard=state_result["recovery_orbit_guard"],
            recovery_position_tolerance=state_result[
                "recovery_position_tolerance"
            ],
            readiness_result={
                **lifecycle_result,
                **initialpose_result,
                **tf_ready_result,
                "goal_stamp_zero": True,
            },
        )
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _read_only_response(action: str) -> dict[str, Any]:
    return _base_response(
        ack_status="not_requested",
        ack_source=_bridge_source(),
        runtime_progress_observed=False,
        completion_observed=False,
        nav2_status="not_requested",
        state_result={
            "nav2_action_server_available": False,
            "pose_observed": False,
            "robot_motion_observed": False,
        },
        progress_result={
            "runtime_progress_observed": False,
            "completion_observed": False,
            "robot_motion_observed": False,
            "nav2_status": "not_requested",
        },
        blocking_reasons=[f"{action}_requires_prior_send_goal_pose_in_same_bridge"],
    )


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    invalid = _validate_request(request)
    if invalid is not None:
        return invalid
    if not _bridge_enabled():
        return _blocked_response(
            blocking_reasons=[f"{_bridge_source()}_opt_in_not_enabled"],
        )

    action = str(request.get("action") or "")
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    if action == "send_goal_pose":
        return _send_goal_pose(payload)
    if action == "evaluate_recovery_candidates":
        return _evaluate_recovery_candidates(payload)
    if action == "cancel_goal":
        return _blocked_response(blocking_reasons=["nav2_cancel_goal_not_implemented"])
    return _read_only_response(action)


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print(json.dumps(_blocked_response(blocking_reasons=["request_non_json"])))
        return 0
    if not isinstance(request, dict):
        print(json.dumps(_blocked_response(blocking_reasons=["request_not_object"])))
        return 0
    print(json.dumps(handle_request(request), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
