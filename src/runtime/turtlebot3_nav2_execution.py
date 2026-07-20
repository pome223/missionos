"""Bounded TurtleBot Nav2 execution and observation projection.

This module is intentionally downstream of proposal and approval.  It accepts
an already concrete Nav2 goal plus an existing operator-approval reference,
dispatches through the opt-in bridge, and projects bridge observations without
minting authority or deciding whether a route may resume.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import os
from typing import Any

from src.runtime.hardware_adapter_contract import HardwareExecutionMode
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV,
    Ros2Nav2BridgeCommandClient,
    Ros2Nav2BridgeError,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
    build_blocked_ros2_nav2_hardware_adapter_evidence,
)
from src.runtime.turtlebot3_telemetry_sidecar import (
    TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV,
    TurtleBot3TelemetrySidecarError,
    build_turtlebot3_state_correlation,
    build_turtlebot3_telemetry_window_from_jsonl,
)


TURTLEBOT3_HARNESS_STOP_DISPATCH_SCHEMA_VERSION = (
    "missionos_turtlebot3_harness_stop_dispatch.v1"
)


def robot_motion_from_responses(
    responses: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Project observed odometry without treating an ACK as motion."""

    for response in responses:
        state = response.get("state_result")
        if isinstance(state, Mapping):
            return {
                "robot_motion_observed": state.get("robot_motion_observed") is True,
                "odom_delta_m": state.get("odom_delta_m"),
                "odom_topic": state.get("odom_topic"),
                "robot_motion_observation_source": "ros2_nav2_bridge_receipt",
            }
    return {
        "robot_motion_observed": False,
        "odom_delta_m": None,
        "odom_topic": None,
        "robot_motion_observation_source": "not_available",
    }


def sidecar_motion_artifacts(
    *,
    bridge_motion: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], bool]:
    """Prefer source-backed sidecar odometry when that boundary is configured."""

    jsonl_path = os.environ.get(TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV, "").strip()
    if not jsonl_path:
        return {}, dict(bridge_motion), [], False
    try:
        window = build_turtlebot3_telemetry_window_from_jsonl(jsonl_path)
        correlation = build_turtlebot3_state_correlation(
            telemetry_window=window,
            bridge_motion=bridge_motion,
        )
    except TurtleBot3TelemetrySidecarError as exc:
        return (
            {
                "telemetry_sidecar_status": "blocked",
                "telemetry_sidecar_jsonl_path": jsonl_path,
                "telemetry_sidecar_error": str(exc),
                "physical_execution_invoked": False,
                "mission_delivery_completion_claimed": False,
            },
            {
                "robot_motion_observed": False,
                "odom_delta_m": None,
                "odom_topic": None,
                "robot_motion_observation_source": "telemetry_sidecar_error",
            },
            ["telemetry_sidecar_unreadable"],
            True,
        )

    window_payload = window.model_dump(mode="json")
    correlation_payload = correlation.model_dump(mode="json")
    motion = {
        "robot_motion_observed": window.odom_motion_observed,
        "odom_delta_m": window.odom_delta_m,
        "odom_topic": window.odom_topic,
        "robot_motion_observation_source": "ros2_telemetry_sidecar_jsonl",
        "telemetry_window_ref": correlation.telemetry_window_ref,
        "telemetry_raw_logs_ref": window.raw_logs_ref,
    }
    blocking_reasons = (
        list(correlation.blocked_reasons)
        if correlation.correlation_status == "blocked"
        else []
    )
    return (
        {
            "telemetry_sidecar_status": correlation.correlation_status,
            "turtlebot3_telemetry_window": window_payload,
            "turtlebot3_state_correlation": correlation_payload,
            "telemetry_window_ref": correlation.telemetry_window_ref,
            "raw_logs_ref": window.raw_logs_ref,
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
        },
        motion,
        blocking_reasons,
        True,
    )


