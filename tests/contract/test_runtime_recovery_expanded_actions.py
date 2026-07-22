from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
import click
from rich.console import Console

from missionos_cli import cli as missionos_cli
from scripts import smoke_missionos_auto_mission_full_runtime_probe as auto_probe
from src.gateway import server as gateway_server
from src.intelligence import missionos_agent_runtime
from src.runtime import missionos_auto_mission_runner as auto_runner
from src.runtime import px4_gazebo_mission_designer_sitl_live_flight_run as live_run
from src.runtime.px4_gazebo_route.compound_hazard_transition import (
    build_wind_safe_window_evidence,
)
from src.runtime.recovery_window_summary import build_recovery_window_summary
from src.runtime.task_store import TaskStore

pytestmark = pytest.mark.contract


def _auto_monitor_summary_with_nav_context(
    *,
    middle_nav_state: int,
    middle_authority_context: str,
):
    compilation = auto_runner.compile_operator_coordinate_route_auto_mission(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6832,
            "dropoff_longitude": 139.7738,
            "dropoff_roof_height_agl_m": 0.0,
        }
    )
    samples = [
        auto_runner.MissionOSAutoMissionTelemetrySample(
            sample_index=0,
            elapsed_seconds=0.0,
            nav_state=auto_runner.PX4_NAVIGATION_STATE_AUTO_MISSION,
            local_x_m=0.0,
            local_y_m=0.0,
            local_z_m=-30.0,
            mission_current_seq=1,
            telemetry_stale=False,
        ),
        auto_runner.MissionOSAutoMissionTelemetrySample(
            sample_index=1,
            elapsed_seconds=1.0,
            nav_state=middle_nav_state,
            nav_authority_context=middle_authority_context,
            local_x_m=1.0,
            local_y_m=1.0,
            local_z_m=-30.0,
            mission_current_seq=1,
            telemetry_stale=False,
        ),
        auto_runner.MissionOSAutoMissionTelemetrySample(
            sample_index=2,
            elapsed_seconds=2.0,
            nav_state=auto_runner.PX4_NAVIGATION_STATE_AUTO_MISSION,
            local_x_m=2.0,
            local_y_m=2.0,
            local_z_m=-30.0,
            mission_current_seq=1,
            telemetry_stale=False,
        ),
    ]
    return auto_runner.build_auto_mission_runtime_monitor_summary(
        compilation=compilation,
        mission_upload_accepted=True,
        mission_ack_observed=True,
        mission_ack_result=0,
        arm_command_ack_observed=True,
        arm_command_ack_result=0,
        auto_mission_mode_ack_observed=True,
        auto_mission_mode_ack_result=0,
        samples=samples,
        monitor_target_seconds=30.0,
        monitor_elapsed_seconds=2.0,
        heartbeat_samples=3,
        abort_policy_selected_action="land",
        recovery_path_taken=None,
        final_landing_safe=True,
        min_progress_m=1.0,
        no_progress_grace_seconds=20.0,
        min_route_altitude_m=20.0,
        altitude_grace_seconds=0.0,
    )


def test_runtime_monitor_accepts_only_authority_bound_recovery_nav_transitions() -> None:
    authorized_hold = _auto_monitor_summary_with_nav_context(
        middle_nav_state=auto_runner.PX4_NAVIGATION_STATE_AUTO_LOITER,
        middle_authority_context="preauthorized_safety_hold",
    )
    unauthorized_hold = _auto_monitor_summary_with_nav_context(
        middle_nav_state=auto_runner.PX4_NAVIGATION_STATE_AUTO_LOITER,
        middle_authority_context="auto_mission",
    )

    assert authorized_hold.mode_loss_status == "ok"
    assert authorized_hold.authorized_recovery_nav_state_samples == (4,)
    assert authorized_hold.authorized_recovery_nav_state_sample_count == 1
    assert "auto_mission_mode_lost" not in authorized_hold.guard_failure_reasons
    assert unauthorized_hold.mode_loss_status == "blocked"
    assert "auto_mission_mode_lost" in unauthorized_hold.guard_failure_reasons

    authorized_offboard = _auto_monitor_summary_with_nav_context(
        middle_nav_state=auto_runner.PX4_NAVIGATION_STATE_OFFBOARD,
        middle_authority_context="approved_bounded_recovery",
    )
    assert authorized_offboard.mode_loss_status == "ok"
    assert authorized_offboard.authorized_recovery_nav_state_samples == (14,)
    assert authorized_offboard.authorized_recovery_nav_state_sample_count == 1


def test_final_verification_chain_explains_phase3_pending_gate_relationship() -> None:
    chain = auto_probe._final_verification_chain(
        summary={
            "summary_scope": "phase3_runtime_observation_only",
            "runtime_status": "monitor_window_completed",
            "guard_failure_reasons": [],
            "downstream_completion_gate_refs": [
                "waypoint_gate",
                "dropoff_gate",
                "payload_release_sim_gate",
                "sitl_delivery_gate",
            ],
        },
        waypoint_gate={"route_completed_claimed": True},
        dropoff_gate={"dropoff_verified": True},
        payload_release_sim_gate={"payload_release_observed_sim": True},
        sitl_delivery_gate={"sitl_delivery_claimed": True},
    )

    assert chain["relationship"] == "downstream_gates_complete_phase3_observation"
    assert chain["final_verification_status"] == "verified"
    assert chain["delivery_completion_claimed"] is False


def test_verified_recovery_supersedes_only_the_bypassed_route_segment() -> None:
    reached = [1, 2, 3, 4, *range(12, 21)]
    runtime_summary = {
        "route_waypoint_seq_start": 1,
        "route_waypoint_seq_end": 20,
        "mission_item_reached_events": reached,
    }
    probe_observed = {
        "monitor": {
            "operator_recovery": {
                "request": {
                    "operator_approved": True,
                    "explicit_recovery_dispatch_approval": True,
                    "recovery_action": "avoid_obstacle",
                },
                "command": {
                    "target_reached": True,
                    "resume_mission_current_frame_sent": True,
                    "resume_mission_current_seq_observed": True,
                    "resume_mission_current_seq": 12,
                    "resume_mission_seq_after_obstacle": 12,
                    "resume_auto_status": "resumed_auto_mission",
                    "resume_safety_verification": {
                        "verification_status": "verified",
                        "resume_auto_authorized": True,
                        "resume_mission_seq_advanced": True,
                        "blocked_reasons": [],
                    },
                },
            }
        }
    }

    superseded = auto_probe._verified_recovery_superseded_waypoint_sequences(
        runtime_summary=runtime_summary,
        probe_observed=probe_observed,
    )
    gate = auto_runner.build_auto_mission_waypoint_gate_summary(
        route_waypoint_seq_start=1,
        route_waypoint_seq_end=20,
        mission_item_reached_events=reached,
        recovery_superseded_waypoint_sequences=superseded,
        recovery_supersession_verified=True,
    )

    assert superseded == tuple(range(5, 12))
    assert gate.unreached_route_waypoint_sequences == tuple(range(5, 12))
    assert gate.recovery_superseded_waypoint_sequences == tuple(range(5, 12))
    assert gate.missing_route_waypoint_sequences == ()
    assert gate.all_waypoints_reached is False
    assert gate.all_route_requirements_satisfied is True
    assert gate.route_completion_basis == "verified_recovery_supersession"
    assert gate.route_completed_claimed is True


def test_two_verified_recoveries_supersede_two_separate_route_segments() -> None:
    reached = [1, 2, 3, 4, 12, 13, 14, *range(17, 21)]

    def attempt(resume_seq: int) -> dict:
        return {
            "request": {
                "operator_approved": True,
                "explicit_recovery_dispatch_approval": True,
                "recovery_action": "avoid_obstacle",
            },
            "command": {
                "target_reached": True,
                "resume_mission_current_frame_sent": True,
                "resume_mission_current_seq_observed": True,
                "resume_mission_current_seq": resume_seq,
                "resume_mission_seq_after_obstacle": resume_seq,
                "resume_auto_status": "resumed_auto_mission",
                "resume_safety_verification": {
                    "verification_status": "verified",
                    "resume_auto_authorized": True,
                    "resume_mission_seq_advanced": True,
                    "blocked_reasons": [],
                },
            },
        }

    superseded = auto_probe._verified_recovery_superseded_waypoint_sequences(
        runtime_summary={
            "route_waypoint_seq_start": 1,
            "route_waypoint_seq_end": 20,
            "mission_item_reached_events": reached,
        },
        probe_observed={
            "monitor": {
                "operator_recovery_attempts": [attempt(12), attempt(17)],
            }
        },
    )

    assert superseded == (*range(5, 12), 15, 16)


def test_unapproved_recovery_cannot_supersede_missing_waypoints() -> None:
    reached = [1, 2, 3, 4, *range(12, 21)]
    runtime_summary = {
        "route_waypoint_seq_start": 1,
        "route_waypoint_seq_end": 20,
        "mission_item_reached_events": reached,
    }
    superseded = auto_probe._verified_recovery_superseded_waypoint_sequences(
        runtime_summary=runtime_summary,
        probe_observed={
            "monitor": {
                "operator_recovery": {
                    "request": {
                        "operator_approved": False,
                        "explicit_recovery_dispatch_approval": False,
                        "recovery_action": "avoid_obstacle",
                    },
                    "command": {},
                }
            }
        },
    )
    gate = auto_runner.build_auto_mission_waypoint_gate_summary(
        route_waypoint_seq_start=1,
        route_waypoint_seq_end=20,
        mission_item_reached_events=reached,
        recovery_superseded_waypoint_sequences=superseded,
        recovery_supersession_verified=bool(superseded),
    )

    assert superseded == ()
    assert gate.missing_route_waypoint_sequences == tuple(range(5, 12))
    assert gate.route_completed_claimed is False
    assert gate.route_completion_basis == "incomplete"


def test_run_provenance_keeps_origin_hash_without_prompt_or_response_text() -> None:
    origin_without_hash = {
        "schema_version": "missionos_runtime_recovery_proposal_origin.v1",
        "origin_kind": "hosted_llm",
        "provider": "google_adk_gemini",
        "model_id": "gemini-3.1-flash-lite",
        "invocation_kind": "google_adk_function_tool_call",
        "prompt_sha256": "a" * 64,
        "response_sha256": "b" * 64,
        "fallback_reason": "",
        "source_proposal_id": "",
        "contains_prompt_or_response_text": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    origin_hash = live_run._canonical_sha256(origin_without_hash)
    provenance = auto_probe._runtime_recovery_provenance_from_probe(
        probe_observed={
            "monitor": {
                "operator_recovery": {
                    "request_observed": True,
                    "request": {
                        "proposal_id": "runtime_recovery_proposal_test",
                        "proposal_origin": {
                            **origin_without_hash,
                            "origin_sha256": origin_hash,
                        },
                        "proposal_origin_sha256": origin_hash,
                        "approval_ref": "approval_test",
                        "recovery_action": "avoid_obstacle",
                        "recovery_parameters": {
                            "target_x_m": 1.0,
                            "target_y_m": 2.0,
                        },
                        "operator_approved": True,
                    },
                    "command": {
                        "attempted": True,
                        "target_reached": True,
                        "resume_auto_status": "resumed_auto_mission",
                    },
                }
            }
        }
    )

    assert provenance["provenance_status"] == "verified"
    assert provenance["proposal_origin"]["provider"] == "google_adk_gemini"
    assert provenance["proposal_origin"]["model_id"] == "gemini-3.1-flash-lite"
    assert provenance["contains_prompt_or_response_text"] is False


def _assessment(
    action: str,
    *,
    parameters: dict | None = None,
    telemetry: dict | None = None,
) -> dict:
    return missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": action,
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": parameters or {},
        },
        telemetry_snapshot=telemetry or {},
        recovery_policy={
            "preauthorized_actions": [
                "return_to_launch",
                "land",
                "adjust_altitude",
                "adjust_speed",
                "reroute",
                "avoid_obstacle",
            ]
        },
    )


def _planner_tool_telemetry() -> dict:
    return {
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 25.0,
        },
        "terrain": {
            "terrain_clearance_m": 18.0,
            "terrain_clearance_target_m": 30.0,
            "terrain_clearance_margin_m": -12.0,
            "terrain_clearance_below_minimum": True,
        },
        "route": {
            "active_leg": {
                "from_x_m": 0.0,
                "from_y_m": 0.0,
                "to_x_m": 200.0,
                "to_y_m": 0.0,
            }
        },
        "obstacle": {
            "obstacle_detected": True,
            "building_risk_detected": True,
            "obstacle_manifest": {
                "obstacles": [
                    {
                        "name": "missionos_landing_zone_blocker",
                        "kind": "building_box",
                        "source": "gazebo_pose_readback",
                        "x_m": 100.0,
                        "y_m": 0.0,
                        "size_x_m": 20.0,
                        "size_y_m": 20.0,
                    }
                ]
            },
        },
    }


def _planner_policy() -> dict:
    return {
        "policy_ref": "test_recovery_tool_policy",
        "preauthorized_actions": [
            "adjust_altitude",
            "reroute",
            "avoid_obstacle",
        ],
        "min_terrain_clearance_m": 30.0,
        "max_adjust_altitude_m": 120.0,
        "max_reroute_target_abs_m": 5000.0,
    }


def test_runtime_recovery_prompt_advertises_planner_function_tool() -> None:
    payload = missionos_agent_runtime._runtime_recovery_prompt_payload(
        telemetry_snapshot={},
        mission_context={},
        recovery_policy={},
    )

    tool = payload["role_contract"]["function_tools"][0]
    assert tool["name"] == missionos_agent_runtime.MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME
    assert tool["must_use_before_actions"] == [
        "adjust_altitude",
        "reroute",
        "avoid_obstacle",
    ]
    assert tool["copy_tool_proposed_parameters_exactly"] is True


def test_runtime_recovery_planner_tool_computes_altitude_and_obstacle_targets() -> None:
    avoid_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=_planner_tool_telemetry(),
        mission_context={},
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
        request_reason="source-backed building risk near original route",
    )
    altitude_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=_planner_tool_telemetry(),
        mission_context={},
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
    )

    assert avoid_result["tool_status"] == "computed"
    avoid_candidate = avoid_result["recommended_candidate"]
    assert avoid_candidate["selected_bounded_action"] == "avoid_obstacle"
    assert avoid_candidate["proposed_parameters"] == {
        "target_x_m": 130.0,
        "target_y_m": 59.429,
        "target_altitude_m": 45.0,
        "source_obstacle_name": "missionos_landing_zone_blocker",
    }
    assert avoid_candidate["basis"]["target_is_beyond_obstacle"] is True
    assert avoid_candidate["basis"]["pass_distance_after_obstacle_m"] == 30.0
    assert avoid_candidate["basis"]["required_lateral_clearance_m"] == 30.0
    assert avoid_candidate["basis"]["expanded_half_along_route_m"] == 30.0
    assert avoid_candidate["basis"]["clearance_entry_along_track_m"] == 70.0
    lateral_at_expanded_near_face = (
        avoid_candidate["proposed_parameters"]["target_y_m"] * 70.0 / 130.0
    )
    assert lateral_at_expanded_near_face > 30.0
    assert (
        avoid_candidate["basis"]["route_vector_source_ref"] == "telemetry_snapshot.route.active_leg"
    )
    assert "avoid_obstacle" in avoid_result["candidate_actions"]
    assert "reroute" not in avoid_result["candidate_actions"]
    assert avoid_result["dispatch_authority_created"] is False
    assert avoid_result["physical_execution_invoked"] is False
    assert avoid_result["progress_counted"] is False

    assert altitude_result["tool_status"] == "computed"
    assert altitude_result["recommended_candidate"]["proposed_parameters"] == {
        "target_altitude_m": 42.0
    }


