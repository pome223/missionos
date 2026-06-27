#!/usr/bin/env python3
"""Render a read-only Mission Designer realism report from a SITL summary."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REALISM_REPORT_BOUNDARY_NOTE = (
    "This report is a read-only operator review surface. It summarizes existing "
    "Mission Designer realism artifacts and does not grant verifier, gate, "
    "dispatch, PX4/Gazebo command, hardware, physical execution, task-status, or "
    "delivery-completion authority."
)


@dataclass(frozen=True)
class RealismSlice:
    label: str
    profile_key: str
    application_key: str
    evidence_key: str
    closure_category: str
    request_field: str = ""


REALISM_SLICES: tuple[RealismSlice, ...] = (
    RealismSlice(
        "wind gust",
        "environment_condition_profile",
        "simulator_condition_application",
        "observed_environment_evidence",
        "executable / observed",
    ),
    RealismSlice(
        "payload mass",
        "vehicle_condition_profile",
        "payload_simulator_condition_application",
        "observed_vehicle_condition_evidence",
        "executable / observed",
    ),
    RealismSlice(
        "battery threshold",
        "battery_condition_profile",
        "battery_simulator_condition_application",
        "observed_battery_condition_evidence",
        "executable / observed",
    ),
    RealismSlice(
        "sensor failure",
        "sensor_condition_profile",
        "sensor_failure_injection_application",
        "observed_sensor_condition_evidence",
        "support-detection / unsupported",
    ),
    RealismSlice(
        "landing-zone marker",
        "gazebo_world_condition_profile",
        "gazebo_world_application",
        "observed_world_condition_evidence",
        "visual-only world / operational",
    ),
    RealismSlice(
        "visibility marker",
        "visibility_condition_profile",
        "visibility_application",
        "observed_visibility_condition_evidence",
        "visual-only world / operational",
    ),
    RealismSlice(
        "operational markers",
        "operational_condition_profile",
        "operational_application",
        "observed_operational_condition_evidence",
        "visual-only world / operational",
        "no_fly_zone_marker",
    ),
    RealismSlice(
        "moving actor",
        "dynamic_actor_profile",
        "operational_application",
        "observed_operational_condition_evidence",
        "visual-only world / operational",
    ),
    RealismSlice(
        "moving actor pose",
        "dynamic_actor_profile",
        "operational_application",
        "moving_actor_pose_observation",
        "observer-only",
    ),
    RealismSlice(
        "moving actor proximity",
        "dynamic_actor_profile",
        "operational_application",
        "moving_actor_proximity_evidence",
        "observer-only",
    ),
    RealismSlice(
        "collision-enabled obstacle",
        "collision_obstacle_profile",
        "operational_application",
        "collision_obstacle_evidence",
        "executable / observed",
    ),
    RealismSlice(
        "Gazebo route-corridor obstacle spawn applicator",
        "collision_obstacle_profile",
        "gazebo_route_corridor_obstacle_spawn_application",
        "gazebo_route_corridor_obstacle_spawn_application",
        "executable / applied",
    ),
    RealismSlice(
        "route blocking candidate",
        "collision_obstacle_profile",
        "operational_application",
        "route_blocking_candidate_evidence",
        "observer-only",
    ),
    RealismSlice(
        "contact event incident candidate",
        "collision_obstacle_profile",
        "operational_application",
        "contact_event_incident_evidence",
        "operator-review",
    ),
    RealismSlice(
        "horizontal route contact topic integration",
        "collision_obstacle_profile",
        "operational_application",
        "horizontal_route_contact_topic_integration",
        "operator-review",
    ),
    RealismSlice(
        "horizontal route contact scoped verifier candidate",
        "collision_obstacle_profile",
        "operational_application",
        "horizontal_route_contact_scoped_verifier_candidate",
        "scoped verifier candidate",
    ),
    RealismSlice(
        "horizontal route contact incident verifier",
        "collision_obstacle_profile",
        "operational_application",
        "horizontal_route_contact_incident_verification",
        "scoped verifier",
    ),
    RealismSlice(
        "incident-informed traffic conflict verifier",
        "collision_obstacle_profile",
        "operational_application",
        "horizontal_route_incident_informed_traffic_conflict_verification",
        "scoped verifier",
    ),
    RealismSlice(
        "incident-informed route blocking verifier",
        "collision_obstacle_profile",
        "operational_application",
        "horizontal_route_incident_informed_route_blocking_verification",
        "scoped verifier",
    ),
    RealismSlice(
        "operational incident report",
        "collision_obstacle_profile",
        "operational_application",
        "operational_incident_report",
        "operator-review",
    ),
    RealismSlice(
        "traffic conflict verifier",
        "collision_obstacle_profile",
        "operational_application",
        "traffic_conflict_verification",
        "scoped verifier",
    ),
    RealismSlice(
        "route blocking verifier",
        "collision_obstacle_profile",
        "operational_application",
        "route_blocking_verification",
        "scoped verifier",
    ),
    RealismSlice(
        "alternate landing candidate",
        "alternate_landing_profile",
        "operational_application",
        "alternate_landing_candidate_evidence",
        "operator-review",
    ),
    RealismSlice(
        "alternate landing execution",
        "alternate_landing_execution_request",
        "alternate_landing_command_dispatch",
        "alternate_landing_behavior_observation",
        "executable / observed",
    ),
    RealismSlice(
        "alternate mission upload",
        "alternate_mission_upload_request",
        "alternate_mission_upload_receipt",
        "alternate_route_behavior_observation",
        "executable / observed",
    ),
    RealismSlice(
        "RTH behavior observation",
        "rth_execution_request",
        "rth_command_dispatch",
        "rth_behavior_observation",
        "executable / observed",
    ),
    RealismSlice(
        "multi-drone conflict probe",
        "operational_condition_profile",
        "operational_application",
        "observed_operational_condition_evidence",
        "support-detection / unsupported",
        "multi_drone_conflict_probe",
    ),
    RealismSlice(
        "multi-vehicle frame contract",
        "multi_vehicle_frame_contract",
        "operational_application",
        "observed_operational_condition_evidence",
        "support-detection / unsupported",
    ),
    RealismSlice(
        "observer telemetry dropout",
        "telemetry_degradation_profile",
        "telemetry_degradation_application",
        "observed_telemetry_gap_evidence",
        "observer-only",
    ),
    RealismSlice(
        "MAVLink heartbeat / link degradation",
        "mavlink_link_degradation_profile",
        "mavlink_link_degradation_application",
        "observed_mavlink_gap_evidence",
        "observer-only",
    ),
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _bool_text(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _value_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return _bool_text(value)
    if isinstance(value, Mapping):
        if not value:
            return "-"
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            return "-"
        return json.dumps(list(value), ensure_ascii=False, sort_keys=True)
    return str(value)


def _table_cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")


def _request_status(profile: Mapping[str, Any]) -> str:
    if not profile:
        return "not attached"
    requested_present = profile.get("requested_present")
    if requested_present is True:
        return "requested"
    if requested_present is False:
        return "not requested"
    requested = profile.get("requested")
    if isinstance(requested, Mapping):
        if any(value not in (None, "", [], {}, False) for value in requested.values()):
            return "requested"
        return "not requested"
    actors = profile.get("actors")
    if isinstance(actors, Sequence) and not isinstance(actors, (str, bytes, bytearray)) and actors:
        return "requested"
    if isinstance(actors, Sequence) and not isinstance(actors, (str, bytes, bytearray)):
        return "not requested"
    return "attached"


def _slice_request_status(profile: Mapping[str, Any], slice_: RealismSlice) -> str:
    if slice_.request_field:
        requested = _as_mapping(profile.get("requested"))
        if slice_.request_field in requested:
            return "requested" if requested.get(slice_.request_field) is True else "not requested"
    return _request_status(profile)


def _application_status(application: Mapping[str, Any]) -> str:
    if not application:
        return "not attached"
    status = application.get("application_status")
    if status:
        return str(status)
    status = application.get("upload_status")
    if status:
        return str(status)
    if application.get("unsupported_reasons"):
        return "unsupported"
    if application.get("applied"):
        return "applied"
    return "attached"


def _observation_status(evidence: Mapping[str, Any]) -> str:
    if not evidence:
        return "not attached"
    status = evidence.get("observation_status")
    if status:
        return str(status)
    status = evidence.get("report_status")
    if status:
        return str(status)
    status = evidence.get("verification_status")
    if status:
        return str(status)
    observed = evidence.get("observed")
    if isinstance(observed, Mapping) and observed:
        return "observed"
    return "attached"


def _unsupported_text(application: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    reasons: list[Any] = []
    reasons.extend(_as_sequence(application.get("unsupported_reasons")))
    reasons.extend(_as_sequence(application.get("approximation_reasons")))
    observed = _as_mapping(evidence.get("observed"))
    reasons.extend(_as_sequence(observed.get("unsupported_reasons")))
    reasons.extend(_as_sequence(observed.get("approximation_reasons")))
    return _value_text(reasons)


def _cleanup_status(summary: Mapping[str, Any]) -> str:
    cleanup = _as_mapping(summary.get("scenario_cleanup_receipt"))
    return str(cleanup.get("cleanup_status") or "not attached")


def _runtime_lines(summary: Mapping[str, Any]) -> list[str]:
    return [
        f"- task_status: {_value_text(summary.get('task_status'))}",
        f"- final_status: {_value_text(summary.get('final_status'))}",
        f"- payload_release_observed: {_bool_text(summary.get('payload_release_observed'))}",
        f"- dropoff_verified: {_bool_text(summary.get('dropoff_verified'))}",
        f"- delivery_completion_claimed: {_bool_text(summary.get('delivery_completion_claimed'))}",
        f"- hardware_target_allowed: {_bool_text(summary.get('hardware_target_allowed'))}",
        f"- physical_execution_invoked: {_bool_text(summary.get('physical_execution_invoked'))}",
    ]


def _slice_row(summary: Mapping[str, Any], slice_: RealismSlice) -> str:
    profile = _as_mapping(summary.get(slice_.profile_key))
    application = _as_mapping(summary.get(slice_.application_key))
    evidence = _as_mapping(summary.get(slice_.evidence_key))
    request_status = _slice_request_status(profile, slice_)
    application_status = (
        "not_requested"
        if request_status == "not requested"
        else _application_status(application)
    )
    observation_status = (
        "not_requested"
        if request_status == "not requested"
        else _observation_status(evidence)
    )
    return (
        f"| {_table_cell(slice_.label)} | {_table_cell(request_status)} | "
        f"{_table_cell(application_status)} | "
        f"{_table_cell(observation_status)} | "
        f"{_table_cell('-' if request_status == 'not requested' else _unsupported_text(application, evidence))} | "
        f"{_table_cell(_cleanup_status(summary))} |"
    )


def _slice_is_active(summary: Mapping[str, Any], slice_: RealismSlice) -> bool:
    profile = _as_mapping(summary.get(slice_.profile_key))
    evidence = _as_mapping(summary.get(slice_.evidence_key))
    request_status = _slice_request_status(profile, slice_)
    if request_status == "requested":
        return True
    if request_status in {"not requested", "not_requested"}:
        return False
    return _observation_status(evidence) not in {
        "not attached",
        "not requested",
        "not_requested",
        "attached",
    }


def _slice_closure_category(summary: Mapping[str, Any], slice_: RealismSlice) -> str:
    if slice_.profile_key == "mavlink_link_degradation_profile":
        application = _as_mapping(summary.get(slice_.application_key))
        applied = _as_mapping(application.get("applied"))
        if applied.get("method") == "px4_mavlink_stop_restart_bounded_sitl":
            return "executable / observed"
    return slice_.closure_category


def _closure_lines(summary: Mapping[str, Any]) -> list[str]:
    grouped: dict[str, list[str]] = {}
    for slice_ in REALISM_SLICES:
        if not _slice_is_active(summary, slice_):
            continue
        grouped.setdefault(_slice_closure_category(summary, slice_), []).append(
            slice_.label
        )

    if not grouped:
        return ["- active realism slices: none attached"]

    lines = [
        "- active realism slices are classified by what they actually prove, not by what future work may add.",
    ]
    category_order = (
        "executable / applied",
        "executable / observed",
        "observer-only",
        "operator-review",
        "scoped verifier",
        "visual-only world / operational",
        "support-detection / unsupported",
    )
    for category in category_order:
        labels = grouped.get(category)
        if labels:
            lines.append(f"- {category}: {', '.join(labels)}")
    lines.extend(
        [
            "- support-detection and visual-only entries are valid epic progress, but remain non-authoritative.",
            "- unsupported entries must not be used as proxies for simulator behavior, route blocking, incidents, gates, or completion.",
        ]
    )
    return lines


def _detail_lines(summary: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    mavlink_evidence = _as_mapping(summary.get("observed_mavlink_gap_evidence"))
    mavlink_observed = _as_mapping(mavlink_evidence.get("observed"))
    if mavlink_observed:
        mavlink_application = _as_mapping(summary.get("mavlink_link_degradation_application"))
        mavlink_applied = _as_mapping(mavlink_application.get("applied"))
        mavlink_label = (
            "MAVLink bounded endpoint loss"
            if mavlink_applied.get("method") == "px4_mavlink_stop_restart_bounded_sitl"
            else "MAVLink heartbeat observer"
        )
        lines.extend(
            [
                f"- {mavlink_label}:",
                f"  - heartbeat_count: {_value_text(mavlink_observed.get('heartbeat_count'))}",
                f"  - heartbeat_gap_count: {_value_text(mavlink_observed.get('heartbeat_gap_count'))}",
                f"  - max_heartbeat_interval_seconds: {_value_text(mavlink_observed.get('max_heartbeat_interval_seconds'))}",
                f"  - mavlink_link_loss_observed: {_bool_text(mavlink_observed.get('mavlink_link_loss_observed'))}",
                f"  - endpoint_stop_performed: {_bool_text(mavlink_observed.get('endpoint_stop_performed'))}",
                f"  - endpoint_restart_performed: {_bool_text(mavlink_observed.get('endpoint_restart_performed'))}",
                f"  - rf_link_loss_observed: {_bool_text(mavlink_observed.get('rf_link_loss_observed'))}",
                f"  - packet_drop_performed: {_bool_text(mavlink_observed.get('packet_drop_performed'))}",
                f"  - vehicle_failsafe_observed: {_bool_text(mavlink_observed.get('vehicle_failsafe_observed'))}",
            ]
        )

    telemetry_evidence = _as_mapping(summary.get("observed_telemetry_gap_evidence"))
    telemetry_observed = _as_mapping(telemetry_evidence.get("observed"))
    if telemetry_observed:
        lines.extend(
            [
                "- observer telemetry gaps:",
                f"  - gap_count: {_value_text(telemetry_observed.get('gap_count'))}",
                f"  - max_gap_seconds: {_value_text(telemetry_observed.get('max_gap_seconds'))}",
                f"  - missing_sample_count: {_value_text(telemetry_observed.get('missing_sample_count'))}",
                f"  - baseline_observer_sample_observed: {_bool_text(telemetry_observed.get('baseline_observer_sample_observed'))}",
                f"  - observer_sample_pause_performed: {_bool_text(telemetry_observed.get('observer_sample_pause_performed'))}",
                f"  - post_pause_observer_sample_observed: {_bool_text(telemetry_observed.get('post_pause_observer_sample_observed'))}",
                f"  - publisher_transport_loss_observed: {_bool_text(telemetry_observed.get('publisher_transport_loss_observed'))}",
            ]
        )

    payload_advisory = _as_mapping(summary.get("payload_feasibility_advisory"))
    payload_advisory_source = _as_mapping(payload_advisory.get("advisory_source_refs"))
    if payload_advisory:
        lines.extend(
            [
                "- payload feasibility advisory:",
                f"  - advisory_ref: {_value_text(payload_advisory.get('advisory_ref'))}",
                f"  - advisory_status: {_value_text(payload_advisory.get('advisory_status'))}",
                f"  - causal_form: {_value_text(payload_advisory.get('causal_form'))}",
                f"  - form2_subtype: {_value_text(payload_advisory.get('form2_subtype'))}",
                f"  - trigger_level: {_value_text(payload_advisory.get('trigger_level'))}",
                f"  - mission_response_kind: {_value_text(payload_advisory.get('mission_response_kind'))}",
                f"  - operator_review_required: {_bool_text(payload_advisory.get('operator_review_required'))}",
                f"  - automatic_dispatch_suppressed: {_bool_text(payload_advisory.get('automatic_dispatch_suppressed'))}",
                f"  - eligible_for_direct_trigger: {_bool_text(payload_advisory.get('eligible_for_direct_trigger'))}",
                f"  - eligible_for_advisory_only: {_bool_text(payload_advisory.get('eligible_for_advisory_only'))}",
                f"  - behavior_delta_margin: {_value_text(payload_advisory.get('behavior_delta_margin'))}",
                f"  - decisive_threshold: {_value_text(payload_advisory.get('decisive_threshold'))}",
                f"  - mission_response_advisory_reason: {_value_text(payload_advisory.get('mission_response_advisory_reason'))}",
                f"  - required_action: {_value_text(payload_advisory.get('required_action'))}",
                f"  - forbidden_action: {_value_text(payload_advisory.get('forbidden_action'))}",
                f"  - advisory_lifecycle_state: {_value_text(payload_advisory.get('advisory_lifecycle_state'))}",
                f"  - climb_delay_audit_ref: {_value_text(payload_advisory_source.get('climb_delay_audit_ref'))}",
                f"  - auto_gate: {_bool_text(payload_advisory.get('auto_gate'))}",
                f"  - task_status_mutated: {_bool_text(payload_advisory.get('task_status_mutated'))}",
                f"  - gate_status_mutated: {_bool_text(payload_advisory.get('gate_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(payload_advisory.get('delivery_completion_claimed'))}",
            ]
        )

    payload_recovery_action = _as_mapping(
        summary.get("payload_recovery_action_artifact")
        or summary.get("payload_recovery_action")
    )
    if payload_recovery_action:
        lines.extend(
            [
                "- payload recovery action:",
                f"  - action_ref: {_value_text(payload_recovery_action.get('action_ref'))}",
                f"  - advisory_ref: {_value_text(payload_recovery_action.get('advisory_ref'))}",
                f"  - advisory_consumed_by_ref: {_value_text(payload_recovery_action.get('advisory_consumed_by_ref'))}",
                f"  - causal_form: {_value_text(payload_recovery_action.get('causal_form'))}",
                f"  - form2_subtype: {_value_text(payload_recovery_action.get('form2_subtype'))}",
                f"  - trigger_level: {_value_text(payload_recovery_action.get('trigger_level'))}",
                f"  - mission_response_kind: {_value_text(payload_recovery_action.get('mission_response_kind'))}",
                f"  - operator_approval_performed: {_bool_text(payload_recovery_action.get('operator_approval_performed'))}",
                f"  - approval_ref: {_value_text(payload_recovery_action.get('approval_ref'))}",
                f"  - dispatch_ref: {_value_text(payload_recovery_action.get('dispatch_ref'))}",
                f"  - bounded_action_kind: {_value_text(payload_recovery_action.get('bounded_action_kind'))}",
                f"  - dispatch_status: {_value_text(payload_recovery_action.get('dispatch_status'))}",
                f"  - recovery_state_observed: {_bool_text(payload_recovery_action.get('recovery_state_observed'))}",
                f"  - recovery_completed: {_bool_text(payload_recovery_action.get('recovery_completed'))}",
                f"  - automatic_dispatch_suppressed: {_bool_text(payload_recovery_action.get('automatic_dispatch_suppressed'))}",
                f"  - approval_free_recovery_dispatch_allowed: {_bool_text(payload_recovery_action.get('approval_free_recovery_dispatch_allowed'))}",
                f"  - delivery_completion_claimed: {_bool_text(payload_recovery_action.get('delivery_completion_claimed'))}",
            ]
        )

    visibility_profile = _as_mapping(summary.get("visibility_condition_profile"))
    visibility_capability = _as_mapping(summary.get("visibility_capability_matrix"))
    visibility_application = _as_mapping(summary.get("visibility_application"))
    visibility_evidence = _as_mapping(summary.get("observed_visibility_condition_evidence"))
    if visibility_profile or visibility_capability or visibility_application or visibility_evidence:
        visibility_applied = _as_mapping(visibility_application.get("applied"))
        visibility_observed = _as_mapping(visibility_evidence.get("observed"))
        fog_materialized = (
            visibility_applied.get("visibility_fog_render_marker_materialized")
            if "visibility_fog_render_marker_materialized" in visibility_applied
            else visibility_observed.get("visibility_fog_render_marker_materialized")
        )
        meters_materialized = (
            visibility_applied.get("visibility_meters_target_materialized")
            if "visibility_meters_target_materialized" in visibility_applied
            else visibility_observed.get("visibility_meters_target_materialized")
        )
        fog_matches = (
            visibility_applied.get("observed_fog_render_matches_requested")
            if "observed_fog_render_matches_requested" in visibility_applied
            else visibility_observed.get("observed_fog_render_matches_requested")
        )
        lines.extend(
            [
                "- visibility fog render marker:",
                f"  - fog_render_marker: {_value_text(visibility_capability.get('fog_render_marker'))}",
                f"  - smoke_render_marker: {_value_text(visibility_capability.get('smoke_render_marker'))}",
                f"  - visibility_meters_target: {_value_text(visibility_capability.get('visibility_meters_target'))}",
                f"  - application_status: {_value_text(visibility_application.get('application_status'))}",
                f"  - observation_status: {_value_text(visibility_evidence.get('observation_status'))}",
                f"  - visibility_fog_render_marker_materialized: {_bool_text(fog_materialized)}",
                f"  - visibility_meters_target_materialized: {_bool_text(meters_materialized)}",
                f"  - observed_fog_render_matches_requested: {_bool_text(fog_matches)}",
            ]
        )

    cleanup = _as_mapping(summary.get("scenario_cleanup_receipt"))
    if cleanup:
        lines.extend(
            [
                "- cleanup:",
                f"  - cleanup_status: {_value_text(cleanup.get('cleanup_status'))}",
                f"  - cleanup_scope: {_value_text(cleanup.get('cleanup_scope'))}",
                f"  - condition_refs: {_value_text(cleanup.get('condition_refs'))}",
            ]
        )
    dynamic_actor = _as_mapping(summary.get("dynamic_actor_profile"))
    actors = _as_sequence(dynamic_actor.get("actors"))
    operational_profile = _as_mapping(summary.get("operational_condition_profile"))
    operational_requested = _as_mapping(operational_profile.get("requested"))
    operational_capability = _as_mapping(summary.get("operational_capability_matrix"))
    operational_evidence = _as_mapping(summary.get("observed_operational_condition_evidence"))
    operational_observed = _as_mapping(operational_evidence.get("observed"))
    multi_vehicle_frame_contract = _as_mapping(summary.get("multi_vehicle_frame_contract"))
    if operational_requested.get("multi_drone_conflict_probe"):
        lines.extend(
            [
                "- multi-drone conflict support:",
                f"  - requested: {_bool_text(operational_requested.get('multi_drone_conflict_probe'))}",
                f"  - capability: {_value_text(operational_capability.get('multi_drone_conflict_probe'))}",
                f"  - multi_vehicle_enabled: {_bool_text(operational_requested.get('multi_vehicle_enabled'))}",
                f"  - primary_vehicle_id: {_value_text(multi_vehicle_frame_contract.get('primary_vehicle_id'))}",
                f"  - additional_vehicle_ids: {_value_text(multi_vehicle_frame_contract.get('additional_vehicle_ids'))}",
                f"  - explicit_vehicle_ids_observed: {_value_text(operational_observed.get('explicit_vehicle_ids_observed'))}",
                f"  - multi_drone_conflict_verified: {_bool_text(operational_observed.get('multi_drone_conflict_verified'))}",
            ]
        )
    if actors:
        actor = _as_mapping(actors[0])
        pose_observation = _as_mapping(summary.get("moving_actor_pose_observation"))
        pose_observed = _as_mapping(pose_observation.get("observed"))
        proximity = _as_mapping(summary.get("moving_actor_proximity_evidence"))
        proximity_observed = _as_mapping(proximity.get("observed"))
        lines.extend(
            [
                "- dynamic actors:",
                f"  - actor_count: {len(actors)}",
                f"  - first_actor_id: {_value_text(actor.get('actor_id'))}",
                f"  - visual_only: {_bool_text(actor.get('visual_only'))}",
                f"  - sdf_scripted_motion_enabled: {_bool_text(actor.get('sdf_scripted_motion_enabled'))}",
                f"  - trajectory_follower_plugin_enabled: {_bool_text(actor.get('trajectory_follower_plugin_enabled'))}",
                f"  - pose_observation_status: {_value_text(pose_observation.get('observation_status'))}",
                f"  - pose_motion_observed: {_bool_text(pose_observed.get('pose_motion_observed'))}",
                f"  - displacement_xy_m: {_value_text(pose_observed.get('displacement_xy_m'))}",
                f"  - proximity_observation_status: {_value_text(proximity.get('observation_status'))}",
                f"  - proximity_advisory_status: {_value_text(proximity_observed.get('advisory_status'))}",
                f"  - min_distance_to_route_m: {_value_text(proximity_observed.get('min_distance_to_route_m'))}",
                f"  - min_distance_to_dropoff_m: {_value_text(proximity_observed.get('min_distance_to_dropoff_m'))}",
                f"  - collision_enabled: {_bool_text(actor.get('collision_enabled'))}",
                f"  - sensor_visible_claimed: {_bool_text(actor.get('sensor_visible_claimed'))}",
                f"  - route_blocking_enabled: {_bool_text(actor.get('route_blocking_enabled'))}",
                f"  - incident_claimed: {_bool_text(actor.get('incident_claimed'))}",
            ]
        )
    collision_profile = _as_mapping(summary.get("collision_obstacle_profile"))
    collision_obstacles = _as_sequence(collision_profile.get("obstacles"))
    if collision_obstacles:
        obstacle = _as_mapping(collision_obstacles[0])
        spawn_application = _as_mapping(
            summary.get("gazebo_route_corridor_obstacle_spawn_application")
        )
        spawn_applied = _as_mapping(spawn_application.get("applied"))
        spawn_observed = _as_mapping(spawn_application.get("observed"))
        collision_evidence = _as_mapping(summary.get("collision_obstacle_evidence"))
        collision_observed = _as_mapping(collision_evidence.get("observed"))
        lines.extend(
            [
                "- collision-enabled obstacle:",
                f"  - obstacle_count: {len(collision_obstacles)}",
                f"  - first_obstacle_id: {_value_text(obstacle.get('obstacle_id'))}",
                f"  - collision_enabled: {_bool_text(obstacle.get('collision_enabled'))}",
                f"  - trajectory_follower_plugin_enabled: {_bool_text(obstacle.get('trajectory_follower_plugin_enabled'))}",
                f"  - observation_status: {_value_text(collision_evidence.get('observation_status'))}",
                f"  - collision_geometry_observed: {_bool_text(collision_observed.get('collision_geometry_observed'))}",
                f"  - pose_observed: {_bool_text(collision_observed.get('pose_observed'))}",
                f"  - min_distance_to_route_m: {_value_text(collision_observed.get('min_distance_to_route_m'))}",
                f"  - min_distance_to_dropoff_m: {_value_text(collision_observed.get('min_distance_to_dropoff_m'))}",
                f"  - contact_topic_observed: {_bool_text(collision_observed.get('contact_topic_observed'))}",
                f"  - contact_event_observed: {_bool_text(collision_observed.get('contact_event_observed'))}",
                f"  - route_blocking_candidate: {_bool_text(collision_observed.get('route_blocking_candidate'))}",
                f"  - route_blocking_observed: {_bool_text(collision_observed.get('route_blocking_observed'))}",
                f"  - incident_observed: {_bool_text(collision_observed.get('incident_observed'))}",
                f"  - traffic_conflict_verified: {_bool_text(collision_observed.get('traffic_conflict_verified'))}",
                "- Gazebo route-corridor obstacle spawn applicator:",
                f"  - application_status: {_value_text(spawn_application.get('application_status'))}",
                f"  - method: {_value_text(spawn_applied.get('method'))}",
                f"  - world_sdf_hash_match: {_bool_text(spawn_observed.get('world_sdf_hash_match'))}",
                f"  - model_materialized: {_bool_text(spawn_observed.get('model_materialized'))}",
                f"  - collision_geometry_materialized: {_bool_text(spawn_observed.get('collision_geometry_materialized'))}",
                f"  - trajectory_follower_materialized: {_bool_text(spawn_observed.get('trajectory_follower_materialized'))}",
                f"  - route_blocking_verified: {_bool_text(spawn_observed.get('route_blocking_verified'))}",
                f"  - task_status_mutated: {_bool_text(spawn_observed.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(spawn_observed.get('delivery_completion_claimed'))}",
            ]
        )
    route_blocking_candidate = _as_mapping(summary.get("route_blocking_candidate_evidence"))
    route_blocking_observed = _as_mapping(route_blocking_candidate.get("observed"))
    if route_blocking_observed:
        lines.extend(
            [
                "- route blocking candidate:",
                f"  - observation_status: {_value_text(route_blocking_candidate.get('observation_status'))}",
                f"  - candidate_threshold_m: {_value_text(route_blocking_observed.get('candidate_threshold_m'))}",
                f"  - min_distance_to_route_m: {_value_text(route_blocking_observed.get('min_distance_to_route_m'))}",
                f"  - route_blocking_candidate: {_bool_text(route_blocking_observed.get('route_blocking_candidate'))}",
                f"  - route_blocking_verified: {_bool_text(route_blocking_observed.get('route_blocking_verified'))}",
                f"  - operator_review_required: {_bool_text(route_blocking_observed.get('operator_review_required'))}",
                f"  - incident_report_created: {_bool_text(route_blocking_observed.get('incident_report_created'))}",
                f"  - task_status_mutated: {_bool_text(route_blocking_observed.get('task_status_mutated'))}",
            ]
        )
    contact_incident = _as_mapping(summary.get("contact_event_incident_evidence"))
    contact_incident_observed = _as_mapping(contact_incident.get("observed"))
    if contact_incident_observed:
        lines.extend(
            [
                "- contact event incident candidate:",
                f"  - observation_status: {_value_text(contact_incident.get('observation_status'))}",
                f"  - contact_event_observed: {_bool_text(contact_incident_observed.get('contact_event_observed'))}",
                f"  - contact_event_incident_candidate: {_bool_text(contact_incident_observed.get('contact_event_incident_candidate'))}",
                f"  - operator_review_required: {_bool_text(contact_incident_observed.get('operator_review_required'))}",
                f"  - incident_verified: {_bool_text(contact_incident_observed.get('incident_verified'))}",
                f"  - route_blocking_verified: {_bool_text(contact_incident_observed.get('route_blocking_verified'))}",
                f"  - task_status_mutated: {_bool_text(contact_incident_observed.get('task_status_mutated'))}",
            ]
        )
    horizontal_contact = _as_mapping(summary.get("horizontal_route_contact_topic_integration"))
    horizontal_contact_observed = _as_mapping(horizontal_contact.get("observed"))
    if horizontal_contact_observed:
        lines.extend(
            [
                "- horizontal route contact topic integration:",
                f"  - integration_status: {_value_text(horizontal_contact.get('integration_status'))}",
                f"  - integration_mode: {_value_text(horizontal_contact.get('integration_mode'))}",
                f"  - route_world_contact_sensor_injected: {_bool_text(horizontal_contact.get('horizontal_route_world_contact_sensor_injected'))}",
                f"  - contact_event_observed: {_bool_text(horizontal_contact_observed.get('contact_event_observed'))}",
                f"  - operator_review_required: {_bool_text(horizontal_contact_observed.get('operator_review_required'))}",
                f"  - task_status_mutated: {_bool_text(horizontal_contact_observed.get('task_status_mutated'))}",
            ]
        )
    horizontal_contact_candidate = _as_mapping(
        summary.get("horizontal_route_contact_scoped_verifier_candidate")
    )
    horizontal_contact_candidate_observed = _as_mapping(
        horizontal_contact_candidate.get("observed")
    )
    if horizontal_contact_candidate_observed:
        lines.extend(
            [
                "- horizontal route contact scoped verifier candidate:",
                f"  - candidate_status: {_value_text(horizontal_contact_candidate.get('candidate_status'))}",
                f"  - contact_event_observed: {_bool_text(horizontal_contact_candidate_observed.get('contact_event_observed'))}",
                f"  - scoped_verifier_candidate: {_bool_text(horizontal_contact_candidate_observed.get('scoped_verifier_candidate'))}",
                f"  - operator_review_required: {_bool_text(horizontal_contact_candidate_observed.get('operator_review_required'))}",
                f"  - traffic_conflict_verified: {_bool_text(horizontal_contact_candidate_observed.get('traffic_conflict_verified'))}",
                f"  - task_status_mutated: {_bool_text(horizontal_contact_candidate_observed.get('task_status_mutated'))}",
            ]
        )
    horizontal_contact_verification = _as_mapping(
        summary.get("horizontal_route_contact_incident_verification")
    )
    horizontal_contact_verification_observed = _as_mapping(
        horizontal_contact_verification.get("observed")
    )
    if horizontal_contact_verification_observed:
        lines.extend(
            [
                "- horizontal route contact incident verifier:",
                f"  - verification_status: {_value_text(horizontal_contact_verification.get('verification_status'))}",
                f"  - verification_scope: {_value_text(horizontal_contact_verification.get('verification_scope'))}",
                f"  - contact_event_observed: {_bool_text(horizontal_contact_verification_observed.get('contact_event_observed'))}",
                f"  - incident_verified: {_bool_text(horizontal_contact_verification_observed.get('incident_verified'))}",
                f"  - route_blocking_verified: {_bool_text(horizontal_contact_verification_observed.get('route_blocking_verified'))}",
                f"  - traffic_conflict_verified: {_bool_text(horizontal_contact_verification_observed.get('traffic_conflict_verified'))}",
                f"  - auto_gate: {_bool_text(horizontal_contact_verification_observed.get('auto_gate'))}",
                f"  - task_status_mutated: {_bool_text(horizontal_contact_verification_observed.get('task_status_mutated'))}",
            ]
        )
    incident_informed_traffic = _as_mapping(
        summary.get("horizontal_route_incident_informed_traffic_conflict_verification")
    )
    incident_informed_traffic_observed = _as_mapping(
        incident_informed_traffic.get("observed")
    )
    if incident_informed_traffic_observed:
        lines.extend(
            [
                "- incident-informed traffic conflict verifier:",
                f"  - verification_status: {_value_text(incident_informed_traffic.get('verification_status'))}",
                f"  - verification_scope: {_value_text(incident_informed_traffic.get('verification_scope'))}",
                f"  - incident_verified: {_bool_text(incident_informed_traffic_observed.get('incident_verified'))}",
                f"  - traffic_conflict_verified: {_bool_text(incident_informed_traffic_observed.get('traffic_conflict_verified'))}",
                f"  - route_blocking_verified: {_bool_text(incident_informed_traffic_observed.get('route_blocking_verified'))}",
                f"  - auto_gate: {_bool_text(incident_informed_traffic_observed.get('auto_gate'))}",
                f"  - task_status_mutated: {_bool_text(incident_informed_traffic_observed.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(incident_informed_traffic_observed.get('delivery_completion_claimed'))}",
            ]
        )
    incident_informed_route_blocking = _as_mapping(
        summary.get("horizontal_route_incident_informed_route_blocking_verification")
    )
    incident_informed_route_blocking_observed = _as_mapping(
        incident_informed_route_blocking.get("observed")
    )
    if incident_informed_route_blocking_observed:
        lines.extend(
            [
                "- incident-informed route blocking verifier:",
                f"  - verification_status: {_value_text(incident_informed_route_blocking.get('verification_status'))}",
                f"  - verification_scope: {_value_text(incident_informed_route_blocking.get('verification_scope'))}",
                f"  - traffic_conflict_verified: {_bool_text(incident_informed_route_blocking_observed.get('traffic_conflict_verified'))}",
                f"  - route_blocking_candidate: {_bool_text(incident_informed_route_blocking_observed.get('route_blocking_candidate'))}",
                f"  - route_blocking_verified: {_bool_text(incident_informed_route_blocking_observed.get('route_blocking_verified'))}",
                f"  - auto_gate: {_bool_text(incident_informed_route_blocking_observed.get('auto_gate'))}",
                f"  - task_status_mutated: {_bool_text(incident_informed_route_blocking_observed.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(incident_informed_route_blocking_observed.get('delivery_completion_claimed'))}",
            ]
        )
    incident_report = _as_mapping(summary.get("operational_incident_report"))
    incident_observed = _as_mapping(incident_report.get("observed"))
    if incident_observed:
        lines.extend(
            [
                "- operational incident report:",
                f"  - report_status: {_value_text(incident_report.get('report_status'))}",
                f"  - route_blocking_candidate: {_bool_text(incident_observed.get('route_blocking_candidate'))}",
                f"  - contact_event_incident_candidate: {_bool_text(incident_observed.get('contact_event_incident_candidate'))}",
                f"  - contact_event_observed: {_bool_text(incident_observed.get('contact_event_observed'))}",
                f"  - operator_review_required: {_bool_text(incident_observed.get('operator_review_required'))}",
                f"  - auto_gate: {_bool_text(incident_observed.get('auto_gate'))}",
                f"  - incident_verified: {_bool_text(incident_observed.get('incident_verified'))}",
                f"  - route_blocking_verified: {_bool_text(incident_observed.get('route_blocking_verified'))}",
                f"  - task_status_mutated: {_bool_text(incident_observed.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(incident_observed.get('delivery_completion_claimed'))}",
            ]
        )
    traffic_verification = _as_mapping(summary.get("traffic_conflict_verification"))
    traffic_observed = _as_mapping(traffic_verification.get("observed"))
    if traffic_observed:
        lines.extend(
            [
                "- traffic conflict verifier:",
                f"  - verification_status: {_value_text(traffic_verification.get('verification_status'))}",
                f"  - verification_scope: {_value_text(traffic_observed.get('verification_scope'))}",
                f"  - traffic_conflict_verified: {_bool_text(traffic_observed.get('traffic_conflict_verified'))}",
                f"  - route_blocking_verified: {_bool_text(traffic_observed.get('route_blocking_verified'))}",
                f"  - dropoff_verified: {_bool_text(traffic_observed.get('dropoff_verified'))}",
                f"  - task_status_mutated: {_bool_text(traffic_observed.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(traffic_observed.get('delivery_completion_claimed'))}",
            ]
        )
    route_blocking_verification = _as_mapping(summary.get("route_blocking_verification"))
    route_blocking_verified = _as_mapping(route_blocking_verification.get("observed"))
    if route_blocking_verified:
        lines.extend(
            [
                "- route blocking verifier:",
                f"  - verification_status: {_value_text(route_blocking_verification.get('verification_status'))}",
                f"  - verification_scope: {_value_text(route_blocking_verified.get('verification_scope'))}",
                f"  - route_blocking_verified: {_bool_text(route_blocking_verified.get('route_blocking_verified'))}",
                f"  - gate_candidate: {_bool_text(route_blocking_verified.get('gate_candidate'))}",
                f"  - auto_gate: {_bool_text(route_blocking_verified.get('auto_gate'))}",
                f"  - task_status_mutated: {_bool_text(route_blocking_verified.get('task_status_mutated'))}",
                f"  - gate_status_mutated: {_bool_text(route_blocking_verified.get('gate_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(route_blocking_verified.get('delivery_completion_claimed'))}",
            ]
        )
    alternate_candidate = _as_mapping(summary.get("alternate_landing_candidate_evidence"))
    alternate_observed = _as_mapping(alternate_candidate.get("observed"))
    if alternate_observed:
        lines.extend(
            [
                "- alternate landing candidate:",
                f"  - observation_status: {_value_text(alternate_candidate.get('observation_status'))}",
                f"  - alternate_landing_candidate: {_bool_text(alternate_observed.get('alternate_landing_candidate'))}",
                f"  - candidate_id: {_value_text(alternate_observed.get('candidate_id'))}",
                f"  - route_blocking_verified: {_bool_text(alternate_observed.get('route_blocking_verified'))}",
                f"  - px4_route_changed: {_bool_text(alternate_observed.get('px4_route_changed'))}",
                f"  - rth_commanded: {_bool_text(alternate_observed.get('rth_commanded'))}",
                f"  - task_status_mutated: {_bool_text(alternate_observed.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(alternate_observed.get('delivery_completion_claimed'))}",
            ]
        )
    alternate_execution = _as_mapping(summary.get("alternate_landing_execution_request"))
    alternate_dispatch = _as_mapping(summary.get("alternate_landing_command_dispatch"))
    alternate_behavior = _as_mapping(summary.get("alternate_landing_behavior_observation"))
    if alternate_behavior:
        lines.extend(
            [
                "- alternate landing execution:",
                f"  - request_status: {_value_text(alternate_execution.get('request_status'))}",
                f"  - dispatch_status: {_value_text(alternate_dispatch.get('dispatch_status'))}",
                f"  - command_ack_observed: {_bool_text(alternate_dispatch.get('command_ack_observed'))}",
                f"  - command_ack_result_name: {_value_text(alternate_dispatch.get('command_ack_result_name'))}",
                f"  - completion_basis: {_value_text(alternate_behavior.get('completion_basis') or alternate_dispatch.get('completion_basis'))}",
                f"  - behavior_status: {_value_text(alternate_behavior.get('observation_status'))}",
                f"  - alternate_landing_behavior_observed: {_bool_text(alternate_behavior.get('alternate_landing_behavior_observed'))}",
                f"  - land_commanded: {_bool_text(alternate_behavior.get('land_commanded'))}",
                f"  - landing_observed: {_bool_text(alternate_behavior.get('landing_observed'))}",
                f"  - px4_route_changed: {_bool_text(alternate_behavior.get('px4_route_changed'))}",
                f"  - delivery_completion_claimed: {_bool_text(alternate_behavior.get('delivery_completion_claimed'))}",
            ]
        )
    alternate_upload_request = _as_mapping(summary.get("alternate_mission_upload_request"))
    alternate_upload_receipt = _as_mapping(summary.get("alternate_mission_upload_receipt"))
    alternate_route_behavior = _as_mapping(summary.get("alternate_route_behavior_observation"))
    alternate_route_execution = _as_mapping(summary.get("alternate_route_execution_evidence"))
    alternate_route_execution_observed = _as_mapping(alternate_route_execution.get("observed"))
    if alternate_route_behavior:
        lines.extend(
            [
                "- alternate mission upload:",
                f"  - request_status: {_value_text(alternate_upload_request.get('request_status'))}",
                f"  - upload_status: {_value_text(alternate_upload_receipt.get('upload_status'))}",
                f"  - mission_ack_observed: {_bool_text(alternate_upload_receipt.get('mission_ack_observed'))}",
                f"  - mission_ack_type: {_value_text(alternate_upload_receipt.get('mission_ack_type'))}",
                f"  - mission_item_count: {_value_text(alternate_upload_receipt.get('mission_item_count'))}",
                f"  - contains_waypoint_item: {_bool_text(alternate_upload_request.get('contains_waypoint_item'))}",
                f"  - contains_land_item: {_bool_text(alternate_upload_request.get('contains_land_item'))}",
                f"  - alternate_mission_uploaded: {_bool_text(alternate_route_behavior.get('alternate_mission_uploaded'))}",
                f"  - alternate_route_execution_observed: {_bool_text(alternate_route_behavior.get('alternate_route_execution_observed'))}",
                f"  - alternate_waypoint_reached_observed: {_bool_text(alternate_route_behavior.get('alternate_waypoint_reached_observed'))}",
                f"  - alternate_landing_behavior_observed: {_bool_text(alternate_route_behavior.get('alternate_landing_behavior_observed'))}",
                f"  - dropoff_verified: {_bool_text(alternate_route_behavior.get('dropoff_verified'))}",
                f"  - task_status_mutated: {_bool_text(alternate_route_behavior.get('task_status_mutated'))}",
                f"  - delivery_completion_claimed: {_bool_text(alternate_route_behavior.get('delivery_completion_claimed'))}",
            ]
        )
    if alternate_route_execution:
        lines.extend(
            [
                "- alternate route execution evidence:",
                f"  - observation_status: {_value_text(alternate_route_execution.get('observation_status'))}",
                f"  - alternate_route_execution_observed: {_bool_text(alternate_route_execution.get('alternate_route_execution_observed'))}",
                f"  - alternate_waypoint_reached_observed: {_bool_text(alternate_route_execution.get('alternate_waypoint_reached_observed'))}",
                f"  - horizontal_progress_toward_alternate_waypoint_m: {_value_text(alternate_route_execution_observed.get('horizontal_progress_toward_alternate_waypoint_m'))}",
                f"  - final_distance_to_alternate_waypoint_m: {_value_text(alternate_route_execution_observed.get('final_distance_to_alternate_waypoint_m'))}",
                f"  - completion_basis: {_value_text(alternate_route_execution_observed.get('completion_basis'))}",
                f"  - dropoff_verified: {_bool_text(alternate_route_execution_observed.get('dropoff_verified'))}",
                f"  - delivery_completion_claimed: {_bool_text(alternate_route_execution_observed.get('delivery_completion_claimed'))}",
            ]
        )
    rth_execution = _as_mapping(summary.get("rth_execution_request"))
    rth_dispatch = _as_mapping(summary.get("rth_command_dispatch"))
    rth_behavior = _as_mapping(summary.get("rth_behavior_observation"))
    if rth_behavior:
        lines.extend(
            [
                "- RTH behavior observation:",
                f"  - request_status: {_value_text(rth_execution.get('request_status'))}",
                f"  - dispatch_status: {_value_text(rth_dispatch.get('dispatch_status'))}",
                f"  - command_ack_observed: {_bool_text(rth_dispatch.get('command_ack_observed'))}",
                f"  - command_ack_result_name: {_value_text(rth_dispatch.get('command_ack_result_name'))}",
                f"  - completion_basis: {_value_text(rth_behavior.get('completion_basis') or rth_dispatch.get('completion_basis'))}",
                f"  - behavior_status: {_value_text(rth_behavior.get('observation_status'))}",
                f"  - return_to_home_behavior_observed: {_bool_text(rth_behavior.get('return_to_home_behavior_observed'))}",
                f"  - rth_commanded: {_bool_text(rth_behavior.get('rth_commanded'))}",
                f"  - rth_state_observed: {_bool_text(rth_behavior.get('rth_state_observed'))}",
                f"  - rth_state_label: {_value_text(rth_behavior.get('rth_state_label'))}",
                f"  - px4_route_changed: {_bool_text(rth_behavior.get('px4_route_changed'))}",
                f"  - delivery_completion_claimed: {_bool_text(rth_behavior.get('delivery_completion_claimed'))}",
            ]
        )
    return lines


def render_mission_designer_realism_report(summary: Mapping[str, Any]) -> str:
    """Render existing realism artifacts as a non-authoritative Markdown report."""

    lines = [
        "# Mission Designer Realism Report",
        "",
        "## Boundary",
        "",
        REALISM_REPORT_BOUNDARY_NOTE,
        "",
        "## Runtime Evidence",
        "",
        *_runtime_lines(summary),
        "",
        "## Epic Closure View",
        "",
        *_closure_lines(summary),
        "",
        "## Requested / Applied / Observed Matrix",
        "",
        "| condition | requested | applied | observed | unsupported / approximated | cleanup |",
        "| --- | --- | --- | --- | --- | --- |",
        *[_slice_row(summary, slice_) for slice_ in REALISM_SLICES],
        "",
        "## Evidence Details",
        "",
        *(_detail_lines(summary) or ["- no realism detail artifacts attached"]),
        "",
        "## Non-Goals",
        "",
        "- This report does not mutate task status or task artifacts.",
        "- This report does not infer dropoff verification from replay, telemetry gaps, heartbeat observations, markers, or scenario labels.",
        "- This report does not claim MAVLink/RF/link loss, publisher transport loss, vehicle recovery/failsafe behavior, mission failure, upload/progress mutation, task/gate mutation, or delivery completion from observer sample pause.",
        "- This report does not claim delivery completion.",
    ]
    return "\n".join(lines) + "\n"


def _load_summary(text: str) -> dict[str, Any]:
    stripped = text.strip()
    for line in stripped.splitlines():
        if line.startswith("SMOKE_SUMMARY_JSON "):
            return json.loads(line.removeprefix("SMOKE_SUMMARY_JSON "))
    return json.loads(stripped)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a read-only Mission Designer realism report.",
    )
    parser.add_argument(
        "summary_json",
        nargs="?",
        help="Path to a Mission Designer SITL summary JSON file. Reads stdin when omitted.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.summary_json:
        text = Path(args.summary_json).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    print(render_mission_designer_realism_report(_load_summary(text)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
