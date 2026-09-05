"""Consistency audit for a completed PX4/Gazebo route smoke summary.

The audit checks already-recorded artifacts and evidence.  It is not the
MissionOS Verifier, does not create a verifier verdict, and cannot promote a
proposal, approval, dispatch, observation, or completion claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RouteAuditExpectations:
    route_target_x_m: float
    route_target_y_m: float
    route_target_z_m: float
    landing_z_threshold_m: float
    preupload_requested: bool = False
    payload_release_requested: bool = False
    contact_topic_requested: bool = False


@dataclass(frozen=True)
class PayloadRecoveryAuditExpectations:
    advisory_ref_prefix: str
    payload_action_ref: str
    payload_action: str
    landing_z_threshold_m: float
    supervisor_loop_requested: bool = False


@dataclass(frozen=True)
class RouteDeviationRecoveryAuditExpectations:
    on_deviation_action: str
    post_recovery_action: str
    landing_z_threshold_m: float


def _artifact_bundle_exists(summary: Mapping[str, Any]) -> None:
    artifact_dir = Path(str(summary["artifact_dir"]))
    assert artifact_dir.exists()
    for name in (
        "summary.json",
        "tasks.db",
        "px4_docker.log",
        "pose_samples.jsonl",
        "mission_artifacts.json",
    ):
        assert (artifact_dir / name).exists()


def _observed_modes(summary: Mapping[str, Any]) -> dict[str, Any]:
    alternate_behavior = summary.get("alternate_landing_behavior_observation", {})
    rth_behavior = summary.get("rth_behavior_observation", {})
    return {
        "alternate_behavior": alternate_behavior,
        "alternate_behavior_observed": (
            alternate_behavior.get("alternate_landing_behavior_observed") is True
        ),
        "rth_behavior": rth_behavior,
        "rth_behavior_observed": (rth_behavior.get("return_to_home_behavior_observed") is True),
        "route_blocking_observed": (
            summary.get("route_blocking_verification", {})
            .get("observed", {})
            .get("route_blocking_verified")
            is True
        ),
        "incident_route_blocking_observed": (
            summary.get(
                "horizontal_route_incident_informed_route_blocking_verification",
                {},
            )
            .get("observed", {})
            .get("route_blocking_verified")
            is True
        ),
    }


def _validate_outcome_branch(
    summary: Mapping[str, Any],
    modes: Mapping[str, Any],
) -> None:
    alternate_behavior = modes["alternate_behavior"]
    rth_behavior = modes["rth_behavior"]
    if modes["alternate_behavior_observed"]:
        assert (
            summary["alternate_landing_execution_request"]["request_status"]
            == "approved_for_sitl_alternate_landing"
        )
        assert (
            summary["alternate_mission_upload_request"]["request_status"]
            == "approved_for_sitl_alternate_mission_upload"
        )
        assert summary["alternate_mission_upload_request"]["contains_waypoint_item"] is True
        assert summary["alternate_mission_upload_request"]["contains_land_item"] is True
        assert summary["alternate_mission_upload_receipt"]["upload_status"] == "uploaded"
        assert summary["alternate_mission_upload_receipt"]["mission_ack_observed"] is True
        assert summary["alternate_mission_upload_receipt"]["mission_ack_type"] == 0
        assert summary["alternate_route_behavior_observation"]["alternate_mission_uploaded"] is True
        assert (
            summary["alternate_route_behavior_observation"]["alternate_landing_behavior_observed"]
            is True
        )
        assert summary["alternate_route_behavior_observation"]["dropoff_verified"] is False
        assert (
            summary["alternate_route_behavior_observation"]["delivery_completion_claimed"] is False
        )
        assert summary["alternate_landing_command_dispatch"]["mavlink_dispatch_performed"] is True
        assert alternate_behavior["land_commanded"] is True
        assert alternate_behavior["landing_observed"] is True
        assert alternate_behavior["delivery_completion_claimed"] is False
    elif modes["rth_behavior_observed"]:
        assert summary["rth_execution_request"]["request_status"] == "approved_for_sitl_rth"
        assert summary["rth_command_dispatch"]["mavlink_dispatch_performed"] is True
        assert rth_behavior["rth_commanded"] is True
        assert rth_behavior["rth_state_observed"] is True
        assert rth_behavior["delivery_completion_claimed"] is False
    elif modes["incident_route_blocking_observed"]:
        assert (
            summary["horizontal_route_incident_informed_route_blocking_verification"][
                "verification_status"
            ]
            == "route_blocking_verified"
        )
    elif modes["route_blocking_observed"]:
        assert (
            summary["route_blocking_verification"]["verification_status"]
            == "route_blocking_verified"
        )
        assert (
            summary["gazebo_route_corridor_obstacle_spawn_application"]["application_status"]
            == "applied"
        )
    else:
        assert summary["task_status"] == "completed"
        assert summary["final_status"] == "completed"
        assert summary["dropoff_region_reached"] is True
        assert summary["blocked_reasons"] == []


def _validate_contact_evidence(summary: Mapping[str, Any]) -> None:
    contact_integration = summary["horizontal_route_contact_topic_integration"]
    contact_observed = contact_integration["observed"]
    contact_incident_verification = summary["horizontal_route_contact_incident_verification"]
    contact_incident_verified = contact_incident_verification["observed"]
    assert contact_integration["integration_status"] == "sidecar_contact_event_observed"
    assert contact_observed["contact_event_observed"] is True
    assert contact_observed["collision_names"] != []
    assert contact_integration["horizontal_route_world_contact_sensor_injected"] is False
    assert contact_integration["horizontal_route_px4_home_boundary_protected"] is True
    assert contact_incident_verification["verification_status"] == "incident_verified"
    assert contact_incident_verified["incident_verified"] is True
    assert contact_incident_verified["route_blocking_verified"] is False
    assert contact_incident_verified["traffic_conflict_verified"] is False
    assert contact_incident_verified["auto_gate"] is False

    incident_informed_traffic = summary[
        "horizontal_route_incident_informed_traffic_conflict_verification"
    ]
    incident_informed_traffic_observed = incident_informed_traffic["observed"]
    assert incident_informed_traffic["verification_status"] == "traffic_conflict_verified"
    assert incident_informed_traffic_observed["incident_verified"] is True
    assert incident_informed_traffic_observed["traffic_conflict_verified"] is True
    assert incident_informed_traffic_observed["route_blocking_verified"] is False
    assert incident_informed_traffic_observed["auto_gate"] is False
    assert contact_observed["task_status_mutated"] is False
    assert contact_observed["delivery_completion_claimed"] is False
    assert contact_incident_verified["task_status_mutated"] is False
    assert contact_incident_verified["delivery_completion_claimed"] is False
    assert incident_informed_traffic_observed["task_status_mutated"] is False
    assert incident_informed_traffic_observed["delivery_completion_claimed"] is False

    incident_informed_route_blocking = summary[
        "horizontal_route_incident_informed_route_blocking_verification"
    ]
    incident_route_observed = incident_informed_route_blocking["observed"]
    assert incident_informed_route_blocking["verification_status"] == "route_blocking_verified"
    assert incident_route_observed["traffic_conflict_verified"] is True
    assert incident_route_observed["route_blocking_candidate"] is True
    assert incident_route_observed["route_blocking_verified"] is True
    assert incident_route_observed["auto_gate"] is False
    assert incident_route_observed["task_status_mutated"] is False
    assert incident_route_observed["gate_status_mutated"] is False
    assert incident_route_observed["dropoff_verified"] is False
    assert incident_route_observed["delivery_completion_claimed"] is False


def audit_route_summary(
    summary: Mapping[str, Any],
    *,
    expectations: RouteAuditExpectations,
) -> None:
    """Raise when a recorded smoke summary is internally inconsistent."""

    _artifact_bundle_exists(summary)
    modes = _observed_modes(summary)
    blocked_observed = any(
        modes[name]
        for name in (
            "alternate_behavior_observed",
            "rth_behavior_observed",
            "route_blocking_observed",
            "incident_route_blocking_observed",
        )
    )
    assert summary["existing_artifacts_retained"] is True
    if blocked_observed:
        assert summary["task_status"] == "blocked"
        assert summary["final_status"] == "blocked"
        assert summary["dropoff_region_reached"] is False
        assert "dropoff_region_not_reached" in summary["blocked_reasons"]
    _validate_outcome_branch(summary, modes)

    if expectations.preupload_requested:
        assert summary["preupload_mission_performed"] is True
        assert summary["preupload_mission_ack_observed"] is True
        assert summary["preupload_mission_ack_type"] == 0
        assert summary["preupload_mission_request_sequences"] == [0, 1, 2, 3]
    if expectations.payload_release_requested and not modes["rth_behavior_observed"]:
        assert summary["payload_release_observed"] is True
        assert summary["payload_release_event_source"] == "gazebo_detachable_joint_detach_event"
        assert summary["payload_release_position_x_m"] is not None
        assert summary["payload_release_position_y_m"] is not None
        assert summary["payload_release_position_z_m"] is not None

    assert summary["actual_px4_gazebo_horizontal_smoke_observed"] is True
    assert isinstance(summary["delivery_completion_claimed"], bool)
    assert summary["route_terminal_pose"]["phase"] == "route"
    assert summary["route_terminal_pose"]["observed"] is True
    assert summary["landing_terminal_pose"]["phase"] == "landing"
    assert summary["completed_terminal_pose"]["phase"] == "completed"
    assert summary["route_terminal_progress_m"] == summary["horizontal_progress_m"]
    assert summary["horizontal_progress_m"] >= (0.0 if blocked_observed else 5.0)
    if modes["alternate_behavior_observed"]:
        assert summary["route_geofence_violation"] in (False, True)
        if summary["route_geofence_violation"] is True:
            assert "route_geofence_violation" in summary["blocked_reasons"]
            assert summary.get("delivery_completion_claimed") is not True
            assert summary["dropoff_region_reached"] is False
    else:
        assert summary["route_geofence_violation"] is False

    assert summary["pose_deviation_gate_active"] is True
    assert summary["pose_deviation_aborted"] is False
    resumed_after_assurance_continue = bool(
        summary.get("initial_pose_deviation_aborted") is True
        and summary.get("route_stream_resumed_after_mission_assurance_continue")
        is True
        and summary.get("mission_assurance_continue_effect_observed") is True
        and summary.get("mission_assurance_continue_route_completion_observed")
        is True
    )
    if resumed_after_assurance_continue:
        assert summary["deviation_samples"] != []
        assert all(
            float(sample["deviation_xy_m"]) > 0.0
            for sample in summary["deviation_samples"]
        )
    else:
        assert summary["deviation_samples"] == []
    if expectations.contact_topic_requested:
        _validate_contact_evidence(summary)

    assert summary["route_primitive"] == "bounded_position_setpoint_stream"
    assert summary["bounded_setpoint_stream_allowed"] is True
    assert summary["unbounded_setpoint_stream_allowed"] is False
    assert summary["offboard_mode_switch_allowed"] is True
    assert summary["offboard_mode_switch_command_id"] == 176
    assert summary["offboard_mode_switch_frame_sent"] is True
    assert summary["offboard_mode_switch_ack_required"] is True
    assert summary["offboard_mode_switch_ack_command_id"] == 176
    assert summary["offboard_mode_switch_ack_observed"] is True
    assert summary["offboard_mode_switch_ack_result_code"] == 0
    assert summary["offboard_mode_switch_ack_result_name"] == "ACCEPTED"
    assert summary["route_target_x_m"] == expectations.route_target_x_m
    assert summary["route_target_y_m"] == expectations.route_target_y_m
    assert summary["route_target_z_m"] == expectations.route_target_z_m
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    if not modes["rth_behavior_observed"]:
        assert float(summary["completed_pose_z_m"]) <= expectations.landing_z_threshold_m


def audit_payload_recovery_summary(
    summary: Mapping[str, Any],
    *,
    expectations: PayloadRecoveryAuditExpectations,
) -> None:
    """Audit a payload recovery report without promoting delivery completion."""

    assert summary["delivery_completion_claimed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["payload_feasibility_advisory_ref"].startswith(expectations.advisory_ref_prefix)
    assert summary["payload_advisory_consumed_by_ref"] == expectations.payload_action_ref
    if expectations.payload_action == "land":
        assert summary["payload_recovery_dispatch_status"] in (
            "accepted",
            "timeout",
        )
        assert summary["payload_recovery_completed"] is True
        assert summary["payload_recovery_state_observed"] is True
        assert float(summary["payload_recovery_pose_z_m"]) <= expectations.landing_z_threshold_m
    if expectations.payload_action == "rtl":
        assert summary["payload_recovery_dispatch_status"] in (
            "accepted",
            "timeout",
        )
        assert summary["payload_recovery_completed"] is True
        assert summary["payload_recovery_state_observed"] is True
        assert summary["payload_recovery_state_label"] == "return_to_launch_state_observed"
    if expectations.supervisor_loop_requested:
        assert summary["decision_loop_driver"] == "mission_os_supervisor"
        assert summary["supervisor_scope"] == "payload_form3_sitl_only"
        assert summary["full_gateway_runtime_loop"] is False
        assert summary["supervisor_loop_claim_supported"] is True
        assert summary["payload_route_progress_away_from_pickup_observed"] is True
        assert float(summary["payload_pre_recovery_distance_to_pickup_m"]) >= 2.5
        assert float(summary["payload_recovery_distance_to_pickup_m"]) <= 2.0
        assert float(summary["payload_recovery_distance_to_pickup_m"]) < float(
            summary["payload_pre_recovery_distance_to_pickup_m"]
        )
        assert summary["post_recovery_action_taken"] == "land"
        assert summary["post_recovery_dispatch_status"] in (
            "accepted",
            "timeout",
        )
        assert summary["post_recovery_completed"] is True
        assert summary["post_recovery_state_observed"] is True
        assert float(summary["post_recovery_pose_z_m"]) <= expectations.landing_z_threshold_m


def audit_route_deviation_recovery_summary(
    summary: Mapping[str, Any],
    *,
    expectations: RouteDeviationRecoveryAuditExpectations,
) -> None:
    """Audit bounded route-deviation recovery and its optional second cycle."""

    action = expectations.on_deviation_action
    post_action = expectations.post_recovery_action
    mission_assurance_guard = summary.get("mission_assurance_live_guard")
    if (
        isinstance(mission_assurance_guard, Mapping)
        and mission_assurance_guard.get("guard_status") != "dispatch_eligible"
    ):
        assert summary["final_status"] == "aborted_pose_deviation"
        assert summary["task_status"] == "blocked"
        assert summary["recovery_action_taken"] is None
        assert summary["recovery_dispatch_ref"] is None
        action = "abort_only"
    if action == "abort_only":
        assert summary["final_status"] == "aborted_pose_deviation"
        assert summary["task_status"] == "blocked"
    if action == "land":
        assert summary["final_status"] in (
            "recovered_land",
            "recovered_land_state_observed_ack_timeout",
        )
        assert summary["task_status"] == "completed"
        assert summary["recovery_completed"] is True
        assert summary["recovery_state_observed"] is True
        assert summary["route_stream_terminated_before_recovery_dispatch"]
        assert summary["route_stream_stop_reason"] in (
            "pose_deviation",
            "pose_deviation_forced_kill",
        )
        assert summary["recovery_dispatch_status"] in ("accepted", "timeout")
        if summary["recovery_dispatch_status"] == "timeout":
            assert summary["recovery_ack_complete"] is False
            assert summary["recovery_completion_basis"] == "state_observed_after_dispatch_timeout"
        assert float(summary["recovery_pose_z_m"]) <= expectations.landing_z_threshold_m
    if action == "hold":
        if summary["recovery_state_label"] == "hold_command_unsupported":
            assert summary["final_status"] == "emergency_recovery_unconfirmed"
            assert summary["task_status"] == "blocked"
            assert summary["recovery_completed"] is False
            assert summary["recovery_state_observed"] is False
        else:
            if post_action == "none":
                assert summary["final_status"] in (
                    "recovered_hold",
                    "recovered_hold_state_observed_ack_timeout",
                )
            else:
                assert summary["final_status"].startswith("post_recovery_")
            assert summary["task_status"] == "completed"
            assert summary["recovery_completed"] is True
            assert summary["recovery_state_observed"] is True
            assert summary["recovery_state_label"] == "hold_state_observed"
        _audit_post_recovery_land(
            summary,
            post_action=post_action,
            landing_z_threshold_m=expectations.landing_z_threshold_m,
        )
    if action == "rtl":
        if post_action == "none":
            assert summary["final_status"] in (
                "recovered_rtl",
                "recovered_rtl_state_observed_ack_timeout",
            )
        else:
            assert summary["final_status"].startswith("post_recovery_")
        assert summary["task_status"] == "completed"
        assert summary["recovery_completed"] is True
        assert summary["recovery_state_observed"] is True
        assert summary["recovery_state_label"] == "return_to_launch_state_observed"
        _audit_post_recovery_land(
            summary,
            post_action=post_action,
            landing_z_threshold_m=expectations.landing_z_threshold_m,
        )
    assert summary["existing_artifacts_retained"] is True
    assert summary["deviation_samples"]
    assert summary["delivery_completion_claimed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False


def _audit_post_recovery_land(
    summary: Mapping[str, Any],
    *,
    post_action: str,
    landing_z_threshold_m: float,
) -> None:
    if post_action != "land":
        return
    assert summary["post_recovery_dispatch_status"] in ("accepted", "timeout")
    assert summary["post_recovery_completed"] is True
    assert summary["post_recovery_state_observed"] is True
    assert float(summary["post_recovery_pose_z_m"]) <= landing_z_threshold_m


__all__ = [
    "PayloadRecoveryAuditExpectations",
    "RouteAuditExpectations",
    "RouteDeviationRecoveryAuditExpectations",
    "audit_payload_recovery_summary",
    "audit_route_deviation_recovery_summary",
    "audit_route_summary",
]