def test_failed_original_dropoff_resume_proposes_fresh_alternate_reroute() -> None:
    telemetry = _planner_tool_telemetry()
    alternate_candidate = {
        "selected_bounded_action": "reroute",
        "proposed_parameters": {
            "target_x_m": 100.0,
            "target_y_m": 30.0,
            "target_altitude_m": 30.0,
            "alternate_dropoff": True,
            "resume_original_route": False,
            "source_obstacle_name": "missionos_landing_zone_blocker",
        },
        "basis": {"required_horizontal_clearance_m": 30.0},
    }
    telemetry["obstacle"]["obstacle_manifest"].update(
        {
            "landing_zone_blocked": True,
            "original_dropoff_available": False,
            "alternate_dropoff_candidate": alternate_candidate,
        }
    )
    initial_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="reroute",
        request_reason="the source-backed original dropoff is collision occupied",
    )
    assert (
        initial_result["recommended_candidate"]["proposed_parameters"]
        == (alternate_candidate["proposed_parameters"])
    )
    telemetry["recovery"] = {
        "resume_safety_verification": {
            "verification_status": "failed",
            "original_dropoff_available": False,
            "alternate_dropoff_candidate": alternate_candidate,
            "blocked_reasons": ["original_dropoff_collision_occupied"],
        }
    }

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="reroute",
        request_reason="the original dropoff is collision occupied",
    )

    assert result["tool_status"] == "computed"
    assert result["candidate_actions"] == ["reroute", "adjust_altitude"]
    candidate = result["recommended_candidate"]
    assert candidate["selected_bounded_action"] == "reroute"
    assert candidate["proposed_parameters"] == alternate_candidate["proposed_parameters"]
    assert result["operator_approval_required"] is True
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False
    guarded = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "reroute",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            # The LLM chooses the intent and repeats the concrete target. The
            # deterministic compiler preserves the non-coordinate safety flags.
            "proposed_parameters": {
                "target_x_m": 100.0,
                "target_y_m": 30.0,
                "target_altitude_m": 30.0,
            },
        },
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        planner_tool_results=[result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )
    assert guarded["assessment_status"] == "proposal_guardrail_passed"
    assert guarded["proposed_parameters"] == alternate_candidate["proposed_parameters"]


def test_gateway_preserves_only_safe_alternate_dropoff_metadata() -> None:
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="reroute",
        body={
            "recovery_parameters": {
                "target_x_m": 100.0,
                "target_y_m": 30.0,
                "target_altitude_m": 30.0,
                "alternate_dropoff": True,
                "resume_original_route": False,
                "source_obstacle_name": "missionos_landing_zone_blocker",
            }
        },
    )

    assert parameters["alternate_dropoff"] is True
    assert parameters["resume_original_route"] is False
    assert parameters["source_obstacle_name"] == ("missionos_landing_zone_blocker")
    with pytest.raises(
        gateway_server.HTTPException,
        match="must not resume the original route",
    ):
        gateway_server._bounded_operator_recovery_parameters(
            recovery_action="reroute",
            body={
                "recovery_parameters": {
                    "target_x_m": 100.0,
                    "target_y_m": 30.0,
                    "alternate_dropoff": True,
                    "resume_original_route": True,
                    "source_obstacle_name": ("missionos_landing_zone_blocker"),
                }
            },
        )


def test_alternate_dropoff_dispatch_revalidates_exact_fresh_proposal() -> None:
    now = datetime.now(timezone.utc)
    parameters = {
        "target_x_m": 100.0,
        "target_y_m": 30.0,
        "target_altitude_m": 30.0,
        "alternate_dropoff": True,
        "resume_original_route": False,
        "source_obstacle_name": "missionos_landing_zone_blocker",
    }
    result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": {
                "proposal_id": "proposal_alternate_dropoff",
                "proposal_status": "awaiting_operator_approval",
                "observed_at": now.isoformat(),
                "valid_until": now.replace(year=now.year + 1).isoformat(),
                "origin_position": {"local_x_m": 90.0, "local_y_m": 10.0},
                "max_origin_drift_m": 50.0,
                "runtime_recovery_agent_result": {
                    "assessment": {
                        "recovery_planner_tool_candidate": {
                            "selected_bounded_action": "reroute",
                            "proposed_parameters": parameters,
                        }
                    }
                },
            },
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": {
                    # A manifest-bound terminal replacement remains the same
                    # action even after the aircraft advances.  Its obstacle
                    # binding, not the old local origin, is revalidated.
                    "position": {"local_x_m": 90.0, "local_y_m": 100.0},
                    "telemetry": {"stale": False},
                    "obstacle": {
                        "obstacle_manifest": {
                            "original_dropoff_available": False,
                            "alternate_dropoff_candidate": {
                                "selected_bounded_action": "reroute",
                                "proposed_parameters": parameters,
                            },
                            "obstacles": [
                                {
                                    "name": "missionos_landing_zone_blocker",
                                    "collision_enabled": True,
                                }
                            ],
                        }
                    },
                }
            },
        },
        recovery_action="reroute",
        recovery_parameters=parameters,
        now=now,
    )

    assert result["validation_status"] == "valid"
    assert result["parameters_match"] is True
    assert result["manifest_bound_alternate_dropoff"] is True
    assert result["alternate_dropoff_manifest_binding_valid"] is True
    assert "origin_drift_m" not in result
    assert result["reasons"] == []


def test_alternate_dropoff_revalidation_rejects_changed_obstacle_manifest() -> None:
    now = datetime.now(timezone.utc)
    parameters = {
        "target_x_m": 100.0,
        "target_y_m": 30.0,
        "target_altitude_m": 30.0,
        "alternate_dropoff": True,
        "resume_original_route": False,
        "source_obstacle_name": "missionos_landing_zone_blocker",
    }
    result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": {
                "proposal_id": "proposal_alternate_dropoff",
                "proposal_status": "awaiting_operator_approval",
                "observed_at": now.isoformat(),
                "valid_until": now.replace(year=now.year + 1).isoformat(),
                "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
                "max_origin_drift_m": 30.0,
                "runtime_recovery_agent_result": {
                    "assessment": {
                        "recovery_planner_tool_candidate": {
                            "selected_bounded_action": "reroute",
                            "proposed_parameters": parameters,
                        }
                    }
                },
            },
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": {
                    "position": {"local_x_m": 0.0, "local_y_m": 100.0},
                    "telemetry": {"stale": False},
                    "obstacle": {
                        "obstacle_manifest": {
                            "original_dropoff_available": True,
                            "alternate_dropoff_candidate": {
                                "selected_bounded_action": "reroute",
                                "proposed_parameters": parameters,
                            },
                            "obstacles": [],
                        }
                    },
                }
            },
        },
        recovery_action="reroute",
        recovery_parameters=parameters,
        now=now,
    )

    assert result["validation_status"] == "blocked"
    assert "runtime_recovery_alternate_dropoff_manifest_binding_invalidated" in result["reasons"]


def test_pending_manifest_bound_alternate_survives_origin_drift() -> None:
    now = datetime.now(timezone.utc)
    parameters = {
        "target_x_m": 100.0,
        "target_y_m": 30.0,
        "target_altitude_m": 30.0,
        "alternate_dropoff": True,
        "resume_original_route": False,
        "source_obstacle_name": "missionos_landing_zone_blocker",
    }
    proposal = {
        "valid_until": now.replace(year=now.year + 1).isoformat(),
        "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
        "max_origin_drift_m": 30.0,
        "runtime_recovery_agent_result": {
            "assessment": {
                "recovery_planner_tool_candidate": {
                    "selected_bounded_action": "reroute",
                    "proposed_parameters": parameters,
                }
            }
        },
    }
    telemetry = {
        "position": {"local_x_m": 0.0, "local_y_m": 100.0},
        "obstacle": {
            "obstacle_manifest": {
                "original_dropoff_available": False,
                "alternate_dropoff_candidate": {
                    "selected_bounded_action": "reroute",
                    "proposed_parameters": parameters,
                },
                "obstacles": [
                    {
                        "name": "missionos_landing_zone_blocker",
                        "collision_enabled": True,
                    }
                ],
            }
        },
    }

    reasons = live_run._runtime_recovery_pending_proposal_invalidation_reasons(
        proposal=proposal,
        telemetry_snapshot=telemetry,
        now=now,
    )

    assert reasons == []


def test_completed_recovery_attempt_preserves_verifier_evidence() -> None:
    verification = {
        "schema_version": ("missionos_px4_recovery_resume_safety_verification.v1"),
        "verification_status": "verified",
        "original_dropoff_available": False,
        "target_clearance_verified": True,
        "resume_auto_authorized": False,
    }
    evidence = live_run._runtime_recovery_attempt_evidence(
        task_id="task_alternate_dropoff",
        telemetry_snapshot={
            "sample_index": 126,
            "position": {"local_x_m": -27.8, "local_y_m": 539.0},
            "recovery": {
                "action": "reroute",
                "parameters": {
                    "target_x_m": -27.754,
                    "target_y_m": 538.952,
                    "alternate_dropoff": True,
                    "resume_original_route": False,
                },
                "command_ack_observed": True,
                "assist_attempted": True,
                "assist_status": "target_reached",
                "target_reached": True,
                "target_distance_m": 1.037,
                "resume_status": ("held_at_alternate_dropoff_awaiting_operator_decision"),
                "resume_auto_attempted": False,
                "resume_safety_verification": verification,
            },
        },
        observation_state="held_at_alternate_dropoff",
        receipt={
            "dispatch_authority_created": True,
            "proposal_revalidation": {"proposal_id": "proposal_fresh"},
        },
        last_proposal={"proposal_id": "proposal_stale"},
        observed_at="2026-07-17T16:08:30+00:00",
    )

    assert evidence is not None
    assert evidence["source_proposal_id"] == "proposal_fresh"
    assert evidence["attempt_status"] == "held_at_alternate_dropoff"
    assert evidence["target_reached"] is True
    assert evidence["target_distance_m"] == 1.037
    assert evidence["resume_auto_attempted"] is False
    assert evidence["resume_safety_verification"] == verification
    assert evidence["simulator_execution_observed"] is True
    assert evidence["delivery_completion_claimed"] is False
    assert evidence["physical_execution_invoked"] is False


def test_runtime_recovery_planner_does_not_substitute_a_different_action() -> None:
    telemetry = _planner_tool_telemetry()
    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={},
        recovery_policy=_planner_policy(),
        requested_action="reroute",
        request_reason="route is obstructed",
    )
    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=result,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    assert result["requested_action"] == "reroute"
    assert result["requested_action_matched"] is False
    assert result["selection_basis"] == "requested_action_not_compilable"
    assert result["tool_status"] == "insufficient_context"
    assert result["recommended_candidate"] == {}
    assert "avoid_obstacle" in result["candidate_actions"]
    assert guarded["guardrail_status"] == "skipped_no_candidate"
    assert guarded["recovery_guardrail_assessment"] == {}


def test_runtime_recovery_planner_honors_operator_requested_altitude() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["terrain"] = {
        "terrain_clearance_m": 40.0,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": 10.0,
        "terrain_clearance_below_minimum": False,
    }

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={
            "operator_recovery_request": {
                "requested_action": "adjust_altitude",
                "target_altitude_m": 50.0,
            }
        },
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
        request_reason="operator asked to climb to 50m",
    )

    assert result["tool_status"] == "computed"
    assert result["recommended_candidate"]["selected_bounded_action"] == ("adjust_altitude")
    assert result["recommended_candidate"]["proposed_parameters"] == {"target_altitude_m": 50.0}
    assert (
        "mission_context.operator_recovery_request"
        in result["recommended_candidate"]["source_refs"]
    )
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_runtime_recovery_planner_treats_altitude_delta_as_signed() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["terrain"] = {
        "terrain_clearance_m": 40.0,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": 10.0,
        "terrain_clearance_below_minimum": False,
    }

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={
            "operator_recovery_request": {
                "requested_action": "adjust_altitude",
                "altitude_delta_m": -15.0,
            }
        },
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
        request_reason="operator asked to descend by 15m",
    )

    assert result["tool_status"] == "computed"
    candidate = result["recommended_candidate"]
    assert candidate["selected_bounded_action"] == "adjust_altitude"
    assert candidate["proposed_parameters"] == {"target_altitude_m": 10.0}
    assert candidate["basis"]["requested_delta_m"] == -15.0
    assert candidate["basis"]["adjustment_m"] == -15.0
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_runtime_recovery_planner_derives_requested_reroute_without_coordinates() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["obstacle"] = {}

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={
            "operator_recovery_request": {
                "requested_action": "reroute",
            }
        },
        recovery_policy={
            **_planner_policy(),
            "operator_reroute_forward_m": 80.0,
            "operator_reroute_lateral_m": 30.0,
        },
        requested_action="reroute",
        request_reason="operator asked for a route change",
    )

    assert result["tool_status"] == "computed"
    assert result["recommended_candidate"]["selected_bounded_action"] == "reroute"
    assert result["recommended_candidate"]["proposed_parameters"] == {
        "target_x_m": 80.0,
        "target_y_m": 30.0,
    }
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_runtime_recovery_guard_requires_tool_match_for_parameterized_agent_actions() -> None:
    telemetry = _planner_tool_telemetry()
    policy = _planner_policy()
    tool_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        requested_action="avoid_obstacle",
    )
    tool_parameters = tool_result["recommended_candidate"]["proposed_parameters"]

    matching = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "avoid_obstacle",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": tool_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[tool_result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )
    no_tool_call = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "avoid_obstacle",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": tool_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[],
        require_parameter_tool_call=True,
        parameter_tool_called=False,
    )
    invented_parameters = dict(tool_parameters)
    invented_parameters["target_y_m"] = 75.0
    invented = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "avoid_obstacle",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": invented_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[tool_result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )
    mismatched_action = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "reroute",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": tool_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[tool_result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )

    assert matching["assessment_status"] == "proposal_guardrail_passed"
    assert matching["proposed_parameters_source"] == ("runtime_recovery_planner_function_tool")
    assert matching["recovery_planner_tool_called"] is True
    assert no_tool_call["selected_bounded_action"] == "operator_review"
    assert (
        "parameterized_recovery_requires_runtime_recovery_planner_tool_call"
        in no_tool_call["blocking_reasons"]
    )
    assert invented["selected_bounded_action"] == "operator_review"
    assert (
        "parameterized_recovery_parameters_must_match_runtime_recovery_planner_tool_candidate"
        in invented["blocking_reasons"]
    )
    assert mismatched_action["selected_bounded_action"] == "operator_review"
    assert (
        "parameterized_recovery_action_must_match_runtime_recovery_planner_"
        "recommendation" in mismatched_action["blocking_reasons"]
    )


def test_runtime_recovery_direct_planner_result_uses_shared_guardrail() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["obstacle"] = {}
    malicious_candidate = {
        "selected_bounded_action": "avoid_obstacle",
        "proposed_parameters": {"target_x_m": 40.0, "target_y_m": 20.0},
        "source_refs": ["test.malicious_candidate"],
    }
    planner_result = {
        "schema_version": (
            missionos_agent_runtime.MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_SCHEMA_VERSION
        ),
        "tool_name": missionos_agent_runtime.MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
        "tool_status": "computed",
        "requested_action": "avoid_obstacle",
        "request_reason": "operator asked to avoid an obstacle",
        "recommended_candidate": malicious_candidate,
        "candidates": [malicious_candidate],
        "candidate_actions": ["avoid_obstacle"],
        "dispatch_authority_created": False,
        "operator_approval_required": True,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }

    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    assert guarded["tool_status"] == "guardrail_blocked"
    assert guarded["recommended_candidate"]["selected_bounded_action"] == ("operator_review")
    assert guarded["recommended_candidate"]["proposed_parameters"] == {}
    assert guarded["recovery_guardrail_assessment"]["selected_bounded_action"] == "operator_review"
    assert (
        "avoid_obstacle_requires_source_backed_obstacle_or_building_risk"
        in guarded["recovery_guardrail_assessment"]["blocking_reasons"]
    )
    assert guarded["dispatch_authority_created"] is False
    assert guarded["physical_execution_invoked"] is False


def test_runtime_recovery_direct_planner_guard_preserves_valid_candidate() -> None:
    telemetry = _planner_tool_telemetry()
    planner_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
    )

    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    assert guarded["tool_status"] == "computed"
    assert guarded["guardrail_status"] == "proposal_guardrail_passed"
    assert guarded["recommended_candidate"] == planner_result["recommended_candidate"]
    assert guarded["recovery_guardrail_assessment"]["assessment_status"] == (
        "proposal_guardrail_passed"
    )


def test_runtime_recovery_tool_response_is_guarded_proposal_only_and_stops_turn() -> None:
    telemetry = _planner_tool_telemetry()
    planner_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
        request_reason="source-backed building risk near route",
    )
    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    class _Actions:
        skip_summarization = False

    class _ToolContext:
        actions = _Actions()

    tool_context = _ToolContext()
    output = missionos_agent_runtime._finalize_runtime_recovery_tool_response(
        planner_result=guarded,
        tool_context=tool_context,
    )

    assert tool_context.actions.skip_summarization is True
    assert output["intent"] == "runtime_recovery"
    assert output["selected_bounded_action"] == "avoid_obstacle"
    assert (
        output["proposed_parameters"] == (guarded["recommended_candidate"]["proposed_parameters"])
    )
    assert output["requires_human_approval"] is True
    assert not {
        "approved",
        "dispatch_authority_created",
        "physical_execution_invoked",
    }.intersection(output)