def obstacle_observation_from_responses(
    responses: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Project obstacle facts observed by the bridge, never inferred from ACK."""

    for response in responses:
        state = response.get("state_result")
        progress = response.get("progress_result")
        state = state if isinstance(state, Mapping) else {}
        progress = progress if isinstance(progress, Mapping) else {}
        trajectory = response.get("trajectory_result")
        if not isinstance(trajectory, Mapping):
            trajectory = state.get("trajectory_result")
        if not isinstance(trajectory, Mapping):
            trajectory = progress.get("trajectory_result")
        trajectory = trajectory if isinstance(trajectory, Mapping) else {}
        obstacle_detected = (
            response.get("obstacle_detected") is True
            or response.get("costmap_obstacle_observed") is True
            or state.get("obstacle_detected") is True
            or state.get("costmap_obstacle_observed") is True
            or progress.get("obstacle_detected") is True
            or progress.get("costmap_obstacle_observed") is True
        )
        obstacle_avoidance_observed = (
            response.get("obstacle_avoidance_observed") is True
            or state.get("obstacle_avoidance_observed") is True
            or progress.get("obstacle_avoidance_observed") is True
        )
        if obstacle_detected or obstacle_avoidance_observed:
            return {
                "obstacle_detected": obstacle_detected,
                "costmap_obstacle_observed": (
                    response.get("costmap_obstacle_observed") is True
                    or state.get("costmap_obstacle_observed") is True
                    or progress.get("costmap_obstacle_observed") is True
                ),
                "obstacle_avoidance_observed": obstacle_avoidance_observed,
                "trajectory_lateral_deviation_observed": (
                    trajectory.get("trajectory_lateral_deviation_observed") is True
                ),
                "max_lateral_deviation_m": trajectory.get(
                    "max_lateral_deviation_m"
                ),
                "avoidance_observation_source": str(
                    response.get("ack_source") or response.get("action") or "bridge"
                ),
            }
    return {
        "obstacle_detected": False,
        "costmap_obstacle_observed": False,
        "obstacle_avoidance_observed": False,
        "trajectory_lateral_deviation_observed": False,
        "max_lateral_deviation_m": None,
        "avoidance_observation_source": None,
    }


def dispatch_harness_stop(
    *,
    reflex: Mapping[str, Any],
    mission_ref: str,
) -> dict[str, Any]:
    """Dispatch a bounded cancel and separately observe whether motion stopped."""

    trigger = str(reflex.get("trigger") or "")
    record: dict[str, Any] = {
        "schema_version": TURTLEBOT3_HARNESS_STOP_DISPATCH_SCHEMA_VERSION,
        "harness_action": "hold",
        "bridge_action": "cancel_goal",
        "authority_source": "emergency_harness",
        "trigger": trigger,
        "recorded_reason": f"reflex_first_recovery_entry:{trigger}",
        "mission_ref": mission_ref,
        "cancel_accepted": False,
        "stop_observed": False,
        "stop_confirmed": False,
        "bridge_error": "",
        "bridge_receipt": {},
        "physical_execution_invoked": False,
        "progress_counted": False,
        "mission_delivery_completion_claimed": False,
    }
    client = Ros2Nav2BridgeCommandClient()
    try:
        response = client.cancel_goal()
    except Ros2Nav2BridgeError as exc:
        record["bridge_error"] = str(exc)
        return record
    blocking_reasons = [
        str(item) for item in (response.get("blocking_reasons") or ())
    ]
    ack_status = str(response.get("ack_status") or "")
    record["bridge_receipt"] = {
        "ack_status": ack_status,
        "ack_source": str(response.get("ack_source") or ""),
        "nav2_status": str(response.get("nav2_status") or ""),
        "blocking_reasons": blocking_reasons,
        "post_cancel_odom_delta_m": response.get("post_cancel_odom_delta_m"),
        "stop_observation_window_s": response.get("stop_observation_window_s"),
        "stop_observation_source": str(
            response.get("stop_observation_source") or ""
        ),
    }
    record["cancel_accepted"] = ack_status == "accepted" and not blocking_reasons
    record["stop_observed"] = response.get("stop_observed") is True
    record["stop_confirmed"] = record["cancel_accepted"] and record["stop_observed"]
    return record


def dispatch_nav2_goal(
    *,
    proposal_id: str,
    approval_actor: str,
    goal: Nav2GoalPose,
    approval_ref: str,
    dispatched_at: datetime,
    action_ref_suffix: str,
    raw_logs_ref: str | None,
    publish_initialpose: bool,
    simulate_cancel_after_accept: bool = False,
) -> dict[str, Any]:
    """Dispatch one already approved goal and return claim-safe observations."""

    config = Ros2Nav2HardwareAdapterConfig(
        missionos_action_ref=f"{proposal_id}:{action_ref_suffix}",
        goal_pose=goal,
        execution_mode=HardwareExecutionMode.SIM,
        operator_approval_ref=approval_ref or None,
        approval_actor=approval_actor,
        approval_timestamp=dispatched_at,
        max_distance_m=goal.max_distance_m,
        raw_logs_ref=raw_logs_ref,
    )
    env_overrides: dict[str, str] = {}
    if not publish_initialpose:
        env_overrides["ROS2_NAV2_INITIALPOSE_ENABLE"] = "0"
    if simulate_cancel_after_accept:
        env_overrides[ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV] = "1"
    client = Ros2Nav2BridgeCommandClient(env_overrides=env_overrides)
    adapter = Ros2Nav2HardwareAdapter(config=config, client=client)
    bridge_error = ""
    try:
        evidence = adapter.dispatch_approved_action()
        bridge_responses = client.collect_responses()
    except Ros2Nav2BridgeError as exc:
        bridge_error = str(exc)
        evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
            config=config,
            blocking_reasons=(
                "ros2_nav2_bridge_receipt_unavailable",
                "ros2_nav2_bridge_error",
            ),
        )
        bridge_responses = ()
    motion = robot_motion_from_responses(bridge_responses)
    obstacle = obstacle_observation_from_responses(bridge_responses)
    return {
        "segment_ref": action_ref_suffix,
        "goal_pose": goal.model_dump(mode="json"),
        "publish_initialpose": publish_initialpose,
        "simulated_transient_fault_requested": simulate_cancel_after_accept,
        "adapter_evidence": evidence.model_dump(mode="json"),
        "bridge_responses": [dict(response) for response in bridge_responses],
        "bridge_error": bridge_error,
        "dispatch_request_sent": evidence.dispatch_request_sent,
        "completion_claimed": evidence.completion_claimed,
        "completion_scope": (
            evidence.completion_scope if evidence.completion_claimed else "none"
        ),
        "blocking_reasons": list(evidence.blocking_reasons),
        "unproven_claims": list(evidence.unproven_claims),
        **motion,
        **obstacle,
    }
