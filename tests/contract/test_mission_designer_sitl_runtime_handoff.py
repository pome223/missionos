from __future__ import annotations

import subprocess
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime import px4_gazebo_mission_designer_sitl_live_flight_run as live
from src.runtime.px4_gazebo_sitl_mission_upload import (
    PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV,
    PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV,
    PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV,
)


def _task(*, startup_container: str = "upload-sitl") -> dict[str, Any]:
    return {
        "artifacts": {
            "px4_gazebo_mission_designer_sitl_startup": {
                "container_name": startup_container,
            },
            "px4_gazebo_sitl_mission_upload_receipt": {
                "receipt_id": "receipt-1",
                "upload_status": "uploaded",
                "mission_ack_observed": True,
                "mission_ack_type": 0,
            },
        }
    }


def _enable_handoff(monkeypatch: pytest.MonkeyPatch, container: str) -> None:
    monkeypatch.setenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV, "1")
    monkeypatch.setenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV, "1")
    monkeypatch.setenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV, container)


def test_handoff_stops_only_the_task_bound_upload_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_handoff(monkeypatch, "upload-sitl")
    calls: list[dict[str, Any]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout="upload-sitl\n", stderr="")

    handoff = live.stop_gateway_upload_runtime_before_live_flight(
        task=_task(), runner=runner
    )

    assert calls[0]["command"] == ["docker", "rm", "-f", "upload-sitl"]
    assert handoff["handoff_status"] == "upload_runtime_stopped"
    assert handoff["upload_runtime_stopped"] is True
    assert handoff["approval_created"] is False
    assert handoff["dispatch_authority_created"] is False


def test_handoff_fails_closed_on_container_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_handoff(monkeypatch, "different-container")

    with pytest.raises(
        live.PX4GazeboMissionDesignerSITLLiveFlightRunError,
        match="container_binding_mismatch",
    ):
        live.stop_gateway_upload_runtime_before_live_flight(
            task=_task(),
            runner=lambda *_args, **_kwargs: pytest.fail("docker must not run"),
        )


def test_handoff_is_noop_without_reusable_gateway_uploader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV, raising=False)
    monkeypatch.delenv(
        PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV, raising=False
    )

    handoff = live.stop_gateway_upload_runtime_before_live_flight(
        task=_task(),
        runner=lambda *_args, **_kwargs: pytest.fail("docker must not run"),
    )

    assert handoff["handoff_status"] == "not_required"
    assert handoff["upload_runtime_stopped"] is False


def test_generic_scenario_does_not_load_generated_digital_twin_world() -> None:
    env = live._mission_designer_terrain_world_env(
        {
            "artifacts": {
                "mission_scenario_designer_summary": {
                    "coordinate_pair_route_mode": False,
                },
                "gazebo_world_artifact": {
                    "world_file_path_or_artifact_uri": "unused-heavy-world.sdf",
                    "world_file_sha256": "sha256",
                },
            }
        }
    )

    assert env == {}


def test_explicit_coordinate_scenario_can_load_source_bound_terrain_world(
    tmp_path: Any,
) -> None:
    world = tmp_path / "coordinate-world.sdf"
    world.write_text("<sdf version='1.9'/>")

    env = live._mission_designer_terrain_world_env(
        {
            "artifacts": {
                "mission_scenario_designer_summary": {
                    "coordinate_pair_route_mode": True,
                },
                "gazebo_world_artifact": {
                    "world_artifact_id": "world-1",
                    "world_file_path_or_artifact_uri": str(world),
                    "world_file_sha256": "sha256",
                },
            }
        }
    )

    assert env[live.MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SDF_ENV] == str(world)


def test_mission_assurance_route_keeps_delivery_payload() -> None:
    env: dict[str, str] = {}

    live.configure_horizontal_route_payload_model(
        env,
        mission_assurance_on_deviation=True,
    )

    assert (
        env[live.MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL_ENV]
        == "1"
    )


def test_nominal_delivery_route_keeps_payload_model() -> None:
    env: dict[str, str] = {}

    live.configure_horizontal_route_payload_model(
        env,
        mission_assurance_on_deviation=False,
    )

    assert (
        env[live.MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL_ENV]
        == "1"
    )


def test_mission_assurance_defers_wind_until_airborne() -> None:
    env: dict[str, str] = {}

    live.configure_horizontal_route_wind_activation(
        env,
        mission_assurance_on_deviation=True,
    )

    assert (
        env[live.MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_DEFER_WIND_UNTIL_AIRBORNE_ENV]
        == "1"
    )


def test_nominal_delivery_does_not_defer_wind() -> None:
    env = {
        live.MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_DEFER_WIND_UNTIL_AIRBORNE_ENV: "1"
    }

    live.configure_horizontal_route_wind_activation(
        env,
        mission_assurance_on_deviation=False,
    )

    assert (
        live.MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_DEFER_WIND_UNTIL_AIRBORNE_ENV
        not in env
    )