def test_runtime_recovery_accepts_bounded_altitude_speed_and_reroute_parameters() -> None:
    altitude = _assessment("adjust_altitude", parameters={"target_altitude_m": 45.0})
    speed = _assessment("adjust_speed", parameters={"target_speed_mps": 8.0})
    reroute = _assessment(
        "reroute",
        parameters={"target_x_m": 120.0, "target_y_m": -20.0, "target_altitude_m": 35.0},
    )

    assert altitude["assessment_status"] == "proposal_guardrail_passed"
    assert altitude["selected_bounded_action"] == "adjust_altitude"
    assert speed["assessment_status"] == "proposal_guardrail_passed"
    assert speed["selected_bounded_action"] == "adjust_speed"
    assert reroute["assessment_status"] == "proposal_guardrail_passed"
    assert reroute["selected_bounded_action"] == "reroute"


def test_runtime_recovery_blocks_unbounded_or_unsourced_obstacle_maneuvers() -> None:
    missing_parameter = _assessment("adjust_altitude")
    unsourced_obstacle = _assessment(
        "avoid_obstacle",
        parameters={"target_x_m": 40.0, "target_y_m": 20.0},
    )
    sourced_obstacle = _assessment(
        "avoid_obstacle",
        parameters={"target_x_m": 40.0, "target_y_m": 20.0},
        telemetry={"obstacle": {"obstacle_detected": True}},
    )

    assert missing_parameter["selected_bounded_action"] == "operator_review"
    assert "adjust_altitude_requires_target_altitude_m" in missing_parameter["blocking_reasons"]
    assert unsourced_obstacle["selected_bounded_action"] == "operator_review"
    assert (
        "avoid_obstacle_requires_source_backed_obstacle_or_building_risk"
        in unsourced_obstacle["blocking_reasons"]
    )
    assert sourced_obstacle["assessment_status"] == "proposal_guardrail_passed"
    assert sourced_obstacle["selected_bounded_action"] == "avoid_obstacle"


def test_gateway_bounds_parameterized_recovery_requests_and_marks_maneuver_approval() -> None:
    params = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="reroute",
        body={
            "recovery_parameters": {
                "target_x_m": "150",
                "target_y_m": "-25",
                "target_altitude_m": "40",
            }
        },
    )
    approval, allowlist = gateway_server._operator_recovery_approval_payload(
        recovery_action="reroute",
        task_id="task_expanded_recovery",
        parameters=params,
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert params == {
        "target_x_m": 150.0,
        "target_y_m": -25.0,
        "target_altitude_m": 40.0,
    }
    assert approval["operator_approval_performed"] is True
    assert approval["approved_recovery_action"] == "reroute"
    assert approval["physical_execution_invoked"] is False
    assert allowlist["active_runner_required"] is True
    assert "SET_POSITION_TARGET_LOCAL_NED" in allowlist["allowed_mavlink_message_ids"]


def test_operate_action_panel_surfaces_parameterized_recovery_without_rtl_land_keys() -> None:
    panel = missionos_cli._render_action_panel(
        {
            "action": "adjust_altitude",
            "status": "proposal_guardrail_passed",
            "risks": ["terrain_clearance_below_minimum"],
            "parameters": {"target_altitude_m": 45.0},
        },
        confirming=None,
    )
    rendered = str(panel.renderable)

    assert "params = target_altitude_m=45.0" in rendered
    assert "climb <m>" in rendered
    assert "speed <m/s>" in rendered
    assert "reroute <x> <y> (alt)" in rendered
    assert "approve RTL" not in rendered
    assert "approve LAND" not in rendered


def test_operate_console_parses_direct_recovery_commands() -> None:
    climb = missionos_cli._parse_operate_console_command("climb 45")
    speed = missionos_cli._parse_operate_console_command("speed 7")
    reroute = missionos_cli._parse_operate_console_command("reroute 120 -20 35")
    avoid = missionos_cli._parse_operate_console_command("avoid x=40 y=20 alt=45")
    rtl = missionos_cli._parse_operate_console_command("rtl")

    assert climb.kind == "dispatch"
    assert climb.action == "adjust_altitude"
    assert climb.parameters == {"target_altitude_m": 45.0}
    assert speed.action == "adjust_speed"
    assert speed.parameters == {"target_speed_mps": 7.0}
    assert reroute.action == "reroute"
    assert reroute.parameters == {
        "target_x_m": 120.0,
        "target_y_m": -20.0,
        "target_altitude_m": 35.0,
    }
    assert avoid.action == "avoid_obstacle"
    assert avoid.parameters == {
        "target_x_m": 40.0,
        "target_y_m": 20.0,
        "target_altitude_m": 45.0,
    }
    assert rtl.action == "return_to_launch"
    assert rtl.parameters == {}


def test_operate_console_rejects_missing_direct_command_parameters() -> None:
    with pytest.raises(click.ClickException, match="usage: climb"):
        missionos_cli._parse_operate_console_command("climb")
    with pytest.raises(click.ClickException, match="usage: reroute"):
        missionos_cli._parse_operate_console_command("reroute 120")


def test_gazebo_obstacle_manifest_materializes_landing_zone_blocker() -> None:
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6984,
            "dropoff_longitude": 139.773,
            "landing_zone_blocked": True,
        }
    )

    assert manifest["manifest_status"] == "configured"
    assert manifest["landing_zone_blocked"] is True
    assert manifest["building_risk_detected"] is True
    assert manifest["gazebo_obstacle_model_spawn_requested"] is True
    assert manifest["gazebo_obstacle_model_spawned"] is False
    assert manifest["original_dropoff_available"] is False
    assert manifest["obstacles"][0]["name"] == "missionos_landing_zone_blocker"
    assert manifest["obstacles"][0]["frame"] == "gazebo_world_local_ned"
    assert manifest["obstacles"][0]["collision_enabled"] is True
    assert manifest["obstacles"][0]["visual_only"] is False
    assert manifest["obstacles"][0]["bounds_local_xyz_m"]["max_z_m"] == 20.0
    alternate = manifest["alternate_dropoff_candidate"]
    assert alternate["selected_bounded_action"] == "reroute"
    assert alternate["proposed_parameters"]["alternate_dropoff"] is True
    assert alternate["proposed_parameters"]["resume_original_route"] is False


def test_gazebo_obstacle_manifest_materializes_mid_route_collision_box() -> None:
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6812562,
            "dropoff_longitude": 139.7730907,
            "landing_zone_blocked": False,
            "obstacle_route_fraction": 0.5,
            "obstacle_size_x_m": 18.0,
            "obstacle_size_y_m": 18.0,
            "obstacle_size_z_m": 20.0,
        }
    )

    obstacle = manifest["obstacles"][0]
    assert manifest["original_dropoff_available"] is True
    assert manifest["landing_zone_blocked"] is False
    assert manifest["alternate_dropoff_candidate"] is None
    assert obstacle["name"] == "missionos_route_obstacle"
    assert obstacle["collision_enabled"] is True
    assert obstacle["visual_only"] is False
    assert obstacle["route_fraction"] == 0.5
    assert obstacle["x_m"] == pytest.approx(
        manifest["dropoff_local_x_m"] * 0.5,
        abs=0.001,
    )
    assert obstacle["y_m"] == pytest.approx(
        manifest["dropoff_local_y_m"] * 0.5,
        abs=0.001,
    )


def test_recovery_resume_sequence_skips_expanded_mid_route_obstacle() -> None:
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6812562,
            "dropoff_longitude": 139.7730907,
            "landing_zone_blocked": False,
            "obstacle_route_fraction": 0.5,
            "obstacle_size_x_m": 18.0,
            "obstacle_size_y_m": 18.0,
            "obstacle_size_z_m": 20.0,
        }
    )

    resume_seq = auto_probe._recovery_resume_mission_seq_after_obstacle(
        obstacle_manifest=manifest,
        dropoff_dwell_mission_seq=21,
    )

    assert resume_seq == 12


def test_two_route_obstacles_keep_separate_positions_and_resume_sequences() -> None:
    route = {
        "takeoff_latitude": 35.681236,
        "takeoff_longitude": 139.767125,
        "dropoff_latitude": 35.6812562,
        "dropoff_longitude": 139.7730907,
        "landing_zone_blocked": False,
        "obstacles": [
            {
                "name": "missionos_route_obstacle_50pct",
                "route_fraction": 0.5,
                "size_x_m": 18.0,
                "size_y_m": 18.0,
                "size_z_m": 20.0,
            },
            {
                "name": "missionos_route_obstacle_75pct",
                "route_fraction": 0.75,
                "size_x_m": 18.0,
                "size_y_m": 18.0,
                "size_z_m": 20.0,
            },
        ],
    }

    manifest = auto_probe._gazebo_obstacle_manifest_from_route(route)
    obstacles = manifest["obstacles"]
    assert [item["route_fraction"] for item in obstacles] == [0.5, 0.75]
    assert obstacles[0]["y_m"] == pytest.approx(
        manifest["dropoff_local_y_m"] * 0.5,
        abs=0.001,
    )
    assert obstacles[1]["y_m"] == pytest.approx(
        manifest["dropoff_local_y_m"] * 0.75,
        abs=0.001,
    )
    assert (
        auto_probe._recovery_resume_mission_seq_after_obstacle(
            obstacle_manifest=manifest,
            dropoff_dwell_mission_seq=21,
        )
        is None
    )
    sequences = auto_probe._recovery_resume_mission_seq_by_obstacle(
        obstacle_manifest=manifest,
        dropoff_dwell_mission_seq=21,
    )
    assert sequences["missionos_route_obstacle_50pct"] == 12
    assert sequences["missionos_route_obstacle_75pct"] == 17


def test_operator_route_preserves_source_bound_mid_route_obstacle(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        auto_probe.OPERATOR_ROUTE_JSON_ENV,
        json.dumps(
            {
                "takeoff_latitude": 35.681236,
                "takeoff_longitude": 139.767125,
                "dropoff_latitude": 35.6812562,
                "dropoff_longitude": 139.7730907,
                "dropoff_roof_height_agl_m": 30.0,
                "gazebo_obstacle_model_spawn_requested": True,
                "obstacle_route_fraction": 0.5,
                "obstacle_size_x_m": 18.0,
                "obstacle_size_y_m": 18.0,
                "obstacle_size_z_m": 20.0,
                "obstacle_scenario_source": (
                    "operator_instruction_mid_route_bounded_sitl_scenario"
                ),
            }
        ),
    )

    route = auto_probe._operator_route()
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(route)

    assert route["gazebo_obstacle_model_spawn_requested"] is True
    assert route["obstacle_route_fraction"] == 0.5
    assert route["obstacle_scenario_source"] == (
        "operator_instruction_mid_route_bounded_sitl_scenario"
    )
    assert manifest["manifest_status"] == "configured"
    assert manifest["obstacles"][0]["route_fraction"] == 0.5


def test_gazebo_obstacle_manifest_ignores_empty_normalized_obstacle_fields() -> None:
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6812562,
            "dropoff_longitude": 139.7730907,
            "landing_zone_blocked": False,
            "building_risk_detected": False,
            "obstacle_route_fraction": None,
            "obstacle_x_m": None,
            "obstacle_y_m": None,
            "obstacle_z_m": None,
            "obstacle_size_x_m": None,
            "obstacle_size_y_m": None,
            "obstacle_size_z_m": None,
        }
    )

    assert manifest["manifest_status"] == "not_configured"
    assert manifest["obstacles"] == []
    assert manifest["gazebo_obstacle_model_spawn_requested"] is False
    assert manifest["original_dropoff_available"] is True


def test_dropoff_gate_rejects_collision_occupied_original_dropoff() -> None:
    gate = auto_probe.build_auto_mission_dropoff_gate_summary(
        dropoff_latitude_deg=35.6812,
        dropoff_longitude_deg=139.7671,
        release_altitude_target_m=30.0,
        samples=[],
        route_completed_claimed=True,
        original_dropoff_available=False,
        dropoff_obstacle_clearance_verified=False,
    )

    assert gate.dropoff_verified is False
    assert gate.original_dropoff_available is False
    assert gate.dropoff_obstacle_clearance_verified is False
    assert "original_dropoff_collision_occupied" in gate.blocked_reasons
    assert "dropoff_obstacle_clearance_not_verified" in gate.blocked_reasons


def test_explicit_obstacle_realism_env_is_bound_into_auto_runner_route(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        live_run.MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED_ENV,
        "true",
    )

    route = live_run._coordinate_route_with_explicit_realism_env(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6984,
            "dropoff_longitude": 139.773,
        }
    )
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(route)

    assert route["landing_zone_blocked"] is True
    assert manifest["manifest_status"] == "configured"
    assert manifest["gazebo_obstacle_model_spawn_requested"] is True
    assert manifest["obstacles"][0]["name"] == "missionos_landing_zone_blocker"


def test_requested_obstacle_is_not_detected_until_gazebo_pose_readback() -> None:
    projection = live_run._auto_runtime_obstacle_projection(
        artifacts={
            "mission_designer_coordinate_pair_route": {
                "landing_zone_blocked": True,
            }
        }
    )

    assert projection["projection_status"] == "configured_unobserved"
    assert projection["obstacle_condition_requested"] is True
    assert projection["gazebo_obstacle_model_spawned"] is False
    assert projection["obstacle_detected"] is False
    assert projection["building_risk_detected"] is False


def test_terminal_return_tracking_does_not_start_mission_level_recovery(
    tmp_path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_terminal_return_no_recovery",
        kind="contract_test",
        title="Terminal return keeps executor authority",
        status="running",
        artifacts={},
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            "sample_index": 100001,
            "elapsed_seconds": 120.0,
            "post_abort_tracking": True,
            "monitor_window_ended": True,
            "monitor_stop_reason": "operator_recovery_dispatch_acked",
            "progress_m": 260.0,
            "local_x_m": 1.0,
            "local_y_m": 260.0,
            "local_z_m": -30.0,
            "heartbeat_observed": True,
            "nav_state": 5,
            "landed": False,
        },
    )

    stored = store.get(task["task_id"])
    assert stored is not None
    assert "missionos_runtime_recovery_agent_live_bridge" not in stored["artifacts"]
    assert "missionos_runtime_recovery_safety_hold_receipt" not in stored["artifacts"]


