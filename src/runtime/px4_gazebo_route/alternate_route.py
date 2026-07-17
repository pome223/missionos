"""Bounded alternate-route orchestration and truth-preserving projections.

The caller supplies already-created approval and allowlist artifacts plus the
actual pose, route-dispatch, and trace callbacks.  This module never mints
authority and never treats a mission-upload ACK as route execution or waypoint
arrival evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from typing import Any


_ALTERNATE_MISSION_UPLOAD_ITEMS = (
    (0, 22, 35.681236, 139.767125, 15.0, 1, 6),
    (1, 16, 35.681208, 139.767166, 20.0, 0, 6),
    (2, 21, 35.681198, 139.767176, 0.0, 0, 6),
)


def alternate_mission_upload_items() -> tuple[tuple[int, int, float, float, float, int, int], ...]:
    """Return the bounded SITL-only alternate mission used by this scenario."""

    return _ALTERNATE_MISSION_UPLOAD_ITEMS


def alternate_mission_upload_payloads() -> list[dict[str, int | float]]:
    """Return fresh MAVLink upload payloads for the bounded mission items."""

    return [
        {
            "seq": int(item[0]),
            "command": int(item[1]),
            "latitude_deg": float(item[2]),
            "longitude_deg": float(item[3]),
            "altitude_m": float(item[4]),
            "current": int(item[5]),
            "frame": int(item[6]),
        }
        for item in alternate_mission_upload_items()
    ]


def _mission_upload_succeeded(upload_result: Mapping[str, Any] | None) -> bool:
    return bool(
        upload_result
        and upload_result.get("mission_ack_observed") is True
        and int(upload_result.get("mission_ack_type", -1)) == 0
    )


def _candidate_observed(
    candidate_summary: Mapping[str, Any],
) -> Mapping[str, Any]:
    candidate = candidate_summary.get(
        "alternate_landing_candidate_evidence",
        {},
    )
    return candidate.get("observed") or {}


def execute_alternate_route_rewrite(
    *,
    candidate_summary: Mapping[str, Any],
    target_z: float,
    altitude_max_m: float,
    upload_result: Mapping[str, Any] | None,
    approval: Any,
    route_allowlist: Any,
    pose_sample: Callable[[], Mapping[str, float]],
    send_route_with_monitor: Callable[..., Mapping[str, Any]],
    append_live_pose_row: Callable[[str, Mapping[str, float]], None],
) -> dict[str, Any]:
    """Run one supplied-authority alternate route and preserve observed facts.

    The actual transport remains an injected callback.  A confirmed mission
    upload is necessary but insufficient: observed pose progress and waypoint
    proximity are separately required before execution is marked observed.
    """

    uploaded = _mission_upload_succeeded(upload_result)
    candidate_observed = _candidate_observed(candidate_summary)
    candidate_xy = candidate_observed.get("candidate_xy_m")
    candidate_id = str(candidate_observed.get("candidate_id") or "")
    blocked_reasons: list[str] = []
    if not uploaded:
        blocked_reasons.append("alternate_mission_upload_ack_not_observed")
    if (
        not isinstance(candidate_xy, list)
        or len(candidate_xy) != 2
        or any(value is None for value in candidate_xy)
    ):
        blocked_reasons.append("alternate_landing_candidate_xy_missing")
    approval_id = str(getattr(approval, "approval_id", ""))
    approval_ref = f"px4_gazebo_coupled_command_approval:{approval_id}" if approval_id else ""
    allowlist_id = str(getattr(route_allowlist, "allowlist_id", ""))
    allowlist_ref = f"px4_gazebo_route_command_allowlist:{allowlist_id}" if allowlist_id else ""
    if getattr(approval, "operator_approval_performed", False) is not True:
        blocked_reasons.append("alternate_route_operator_approval_missing")
    if not approval_ref or getattr(route_allowlist, "operator_approval_ref", "") != approval_ref:
        blocked_reasons.append("alternate_route_allowlist_approval_mismatch")
    if blocked_reasons:
        return {
            "mode": "alternate_route_rewrite",
            "sent": False,
            "blocked_reasons": blocked_reasons,
            "dispatch_evidence": {
                "schema_version": "alternate_route_command_dispatch.v1",
                "dispatch_id": ("alternate_route_command_dispatch:mission_designer_route_blocking"),
                "dispatch_status": "blocked",
                "approval_ref": approval_ref,
                "allowlist_ref": allowlist_ref,
                "candidate_evidence_ref": (
                    "alternate_landing_candidate_evidence:mission_designer_route_blocking"
                ),
                "alternate_mission_ack_required": True,
                "alternate_mission_ack_observed": uploaded,
                "blocked_reasons": blocked_reasons,
                "mavlink_message_name": "SET_POSITION_TARGET_LOCAL_NED",
                "mavlink_dispatch_performed": False,
                "bounded_sitl_only": True,
                "approval_free_dispatch_allowed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "delivery_completion_claimed": False,
            },
            "execution_error": "",
            "alternate_route_execution_observed": False,
            "alternate_waypoint_reached_observed": False,
        }

    alternate_observed_waypoint_x = float(candidate_xy[0])
    alternate_observed_waypoint_y = float(candidate_xy[1])
    # The active SITL helper maps the alternate segment into Gazebo local x/y
    # as observed=(sent_y, sent_x).  Preserve both frames in evidence.
    alternate_setpoint_x = alternate_observed_waypoint_y
    alternate_setpoint_y = alternate_observed_waypoint_x
    start_pose = pose_sample()
    start_distance = math.hypot(
        float(start_pose["x"]) - alternate_observed_waypoint_x,
        float(start_pose["y"]) - alternate_observed_waypoint_y,
    )
    try:
        result = dict(
            send_route_with_monitor(
                target_x=alternate_setpoint_x,
                target_y=alternate_setpoint_y,
                target_z=target_z,
                expected_target_x=alternate_observed_waypoint_x,
                expected_target_y=alternate_observed_waypoint_y,
                pickup_pose=start_pose,
                altitude_max_m=altitude_max_m,
                max_pose_deviation_xy_m=8.0,
                max_pose_deviation_z_m=max(10.0, altitude_max_m + 5.0),
                duration_seconds=12.0,
                timeout=22,
                on_deviation=None,
            )
        )
        execution_error = ""
    except Exception as exc:
        result = {
            "mode": "route",
            "sent": False,
            "blocked_reasons": ["alternate_route_rewrite_execution_failed"],
        }
        execution_error = str(exc)[-500:]
    final_pose = pose_sample()
    append_live_pose_row("alternate_route_rewrite", final_pose)
    final_distance = math.hypot(
        float(final_pose["x"]) - alternate_observed_waypoint_x,
        float(final_pose["y"]) - alternate_observed_waypoint_y,
    )
    horizontal_progress = max(0.0, start_distance - final_distance)
    waypoint_reached = final_distance <= 3.0
    route_executed = bool(result.get("sent") is True and horizontal_progress >= 1.0)
    route_execution_observed = route_executed and waypoint_reached
    return {
        "mode": "alternate_route_rewrite",
        "sent": bool(result.get("sent") is True),
        "blocked_reasons": list(result.get("blocked_reasons", [])),
        "dispatch_evidence": {
            "schema_version": "alternate_route_command_dispatch.v1",
            "dispatch_id": ("alternate_route_command_dispatch:mission_designer_route_blocking"),
            "dispatch_status": ("sent" if result.get("sent") is True else "blocked"),
            "approval_ref": approval_ref,
            "allowlist_ref": allowlist_ref,
            "candidate_evidence_ref": (
                "alternate_landing_candidate_evidence:mission_designer_route_blocking"
            ),
            "candidate_id": candidate_id,
            "alternate_mission_ack_required": True,
            "alternate_mission_ack_observed": uploaded,
            "mavlink_message_name": "SET_POSITION_TARGET_LOCAL_NED",
            "target_frame": "px4_local_ned_setpoint",
            "observed_frame": "gazebo_world_local",
            "sent_setpoint_xy_m": [
                alternate_setpoint_x,
                alternate_setpoint_y,
            ],
            "observed_waypoint_xy_m": [
                alternate_observed_waypoint_x,
                alternate_observed_waypoint_y,
            ],
            "frame_mapping_basis": ("runtime_observed_alternate_route_axis_mapping"),
            "mavlink_dispatch_performed": bool(result.get("sent") is True),
            "bounded_sitl_only": True,
            "approval_free_dispatch_allowed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "route_helper_result": result,
        "execution_error": execution_error,
        "sent_setpoint_xy_m": [alternate_setpoint_x, alternate_setpoint_y],
        "observed_waypoint_xy_m": [
            alternate_observed_waypoint_x,
            alternate_observed_waypoint_y,
        ],
        "target_z_m": target_z,
        "start_pose_xyz_m": [
            start_pose.get("x"),
            start_pose.get("y"),
            start_pose.get("z"),
        ],
        "final_pose_xyz_m": [
            final_pose.get("x"),
            final_pose.get("y"),
            final_pose.get("z"),
        ],
        "start_distance_to_alternate_waypoint_m": start_distance,
        "final_distance_to_alternate_waypoint_m": final_distance,
        "horizontal_progress_toward_alternate_waypoint_m": horizontal_progress,
        "alternate_waypoint_reached_observed": waypoint_reached,
        "alternate_route_execution_observed": route_execution_observed,
        "completion_basis": (
            "alternate_waypoint_reached_from_pose_progress"
            if route_execution_observed
            else (
                "alternate_route_progress_observed_waypoint_pending"
                if route_executed
                else "alternate_route_execution_not_observed"
            )
        ),
        "route_execution_authority": "operator_approved_sitl_only",
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }


def project_alternate_mission_upload(
    *,
    candidate_summary: Mapping[str, Any],
    upload_result: Mapping[str, Any] | None,
    alternate_behavior_observation: Mapping[str, Any],
    alternate_route_execution: Mapping[str, Any] | None,
    route_endpoint_port: int,
    operator_approval_performed: bool,
) -> dict[str, Any]:
    """Project supplied upload, dispatch, and pose facts into artifacts."""

    candidate_observed = _candidate_observed(candidate_summary)
    requested = bool(candidate_observed.get("alternate_landing_candidate"))
    uploaded = _mission_upload_succeeded(upload_result)
    mission_items = alternate_mission_upload_payloads() if requested else []
    command_ids = [item["command"] for item in mission_items]
    behavior_observed = bool(
        alternate_behavior_observation.get("alternate_landing_behavior_observed")
    )
    route_execution = dict(alternate_route_execution or {})
    route_dispatch = dict(route_execution.get("dispatch_evidence") or {})
    route_execution_observed = bool(
        uploaded
        and route_dispatch.get("alternate_mission_ack_observed") is True
        and route_execution.get("alternate_route_execution_observed")
    )
    waypoint_reached_observed = bool(
        uploaded
        and route_dispatch.get("alternate_mission_ack_observed") is True
        and route_execution.get("alternate_waypoint_reached_observed")
    )
    return {
        "alternate_mission_upload_request": {
            "schema_version": "alternate_mission_upload_request.v1",
            "request_id": ("alternate_mission_upload_request:mission_designer_route_blocking"),
            "request_status": (
                "approved_for_sitl_alternate_mission_upload"
                if requested and operator_approval_performed
                else "approval_missing"
                if requested
                else "not_requested"
            ),
            "requested_present": requested,
            "candidate_evidence_ref": (
                "alternate_landing_candidate_evidence:mission_designer_route_blocking"
            ),
            "operator_approval_performed": bool(requested and operator_approval_performed),
            "sitl_opt_in": True,
            "mission_items_source": (
                "alternate_landing_candidate_route_blocking" if requested else ""
            ),
            "mission_item_count": len(mission_items),
            "contains_waypoint_item": 16 in command_ids,
            "contains_land_item": 21 in command_ids,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_mission_upload_receipt": {
            "schema_version": "alternate_mission_upload_receipt.v1",
            "receipt_id": ("alternate_mission_upload_receipt:mission_designer_route_blocking"),
            "upload_status": (
                "uploaded"
                if uploaded
                else "not_requested"
                if not requested
                else "failed_or_unconfirmed"
            ),
            "target_endpoint": f"udp://127.0.0.1:{route_endpoint_port}",
            "mission_items": mission_items,
            "mission_item_count": len(mission_items),
            "mission_request_sequences": (
                list(upload_result.get("mission_request_sequences", [])) if upload_result else []
            ),
            "mission_ack_observed": bool(
                upload_result and upload_result.get("mission_ack_observed") is True
            ),
            "mission_ack_type": (upload_result.get("mission_ack_type") if upload_result else None),
            "alternate_mission_uploaded": uploaded,
            "px4_mission_upload_performed": uploaded,
            "mavlink_dispatch_performed": uploaded,
            "bounded_sitl_only": True,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_route_behavior_observation": {
            "schema_version": "alternate_route_behavior_observation.v1",
            "observation_id": (
                "alternate_route_behavior_observation:mission_designer_route_blocking"
            ),
            "observation_status": (
                "alternate_mission_uploaded_and_landing_observed"
                if uploaded and behavior_observed
                else "alternate_mission_uploaded_behavior_pending"
                if uploaded
                else "not_requested"
                if not requested
                else "alternate_mission_upload_unconfirmed"
            ),
            "alternate_mission_uploaded": uploaded,
            "alternate_route_execution_observed": route_execution_observed,
            "alternate_waypoint_reached_observed": (waypoint_reached_observed),
            "alternate_route_execution_ref": (
                "alternate_route_execution_evidence:mission_designer_route_blocking"
                if route_execution_observed
                else ""
            ),
            "alternate_landing_behavior_observed": behavior_observed,
            "behavior_observation_source": (
                "alternate_landing_behavior_observation" if behavior_observed else ""
            ),
            "mission_upload_ack_observed": bool(
                upload_result and upload_result.get("mission_ack_observed") is True
            ),
            "mission_ack_type": (upload_result.get("mission_ack_type") if upload_result else None),
            "original_dropoff_verified": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "alternate_route_command_dispatch": {
            "schema_version": "alternate_route_command_dispatch.v1",
            "dispatch_id": ("alternate_route_command_dispatch:mission_designer_route_blocking"),
            **route_dispatch,
            "alternate_mission_uploaded": uploaded,
            "alternate_mission_ack_required": True,
            "alternate_mission_ack_observed": uploaded,
            "dispatch_status": (
                "not_requested"
                if not requested
                else "blocked"
                if not route_dispatch
                else route_dispatch.get("dispatch_status", "blocked")
            ),
            "mavlink_dispatch_performed": bool(
                uploaded and route_dispatch.get("mavlink_dispatch_performed") is True
            ),
            "approval_free_dispatch_allowed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_route_execution_evidence": {
            "schema_version": "alternate_route_execution_evidence.v1",
            "evidence_id": ("alternate_route_execution_evidence:mission_designer_route_blocking"),
            "observation_status": (
                "alternate_route_waypoint_reached_observed"
                if route_execution_observed and waypoint_reached_observed
                else "alternate_route_progress_observed_waypoint_pending"
                if route_execution_observed
                else "not_requested"
                if not requested
                else "alternate_route_execution_not_observed"
            ),
            "alternate_mission_uploaded": uploaded,
            "alternate_route_execution_observed": route_execution_observed,
            "alternate_waypoint_reached_observed": (waypoint_reached_observed),
            "observed": {
                "source": ("px4_gazebo_local_pose_after_alternate_route_rewrite"),
                "sent_setpoint_xy_m": route_execution.get("sent_setpoint_xy_m", []),
                "observed_waypoint_xy_m": route_execution.get("observed_waypoint_xy_m", []),
                "target_z_m": route_execution.get("target_z_m"),
                "start_pose_xyz_m": route_execution.get("start_pose_xyz_m", []),
                "final_pose_xyz_m": route_execution.get("final_pose_xyz_m", []),
                "start_distance_to_alternate_waypoint_m": (
                    route_execution.get("start_distance_to_alternate_waypoint_m")
                ),
                "final_distance_to_alternate_waypoint_m": (
                    route_execution.get("final_distance_to_alternate_waypoint_m")
                ),
                "horizontal_progress_toward_alternate_waypoint_m": (
                    route_execution.get("horizontal_progress_toward_alternate_waypoint_m")
                ),
                "completion_basis": route_execution.get("completion_basis", ""),
                "alternate_route_command_dispatch_ref": (
                    "alternate_route_command_dispatch:mission_designer_route_blocking"
                    if route_dispatch
                    else ""
                ),
                "candidate_evidence_ref": (
                    "alternate_landing_candidate_evidence:mission_designer_route_blocking"
                ),
                "candidate_id": route_dispatch.get("candidate_id", ""),
                "route_helper_sent": bool(route_execution.get("sent") is True),
                "route_helper_result": route_execution.get("route_helper_result", {}),
                "blocked_reasons": route_execution.get("blocked_reasons", []),
                "execution_error": route_execution.get("execution_error", ""),
                "read_only_observer": False,
                "operator_approved_sitl_only": bool(
                    requested and uploaded and operator_approval_performed
                ),
                "original_dropoff_verified": False,
                "dropoff_verified": False,
                "delivery_completion_claimed": False,
                "auto_gate": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "alternate_route_execution_is_not_original_dropoff_verification": (True),
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


__all__ = [
    "alternate_mission_upload_items",
    "alternate_mission_upload_payloads",
    "execute_alternate_route_rewrite",
    "project_alternate_mission_upload",
]