def test_deferred_wind_starts_world_calm_and_keeps_requested_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(route_entrypoint.DEFER_WIND_UNTIL_AIRBORNE_ENV, "1")
    monkeypatch.setenv("MISSION_DESIGNER_REALISM_WIND_MEAN_MPS", "4.0")
    monkeypatch.setenv("MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG", "90")
    monkeypatch.setattr(route_entrypoint, "WIND_ACTIVATION_ALLOWED", False)

    assert route_entrypoint._initial_world_wind_vector(
        requested_x=4.0,
        requested_y=0.0,
    ) == (0.0, 0.0)
    pending = route_entrypoint._apply_wind_realism(None)

    assert pending["environment_condition_profile"]["requested"]["wind_mean_mps"] == 4.0
    assert (
        pending["simulator_condition_application"]["application_status"]
        == "pending_airborne_activation"
    )
    assert (
        pending["observed_environment_evidence"]["observation_status"]
        == "pending_airborne_activation"
    )


def test_mission_assurance_live_observation_accepts_fresh_approval_wait() -> None:
    observation = live.build_mission_assurance_px4_live_flight_observation(
        task_id="task-1",
        horizontal_summary={
            "mission_designer_task_id": "task-1",
            "mission_designer_live_sitl_run_id": "run-1",
            "same_gateway_execution_run_observed": True,
            "actual_px4_gazebo_horizontal_smoke_observed": True,
            "decision_loop_driver": "runtime_recovery_agent_then_mission_assurance_agent",
            "runtime_recovery_agent_invoked": True,
            "mission_assurance_agent_invoked": True,
            "mission_assurance_live_guard": {
                "guard_status": "awaiting_operator_approval",
                "mission_assurance_response_kind": "return",
                "selected_recovery_action": None,
                "dispatch_request_sent": False,
                "operator_recovery_approval_request": {
                    "request_status": "awaiting_operator_approval",
                    "recovery_action": "rtl",
                    "requires_new_human_approval": True,
                },
                "runtime_recovery_agent_invoked": True,
                "recovery_agent_invoked_before_mission_assurance": True,
            },
            "deviation_samples": [{"sample": {"z": 1.5}}],
            "recovery_command_ack_observed": False,
            "recovery_state_observed": False,
            "recovery_state_label": None,
            "final_status": "aborted_pose_deviation",
            "task_status": "blocked",
            "dropoff_region_reached": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    )

    assert observation["actual_sitl_flight_evidence_observed"] is True
    assert observation["disposition"] == "return_awaiting_operator_approval"
    assert observation["recovery_state_observed"] is False
    assert observation["recovery_command_ack_observed"] is False
    assert observation["delivery_completion_claimed"] is False


def test_mission_assurance_live_observation_accepts_hold_without_rtl() -> None:
    observation = live.build_mission_assurance_px4_live_flight_observation(
        task_id="task-1",
        horizontal_summary={
            "mission_designer_task_id": "task-1",
            "mission_designer_live_sitl_run_id": "run-1",
            "same_gateway_execution_run_observed": True,
            "actual_px4_gazebo_horizontal_smoke_observed": True,
            "decision_loop_driver": "runtime_recovery_agent_then_mission_assurance_agent",
            "runtime_recovery_agent_invoked": True,
            "mission_assurance_agent_invoked": True,
            "mission_assurance_live_guard": {
                "guard_status": "blocked",
                "mission_assurance_response_kind": "hold",
                "selected_recovery_action": None,
                "dispatch_prevented_by_mission_assurance": True,
                "runtime_recovery_agent_invoked": True,
                "recovery_agent_invoked_before_mission_assurance": True,
            },
            "deviation_samples": [{"sample": {"z": 1.5}}],
            "recovery_command_ack_observed": False,
            "recovery_state_observed": False,
            "recovery_state_label": None,
            "final_status": "aborted_pose_deviation",
            "task_status": "blocked",
            "dropoff_region_reached": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    )

    assert observation["disposition"] == "hold_prevented_recovery_dispatch"
    assert observation["selected_recovery_action"] is None
    assert observation["recovery_state_observed"] is False
    assert observation["recovery_command_ack_observed"] is False


def test_mission_assurance_live_observation_escalates_reverse_disagreement() -> None:
    observation = live.build_mission_assurance_px4_live_flight_observation(
        task_id="task-1",
        horizontal_summary={
            "mission_designer_task_id": "task-1",
            "mission_designer_live_sitl_run_id": "run-1",
            "same_gateway_execution_run_observed": True,
            "actual_px4_gazebo_horizontal_smoke_observed": True,
            "decision_loop_driver": (
                "runtime_recovery_agent_then_mission_assurance_agent"
            ),
            "runtime_recovery_agent_invoked": True,
            "mission_assurance_agent_invoked": True,
            "mission_assurance_live_guard": {
                "guard_status": "operator_escalation",
                "mission_assurance_response_kind": "return",
                "selected_recovery_action": None,
                "dispatch_request_sent": False,
                "agent_disagreement_observed": True,
                "agent_disagreement_resolution": "operator_escalation",
                "runtime_recovery_agent_invoked": True,
                "recovery_agent_invoked_before_mission_assurance": True,
                "runtime_recovery_agent_proposal": {
                    "selected_bounded_action": "continue"
                },
            },
            "deviation_samples": [{"sample": {"z": 1.5}}],
            "recovery_command_ack_observed": False,
            "recovery_state_observed": False,
            "recovery_state_label": None,
            "final_status": "aborted_pose_deviation",
            "task_status": "blocked",
            "dropoff_region_reached": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    )

    assert observation["disposition"] == (
        "agent_disagreement_operator_escalation"
    )
    assert observation["selected_recovery_action"] is None
    assert observation["recovery_state_observed"] is False
    assert observation["recovery_command_ack_observed"] is False


def test_mission_assurance_live_observation_accepts_completed_continue() -> None:
    observation = live.build_mission_assurance_px4_live_flight_observation(
        task_id="task-1",
        horizontal_summary={
            "mission_designer_task_id": "task-1",
            "mission_designer_live_sitl_run_id": "run-1",
            "same_gateway_execution_run_observed": True,
            "actual_px4_gazebo_horizontal_smoke_observed": True,
            "decision_loop_driver": "runtime_recovery_agent_then_mission_assurance_agent",
            "runtime_recovery_agent_invoked": True,
            "mission_assurance_agent_invoked": True,
            "mission_assurance_live_guard": {
                "guard_status": "no_dispatch",
                "mission_assurance_response_kind": "continue",
                "selected_recovery_action": None,
                "runtime_recovery_agent_invoked": True,
                "recovery_agent_invoked_before_mission_assurance": True,
                "runtime_recovery_agent_proposal": {
                    "selected_bounded_action": "continue"
                },
            },
            "deviation_samples": [{"sample": {"z": 1.5}}],
            "recovery_command_ack_observed": False,
            "recovery_state_observed": False,
            "recovery_state_label": None,
            "mission_assurance_continue_execution_invoked": True,
            "mission_assurance_continue_effect_observed": True,
            "mission_assurance_continue_route_completion_observed": True,
            "mission_assurance_continue_dropoff_approach_observed": True,
            "mission_assurance_continue_execution": {
                "existing_route_approval_consumed": True,
                "simulator_route_resume_invoked": True,
                "offboard_mode_switch_ack_observed": True,
                "offboard_mode_switch_ack_result_code": 0,
                "setpoint_frames_sent": 50,
                "route_resume_effect_observed": True,
            },
            "final_status": "completed",
            "task_status": "completed",
            "dropoff_region_reached": True,
            "payload_release_observed": True,
            "delivery_completion_claimed": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    )

    assert observation["disposition"] == "continue_completed_route_delivery"
    assert observation["selected_recovery_action"] is None
    assert observation["recovery_state_observed"] is False
    assert observation["mission_assurance_continue_execution_invoked"] is True
    assert observation["mission_assurance_continue_effect_observed"] is True
    assert observation[
        "mission_assurance_continue_route_completion_observed"
    ] is True
    assert observation[
        "mission_assurance_continue_dropoff_approach_observed"
    ] is True
    assert observation["dropoff_region_reached"] is True
    assert observation["payload_release_observed"] is True
    assert observation["delivery_completion_claimed"] is True


def test_mission_assurance_live_observation_rejects_delivery_claim() -> None:
    with pytest.raises(
        live.PX4GazeboMissionDesignerSITLLiveFlightRunError,
        match="must_not_claim_delivery",
    ):
        live.build_mission_assurance_px4_live_flight_observation(
            task_id="task-1",
            horizontal_summary={
                "mission_designer_task_id": "task-1",
                "same_gateway_execution_run_observed": True,
                "actual_px4_gazebo_horizontal_smoke_observed": True,
                "decision_loop_driver": "runtime_recovery_agent_then_mission_assurance_agent",
                "runtime_recovery_agent_invoked": True,
                "mission_assurance_agent_invoked": True,
                "mission_assurance_live_guard": {
                    "guard_status": "dispatch_eligible",
                    "selected_recovery_action": "rtl",
                    "runtime_recovery_agent_invoked": True,
                    "recovery_agent_invoked_before_mission_assurance": True,
                },
                "deviation_samples": [{"sample": {"z": 1.5}}],
                "recovery_state_observed": True,
                "task_status": "completed",
                "dropoff_region_reached": True,
                "delivery_completion_claimed": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
        )