def test_runtime_recovery_agent_waits_for_new_decision_epoch(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_runtime_recovery_decision_epoch",
        kind="contract_test",
        title="Runtime recovery decision epoch",
        status="running",
        artifacts={
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.681236,
                "takeoff_longitude": 139.767125,
                "dropoff_latitude": 35.6984,
                "dropoff_longitude": 139.773,
            },
            "missionos_auto_mission_compilation": {
                "planned_route_m": 2000.0,
            },
        },
    )
    invocations: list[int] = []

    def _proposal(**kwargs) -> dict:
        telemetry = kwargs["telemetry_snapshot"]
        invocations.append(int(telemetry["sample_index"]))
        return {
            "schema_version": "missionos_runtime_recovery_agent_result.v1",
            "runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_output": {
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_invocations": [{"function_tool_called": True}],
            "dispatch_authority_created": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _proposal,
    )

    def _snapshot(sample_index: int, **updates) -> dict:
        snapshot = {
            "sample_index": sample_index,
            "elapsed_seconds": float(sample_index * 20),
            "progress_m": 100.0,
            "local_x_m": 10.0,
            "local_y_m": 5.0,
            "local_z_m": -30.0,
            "altitude_above_home_m": 30.0,
            "battery_remaining_percent": 10.0,
            "heartbeat_observed": True,
            "nav_state": 3,
            "landed": False,
        }
        snapshot.update(updates)
        return snapshot

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(1),
    )
    first = store.get(task["task_id"])
    assert first is not None
    first_artifacts = first["artifacts"]
    first_bridge = first_artifacts["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1]
    assert first_bridge["agent_refresh_status"] == "agent_invoked"
    assert first_bridge["bridge_status"] == "proposal_attached"
    assert (
        first_artifacts["missionos_runtime_recovery_last_proposal"]["proposal_status"]
        == "awaiting_operator_approval"
    )
    proposal_id = first_artifacts["missionos_runtime_recovery_last_proposal"]["proposal_id"]
    first_proposal = first_artifacts["missionos_runtime_recovery_last_proposal"]
    assert first_proposal["origin_position"] == {
        "local_x_m": 10.0,
        "local_y_m": 5.0,
        "local_z_m": -30.0,
        "altitude_above_home_m": 30.0,
        "distance_to_home_m": None,
    }
    assert datetime.fromisoformat(first_proposal["valid_until"]) > datetime.fromisoformat(
        first_proposal["observed_at"]
    )
    assert first_proposal["max_origin_drift_m"] == 30.0
    assert (
        first_artifacts["missionos_runtime_recovery_proposals"][proposal_id]
        == (first_artifacts["missionos_runtime_recovery_last_proposal"])
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(2),
    )
    awaiting = store.get(task["task_id"])
    assert awaiting is not None
    awaiting_bridge = awaiting["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1]
    assert awaiting_bridge["agent_refresh_status"] == "awaiting_operator_approval"
    assert (
        awaiting_bridge["runtime_recovery_agent_result"]["assessment"]["selected_bounded_action"]
        == "adjust_altitude"
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(3, local_x_m=50.0),
    )
    drifted = store.get(task["task_id"])
    assert drifted is not None
    assert invocations == [1]
    assert (
        drifted["artifacts"]["missionos_runtime_recovery_last_proposal"]["proposal_status"]
        == "stale"
    )
    assert (
        drifted["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]["agent_refresh_status"]
        == "proposal_stale"
    )

    store.update(
        task["task_id"],
        artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "observed_at": "9999-01-01T00:00:00+00:00",
                "recovery_action": "adjust_altitude",
                "explicit_recovery_dispatch_approval": True,
                "blocked_reasons": ["runtime_recovery_proposal_stale"],
            }
        },
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(4),
    )
    refreshed = store.get(task["task_id"])
    assert refreshed is not None
    refreshed_proposal = refreshed["artifacts"]["missionos_runtime_recovery_last_proposal"]
    assert invocations == [1, 4]
    assert refreshed_proposal["proposal_id"] != proposal_id

    store.update(
        task["task_id"],
        artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "observed_at": "9999-01-01T00:00:01+00:00",
                "recovery_action": "adjust_altitude",
                "explicit_recovery_dispatch_approval": True,
            }
        },
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(
            5,
            operator_recovery_request_observed=True,
            operator_recovery_command_ack_observed=True,
            operator_recovery_command_ack_result=0,
            operator_recovery_assist_attempted=True,
            operator_recovery_assist_status="running",
        ),
    )
    in_progress = store.get(task["task_id"])
    assert in_progress is not None
    in_progress_bridge = in_progress["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1, 4]
    assert in_progress_bridge["agent_refresh_status"] == "recovery_in_progress"
    assert in_progress_bridge["runtime_recovery_agent_result"]["assessment"] == {}
    assert in_progress_bridge["runtime_recovery_agent_result"]["blocking_reasons"] == [
        "runtime_recovery_action_in_progress"
    ]

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(
            6,
            operator_recovery_request_observed=True,
            operator_recovery_command_ack_observed=True,
            operator_recovery_assist_attempted=True,
            operator_recovery_assist_status="target_reached",
            operator_recovery_target_reached=True,
            operator_recovery_resume_auto_status="resumed_auto_mission",
        ),
    )
    succeeded = store.get(task["task_id"])
    assert succeeded is not None
    succeeded_bridge = succeeded["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1, 4]
    assert succeeded_bridge["agent_refresh_status"] == "recovery_succeeded"

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(
            7,
            nav_state=4,
            operator_recovery_request_observed=True,
            operator_recovery_command_ack_observed=True,
            operator_recovery_assist_attempted=True,
            operator_recovery_assist_status="failed",
            operator_recovery_target_reached=False,
            operator_recovery_resume_auto_status="not_resumed",
        ),
    )
    assert invocations == [1, 4, 7]


def test_runtime_recovery_skips_unstable_preflight_sample(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_runtime_recovery_preflight",
        kind="contract_test",
        title="Runtime recovery preflight",
        status="running",
    )

    def _unexpected_proposal(**_kwargs) -> dict:
        raise AssertionError("preflight telemetry must not invoke the hosted model")

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _unexpected_proposal,
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            "sample_index": 8,
            "elapsed_seconds": 20.0,
            "progress_m": 0.0,
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "local_z_m": -5.6,
            "altitude_above_home_m": 5.6,
            "distance_to_home_m": 0.0,
            "heartbeat_observed": False,
            "landed": False,
        },
    )

    stored = store.get(task["task_id"])
    assert stored is not None
    bridge = stored["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert bridge["agent_refresh_status"] == "preflight"
    assert bridge["recovery_observation_state"] == "preflight"
    assert bridge["runtime_recovery_agent_result"]["blocking_reasons"] == [
        "runtime_recovery_preflight_telemetry_not_ready"
    ]
    assert "missionos_runtime_recovery_last_proposal" not in stored["artifacts"]


def test_safety_hold_preserves_matching_local_avoidance_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    queued_holds: list[dict] = []

    def _queue_hold(**kwargs) -> dict:
        queued_holds.append(dict(kwargs))
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo-sitl",
            "container_path": kwargs["container_path"],
            "bytes_written": 100,
        }

    monkeypatch.setattr(
        live_run,
        "queue_px4_active_runner_recovery_request",
        _queue_hold,
    )
    task = store.create(
        task_id="task_safety_hold_preserves_proposal",
        kind="contract_test",
        title="Safety hold preserves proposal",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "dispatch_status": "running",
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_auto_operator_recovery_request_"
                    "task_safety_hold_preserves_proposal.json"
                ),
            },
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.0,
                "dropoff_longitude": 139.00591,
            },
            "missionos_auto_mission_runtime_snapshot": {
                "gazebo_obstacle_model_spawned": True,
                "obstacle_manifest": {
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacles": [
                        {
                            "name": "dropoff_blocker",
                            "x_m": 0.0,
                            "y_m": 539.0,
                            "size_x_m": 18.0,
                            "size_y_m": 18.0,
                        }
                    ],
                },
            },
        },
    )
    invocations: list[int] = []

    def _proposal(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return {
            "schema_version": "missionos_runtime_recovery_agent_result.v1",
            "runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": "avoid_obstacle",
                "requires_human_approval": True,
                "proposed_parameters": {
                    "target_x_m": -30.0,
                    "target_y_m": 460.0,
                    "target_altitude_m": 45.0,
                },
            },
            "agent_output": {
                "selected_bounded_action": "avoid_obstacle",
                "requires_human_approval": True,
                "proposed_parameters": {
                    "target_x_m": -30.0,
                    "target_y_m": 460.0,
                    "target_altitude_m": 45.0,
                },
            },
            "agent_invocations": [{"function_tool_called": True}],
            "dispatch_authority_created": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _proposal,
    )
    base_snapshot = {
        "sample_index": 40,
        "elapsed_seconds": 80.0,
        "progress_m": 430.0,
        "local_x_m": 0.0,
        "local_y_m": 430.0,
        "local_z_m": -30.0,
        "altitude_above_home_m": 30.0,
        "battery_remaining_percent": 80.0,
        "heartbeat_observed": True,
        "nav_state": 3,
        "landed": False,
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=base_snapshot,
    )
    first = store.get(task["task_id"])
    assert first is not None
    assert invocations == []
    assert len(queued_holds) == 1
    assert "missionos_runtime_recovery_last_proposal" not in first["artifacts"]
    assert (
        first["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]["agent_refresh_status"]
        == "waiting_for_safety_hold"
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 41,
            "elapsed_seconds": 81.0,
            "nav_state": 4,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "safety_hold",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_assist_attempted": True,
            "operator_recovery_assist_status": "safety_hold_observed",
            "operator_recovery_target_reached": False,
            "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
        },
    )

    held = store.get(task["task_id"])
    assert held is not None
    artifacts = held["artifacts"]
    bridge = artifacts["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == []
    assert len(queued_holds) == 1
    assert "missionos_runtime_recovery_last_proposal" not in artifacts
    assert bridge["agent_refresh_status"] == "safety_hold_settling"
    hold_receipt = artifacts["missionos_runtime_recovery_safety_hold_receipt"]
    assert hold_receipt["request_status"] == "observed"
    assert hold_receipt["runner_observed"] is True

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 42,
            "elapsed_seconds": 82.0,
            "nav_state": 4,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "safety_hold",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_assist_attempted": True,
            "operator_recovery_assist_status": "safety_hold_observed",
            "operator_recovery_target_reached": False,
            "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
        },
    )
    proposed = store.get(task["task_id"])
    assert proposed is not None
    current = proposed["artifacts"]["missionos_runtime_recovery_last_proposal"]
    proposal_id = current["proposal_id"]
    assert invocations == [42]
    assert current["proposal_status"] == "awaiting_operator_approval"
    assert (
        proposed["artifacts"]["missionos_runtime_recovery_agent_live_bridge"][
            "agent_refresh_status"
        ]
        == "agent_invoked"
    )

    awaiting_hold = {
        **base_snapshot,
        "nav_state": 4,
        "operator_recovery_request_observed": True,
        "operator_recovery_action": "safety_hold",
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_assist_attempted": True,
        "operator_recovery_assist_status": "safety_hold_observed",
        "operator_recovery_target_reached": False,
        "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
    }
    for sample_index in (43, 44):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot={
                **awaiting_hold,
                "sample_index": sample_index,
                "elapsed_seconds": float(40 + sample_index),
                "heartbeat_observed": False,
            },
        )
    transient_gap = store.get(task["task_id"])
    assert transient_gap is not None
    transient_proposal = transient_gap["artifacts"]["missionos_runtime_recovery_last_proposal"]
    assert invocations == [42]
    assert transient_proposal["proposal_id"] == proposal_id
    assert transient_proposal["proposal_status"] == "awaiting_operator_approval"
    assert (
        transient_gap["artifacts"]["missionos_runtime_recovery_agent_live_bridge"][
            "recovery_window_summary"
        ]["hard_breaches"]["telemetry_lost"]
        is True
    )

    store.update(
        task["task_id"],
        replace_artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "dispatch_status": "queued_for_active_runner",
                "dispatch_authority_created": True,
                "active_runner_request_queued": True,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "proposal_revalidation": {"proposal_id": proposal_id},
            }
        },
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 45,
            "elapsed_seconds": 85.0,
            "nav_state": 4,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "safety_hold",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_assist_attempted": True,
            "operator_recovery_assist_status": "safety_hold_observed",
            "operator_recovery_target_reached": False,
            "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
        },
    )
    authority_pending = store.get(task["task_id"])
    assert authority_pending is not None
    assert invocations == [42]
    assert (
        authority_pending["artifacts"]["missionos_runtime_recovery_agent_live_bridge"][
            "agent_refresh_status"
        ]
        == "dispatch_pending_runner_observation"
    )
    store.update(
        task["task_id"],
        replace_artifacts={"missionos_runtime_recovery_dispatch_receipt": {}},
    )

    expired = {
        **current,
        "valid_until": "2000-01-01T00:00:00+00:00",
    }
    store.update(
        task["task_id"],
        artifacts={"missionos_runtime_recovery_proposals": {proposal_id: expired}},
        replace_artifacts={"missionos_runtime_recovery_last_proposal": expired},
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 46,
            "elapsed_seconds": 86.0,
            "nav_state": 4,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "safety_hold",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_assist_attempted": True,
            "operator_recovery_assist_status": "safety_hold_observed",
            "operator_recovery_target_reached": False,
            "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
        },
    )
    recompiled = store.get(task["task_id"])
    assert recompiled is not None
    recompiled_artifacts = recompiled["artifacts"]
    refreshed = recompiled_artifacts["missionos_runtime_recovery_last_proposal"]
    assert invocations == [42]
    assert refreshed["proposal_id"] != proposal_id
    assert refreshed["proposal_status"] == "awaiting_operator_approval"
    assert refreshed["proposal_source"] == ("deterministic_recompile_of_prior_llm_judgment")
    assert refreshed["source_proposal_id"] == proposal_id
    assert refreshed["hosted_model_invoked_for_proposal"] is False
    assert (
        recompiled_artifacts["missionos_runtime_recovery_proposals"][proposal_id]["proposal_status"]
        == "stale"
    )

    superseded = {
        **refreshed,
        "proposal_status": "superseded",
        "invalidation_reasons": ["runtime_recovery_proposal_superseded_by_material_change"],
    }
    store.update(
        task["task_id"],
        artifacts={"missionos_runtime_recovery_proposals": {refreshed["proposal_id"]: superseded}},
        replace_artifacts={"missionos_runtime_recovery_last_proposal": superseded},
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 47,
            "elapsed_seconds": 87.0,
            "nav_state": 4,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "safety_hold",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_assist_attempted": True,
            "operator_recovery_assist_status": "safety_hold_observed",
            "operator_recovery_target_reached": False,
            "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
        },
    )
    repaired = store.get(task["task_id"])
    assert repaired is not None
    repaired_proposal = repaired["artifacts"]["missionos_runtime_recovery_last_proposal"]
    assert invocations == [42]
    assert repaired_proposal["proposal_id"] != refreshed["proposal_id"]
    assert repaired_proposal["proposal_status"] == "awaiting_operator_approval"
    assert repaired_proposal["proposal_source"] == ("deterministic_recompile_of_prior_llm_judgment")
    assert repaired_proposal["source_proposal_id"] == refreshed["proposal_id"]


