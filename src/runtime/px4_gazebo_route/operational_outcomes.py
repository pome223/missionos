"""Truth-preserving projections for already-dispatched PX4 recovery behavior.

The caller owns candidate selection, fresh human approval, allowlisting,
dispatch, and runtime observation. These functions only serialize supplied
LAND or RTH facts. They do not send commands, mint authority, mutate task or
gate state, or infer observed behavior from an ACK alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _dispatch_dump(dispatch: Any | None) -> dict[str, Any]:
    if dispatch is None:
        return {}
    return dispatch.model_dump(mode="json")


def _approval_ref(approval: Any | None) -> str:
    if approval is None:
        return ""
    return f"px4_gazebo_emergency_command_approval:{approval.approval_id}"


def _allowlist_ref(allowlist: Any | None) -> str:
    if allowlist is None:
        return ""
    return f"px4_gazebo_emergency_command_allowlist:{allowlist.allowlist_id}"


def _dispatch_ref(dispatch: Any | None) -> str:
    if dispatch is None:
        return ""
    return (
        "px4_gazebo_emergency_command_dispatch_result:"
        f"{dispatch.dispatch_result_id}"
    )


def _operator_approval_performed(approval: Any | None) -> bool:
    return bool(
        approval is not None and approval.operator_approval_performed is True
    )


def project_alternate_landing_outcome(
    *,
    alternate_landing_candidate_summary: Mapping[str, Any],
    emergency_approval: Any | None,
    emergency_allowlist: Any | None,
    emergency_dispatch: Any | None,
    completed_pose: Mapping[str, float] | None,
    landing_samples: Sequence[Mapping[str, float]],
) -> dict[str, Any]:
    """Project supplied LAND dispatch and pose facts without executing LAND."""

    candidate = alternate_landing_candidate_summary.get(
        "alternate_landing_candidate_evidence",
        {},
    )
    candidate_observed = candidate.get("observed") or {}
    requested = bool(candidate_observed.get("alternate_landing_candidate"))
    dispatch_dump = _dispatch_dump(emergency_dispatch)
    dispatch_status = dispatch_dump.get("dispatch_status")
    ack_observed = bool(dispatch_dump.get("command_ack_observed"))
    ack_result_code = dispatch_dump.get("command_ack_result_code")
    command_sent = bool(dispatch_dump.get("recovery_command_sent"))
    landing_observed = completed_pose is not None and float(
        completed_pose.get("z", 99.0)
    ) <= 0.15
    ack_complete = ack_observed and ack_result_code == 0
    state_observed_after_dispatch_timeout = (
        command_sent
        and dispatch_status == "timeout"
        and not ack_observed
        and landing_observed
    )
    behavior_observed = bool(
        requested
        and command_sent
        and (ack_complete or state_observed_after_dispatch_timeout)
        and landing_observed
    )
    request_status = (
        "approved_for_sitl_alternate_landing" if requested else "not_requested"
    )
    dispatch_observation_status = (
        "alternate_landing_command_ack_observed"
        if command_sent and ack_complete
        else (
            "alternate_landing_state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "alternate_landing_command_not_dispatched"
                if not requested
                else "alternate_landing_command_unconfirmed"
            )
        )
    )
    behavior_status = (
        "alternate_landing_behavior_observed"
        if behavior_observed
        else "alternate_landing_behavior_not_observed"
        if requested
        else "not_requested"
    )
    completion_basis = (
        "ack_observed_and_state_observed"
        if ack_complete and landing_observed
        else (
            "state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "state_not_observed_or_command_unconfirmed"
                if requested
                else "not_requested"
            )
        )
    )
    final_pose = completed_pose or {}
    return {
        "alternate_landing_execution_request": {
            "schema_version": "alternate_landing_execution_request.v1",
            "request_id": (
                "alternate_landing_execution_request:mission_designer_route_blocking"
            ),
            "request_status": request_status,
            "requested_present": requested,
            "candidate_evidence_ref": (
                "alternate_landing_candidate_evidence:mission_designer_route_blocking"
            ),
            "operator_approval_performed": _operator_approval_performed(
                emergency_approval
            ),
            "sitl_opt_in": True,
            "approved_action": "land" if requested else "",
            "px4_route_changed": False,
            "rth_commanded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_landing_command_dispatch": {
            "schema_version": "alternate_landing_command_dispatch.v1",
            "dispatch_id": (
                "alternate_landing_command_dispatch:mission_designer_route_blocking"
            ),
            "dispatch_status": dispatch_status or "not_requested",
            "application_status": dispatch_observation_status,
            "approval_ref": _approval_ref(emergency_approval),
            "allowlist_ref": _allowlist_ref(emergency_allowlist),
            "emergency_dispatch_ref": _dispatch_ref(emergency_dispatch),
            "command_name": dispatch_dump.get("command_name", ""),
            "command_id": dispatch_dump.get("command_id"),
            "command_ack_observed": ack_observed,
            "command_ack_result_code": ack_result_code,
            "command_ack_result_name": dispatch_dump.get(
                "command_ack_result_name"
            ),
            "completion_basis": completion_basis,
            "observation_status": dispatch_observation_status,
            "mavlink_dispatch_performed": command_sent,
            "bounded_allowlist_enforced": True,
            "approval_free_dispatch_allowed": False,
            "px4_route_changed": False,
            "rth_commanded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_landing_behavior_observation": {
            "schema_version": "alternate_landing_behavior_observation.v1",
            "observation_id": (
                "alternate_landing_behavior_observation:mission_designer_route_blocking"
            ),
            "observation_status": behavior_status,
            "alternate_landing_behavior_observed": behavior_observed,
            "land_commanded": command_sent,
            "rth_commanded": False,
            "command_ack_observed": ack_observed,
            "landing_observed": landing_observed,
            "completion_basis": completion_basis,
            "final_pose_xyz_m": (
                [
                    final_pose.get("x"),
                    final_pose.get("y"),
                    final_pose.get("z"),
                ]
                if final_pose
                else []
            ),
            "landing_sample_count": len(landing_samples),
            "px4_route_changed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_landing_outcome": {
            "schema_version": "alternate_landing_outcome.v1",
            "outcome_id": (
                "alternate_landing_outcome:mission_designer_route_blocking"
            ),
            "outcome_status": (
                "alternate_landing_behavior_observed"
                if behavior_observed
                else (
                    "alternate_landing_behavior_pending_or_unconfirmed"
                    if requested
                    else "not_requested"
                )
            ),
            "alternate_landing_behavior_observed": behavior_observed,
            "task_failed": False,
            "delivery_failed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def project_rth_outcome(
    *,
    route_blocking_verification_summary: Mapping[str, Any],
    rth_requested: bool,
    emergency_approval: Any | None,
    emergency_allowlist: Any | None,
    emergency_dispatch: Any | None,
    rth_state_observed: bool,
    rth_state_label: str | None,
    rth_pose: Mapping[str, float] | None,
    rth_samples: Sequence[Mapping[str, float]],
) -> dict[str, Any]:
    """Project supplied RTH dispatch and state facts without executing RTH."""

    route_blocking = route_blocking_verification_summary.get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking.get("observed") or {}
    requested = bool(
        rth_requested and route_blocking_observed.get("route_blocking_verified")
    )
    dispatch_dump = _dispatch_dump(emergency_dispatch)
    dispatch_status = dispatch_dump.get("dispatch_status")
    ack_observed = bool(dispatch_dump.get("command_ack_observed"))
    ack_result_code = dispatch_dump.get("command_ack_result_code")
    command_sent = bool(dispatch_dump.get("recovery_command_sent"))
    ack_complete = ack_observed and ack_result_code == 0
    state_observed_after_dispatch_timeout = (
        command_sent
        and dispatch_status == "timeout"
        and not ack_observed
        and rth_state_observed
    )
    behavior_observed = bool(
        requested
        and command_sent
        and (ack_complete or state_observed_after_dispatch_timeout)
        and rth_state_observed
    )
    dispatch_observation_status = (
        "rth_command_ack_observed"
        if command_sent and ack_complete
        else (
            "rth_state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "rth_command_not_dispatched"
                if not requested
                else "rth_command_unconfirmed"
            )
        )
    )
    behavior_status = (
        "rth_behavior_observed"
        if behavior_observed
        else "rth_behavior_not_observed"
        if requested
        else "not_requested"
    )
    completion_basis = (
        "ack_observed_and_state_observed"
        if ack_complete and rth_state_observed
        else (
            "state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "state_not_observed_or_command_unconfirmed"
                if requested
                else "not_requested"
            )
        )
    )
    final_pose = rth_pose or {}
    return {
        "rth_execution_request": {
            "schema_version": "rth_execution_request.v1",
            "request_id": "rth_execution_request:mission_designer_route_blocking",
            "request_status": "approved_for_sitl_rth" if requested else "not_requested",
            "requested_present": requested,
            "route_blocking_verification_ref": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
            "operator_approval_performed": _operator_approval_performed(
                emergency_approval
            ),
            "sitl_opt_in": True,
            "approved_action": "rtl" if requested else "",
            "px4_route_changed": False,
            "alternate_mission_uploaded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "rth_command_dispatch": {
            "schema_version": "rth_command_dispatch.v1",
            "dispatch_id": "rth_command_dispatch:mission_designer_route_blocking",
            "dispatch_status": dispatch_status or "not_requested",
            "application_status": dispatch_observation_status,
            "approval_ref": _approval_ref(emergency_approval),
            "allowlist_ref": _allowlist_ref(emergency_allowlist),
            "emergency_dispatch_ref": _dispatch_ref(emergency_dispatch),
            "command_name": dispatch_dump.get("command_name", ""),
            "command_id": dispatch_dump.get("command_id"),
            "command_ack_observed": ack_observed,
            "command_ack_result_code": ack_result_code,
            "command_ack_result_name": dispatch_dump.get(
                "command_ack_result_name"
            ),
            "completion_basis": completion_basis,
            "mavlink_dispatch_performed": command_sent,
            "bounded_allowlist_enforced": True,
            "approval_free_dispatch_allowed": False,
            "px4_route_changed": False,
            "alternate_mission_uploaded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "rth_behavior_observation": {
            "schema_version": "rth_behavior_observation.v1",
            "observation_id": (
                "rth_behavior_observation:mission_designer_route_blocking"
            ),
            "observation_status": behavior_status,
            "return_to_home_behavior_observed": behavior_observed,
            "rth_commanded": command_sent,
            "command_ack_observed": ack_observed,
            "completion_basis": completion_basis,
            "rth_state_observed": bool(rth_state_observed),
            "rth_state_label": rth_state_label or "",
            "final_pose_xyz_m": (
                [
                    final_pose.get("x"),
                    final_pose.get("y"),
                    final_pose.get("z"),
                ]
                if final_pose
                else []
            ),
            "rth_sample_count": len(rth_samples),
            "px4_route_changed": False,
            "alternate_mission_uploaded": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "rth_outcome": {
            "schema_version": "rth_outcome.v1",
            "outcome_id": "rth_outcome:mission_designer_route_blocking",
            "outcome_status": (
                "rth_behavior_observed"
                if behavior_observed
                else (
                    "rth_behavior_pending_or_unconfirmed"
                    if requested
                    else "not_requested"
                )
            ),
            "return_to_home_behavior_observed": behavior_observed,
            "task_failed": False,
            "delivery_failed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


__all__ = [
    "project_alternate_landing_outcome",
    "project_rth_outcome",
]