def test_successful_recovery_does_not_queue_a_second_hold(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    queued_holds: list[dict] = []

    def _queue_hold(**kwargs) -> dict:
        queued_holds.append(dict(kwargs))
        return {"request_status": "queued"}

    def _unexpected_proposal(**_kwargs) -> dict:
        raise AssertionError("completed recovery must not invoke the hosted model")

    monkeypatch.setattr(
        live_run,
        "queue_px4_active_runner_recovery_request",
        _queue_hold,
    )
    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _unexpected_proposal,
    )
    task = store.create(
        task_id="task_successful_recovery_no_second_hold",
        kind="contract_test",
        title="Successful recovery does not re-HOLD",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "dispatch_status": "running",
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_auto_operator_recovery_request_"
                    "task_successful_recovery_no_second_hold.json"
                ),
            },
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.0,
                "dropoff_longitude": 139.00591,
            },
            "missionos_auto_mission_runtime_snapshot": {
                "gazebo_obstacle_model_spawned": True,
                "obstacle_manifest": {
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacles": [
                        {
                            "name": "route_obstacle",
                            "x_m": 0.0,
                            "y_m": 269.0,
                            "size_x_m": 18.0,
                            "size_y_m": 18.0,
                        }
                    ],
                },
            },
        },
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            "sample_index": 60,
            "elapsed_seconds": 100.0,
            "progress_m": 165.0,
            "local_x_m": -29.0,
            "local_y_m": 165.0,
            "local_z_m": -45.0,
            "altitude_above_home_m": 45.0,
            "battery_remaining_percent": 80.0,
            "heartbeat_observed": True,
            "nav_state": 3,
            "landed": False,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "avoid_obstacle",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_assist_attempted": True,
            "operator_recovery_assist_status": "target_reached",
            "operator_recovery_target_reached": True,
            "operator_recovery_resume_auto_status": "resumed_auto_mission",
        },
    )

    stored = store.get(task["task_id"])
    assert stored is not None
    bridge = stored["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert queued_holds == []
    assert bridge["recovery_observation_state"] == "succeeded"
    assert bridge["agent_refresh_status"] == "recovery_succeeded"
    assert "missionos_runtime_recovery_last_proposal" not in stored["artifacts"]


@pytest.mark.parametrize(
    ("expire_before_safe_window", "final_obstacle_name"),
    [
        (False, "missionos_route_obstacle_50pct"),
        (True, "missionos_route_obstacle_50pct"),
        (False, "missionos_route_obstacle_75pct"),
    ],
)
def test_strong_gust_hold_rejudges_obstacle_after_verified_wind_safe_window(
    tmp_path,
    monkeypatch,
    expire_before_safe_window: bool,
    final_obstacle_name: str,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    queued_holds: list[dict] = []

    def _queue_hold(**kwargs) -> dict:
        queued_holds.append(dict(kwargs))
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo-sitl",
            "container_path": kwargs["container_path"],
            "bytes_written": 100,
        }

    monkeypatch.setattr(
        live_run,
        "queue_px4_active_runner_recovery_request",
        _queue_hold,
    )
    task = store.create(
        task_id="task_compound_wind_obstacle_rejudgment",
        kind="contract_test",
        title="Strong gust then obstacle rejudgment",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "dispatch_status": "running",
                "operator_recovery_request_container_path": (
                    "/tmp/task_compound_wind_obstacle_rejudgment.json"
                ),
            },
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.0,
                "dropoff_longitude": 139.00591,
            },
            "missionos_auto_mission_runtime_snapshot": {
                "gazebo_obstacle_model_spawned": True,
                "obstacle_manifest": {
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacles": [
                        {
                            "name": "missionos_route_obstacle_50pct",
                            "x_m": 0.0,
                            "y_m": 350.0,
                            "size_x_m": 18.0,
                            "size_y_m": 18.0,
                        }
                    ],
                },
            },
        },
    )
    invocations: list[tuple[int, float]] = []

    def _proposal(**kwargs) -> dict:
        telemetry = kwargs["telemetry_snapshot"]
        wind_speed = float(telemetry["wind"]["speed_mps"])
        sample_index = int(telemetry["sample_index"])
        invocations.append((sample_index, wind_speed))
        wind_blocked = wind_speed > 6.0
        action = "operator_review" if wind_blocked else "avoid_obstacle"
        parameters = (
            {}
            if wind_blocked
            else {
                "target_x_m": -30.0,
                "target_y_m": 290.0,
                "target_altitude_m": 30.0,
                "source_obstacle_name": "missionos_route_obstacle_50pct",
            }
        )
        trigger_reasons = [
            *(["wind_above_recovery_limit"] if wind_blocked else []),
            "obstacle_or_building_risk",
        ]
        return {
            "schema_version": "missionos_runtime_recovery_agent_result.v1",
            "runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": action,
                "requires_human_approval": True,
                "proposed_parameters": parameters,
                "observed_risk_reasons": trigger_reasons,
            },
            "agent_output": {
                "selected_bounded_action": action,
                "requires_human_approval": True,
                "proposed_parameters": parameters,
                "trigger_reasons": trigger_reasons,
            },
            "agent_invocations": [{"function_tool_called": True}],
            "dispatch_authority_created": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _proposal,
    )
    base_snapshot = {
        "progress_m": 250.0,
        "local_x_m": 0.0,
        "local_y_m": 250.0,
        "local_z_m": -30.0,
        "local_vx_mps": 0.0,
        "local_vy_mps": 10.0,
        "altitude_above_home_m": 30.0,
        "battery_remaining_percent": 80.0,
        "heartbeat_observed": True,
        "nav_state": 3,
        "landed": False,
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 80,
            "elapsed_seconds": 80.0,
            "wind_speed_mps": 12.0,
        },
    )
    assert len(queued_holds) == 1
    assert invocations == []

    held_snapshot = {
        **base_snapshot,
        "nav_state": 4,
        "local_vy_mps": 0.0,
        "operator_recovery_request_observed": True,
        "operator_recovery_action": "safety_hold",
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_assist_attempted": True,
        "operator_recovery_assist_status": "safety_hold_observed",
        "operator_recovery_target_reached": False,
        "operator_recovery_resume_auto_status": (
            "held_awaiting_operator_recovery_approval"
        ),
    }
    for sample_index in (81, 82):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot={
                **held_snapshot,
                "sample_index": sample_index,
                "elapsed_seconds": float(sample_index),
                "wind_speed_mps": 12.0,
            },
        )
    wind_blocked = store.get(task["task_id"])
    assert wind_blocked is not None
    wind_blocked_artifacts = wind_blocked["artifacts"]
    wind_blocked_proposal = wind_blocked_artifacts[
        "missionos_runtime_recovery_last_proposal"
    ]
    wind_blocked_proposal_id = wind_blocked_proposal["proposal_id"]
    assert wind_blocked_proposal["source_obstacle_name"] == (
        "missionos_route_obstacle_50pct"
    )
    assert invocations == [(82, 12.0)]
    assert (
        wind_blocked_proposal["runtime_recovery_agent_result"]["assessment"][
            "selected_bounded_action"
        ]
        == "operator_review"
    )
    if expire_before_safe_window:
        expired = {
            **wind_blocked_proposal,
            "valid_until": "2000-01-01T00:00:00+00:00",
        }
        store.update(
            task["task_id"],
            artifacts={
                "missionos_runtime_recovery_proposals": {
                    wind_blocked_proposal_id: expired
                }
            },
            replace_artifacts={
                "missionos_runtime_recovery_last_proposal": expired
            },
        )

    for sample_index in (89, 99, 109, 119):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot={
                **held_snapshot,
                "sample_index": sample_index,
                "elapsed_seconds": float(sample_index),
                "wind_speed_mps": 7.0,
            },
        )
        assert invocations == [(82, 12.0)]

    sustained_wind = store.get(task["task_id"])
    assert sustained_wind is not None
    assert (
        "missionos_runtime_recovery_compound_hazard_transition"
        not in sustained_wind["artifacts"]
    )

    for sample_index in (129, 140, 151):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot={
                **held_snapshot,
                "sample_index": sample_index,
                "elapsed_seconds": float(sample_index),
                "wind_speed_mps": 5.0,
            },
        )
        assert invocations == [(82, 12.0)]

    if final_obstacle_name != "missionos_route_obstacle_50pct":
        store.update(
            task["task_id"],
            replace_artifacts={
                "missionos_auto_mission_runtime_snapshot": {
                    "gazebo_obstacle_model_spawned": True,
                    "obstacle_manifest": {
                        "building_risk_detected": True,
                        "gazebo_obstacle_model_spawned": True,
                        "obstacles": [
                            {
                                "name": final_obstacle_name,
                                "x_m": 0.0,
                                "y_m": 350.0,
                                "size_x_m": 18.0,
                                "size_y_m": 18.0,
                            }
                        ],
                    },
                }
            },
        )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **held_snapshot,
            "sample_index": 162,
            "elapsed_seconds": 162.0,
            "wind_speed_mps": 5.0,
        },
    )
    if final_obstacle_name != "missionos_route_obstacle_50pct":
        changed_obstacle = store.get(task["task_id"])
        assert changed_obstacle is not None
        assert invocations == [(82, 12.0)]
        assert (
            "missionos_runtime_recovery_compound_hazard_transition"
            not in changed_obstacle["artifacts"]
        )
        return
    rejudged = store.get(task["task_id"])
    assert rejudged is not None
    artifacts = rejudged["artifacts"]
    current = artifacts["missionos_runtime_recovery_last_proposal"]
    assert invocations == [(82, 12.0), (162, 5.0)]
    assert current["proposal_id"] != wind_blocked_proposal_id
    assert current["proposal_status"] == "awaiting_operator_approval"
    assert (
        current["runtime_recovery_agent_result"]["assessment"][
            "selected_bounded_action"
        ]
        == "avoid_obstacle"
    )
    superseded = artifacts["missionos_runtime_recovery_proposals"][
        wind_blocked_proposal_id
    ]
    assert superseded["proposal_status"] == (
        "stale" if expire_before_safe_window else "superseded"
    )
    if expire_before_safe_window:
        assert "runtime_recovery_proposal_stale" in superseded[
            "invalidation_reasons"
        ]
    else:
        assert "runtime_recovery_wind_safe_window_observed" in superseded[
            "invalidation_reasons"
        ]
    transition = artifacts[
        "missionos_runtime_recovery_compound_hazard_transition"
    ]
    assert transition["transition_status"] == "wind_safe_window_observed"
    assert transition["source_obstacle_name"] == (
        "missionos_route_obstacle_50pct"
    )
    assert transition["wind_safe_window"]["safe_window_observed"] is True
    assert transition["wind_safe_window"]["observed_window_s"] == 30.0
    assert transition["wind_safe_window"]["wind_speed_max_mps"] == 5.0
    assert transition["dispatch_authority_created"] is False
    assert transition["physical_execution_invoked"] is False


def test_wind_safe_window_evidence_fails_closed() -> None:
    policy = {"max_wind_speed_mps": 6.0}

    def _sample(elapsed_s: float, wind_mps: float | None, *, stale: bool = False):
        return {
            "elapsed_seconds": elapsed_s,
            "wind": {"speed_mps": wind_mps},
            "telemetry": {"stale": stale},
        }

    verified = build_wind_safe_window_evidence(
        [_sample(0.0, 5.0), _sample(10.0, 5.5), _sample(20.0, 5.0), _sample(30.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )
    irregular_cadence = build_wind_safe_window_evidence(
        [_sample(0.0, 5.0), _sample(11.0, 5.0), _sample(22.0, 5.0), _sample(33.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )
    too_short = build_wind_safe_window_evidence(
        [_sample(20.0, 5.0), _sample(30.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )
    too_sparse = build_wind_safe_window_evidence(
        [_sample(0.0, 5.0), _sample(30.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )
    gust_present = build_wind_safe_window_evidence(
        [_sample(0.0, 5.0), _sample(10.0, 12.0), _sample(20.0, 5.0), _sample(30.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )
    stale = build_wind_safe_window_evidence(
        [_sample(0.0, 5.0), _sample(10.0, 5.0), _sample(20.0, 5.0, stale=True), _sample(30.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )
    missing = build_wind_safe_window_evidence(
        [_sample(0.0, 5.0), _sample(10.0, None), _sample(20.0, 5.0), _sample(30.0, 5.0)],
        recovery_policy=policy,
        minimum_window_s=30.0,
        maximum_sample_gap_s=15.0,
    )

    assert verified["verification_status"] == "verified_safe"
    assert verified["safe_window_observed"] is True
    assert irregular_cadence["verification_status"] == "verified_safe"
    assert irregular_cadence["observed_window_s"] == 30.0
    assert too_short["blocking_reasons"] == [
        "wind_safe_window_duration_insufficient"
    ]
    assert "wind_safe_window_sample_gap_exceeded" in too_sparse[
        "blocking_reasons"
    ]
    assert "wind_safe_window_limit_exceeded" in gust_present["blocking_reasons"]
    assert "wind_safe_window_telemetry_stale" in stale["blocking_reasons"]
    assert "wind_safe_window_observation_missing" in missing["blocking_reasons"]


def test_expired_proposal_does_not_block_materially_new_decision_epoch(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_runtime_recovery_stale_epoch",
        kind="contract_test",
        title="Runtime recovery stale proposal epoch",
        status="running",
    )
    invocations: list[int] = []

    def _proposal(**kwargs) -> dict:
        sample_index = int(kwargs["telemetry_snapshot"]["sample_index"])
        invocations.append(sample_index)
        return {
            "schema_version": "missionos_runtime_recovery_agent_result.v1",
            "runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_output": {
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_invocations": [{"function_tool_called": True}],
            "dispatch_authority_created": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _proposal,
    )

    def _snapshot(sample_index: int, *, nav_state: int = 3) -> dict:
        return {
            "sample_index": sample_index,
            "elapsed_seconds": float(sample_index * 20),
            "progress_m": 100.0,
            "local_x_m": 10.0,
            "local_y_m": 5.0,
            "local_z_m": -30.0,
            "altitude_above_home_m": 30.0,
            "battery_remaining_percent": 10.0,
            "heartbeat_observed": True,
            "nav_state": nav_state,
            "landed": False,
        }

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(1),
    )
    first = store.get(task["task_id"])
    assert first is not None
    first_proposal = first["artifacts"]["missionos_runtime_recovery_last_proposal"]
    first_id = first_proposal["proposal_id"]
    expired_proposal = {
        **first_proposal,
        "valid_until": "2000-01-01T00:00:00+00:00",
    }
    store.update(
        task["task_id"],
        artifacts={
            "missionos_runtime_recovery_proposals": {
                first_id: expired_proposal,
            }
        },
        replace_artifacts={
            "missionos_runtime_recovery_last_proposal": expired_proposal,
        },
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(2),
    )
    stale = store.get(task["task_id"])
    assert stale is not None
    stale_artifacts = stale["artifacts"]
    assert invocations == [1]
    assert stale_artifacts["missionos_runtime_recovery_last_proposal"]["proposal_status"] == "stale"
    assert (
        stale_artifacts["missionos_runtime_recovery_agent_live_bridge"]["agent_refresh_status"]
        == "proposal_stale"
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_snapshot(3, nav_state=4),
    )
    refreshed = store.get(task["task_id"])
    assert refreshed is not None
    refreshed_artifacts = refreshed["artifacts"]
    refreshed_proposal = refreshed_artifacts["missionos_runtime_recovery_last_proposal"]
    assert invocations == [1, 3]
    assert refreshed_proposal["proposal_id"] != first_id
    assert refreshed_proposal["proposal_status"] == "awaiting_operator_approval"
    assert (
        refreshed_artifacts["missionos_runtime_recovery_proposals"][first_id]["proposal_status"]
        == "stale"
    )


def test_dispatch_revalidation_failure_marks_prior_proposal_stale(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_runtime_recovery_revalidation_stale",
        kind="contract_test",
        title="Failed dispatch revalidation closes prior proposal",
        status="running",
    )

    def _proposal(**_kwargs) -> dict:
        return {
            "schema_version": "missionos_runtime_recovery_agent_result.v1",
            "runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_output": {
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_invocations": [{"function_tool_called": True}],
            "dispatch_authority_created": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _proposal,
    )
    base_snapshot = {
        "elapsed_seconds": 20.0,
        "progress_m": 100.0,
        "local_x_m": 10.0,
        "local_y_m": 5.0,
        "local_z_m": -30.0,
        "altitude_above_home_m": 30.0,
        "battery_remaining_percent": 10.0,
        "heartbeat_observed": True,
        "nav_state": 3,
        "landed": False,
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={**base_snapshot, "sample_index": 1},
    )
    first = store.get(task["task_id"])
    assert first is not None
    first_proposal = first["artifacts"]["missionos_runtime_recovery_last_proposal"]
    first_id = first_proposal["proposal_id"]
    store.update(
        task["task_id"],
        replace_artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "dispatch_status": "blocked",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "blocked_reasons": ["runtime_recovery_current_telemetry_stale"],
                "proposal_revalidation": {"proposal_id": first_id},
                "dispatch_authority_created": False,
            }
        },
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={**base_snapshot, "sample_index": 2, "elapsed_seconds": 40.0},
    )

    refreshed = store.get(task["task_id"])
    assert refreshed is not None
    proposals = refreshed["artifacts"]["missionos_runtime_recovery_proposals"]
    assert proposals[first_id]["proposal_status"] == "stale"
    assert proposals[first_id]["invalidation_reasons"] == [
        "runtime_recovery_current_telemetry_stale"
    ]


def test_new_hard_obstacle_supersedes_valid_pending_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    queued_holds: list[dict] = []

    def _queue_hold(**kwargs) -> dict:
        queued_holds.append(dict(kwargs))
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo-sitl",
            "container_path": kwargs["container_path"],
            "bytes_written": 100,
        }

    monkeypatch.setattr(
        live_run,
        "queue_px4_active_runner_recovery_request",
        _queue_hold,
    )
    task = store.create(
        task_id="task_runtime_recovery_obstacle_supersession",
        kind="contract_test",
        title="Runtime recovery obstacle supersession",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "dispatch_status": "running",
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_auto_operator_recovery_request_"
                    "task_runtime_recovery_obstacle_supersession.json"
                ),
            },
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.0,
                "dropoff_longitude": 139.00591,
            },
        },
    )
    invocations: list[int] = []

    def _proposal(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return {
            "schema_version": "missionos_runtime_recovery_agent_result.v1",
            "runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_output": {
                "selected_bounded_action": "adjust_altitude",
                "requires_human_approval": True,
                "proposed_parameters": {"target_altitude_m": 45.0},
            },
            "agent_invocations": [{"function_tool_called": True}],
            "dispatch_authority_created": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _proposal,
    )
    base_snapshot = {
        "sample_index": 1,
        "elapsed_seconds": 20.0,
        "progress_m": 100.0,
        "local_x_m": 0.0,
        "local_y_m": 100.0,
        "local_z_m": -30.0,
        "altitude_above_home_m": 30.0,
        "battery_remaining_percent": 10.0,
        "heartbeat_observed": True,
        "nav_state": 3,
        "landed": False,
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=base_snapshot,
    )
    first = store.get(task["task_id"])
    assert first is not None
    first_id = first["artifacts"]["missionos_runtime_recovery_last_proposal"]["proposal_id"]

    store.update(
        task["task_id"],
        artifacts={
            "missionos_auto_mission_runtime_snapshot": {
                "gazebo_obstacle_model_spawned": True,
                "obstacle_manifest": {
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacles": [
                        {
                            "name": "dropoff_blocker",
                            "x_m": 0.0,
                            "y_m": 539.0,
                            "size_x_m": 18.0,
                            "size_y_m": 18.0,
                        }
                    ],
                },
            }
        },
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **base_snapshot,
            "sample_index": 2,
            "elapsed_seconds": 40.0,
            "local_y_m": 430.0,
            "local_vx_mps": 0.0,
            "local_vy_mps": 10.0,
        },
    )

    refreshed = store.get(task["task_id"])
    assert refreshed is not None
    artifacts = refreshed["artifacts"]
    current = artifacts["missionos_runtime_recovery_last_proposal"]
    assert invocations == [1]
    assert current["proposal_id"] == first_id
    assert current["proposal_status"] == "superseded"
    assert (
        artifacts["missionos_runtime_recovery_proposals"][first_id]["proposal_status"]
        == "superseded"
    )
    invalidation_reasons = artifacts["missionos_runtime_recovery_proposals"][first_id][
        "invalidation_reasons"
    ]
    assert "runtime_recovery_proposal_origin_drift_exceeded" in invalidation_reasons
    assert "runtime_recovery_proposal_superseded_by_material_change" in invalidation_reasons
    assert len(queued_holds) == 1
    hold_request = queued_holds[0]["request_payload"]
    assert hold_request["recovery_action"] == "safety_hold"
    assert hold_request["preauthorized_safety_reflex"] is True
    assert hold_request["operator_approved"] is False
    hold_receipt = artifacts["missionos_runtime_recovery_safety_hold_receipt"]
    assert hold_receipt["request_status"] == "queued"
    assert hold_receipt["safety_policy_created_dispatch_authority"] is True
    assert hold_receipt["agent_created_dispatch_authority"] is False

    held_snapshot = {
        **base_snapshot,
        "nav_state": 4,
        "local_y_m": 430.0,
        "local_vx_mps": 0.0,
        "local_vy_mps": 0.0,
        "operator_recovery_request_observed": True,
        "operator_recovery_action": "safety_hold",
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_assist_attempted": True,
        "operator_recovery_assist_status": "safety_hold_observed",
        "operator_recovery_target_reached": False,
        "operator_recovery_resume_auto_status": ("held_awaiting_operator_recovery_approval"),
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **held_snapshot,
            "sample_index": 3,
            "elapsed_seconds": 41.0,
        },
    )
    assert invocations == [1]
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            **held_snapshot,
            "sample_index": 4,
            "elapsed_seconds": 42.0,
        },
    )
    stabilized = store.get(task["task_id"])
    assert stabilized is not None
    stabilized_proposal = stabilized["artifacts"]["missionos_runtime_recovery_last_proposal"]
    assert invocations == [1, 4]
    assert stabilized_proposal["proposal_id"] != first_id
    assert stabilized_proposal["proposal_status"] == "awaiting_operator_approval"


def test_runtime_probe_never_resumes_auto_before_recovery_target_is_reached() -> None:
    script = auto_probe._inner_runtime_probe_script(
        dropoff_dwell_mission_seq=2,
        land_mission_seq=3,
        release_altitude_target_m=30.0,
        release_altitude_tolerance_m=2.0,
        required_dwell_seconds=2.0,
        monitor_seconds=60.0,
        min_progress_m=1.0,
        no_progress_grace_seconds=10.0,
        min_route_altitude_m=20.0,
        altitude_grace_seconds=10.0,
        min_battery_remaining_percent=20.0,
        post_abort_wait_seconds=10.0,
        land_post_abort_wait_seconds=10.0,
        rtl_post_abort_wait_seconds=10.0,
        rtl_recovery_min_progress_m=5.0,
        sim_battery_min_remaining_percent=15.0,
        sim_battery_drain_seconds=600.0,
        thermal_motor_derate_factor=None,
        wind_mean_mps=None,
        wind_direction_deg=None,
        wind_gust_mps=None,
        wind_variance=None,
        gz_physical_battery_enabled=False,
        resume_mission_seq_after_obstacle=1,
    )

    assert "and resume_safety_verification.get('resume_auto_authorized') is True" in script
    assert "target_reached or action in" not in script
    assert "remaining_route_intersects_collision_geometry" in script
    assert "recovery_leg_lateral_clearance_not_verified" in script
    assert "recovery_leg_clearance_verified" in script
    assert "OPERATOR_RECOVERY_LATERAL_OBSTACLE_MARGIN_M=20.0" in script
    assert "original_dropoff_collision_occupied" in script
    assert "held_remaining_route_or_dropoff_unsafe" in script
    assert "held_at_alternate_dropoff_awaiting_operator_decision" in script
    assert "action == 'safety_hold'" in script
    assert "preauthorized_safety_reflex_required" in script
    assert "NAV_AUTO_LOITER=4" in script
    assert "held_after_recovery_target_not_reached" in script
    assert "'source': 'dispatch_current_position_observation'" in script
    assert "'maneuver_observation_sample_count': len(maneuver_samples)" in script
    assert "'maneuver_observation_samples': maneuver_samples," in script
    assert "maneuver_samples[-5:]" not in script
    assert "MAVLINK_MSG_ID_MISSION_SET_CURRENT=41" in script
    assert "def mission_set_current(mission_seq, seq):" in script
    assert "struct.pack('<HBB', int(mission_seq), 1, 1)" in script
    assert "'protocol': 'mavlink_mission_set_current'" in script
    assert "mission_state=listener('mission', 1)" in script
    assert "current_seq=parse_int(mission_state, 'current_seq')" in script
    assert "RESUME_MISSION_SEQ_AFTER_OBSTACLE=1" in script
    assert "params=request.get('recovery_parameters')" in script
    assert "params=params if isinstance(params, dict) else {}" in script
    assert "deferred_route_obstacles=[]" in script
    assert "separately_guarded_later_obstacle=bool(" in script
    assert "'recovery_target_to_next_known_obstacle_guard_boundary'" in script
    assert "'next_obstacle_recovery_guard_required': bool(deferred_route_obstacles)" in script
    assert "resume_mission_sequence_after_obstacle_not_observed" in script
    assert "wait_mission_current(" in script
    assert "safety_hold_active and nav == NAV_AUTO_LOITER" in script
    assert "while monitor_effective_elapsed() < MONITOR_SECONDS" in script
    assert "monitor_excluded_recovery_seconds" in script
    assert "current-started-monitor_excluded_seconds-active_hold_seconds" in script
    assert "Re-observe PX4 on the" in script
    assert "time.sleep(1.0)\n            continue" in script
    compile(script, "<missionos-auto-runtime-probe>", "exec")


def test_next_bound_obstacle_queues_a_fresh_safety_hold(
    tmp_path,
    monkeypatch,
) -> None:
    queued_holds: list[dict] = []

    def _queue_hold(**kwargs) -> dict:
        queued_holds.append(dict(kwargs))
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo-sitl",
            "container_path": kwargs["container_path"],
            "bytes_written": 100,
        }

    monkeypatch.setattr(
        live_run,
        "queue_px4_active_runner_recovery_request",
        _queue_hold,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_two_obstacle_hold_cycle",
        kind="contract_test",
        title="Two obstacle hold cycle",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "dispatch_status": "running",
                "operator_recovery_request_container_path": "/tmp/two-obstacle.json",
            },
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.0,
                "dropoff_longitude": 139.00591,
            },
            "missionos_auto_mission_runtime_snapshot": {
                "gazebo_obstacle_model_spawned": True,
                "obstacle_manifest": {
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacles": [
                        {
                            "name": "missionos_route_obstacle_50pct",
                            "x_m": 0.0,
                            "y_m": 269.0,
                            "size_x_m": 18.0,
                            "size_y_m": 18.0,
                        },
                        {
                            "name": "missionos_route_obstacle_75pct",
                            "x_m": 0.0,
                            "y_m": 404.0,
                            "size_x_m": 18.0,
                            "size_y_m": 18.0,
                        },
                    ],
                },
            },
            "missionos_runtime_recovery_safety_hold_receipt": {
                "request_status": "observed",
                "conflict_assessment": {
                    "nearest_obstacle": {"obstacle_name": "missionos_route_obstacle_50pct"}
                },
            },
        },
    )

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot={
            "sample_index": 100,
            "elapsed_seconds": 160.0,
            "progress_m": 300.0,
            "local_x_m": 0.0,
            "local_y_m": 300.0,
            "local_z_m": -45.0,
            "altitude_above_home_m": 45.0,
            "battery_remaining_percent": 80.0,
            "heartbeat_observed": True,
            "nav_state": 3,
            "landed": False,
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "avoid_obstacle",
            "operator_recovery_parameters": {
                "source_obstacle_name": "missionos_route_obstacle_50pct"
            },
            "operator_recovery_assist_status": "target_reached",
            "operator_recovery_target_reached": True,
            "operator_recovery_resume_auto_status": "resumed_auto_mission",
        },
    )

    assert len(queued_holds) == 1
    stored = store.get(task["task_id"])
    assert stored is not None
    receipt = stored["artifacts"]["missionos_runtime_recovery_safety_hold_receipt"]
    assert receipt["request_status"] == "queued"
    assert (
        receipt["conflict_assessment"]["nearest_obstacle"]["obstacle_name"]
        == "missionos_route_obstacle_75pct"
    )


def test_stream_window_without_target_is_a_failed_recovery_observation() -> None:
    assert (
        live_run._runtime_recovery_observation_state(
            {
                "landed": False,
                "recovery": {
                    "request_observed": True,
                    "command_ack_observed": True,
                    "assist_attempted": True,
                    "assist_status": "stream_window_complete",
                    "target_reached": False,
                    "resume_status": "not_attempted_target_not_reached",
                },
            }
        )
        == "failed"
    )


def test_unsafe_original_route_resume_is_a_fresh_recovery_observation() -> None:
    assert (
        live_run._runtime_recovery_observation_state(
            {
                "landed": False,
                "recovery": {
                    "request_observed": True,
                    "assist_attempted": True,
                    "assist_status": "target_reached",
                    "target_reached": True,
                    "resume_status": "held_remaining_route_or_dropoff_unsafe",
                    "resume_safety_verification": {
                        "verification_status": "failed",
                        "original_dropoff_available": False,
                    },
                },
            }
        )
        == "failed"
    )


def test_alternate_dropoff_hold_does_not_claim_resume_or_completion() -> None:
    assert (
        live_run._runtime_recovery_observation_state(
            {
                "landed": False,
                "recovery": {
                    "request_observed": True,
                    "assist_attempted": True,
                    "assist_status": "target_reached",
                    "target_reached": True,
                    "resume_status": ("held_at_alternate_dropoff_awaiting_operator_decision"),
                },
            }
        )
        == "held_at_alternate_dropoff"
    )


def test_gazebo_obstacle_artifacts_require_pose_readback_for_spawn_claim() -> None:
    route = {
        "takeoff_latitude": 35.681236,
        "takeoff_longitude": 139.767125,
        "dropoff_latitude": 35.6984,
        "dropoff_longitude": 139.773,
        "landing_zone_blocked": True,
    }
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(route)
    artifacts = auto_probe._gazebo_obstacle_runtime_artifacts(
        route=route,
        probe_observed={
            "gazebo_obstacle_application": {
                "application_status": "applied",
                "gazebo_obstacle_model_spawn_requested": True,
                "gazebo_obstacle_model_spawned": True,
                "requested_model_count": 1,
                "spawned_model_count": 1,
                "obstacle_manifest": {
                    **manifest,
                    "gazebo_obstacle_model_spawned": True,
                },
                "models": [
                    {
                        "name": "missionos_landing_zone_blocker",
                        "pose_readback_observed": True,
                        "pose_readback": {"x": 10.0, "y": 2.0, "z": 10.0},
                    }
                ],
            }
        },
    )

    assert artifacts["gazebo_world_application"]["application_status"] == "applied"
    assert artifacts["obstacle_manifest"]["gazebo_obstacle_model_spawned"] is True
    assert artifacts["observed_world_condition_evidence"]["observation_status"] == (
        "gazebo_obstacle_pose_readback_observed"
    )


def test_running_gazebo_obstacle_spawn_reaches_recovery_projection() -> None:
    marker = {
        "sample_index": 3,
        "gazebo_obstacle_model_spawned": True,
        "gazebo_obstacle_model_spawn_requested": True,
        "gazebo_obstacle_application_status": "applied",
        "obstacle_manifest": {
            "schema_version": "missionos_gazebo_obstacle_manifest.v1",
            "manifest_status": "configured",
            "building_risk_detected": True,
            "landing_zone_blocked": True,
            "gazebo_obstacle_model_spawned": True,
            "obstacles": [{"name": "missionos_landing_zone_blocker"}],
        },
        "gazebo_obstacle_application": {"application_status": "applied"},
    }
    snapshot = auto_probe._build_running_snapshot(marker, waypoint_total=4)
    projection = live_run._auto_runtime_obstacle_projection(
        artifacts={"missionos_auto_mission_runtime_snapshot": snapshot}
    )

    assert projection["projection_status"] == "source_backed"
    assert projection["obstacle_detected"] is True
    assert projection["building_risk_detected"] is True
    assert projection["gazebo_obstacle_model_spawned"] is True


def test_running_snapshot_preserves_effective_wind_for_recovery_decisions() -> None:
    snapshot = auto_probe._build_running_snapshot(
        {
            "sample_index": 9,
            "wind_mean_started": True,
            "gust_active": True,
            "gust_started": True,
            "wind_speed_mps": 12.0,
            "wind_direction_deg": 189.0,
            "wind_gust_trigger_on_obstacle": True,
            "wind_gust_trigger_obstacle_distance_m": 139.5,
        },
        waypoint_total=23,
    )

    assert snapshot["wind_speed_mps"] == 12.0
    assert snapshot["wind_direction_deg"] == 189.0
    assert snapshot["wind_gust_active"] is True
    assert snapshot["wind_gust_started"] is True
    assert snapshot["wind_gust_trigger_on_obstacle"] is True
    assert snapshot["wind_gust_trigger_obstacle_distance_m"] == 139.5


def test_runtime_probe_triggers_gust_from_source_backed_obstacle_approach() -> None:
    script = auto_probe._inner_runtime_probe_script(
        dropoff_dwell_mission_seq=2,
        land_mission_seq=3,
        release_altitude_target_m=30.0,
        release_altitude_tolerance_m=2.0,
        required_dwell_seconds=2.0,
        monitor_seconds=60.0,
        min_progress_m=1.0,
        no_progress_grace_seconds=10.0,
        min_route_altitude_m=20.0,
        altitude_grace_seconds=10.0,
        min_battery_remaining_percent=20.0,
        post_abort_wait_seconds=10.0,
        land_post_abort_wait_seconds=10.0,
        rtl_post_abort_wait_seconds=10.0,
        rtl_recovery_min_progress_m=5.0,
        sim_battery_min_remaining_percent=15.0,
        sim_battery_drain_seconds=600.0,
        thermal_motor_derate_factor=None,
        wind_mean_mps=5.0,
        wind_direction_deg=270.0,
        wind_gust_mps=12.0,
        wind_variance=2.0,
        gz_physical_battery_enabled=False,
        obstacle_manifest={
            "obstacles": [
                {
                    "name": "prompt_defined_obstacle",
                    "collision_enabled": True,
                    "x_m": 100.0,
                    "y_m": 20.0,
                }
            ]
        },
    )

    assert "WIND_GUST_TRIGGER_ON_OBSTACLE=bool(" in script
    assert "WIND_GUST_OBSTACLE_TRIGGER_DISTANCE_M=140.0" in script
    assert "and wind_gust_trigger_ready" in script
    assert "wind_gust_trigger_obstacle_distance_m" in script


def test_obstacle_conflict_projection_waits_until_destination_obstacle_is_local() -> None:
    route = {
        "takeoff_latitude": 35.0,
        "takeoff_longitude": 139.0,
        "dropoff_latitude": 35.0,
        "dropoff_longitude": 139.00591,
    }
    obstacle = {
        "projection_status": "source_backed",
        "obstacle_manifest": {
            "obstacles": [
                {
                    "name": "dropoff_blocker",
                    "x_m": 0.0,
                    "y_m": 539.0,
                    "size_x_m": 18.0,
                    "size_y_m": 18.0,
                }
            ]
        },
    }
    artifacts = {"mission_designer_coordinate_pair_route": route}

    distant = live_run._auto_runtime_obstacle_conflict_projection(
        snapshot={
            "local_x_m": 0.0,
            "local_y_m": 143.0,
            "local_vx_mps": 0.0,
            "local_vy_mps": 10.0,
        },
        artifacts=artifacts,
        obstacle_projection=obstacle,
    )
    local = live_run._auto_runtime_obstacle_conflict_projection(
        snapshot={
            "local_x_m": 0.0,
            "local_y_m": 430.0,
            "local_vx_mps": 0.0,
            "local_vy_mps": 10.0,
        },
        artifacts=artifacts,
        obstacle_projection=obstacle,
    )

    assert distant["local_avoidance_required"] is False
    assert distant["nearest_obstacle"]["distance_to_obstacle_m"] == 396.0
    assert local["local_avoidance_required"] is True
    assert local["nearest_obstacle"]["time_to_conflict_s"] == 10.0


def test_obstacle_conflict_projection_selects_next_local_conflict_not_passed_one() -> None:
    artifacts = {
        "mission_designer_coordinate_pair_route": {
            "takeoff_latitude": 35.0,
            "takeoff_longitude": 139.0,
            "dropoff_latitude": 35.0,
            "dropoff_longitude": 139.00591,
        }
    }
    projection = live_run._auto_runtime_obstacle_conflict_projection(
        snapshot={
            "local_x_m": 0.0,
            "local_y_m": 300.0,
            "local_vx_mps": 0.0,
            "local_vy_mps": 10.0,
        },
        artifacts=artifacts,
        obstacle_projection={
            "projection_status": "source_backed",
            "obstacle_manifest": {
                "obstacles": [
                    {
                        "name": "missionos_route_obstacle_50pct",
                        "x_m": 0.0,
                        "y_m": 269.5,
                        "size_x_m": 18.0,
                        "size_y_m": 18.0,
                    },
                    {
                        "name": "missionos_route_obstacle_75pct",
                        "x_m": 0.0,
                        "y_m": 404.25,
                        "size_x_m": 18.0,
                        "size_y_m": 18.0,
                    },
                ]
            },
        },
    )

    assert projection["local_avoidance_required"] is True
    assert projection["nearest_obstacle"]["obstacle_index"] == 1
    assert projection["nearest_obstacle"]["obstacle_name"] == ("missionos_route_obstacle_75pct")


def test_recovery_window_treats_source_backed_obstacle_as_hard_news() -> None:
    summary = build_recovery_window_summary(
        [
            {
                "sample_index": 1,
                "elapsed_seconds": 1.0,
                "battery_remaining_percent": 98.0,
                "terrain_clearance_m": 30.0,
                "obstacle": {
                    "projection_status": "source_backed",
                    "obstacle_detected": True,
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacle_manifest": {
                        "building_risk_detected": True,
                        "gazebo_obstacle_model_spawned": True,
                    },
                },
            }
        ]
    )

    assert summary["hard_breaches"]["obstacle_or_building_risk"] is True
    assert summary["hard_breaches"]["any"] is True
    assert summary["overall"]["obstacle_or_building_risk_count"] == 1
    assert summary["latest"]["obstacle_or_building_risk"] is True


def test_recovery_window_preserves_explicit_terrain_grace_judgment() -> None:
    summary = build_recovery_window_summary(
        [
            {
                "sample_index": 33,
                "elapsed_seconds": 33.0,
                "terrain_clearance_m": 29.9,
                "terrain_clearance_target_m": 30.0,
                "terrain_clearance_margin_m": -0.1,
                "terrain_clearance_grace_m": 1.0,
                "terrain_clearance_below_minimum": False,
            }
        ],
        min_terrain_clearance_m=30.0,
    )

    assert summary["hard_breaches"]["terrain_clearance_below_minimum"] is False
    assert summary["hard_breaches"]["any"] is False
    assert summary["soft_signals"]["terrain_clearance_near_minimum"] is False


def test_recovery_window_preserves_nested_terrain_grace_judgment() -> None:
    summary = build_recovery_window_summary(
        [
            {
                "sample_index": 34,
                "elapsed_seconds": 34.0,
                "terrain": {
                    "terrain_clearance_m": 29.9,
                    "terrain_clearance_target_m": 30.0,
                    "terrain_clearance_margin_m": -0.1,
                    "terrain_clearance_grace_m": 1.0,
                    "terrain_clearance_below_minimum": False,
                },
            }
        ],
        min_terrain_clearance_m=30.0,
    )

    assert summary["hard_breaches"]["terrain_clearance_below_minimum"] is False
    assert summary["hard_breaches"]["any"] is False
    assert summary["soft_signals"]["terrain_clearance_near_minimum"] is False


def test_recovery_window_warns_near_explicit_terrain_grace_floor() -> None:
    summary = build_recovery_window_summary(
        [
            {
                "sample_index": 35,
                "elapsed_seconds": 35.0,
                "terrain": {
                    "terrain_clearance_m": 29.2,
                    "terrain_clearance_target_m": 30.0,
                    "terrain_clearance_margin_m": -0.8,
                    "terrain_clearance_grace_m": 1.0,
                    "terrain_clearance_below_minimum": False,
                },
            }
        ],
        min_terrain_clearance_m=30.0,
    )

    assert summary["hard_breaches"]["terrain_clearance_below_minimum"] is False
    assert summary["soft_signals"]["terrain_clearance_near_minimum"] is True


def test_recovery_window_uses_numeric_terrain_fallback_without_explicit_judgment() -> None:
    summary = build_recovery_window_summary(
        [
            {
                "sample_index": 2,
                "elapsed_seconds": 2.0,
                "terrain_clearance_m": 29.9,
            }
        ],
        min_terrain_clearance_m=30.0,
    )

    assert summary["hard_breaches"]["terrain_clearance_below_minimum"] is True


def test_recovery_window_debounces_one_missed_heartbeat_poll() -> None:
    transient = build_recovery_window_summary(
        [
            {
                "sample_index": 10,
                "elapsed_seconds": 10.0,
                "heartbeat_observed": False,
            }
        ]
    )
    lost = build_recovery_window_summary(
        [
            {
                "sample_index": 10,
                "elapsed_seconds": 10.0,
                "heartbeat_observed": False,
            },
            {
                "sample_index": 11,
                "elapsed_seconds": 11.0,
                "heartbeat_observed": False,
            },
        ]
    )

    assert transient["latest"]["telemetry_stale"] is True
    assert transient["hard_breaches"]["telemetry_lost"] is False
    assert transient["overall"]["trailing_telemetry_stale_count"] == 1
    assert lost["hard_breaches"]["telemetry_lost"] is True
    assert lost["overall"]["trailing_telemetry_stale_count"] == 2


def test_battery_endurance_projection_waits_for_route_baseline() -> None:
    early = live_run._auto_runtime_battery_endurance_projection(
        {
            "progress_m": 7.0,
            "battery_remaining_percent": 99.0,
            "battery_remaining_delta_percent": -1.0,
        },
        planned_route_m=539.0,
    )
    established = live_run._auto_runtime_battery_endurance_projection(
        {
            "progress_m": 50.0,
            "battery_remaining_percent": 98.0,
            "battery_remaining_delta_percent": -2.0,
        },
        planned_route_m=539.0,
    )

    assert early["projection_status"] == "insufficient_observation"
    assert early["projection_reason"] == ("battery_burn_baseline_distance_insufficient")
    assert early["minimum_progress_m"] == 26.95
    assert "projected_insufficient_for_route" not in early
    assert established["projection_status"] == "computed"


def test_distant_destination_obstacle_is_not_local_recovery_news() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["position"] = {
        "local_x_m": 0.0,
        "local_y_m": 143.0,
        "altitude_above_home_m": 30.0,
    }
    telemetry["route"] = {
        "active_leg": {
            "from_x_m": 0.0,
            "from_y_m": 0.0,
            "to_x_m": 0.0,
            "to_y_m": 539.0,
        },
        "ground_speed_mps": 10.0,
    }
    telemetry["obstacle"]["obstacle_manifest"]["obstacles"][0].update({"x_m": 0.0, "y_m": 539.0})

    planner = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
    )
    assert planner["tool_status"] == "insufficient_context"
    assert "avoid_obstacle" not in planner["candidate_actions"]

    telemetry["obstacle"]["conflict_assessment"] = {
        "assessment_status": "computed",
        "local_avoidance_required": False,
        "conflict_class": "distant_or_non_intersecting_obstacle",
        "distance_to_obstacle_m": 396.0,
        "time_to_conflict_s": 39.6,
    }
    summary = build_recovery_window_summary(
        [{"sample_index": 1, "elapsed_seconds": 1.0, **telemetry}]
    )
    assert summary["hard_breaches"]["obstacle_or_building_risk"] is False
    assert summary["latest"]["obstacle_or_building_risk"] is False


def test_near_route_obstacle_compiles_local_avoidance_with_conflict_facts() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["route"]["ground_speed_mps"] = 10.0
    planner = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
    )

    candidate = planner["recommended_candidate"]
    conflict = candidate["basis"]["conflict_assessment"]
    assert candidate["selected_bounded_action"] == "avoid_obstacle"
    assert conflict["local_avoidance_required"] is True
    assert conflict["route_corridor_intersects"] is True
    assert conflict["time_to_conflict_s"] == 9.0


def test_second_route_obstacle_becomes_the_next_bound_recovery_target() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["position"].update({"local_x_m": 300.0, "local_y_m": 0.0})
    telemetry["route"]["active_leg"] = {
        "from_x_m": 300.0,
        "from_y_m": 0.0,
        "to_x_m": 539.0,
        "to_y_m": 0.0,
    }
    telemetry["route"]["ground_speed_mps"] = 10.0
    telemetry["obstacle"]["obstacle_manifest"]["obstacles"] = [
        {
            "name": "missionos_route_obstacle_50pct",
            "x_m": 269.5,
            "y_m": 0.0,
            "size_x_m": 18.0,
            "size_y_m": 18.0,
        },
        {
            "name": "missionos_route_obstacle_75pct",
            "x_m": 404.25,
            "y_m": 0.0,
            "size_x_m": 18.0,
            "size_y_m": 18.0,
        },
    ]
    telemetry["obstacle"]["conflict_assessment"] = {
        "assessment_status": "computed",
        "local_avoidance_required": True,
        "nearest_obstacle": {
            "obstacle_index": 1,
            "obstacle_name": "missionos_route_obstacle_75pct",
            "local_avoidance_required": True,
        },
    }

    planner = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
    )

    candidate = planner["recommended_candidate"]
    assert candidate["basis"]["obstacle_name"] == ("missionos_route_obstacle_75pct")
    assert candidate["proposed_parameters"]["source_obstacle_name"] == (
        "missionos_route_obstacle_75pct"
    )


def test_local_route_conflict_rejects_unproven_altitude_only_candidate() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["terrain"]["terrain_clearance_below_minimum"] = False
    telemetry["obstacle"]["conflict_assessment"] = {
        "assessment_status": "computed",
        "local_avoidance_required": True,
        "route_corridor_intersects": True,
        "time_to_conflict_s": 9.0,
    }
    planner = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
    )
    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    assessment = guarded["recovery_guardrail_assessment"]
    assert assessment["assessment_status"] == "blocked"
    assert (
        "adjust_altitude_not_supported_for_local_route_conflict_without_"
        "altitude_clearance_evidence" in assessment["blocking_reasons"]
    )


def test_live_recovery_agent_timeout_falls_back_to_guarded_planner() -> None:
    result = live_run._runtime_recovery_agent_fallback_result(
        telemetry_snapshot=_planner_tool_telemetry(),
        task_id="task_timeout_fallback",
        reason="runtime_recovery_agent_timeout",
        detail="timeout_seconds=0.001",
    )

    assert result["runtime_status"] == "proposal_guardrail_passed"
    assert result["assessment"]["selected_bounded_action"] == "avoid_obstacle"
    assert result["assessment"]["proposed_parameters"]["target_altitude_m"] == 45.0
    assert result["agent_invocations"][0]["function_tool_called"] is True
    assert result["agent_invocations"][0]["invocation_kind"] == ("deterministic_guardrail_fallback")
    assert result["dispatch_authority_created"] is False
    assert result["progress_counted"] is False


def test_runtime_recovery_agent_timeout_uses_gemini_budget_and_bounded_cap(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        live_run.MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS_ENV,
        raising=False,
    )
    assert live_run._runtime_recovery_agent_timeout_seconds() == 45.0

    monkeypatch.setenv(
        live_run.MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS_ENV,
        "120",
    )
    assert live_run._runtime_recovery_agent_timeout_seconds() == 90.0


def test_fallback_proposal_origin_is_not_labeled_as_hosted_judgment() -> None:
    result = live_run._runtime_recovery_agent_fallback_result(
        telemetry_snapshot=_planner_tool_telemetry(),
        task_id="task_fallback_origin",
        reason="runtime_recovery_agent_timeout",
        detail="timeout_seconds=45",
    )
    origin = live_run._runtime_recovery_proposal_origin(
        result=result,
        proposal_recompiled=False,
        source_proposal={},
    )

    assert origin["origin_kind"] == "deterministic_guardrail_fallback"
    assert origin["invocation_kind"] == "deterministic_guardrail_fallback"
    assert origin["fallback_reason"] == "runtime_recovery_agent_timeout"
    assert origin["contains_prompt_or_response_text"] is False


def test_runtime_recovery_accepts_adk_skip_summarization_tool_judgment(
    monkeypatch,
) -> None:
    planner_result = {
        "recommended_candidate": {
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": {
                "target_x_m": -29.0,
                "target_y_m": 165.0,
                "target_altitude_m": 45.0,
            },
            "source_refs": ["obstacle.obstacle_manifest"],
            "rationale": "avoid the source-backed route obstacle",
        },
        "recovery_guardrail_assessment": {
            "assessment_status": "proposal_guardrail_passed",
            "trigger_level": "advisory",
            "observed_risk_reasons": ["obstacle_or_building_risk"],
        },
    }
    monkeypatch.setattr(
        missionos_agent_runtime,
        "_invoke_runtime_recovery_agent_text_with_tools",
        lambda **_kwargs: {
            "response_text": "",
            "response_source": "function_tool_result_skip_summarization",
            "function_calls": [
                {
                    "name": "missionos_plan_bounded_recovery_maneuver",
                    "args": {
                        "recovery_action": "avoid_obstacle",
                        "reason": "local route conflict",
                    },
                }
            ],
            "function_responses": [
                {
                    "name": "missionos_plan_bounded_recovery_maneuver",
                    "response_present": True,
                }
            ],
            "function_tool_called": True,
            "tool_arguments": [
                {
                    "recovery_action": "avoid_obstacle",
                    "reason": "local route conflict",
                }
            ],
            "function_tool_results": [planner_result],
        },
    )

    invocation = missionos_agent_runtime._run_runtime_recovery_agent_once(
        prompt_payload={"task": "judge recovery"},
        telemetry_snapshot=_planner_tool_telemetry(),
        mission_context={},
        recovery_policy={},
    )

    assert invocation["guardrail_result"]["guardrail_passed"] is True
    assert invocation["validated_output"]["selected_bounded_action"] == ("avoid_obstacle")
    assert invocation["validated_output_source"] == ("hosted_function_tool_call_and_guarded_result")
    assert len(invocation["function_calls_sha256"]) == 64
    assert len(invocation["function_tool_results_sha256"]) == 64
    origin = live_run._runtime_recovery_proposal_origin(
        result={"agent_invocations": [invocation]},
        proposal_recompiled=False,
        source_proposal={},
    )
    assert origin["origin_kind"] == "hosted_llm"
    assert origin["function_calls_sha256"] == invocation["function_calls_sha256"]
    assert origin["function_tool_results_sha256"] == (invocation["function_tool_results_sha256"])


def test_running_snapshot_preserves_operator_maneuver_observation() -> None:
    marker = {
        "sample_index": 9,
        "elapsed_seconds": 95.0,
        "monitor_effective_elapsed_seconds": 65.0,
        "monitor_excluded_recovery_seconds": 30.0,
        "operator_recovery_request_observed": True,
        "operator_recovery_proposal_id": "runtime_recovery_proposal_test",
        "operator_recovery_proposal_origin": {
            "origin_kind": "hosted_llm",
            "provider": "google_adk_gemini",
            "model_id": "gemini-3.1-flash-lite",
        },
        "operator_recovery_proposal_origin_sha256": "a" * 64,
        "operator_recovery_action": "avoid_obstacle",
        "operator_recovery_parameters": {
            "target_x_m": 150.0,
            "target_y_m": -25.0,
            "target_altitude_m": 45.0,
        },
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_command_ack_result": 0,
        "operator_recovery_path": "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle",
        "operator_recovery_target": {
            "assist_kind": "bounded_offboard_obstacle_avoidance_reroute",
            "target_x_m": 150.0,
            "target_y_m": -25.0,
            "target_z_m": -45.0,
        },
        "operator_recovery_assist_status": "target_reached",
        "operator_recovery_assist_kind": "bounded_offboard_obstacle_avoidance_reroute",
        "operator_recovery_assist_setpoint_frames_sent": 42,
        "operator_recovery_target_reached": True,
        "operator_recovery_target_distance_m": 2.5,
        "operator_recovery_target_altitude_m": 45.0,
        "operator_recovery_altitude_delta_m": 14.2,
        "operator_recovery_local_delta_x_m": 18.0,
        "operator_recovery_local_delta_y_m": -4.5,
        "operator_recovery_terminal": False,
        "operator_recovery_resume_auto_attempted": True,
        "operator_recovery_resume_auto_ack_observed": True,
        "operator_recovery_resume_auto_ack_result": 0,
        "operator_recovery_resume_auto_nav_state_observed": True,
        "operator_recovery_resume_auto_nav_state": 3,
        "operator_recovery_resume_auto_status": "resumed_auto_mission",
        "operator_recovery_resume_safety_verification": {
            "verification_status": "verified",
            "resume_auto_authorized": True,
        },
    }

    snapshot = auto_probe._build_running_snapshot(marker, waypoint_total=4)

    assert snapshot["operator_recovery_action"] == "avoid_obstacle"
    assert snapshot["operator_recovery_proposal_id"] == ("runtime_recovery_proposal_test")
    assert snapshot["operator_recovery_proposal_origin"]["provider"] == ("google_adk_gemini")
    assert snapshot["monitor_effective_elapsed_seconds"] == 65.0
    assert snapshot["monitor_excluded_recovery_seconds"] == 30.0
    assert snapshot["operator_recovery_path"] == "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle"
    assert snapshot["operator_recovery_target"]["target_z_m"] == -45.0
    assert snapshot["operator_recovery_assist_status"] == "target_reached"
    assert snapshot["operator_recovery_target_reached"] is True
    assert snapshot["operator_recovery_target_distance_m"] == 2.5
    assert snapshot["operator_recovery_altitude_delta_m"] == 14.2
    assert snapshot["operator_recovery_terminal"] is False
    assert snapshot["operator_recovery_resume_auto_status"] == "resumed_auto_mission"
    assert snapshot["operator_recovery_resume_auto_nav_state_observed"] is True
    assert snapshot["operator_recovery_resume_safety_verification"] == {
        "verification_status": "verified",
        "resume_auto_authorized": True,
    }

    lines = missionos_cli._recovery_runner_observation_lines(
        {"artifacts": {"missionos_auto_mission_runtime_snapshot": snapshot}}
    )
    assist_line = "\n".join(lines)
    assert "assist=target_reached" in assist_line
    assert "kind=bounded_offboard_obstacle_avoidance_reroute" in assist_line
    assert "target=True" in assist_line
    assert "resume=resumed_auto_mission" in assist_line


def test_runtime_probe_timeout_extends_only_by_observed_recovery_time(
    tmp_path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert auto_probe._running_snapshot_recovery_extension_seconds(run_dir) == 0.0

    (run_dir / "running_snapshot.json").write_text(
        json.dumps({"monitor_excluded_recovery_seconds": 125.5}),
        encoding="utf-8",
    )
    assert auto_probe._running_snapshot_recovery_extension_seconds(run_dir) == 125.5

    (run_dir / "running_snapshot.json").write_text(
        json.dumps({"monitor_excluded_recovery_seconds": -10}),
        encoding="utf-8",
    )
    assert auto_probe._running_snapshot_recovery_extension_seconds(run_dir) == 0.0


def test_gateway_timeout_extends_only_by_observed_recovery_time(tmp_path) -> None:
    artifact_root = tmp_path / "artifacts"
    snapshot_dir = artifact_root / "run"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "running_snapshot.json").write_text(
        json.dumps({"monitor_excluded_recovery_seconds": 80.25}),
        encoding="utf-8",
    )

    assert live_run._auto_running_snapshot_recovery_extension_seconds(artifact_root) == 80.25


def test_live_trajectory_breaks_on_unobserved_sample_gap() -> None:
    first = live_run._updated_auto_live_trajectory(
        existing=None,
        snapshot={
            "task_ref": "artifact://task/task_trajectory_segments",
            "sample_index": 1,
            "elapsed_seconds": 1.0,
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "observed_at": "2026-07-17T00:00:01+00:00",
        },
    )
    second = live_run._updated_auto_live_trajectory(
        existing=first,
        snapshot={
            "task_ref": "artifact://task/task_trajectory_segments",
            "sample_index": 2,
            "elapsed_seconds": 2.0,
            "local_x_m": 10.0,
            "local_y_m": 0.0,
            "observed_at": "2026-07-17T00:00:02+00:00",
        },
    )
    after_gap = live_run._updated_auto_live_trajectory(
        existing=second,
        snapshot={
            "task_ref": "artifact://task/task_trajectory_segments",
            "sample_index": 100000,
            "elapsed_seconds": 1000.0,
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "observed_at": "2026-07-17T00:16:40+00:00",
        },
    )

    assert after_gap["segment_count"] == 2
    assert [sample["segment_index"] for sample in after_gap["samples"]] == [0, 0, 1]
    assert after_gap["samples"][-1]["segment_break_reason"] == "sample_index_gap"


def test_operator_recovery_wait_returns_on_maneuver_assist_status() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, path: str) -> dict:
            self.calls += 1
            assert path == "/tasks/task_maneuver_wait"
            return {
                "task": {
                    "task_id": "task_maneuver_wait",
                    "status": "running",
                    "artifacts": {
                        "missionos_auto_mission_runtime_snapshot": {
                            "operator_recovery_request_observed": True,
                            "operator_recovery_command_ack_observed": True,
                            "operator_recovery_command_ack_result": 0,
                            "operator_recovery_assist_status": "target_reached",
                            "operator_recovery_target_reached": True,
                            "operator_recovery_resume_auto_status": "resumed_auto_mission",
                        }
                    },
                }
            }

    client = Client()
    observed = missionos_cli._wait_for_active_runner_recovery_observation(
        client,  # type: ignore[arg-type]
        {
            "summary": {
                "task_id": "task_maneuver_wait",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
            }
        },
        timeout_seconds=5.0,
        poll_interval=0.01,
    )

    assert observed is not None
    assert client.calls == 1


def test_operator_recovery_wait_ignores_stale_maneuver_assist_parameters() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, path: str) -> dict:
            self.calls += 1
            assert path == "/tasks/task_maneuver_wait"
            target_x = 12.0 if self.calls == 1 else 40.0
            return {
                "task": {
                    "task_id": "task_maneuver_wait",
                    "status": "running",
                    "artifacts": {
                        "missionos_auto_mission_runtime_snapshot": {
                            "operator_recovery_request_observed": True,
                            "operator_recovery_command_ack_observed": True,
                            "operator_recovery_command_ack_result": 0,
                            "operator_recovery_parameters": {
                                "target_x_m": target_x,
                                "target_y_m": 20.0,
                                "target_altitude_m": 45.0,
                            },
                            "operator_recovery_assist_status": "target_reached",
                            "operator_recovery_target_reached": True,
                        }
                    },
                }
            }

    client = Client()
    observed = missionos_cli._wait_for_active_runner_recovery_observation(
        client,  # type: ignore[arg-type]
        {
            "summary": {
                "task_id": "task_maneuver_wait",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
                "recovery_parameters": {
                    "target_x_m": 40.0,
                    "target_y_m": 20.0,
                    "target_altitude_m": 45.0,
                },
            }
        },
        timeout_seconds=5.0,
        poll_interval=0.01,
    )

    assert observed is not None
    assert client.calls == 2


def test_operator_dispatch_summary_keeps_receipt_action_over_terminal_return_snapshot() -> None:
    line = missionos_cli._operator_recovery_dispatch_status_text(
        artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "dispatch_status": "queued_for_active_runner",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
                "recovery_parameters": {
                    "target_x_m": 40.0,
                    "target_y_m": 20.0,
                    "target_altitude_m": 45.0,
                },
            }
        },
        snapshot={
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "return_to_launch",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_command_ack_result": 0,
            "post_abort_tracking": True,
            "post_abort_outcome_status": "return_progress_observed",
        },
    )

    assert line is not None
    assert "action=avoid_obstacle" in line
    assert "target_altitude_m=45.0" in line
    assert "outcome=return_progress_observed" in line


def test_operator_dispatch_summary_surfaces_probe_maneuver_evidence_after_return() -> None:
    line = missionos_cli._operator_recovery_dispatch_status_text(
        artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "dispatch_status": "queued_for_active_runner",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
                "recovery_parameters": {
                    "target_x_m": 99.929,
                    "target_y_m": 72.863,
                    "target_altitude_m": 45.0,
                },
            },
            "missionos_auto_mission_probe_observed": {
                "monitor": {
                    "terminal_snapshot": {
                        "operator_recovery_action": "avoid_obstacle",
                        "operator_recovery_assist_status": "target_reached",
                        "operator_recovery_target_reached": True,
                        "operator_recovery_resume_auto_status": "resumed_auto_mission",
                    }
                }
            },
        },
        snapshot={
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "return_to_launch",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_command_ack_result": 0,
            "post_abort_tracking": True,
            "post_abort_outcome_status": "return_progress_observed",
        },
    )

    assert line is not None
    assert "action=avoid_obstacle" in line
    assert "maneuver=avoid_obstacle" in line
    assert "assist=target_reached" in line
    assert "target=True" in line
    assert "resume=resumed_auto_mission" in line


def test_operator_maneuver_window_handles_stale_approved_setpoints() -> None:
    assert auto_probe.OPERATOR_RECOVERY_ASSIST_MAX_SECONDS >= 30.0


def _obstacle_recovery_map_payload() -> dict:
    return {
        "task_id": "task_obstacle_map_layers",
        "status": "running",
        "artifacts": {
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.681236,
                "takeoff_longitude": 139.767125,
                "dropoff_latitude": 35.6979189,
                "dropoff_longitude": 139.7754511,
                "landing_zone_blocked": True,
            },
            "missionos_auto_mission_compilation": {
                "planned_route_m": 2001.0,
                "mission_items": [
                    {
                        "seq": 0,
                        "command": 22,
                        "latitude_deg": 35.681236,
                        "longitude_deg": 139.767125,
                        "altitude_m": 30.0,
                    },
                    {
                        "seq": 1,
                        "command": 16,
                        "latitude_deg": 35.6895,
                        "longitude_deg": 139.7710,
                        "altitude_m": 30.0,
                    },
                    {
                        "seq": 2,
                        "command": 19,
                        "latitude_deg": 35.6979189,
                        "longitude_deg": 139.7754511,
                        "altitude_m": 30.0,
                    },
                    {
                        "seq": 3,
                        "command": 21,
                        "latitude_deg": 35.6979189,
                        "longitude_deg": 139.7754511,
                        "altitude_m": 0.0,
                    },
                ],
            },
            "missionos_auto_mission_runtime_snapshot": {
                "local_x_m": 80.0,
                "local_y_m": 40.0,
                "altitude_above_home_m": 45.0,
                "operator_recovery_action": "avoid_obstacle",
                "operator_recovery_target_reached": True,
                "operator_recovery_resume_auto_status": "resumed_auto_mission",
            },
            "missionos_auto_mission_runtime_replay": {
                "flight_path_profile": [
                    {
                        "sample_index": 0,
                        "phase": "prepared",
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "relative_alt_m": 0.0,
                    },
                    {
                        "sample_index": 1,
                        "phase": "operator_recovery",
                        "local_x_m": 40.0,
                        "local_y_m": 20.0,
                        "relative_alt_m": 45.0,
                    },
                    {
                        "sample_index": 2,
                        "phase": "auto_mission_resumed",
                        "local_x_m": 80.0,
                        "local_y_m": 40.0,
                        "relative_alt_m": 45.0,
                    },
                ],
            },
            "missionos_auto_mission_probe_observed": {
                "gazebo_obstacle_application": {
                    "gazebo_obstacle_model_spawned": True,
                    "obstacle_manifest": {
                        "gazebo_obstacle_model_spawned": True,
                        "obstacles": [
                            {
                                "name": "missionos_landing_zone_blocker",
                                "kind": "building_box",
                                "source": "landing_zone_blocked",
                                "x_m": 1855.054,
                                "y_m": 752.02,
                                "z_m": 10.0,
                                "size_x_m": 18.0,
                                "size_y_m": 18.0,
                                "size_z_m": 20.0,
                            }
                        ],
                    },
                },
                "monitor": {
                    "operator_recovery": {
                        "command": {
                            "status": "target_reached",
                            "recovery_path": "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle",
                            "target": {
                                "target_x_m": 40.0,
                                "target_y_m": 20.0,
                                "target_z_m": -45.0,
                            },
                            "target_reached": True,
                            "target_distance_m": 0.7,
                            "resume_auto_status": "resumed_auto_mission",
                            "maneuver_observation_samples": [
                                {
                                    "x_m": 30.0,
                                    "y_m": 15.0,
                                    "altitude_above_home_m": 35.0,
                                    "distance_to_target_m": 11.0,
                                },
                                {
                                    "x_m": 40.0,
                                    "y_m": 20.0,
                                    "altitude_above_home_m": 45.0,
                                    "distance_to_target_m": 0.7,
                                },
                            ],
                        }
                    }
                },
            },
        },
    }


def test_mission_map_model_separates_plan_observed_avoidance_and_obstacles() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_obstacle_recovery_map_payload(),
        provider="osm",
        live_task_url=None,
    )

    assert len(model["planned_points"]) == 4
    assert len(model["observed_points"]) == 3
    assert model["latest"]["phase"] == "auto_mission_resumed"
    assert model["obstacles"][0]["name"] == "missionos_landing_zone_blocker"
    assert model["obstacles"][0]["spawned"] is True
    assert model["avoidance"]["action"] == "avoid_obstacle"
    assert model["avoidance"]["target"]["x_m"] == 40.0
    assert len(model["avoidance"]["samples"]) == 2
    assert model["avoidance"]["target_reached"] is True
    assert model["avoidance"]["resume_auto_status"] == "resumed_auto_mission"


def test_mission_map_preserves_two_observed_recovery_attempts() -> None:
    payload = json.loads(json.dumps(_obstacle_recovery_map_payload()))
    monitor = payload["artifacts"]["missionos_auto_mission_probe_observed"]["monitor"]

    def attempt(*, proposal_id: str, obstacle_name: str, x_m: float, y_m: float) -> dict:
        return {
            "request": {
                "proposal_id": proposal_id,
                "recovery_action": "avoid_obstacle",
                "recovery_parameters": {
                    "source_obstacle_name": obstacle_name,
                    "target_x_m": x_m,
                    "target_y_m": y_m,
                    "target_altitude_m": 45.0,
                },
            },
            "command": {
                "status": "target_reached",
                "recovery_path": "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle",
                "target": {
                    "target_x_m": x_m,
                    "target_y_m": y_m,
                    "target_z_m": -45.0,
                },
                "target_reached": True,
                "resume_auto_status": "resumed_auto_mission",
                "maneuver_observation_samples": [
                    {"x_m": x_m - 12.0, "y_m": y_m - 18.0},
                    {"x_m": x_m, "y_m": y_m},
                ],
            },
        }

    monitor["operator_recovery_attempts"] = [
        attempt(
            proposal_id="runtime_recovery_proposal_50",
            obstacle_name="missionos_route_obstacle_50pct",
            x_m=100.0,
            y_m=45.0,
        ),
        attempt(
            proposal_id="runtime_recovery_proposal_75",
            obstacle_name="missionos_route_obstacle_75pct",
            x_m=150.0,
            y_m=55.0,
        ),
    ]
    manifest = payload["artifacts"]["missionos_auto_mission_probe_observed"][
        "gazebo_obstacle_application"
    ]["obstacle_manifest"]
    manifest["obstacles"] = [
        {
            "name": "missionos_route_obstacle_50pct",
            "x_m": 92.0,
            "y_m": 38.0,
            "z_m": 10.0,
            "size_x_m": 18.0,
            "size_y_m": 18.0,
            "size_z_m": 20.0,
        },
        {
            "name": "missionos_route_obstacle_75pct",
            "x_m": 142.0,
            "y_m": 48.0,
            "z_m": 10.0,
            "size_x_m": 18.0,
            "size_y_m": 18.0,
            "size_z_m": 20.0,
        },
    ]

    model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )
    html = missionos_cli._mission_map_html(model)

    assert len(model["avoidances"]) == 2
    assert [item["proposal_id"] for item in model["avoidances"]] == [
        "runtime_recovery_proposal_50",
        "runtime_recovery_proposal_75",
    ]
    assert model["avoidance"] == model["avoidances"][-1]
    assert "recoveryLabel" in html
    assert "avoidancePointSets" in html


def test_mission_map_html_and_watch_surface_obstacle_layers() -> None:
    payload = _obstacle_recovery_map_payload()
    artifacts = payload["artifacts"]
    model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )
    html = missionos_cli._mission_map_html(model)

    assert "planned-path" in html
    assert "observed-path" in html
    assert "avoidance-path" in html
    assert "marker-obstacle" in html
    assert "planned route" in html
    assert "outbound →" in html
    assert "Recovery bypass →" in html
    assert "collision footprint" in html
    assert "Old target" in html

    console = Console(record=True, color_system=None, width=120)
    console.print(
        missionos_cli._render_flight_map(
            trail=[(0.0, 0.0), (40.0, 20.0), (80.0, 40.0)],
            snapshot=artifacts["missionos_auto_mission_runtime_snapshot"],
            artifacts=artifacts,
            status="running",
            task_id="task_obstacle_map_layers",
        )
    )
    rendered = console.export_text()
    assert "p=initial plan" in rendered
    assert "O=obstacle" in rendered
    assert "avoid=target_reached" in rendered
    assert "obstacles=1(spawned)" in rendered
