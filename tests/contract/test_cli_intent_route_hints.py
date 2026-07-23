import copy
import hashlib
from pathlib import Path
import time
from typing import Any

import click
from click.testing import CliRunner
import pytest
from rich.console import Console

import missionos_cli.cli as missionos_cli


class RecordingMissionOSClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.recovery_proposals: list[dict[str, Any]] = []

    def conversation(
        self,
        instruction: str,
        *,
        session_id: str,
        mission_designer_context: dict[str, Any] | None = None,
        coordinate_route: dict[str, Any] | None = None,
        route_hint: str | None = None,
        client_surface: str | None = None,
        robot_profile: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "operator_instruction": instruction,
            "session_id": session_id,
        }
        if mission_designer_context:
            request["mission_designer_context"] = mission_designer_context
        if coordinate_route:
            request["coordinate_route"] = coordinate_route
        if route_hint:
            request["missionos_route_hint"] = route_hint
        if client_surface:
            request["missionos_client_surface"] = client_surface
        if robot_profile:
            request["robot_profile"] = robot_profile
        self.requests.append(request)

        mission_designer: dict[str, Any] = {
            "mission_designer_context_ref": "mission_designer_context:test",
            "mission_designer_context_sha256": "test-sha",
            "mission_designer_context_session_id": session_id,
            "summary": {},
        }
        if route_hint == "execute":
            mission_designer["summary"]["sitl_execution_task_id"] = "task_execute_prepare"

        return {
            "schema_version": "missionos_autonomy_conversation_response.v1",
            "message": "handled",
            "routed_action": route_hint or "plan",
            "routing_source": "test",
            "progress_counted": False,
            "mission_designer": mission_designer,
        }

    def recovery_dispatch(
        self,
        *,
        task_id: str,
        recovery_action: str,
        recovery_parameters: dict[str, Any] | None = None,
        expected_recovery_checkpoint_id: str = "",
        expected_recovery_checkpoint_hash: str = "",
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "recovery_action": recovery_action,
            "recovery_parameters": recovery_parameters or {},
        }
        if expected_recovery_checkpoint_id:
            request["expected_recovery_checkpoint_id"] = (
                expected_recovery_checkpoint_id
            )
        if expected_recovery_checkpoint_hash:
            request["expected_recovery_checkpoint_hash"] = (
                expected_recovery_checkpoint_hash
            )
        self.requests.append(request)
        return {
            "summary": {
                "task_id": task_id,
                "recovery_action": recovery_action,
                "active_runner_request_queued": False,
                "blocked_reasons": [],
            },
            "missionos_runtime_recovery_dispatch_receipt": {
                "task_id": task_id,
                "recovery_action": recovery_action,
                "dispatch_status": "accepted",
                "recovery_parameters": recovery_parameters or {},
            },
        }

    def recovery_agent_propose_for_task(
        self,
        *,
        task_id: str,
        operator_instruction: str,
        requested_action: str,
        requested_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "operator_instruction": operator_instruction,
            "requested_action": requested_action,
            "requested_parameters": requested_parameters or {},
        }
        self.recovery_proposals.append(request)
        if requested_action == "adjust_altitude":
            params = {"target_altitude_m": 45.0}
        elif requested_action == "avoid_obstacle":
            params = {
                "target_x_m": 30.0,
                "target_y_m": 30.0,
                "target_altitude_m": 45.0,
            }
        else:
            params = {"target_x_m": 80.0, "target_y_m": 30.0}
        return {
            "schema_version": "missionos_runtime_recovery_operator_request_proposal.v1",
            "task_id": task_id,
            "proposal_status": "computed",
            "selected_bounded_action": requested_action,
            "proposed_parameters": params,
            "dispatch_authority_created": False,
            "operator_approval_required": True,
            "physical_execution_invoked": False,
            "progress_counted": False,
            "summary": {
                "task_id": task_id,
                "proposal_status": "computed",
                "selected_bounded_action": requested_action,
                "proposed_parameters": params,
            },
        }

    def get(self, path: str) -> dict[str, Any]:
        if path.startswith("/tasks?page="):
            return {
                "items": [
                    {
                        "task_id": "task_chat_avoid",
                        "status": "running",
                        "artifacts": {
                            "px4_gazebo_mission_designer_sitl_execution_request": {},
                            "missionos_auto_mission_gui_dispatch_running_receipt": {
                                "operator_recovery_request_container_path": "/tmp/request.json"
                            },
                        },
                    }
                ]
            }
        return {"task": {"task_id": "task_chat_avoid", "status": "running", "artifacts": {}}}


def _reviewed_recovery_checkpoint_fixture() -> dict[str, Any]:
    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_status": "awaiting_operator_approval",
        "recovery_proposal_id": "recovery_proposal_reviewed",
        "recovery_classification_id": "recovery_classification_reviewed",
        "selected_action": "avoid_obstacle",
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
        "approved_parameters": {
            "target_x_m": -0.2,
            "target_y_m": -1.4,
            "obstacle_avoidance_required": True,
        },
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    checkpoint_hash = missionos_cli._turtlebot3_recovery_checkpoint_content_hash(
        checkpoint
    )
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    return checkpoint


REVIEWED_RECOVERY_CHECKPOINT = _reviewed_recovery_checkpoint_fixture()
REVIEWED_RECOVERY_CHECKPOINT_ID = str(
    REVIEWED_RECOVERY_CHECKPOINT["checkpoint_id"]
)
REVIEWED_RECOVERY_CHECKPOINT_HASH = str(
    REVIEWED_RECOVERY_CHECKPOINT["checkpoint_hash"]
)


class PendingRecoveryApprovalClient(RecordingMissionOSClient):
    def __init__(self) -> None:
        super().__init__()
        self.task_id = "task_pending_recovery"

    def _task_payload(self) -> dict[str, Any]:
        recovery_proposal = {
            "proposal_id": "recovery_proposal_reviewed",
            "proposal_source": "llm",
            "selected_action": "avoid_obstacle",
            "reason": "Use the source-backed avoidance waypoint.",
            "llm_invocation_evidence": {
                "provider": "google_adk_gemini",
                "model_id": "gemini-test",
            },
            "input_observations": {
                "runtime_obstacle_observed": True,
                "recommended_avoidance_target_x_m": -0.2,
                "recommended_avoidance_target_y_m": -1.4,
            },
        }
        recovery_classification = {
            "classification_id": "recovery_classification_reviewed",
            "execution_class": "requires_human_approval",
            "requires_new_human_approval": True,
            "execution_permitted_by_envelope": False,
            "proposal_allowed": True,
        }
        decision_summary = {
            "schema_version": "missionos_turtlebot3_recovery_decision_summary.v1",
            "read_only": True,
            "judgment_required": True,
            "selected_action": "avoid_obstacle",
            "recovery_proposal_source": "llm",
            "rules_execution_class": "requires_human_approval",
            "requires_new_human_approval": True,
            "recovery_dispatch_request_sent": False,
            "decision_summary_creates_dispatch_authority": False,
            "physical_execution_invoked": False,
        }
        summary = {
            "robot_profile": "turtlebot3",
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "runtime_recovery_triggered": True,
            "runtime_recovery_action_kind": "avoid_obstacle",
            "recovery_dispatch_request_sent": False,
            "recovery_proposals": [recovery_proposal],
            "recovery_proposal_classifications": [recovery_classification],
        }
        checkpoint = copy.deepcopy(REVIEWED_RECOVERY_CHECKPOINT)
        summary["turtlebot3_recovery_checkpoint"] = checkpoint
        artifacts = {
            "turtlebot3_home_mission_plan": {
                "robot_profile": "turtlebot3",
                "execution_target": "ros2_nav2_turtlebot3_sim",
            },
            "summary": summary,
            "turtlebot3_recovery_decision_summary": decision_summary,
            "turtlebot3_recovery_checkpoint": checkpoint,
        }
        return {
            "task": {
                "task_id": self.task_id,
                "kind": "turtlebot3_home_mission_execution",
                "status": "pending",
                "artifacts": artifacts,
            }
        }

    def get(self, path: str) -> dict[str, Any]:
        if path.startswith("/tasks?page="):
            task = self._task_payload()["task"]
            return {"items": [task]}
        if path.startswith(f"/tasks/{self.task_id}"):
            return self._task_payload()
        return super().get(path)


class AskHumanPendingRecoveryClient(PendingRecoveryApprovalClient):
    def _task_payload(self) -> dict[str, Any]:
        payload = super()._task_payload()
        artifacts = payload["task"]["artifacts"]
        checkpoint = copy.deepcopy(artifacts["turtlebot3_recovery_checkpoint"])
        checkpoint.update(
            {
                "selected_action": "ask_human",
                "approved_parameters": {},
                "operator_guidance_required": True,
            }
        )
        checkpoint_hash = missionos_cli._turtlebot3_recovery_checkpoint_content_hash(
            checkpoint
        )
        checkpoint["checkpoint_hash"] = checkpoint_hash
        checkpoint["checkpoint_id"] = (
            f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
        )
        artifacts["turtlebot3_recovery_checkpoint"] = checkpoint
        summary = artifacts["summary"]
        summary["turtlebot3_recovery_checkpoint"] = copy.deepcopy(checkpoint)
        proposal = summary["recovery_proposals"][0]
        proposal["selected_action"] = "ask_human"
        proposal["reason"] = "Request bounded operator guidance after re-observation."
        artifacts["turtlebot3_recovery_decision_summary"][
            "selected_action"
        ] = "ask_human"
        return payload


class CheckpointRevisionClient(PendingRecoveryApprovalClient):
    def __init__(self, *, revision_mode: str = "success") -> None:
        super().__init__()
        self.revision_mode = revision_mode
        self.revision_requests: list[dict[str, Any]] = []
        self.generic_proposal_calls = 0
        self.stored_task_payload = PendingRecoveryApprovalClient._task_payload(self)

    def _task_payload(self) -> dict[str, Any]:
        return copy.deepcopy(self.stored_task_payload)

    def recovery_agent_propose_for_task(
        self,
        *,
        task_id: str,
        operator_instruction: str,
        requested_action: str,
        requested_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del task_id, operator_instruction, requested_action, requested_parameters
        self.generic_proposal_calls += 1
        raise AssertionError("generic PX4 recovery proposal route must not be used")

    def turtlebot3_recovery_revision(
        self,
        *,
        task_id: str,
        operator_instruction: str,
        expected_recovery_checkpoint_id: str,
        expected_recovery_checkpoint_hash: str,
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "operator_instruction": operator_instruction,
            "expected_recovery_checkpoint_id": expected_recovery_checkpoint_id,
            "expected_recovery_checkpoint_hash": expected_recovery_checkpoint_hash,
        }
        self.revision_requests.append(request)
        old_checkpoint = self.stored_task_payload["task"]["artifacts"][
            "turtlebot3_recovery_checkpoint"
        ]
        old_id = str(old_checkpoint["checkpoint_id"])
        old_hash = str(old_checkpoint["checkpoint_hash"])
        if self.revision_mode in {"unsupported_altitude", "stale"}:
            blocked_reason = (
                "unsupported_for_ground_robot:adjust_altitude"
                if self.revision_mode == "unsupported_altitude"
                else "reviewed_turtlebot3_recovery_checkpoint_hash_changed"
            )
            return {
                "schema_version": "missionos_turtlebot3_recovery_revision_response.v1",
                "task": self._task_payload()["task"],
                "summary": {
                    "task_id": task_id,
                    "revision_status": "blocked",
                    "previous_checkpoint_id": old_id,
                    "previous_checkpoint_hash": old_hash,
                    "current_checkpoint_id": old_id,
                    "current_checkpoint_hash": old_hash,
                    "blocked_reasons": [blocked_reason],
                    "dispatch_authority_created": False,
                    "operator_approval_created": False,
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                },
            }
        if self.revision_mode == "malformed_success":
            return {
                "schema_version": "missionos_turtlebot3_recovery_revision_response.v1",
                "task": self._task_payload()["task"],
                "summary": {
                    "task_id": task_id,
                    "revision_status": "revised",
                    "previous_checkpoint_id": old_id,
                    "previous_checkpoint_hash": old_hash,
                    "current_checkpoint_id": old_id,
                    "current_checkpoint_hash": old_hash,
                    "blocked_reasons": [],
                    "dispatch_authority_created": False,
                    "operator_approval_created": False,
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                },
            }

        action = "return_home" if self.revision_mode == "return_home" else "avoid_obstacle"
        revision_intent = (
            "return_home" if action == "return_home" else "avoid_right_wide"
        )
        revision_id = "turtlebot3_recovery_revision_test"
        parameters = (
            {
                "target_x_m": 0.0,
                "target_y_m": 0.0,
                "return_home_required": True,
            }
            if action == "return_home"
            else {
                "recovery_waypoints": [
                    {"target_x_m": -0.8, "target_y_m": -1.6},
                    {"target_x_m": -1.1, "target_y_m": -1.8},
                ],
                "obstacle_avoidance_required": True,
            }
        )
        new_checkpoint = {
            **old_checkpoint,
            "checkpoint_status": "awaiting_operator_approval",
            "parent_checkpoint_id": old_id,
            "parent_checkpoint_hash": old_hash,
            "revision_id": revision_id,
            "revision_intent": revision_intent,
            "operator_instruction_sha256": hashlib.sha256(
                operator_instruction.encode("utf-8")
            ).hexdigest(),
            "recovery_proposal_id": "recovery_proposal_revised",
            "recovery_classification_id": "recovery_classification_revised",
            "selected_action": action,
            "approved_parameters": parameters,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
        new_checkpoint.pop("checkpoint_id", None)
        new_checkpoint.pop("checkpoint_hash", None)
        new_checkpoint_hash = (
            missionos_cli._turtlebot3_recovery_checkpoint_content_hash(
                new_checkpoint
            )
        )
        new_checkpoint["checkpoint_hash"] = new_checkpoint_hash
        new_checkpoint["checkpoint_id"] = (
            f"turtlebot3_recovery_checkpoint_{new_checkpoint_hash[:12]}"
        )
        superseded = {
            **old_checkpoint,
            "checkpoint_status": "superseded",
            "superseded_by_checkpoint_id": new_checkpoint["checkpoint_id"],
            "superseded_by_checkpoint_hash": new_checkpoint["checkpoint_hash"],
            "superseded_by_revision_id": revision_id,
            "superseded_by_revision_ref": revision_id,
        }
        artifacts = self.stored_task_payload["task"]["artifacts"]
        artifacts["turtlebot3_recovery_checkpoint"] = new_checkpoint
        artifacts["turtlebot3_recovery_checkpoints"] = {
            old_id: superseded,
            new_checkpoint["checkpoint_id"]: new_checkpoint,
        }
        summary = artifacts["summary"]
        summary["turtlebot3_recovery_checkpoint"] = new_checkpoint
        revision_lineage = {
            "revision_id": revision_id,
            "parent_checkpoint_id": old_id,
            "child_checkpoint_id": new_checkpoint["checkpoint_id"],
            "revision_intent": revision_intent,
            "operator_approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
        artifacts["turtlebot3_home_mission_execution"] = {
            "turtlebot3_recovery_checkpoint": new_checkpoint,
            "recovery_checkpoint_revision": revision_lineage,
        }
        summary["runtime_recovery_action_kind"] = action
        summary["recovery_dispatch_request_sent"] = False
        summary["recovery_proposals"].append(
            {
                "proposal_id": "recovery_proposal_revised",
                "proposal_source": "llm",
                "selected_action": action,
                "reason": "Operator-requested checkpoint-bound revision.",
                "llm_invocation_evidence": {
                    "provider": "google_adk_gemini",
                    "model_id": "gemini-test",
                },
                "input_observations": {},
            }
        )
        summary["recovery_proposal_classifications"].append(
            {
                "classification_id": "recovery_classification_revised",
                "execution_class": "requires_human_approval",
                "requires_new_human_approval": True,
                "execution_permitted_by_envelope": False,
                "proposal_allowed": True,
            }
        )
        summary["recovery_checkpoint_revision"] = revision_lineage
        revision_record = {
            "schema_version": (
                "missionos_turtlebot3_recovery_checkpoint_revision.v1"
            ),
            "revision_status": "proposed",
            "revision_id": revision_id,
            "blocking_reasons": [],
            "parent_checkpoint_id": old_id,
            "parent_checkpoint_hash": old_hash,
            "superseded_checkpoint": copy.deepcopy(superseded),
            "turtlebot3_recovery_checkpoint": copy.deepcopy(new_checkpoint),
            "turtlebot3_home_mission_execution": copy.deepcopy(
                artifacts["turtlebot3_home_mission_execution"]
            ),
            "summary": copy.deepcopy(summary),
            "operator_approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
        artifacts["turtlebot3_recovery_revision"] = copy.deepcopy(revision_record)
        artifacts["turtlebot3_recovery_revisions"] = {
            revision_id: copy.deepcopy(revision_record)
        }
        return {
            "schema_version": "missionos_turtlebot3_recovery_revision_response.v1",
            "task": self._task_payload()["task"],
            "summary": {
                "task_id": task_id,
                "revision_status": "revised",
                "previous_checkpoint_id": old_id,
                "previous_checkpoint_hash": old_hash,
                "current_checkpoint_id": new_checkpoint["checkpoint_id"],
                "current_checkpoint_hash": new_checkpoint["checkpoint_hash"],
                "blocked_reasons": [],
                "dispatch_authority_created": False,
                "operator_approval_created": False,
                "dispatch_request_sent": False,
                "physical_execution_invoked": False,
            },
        }


class BackNavigationMissionOSClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.started: list[str] = []

    def conversation(
        self,
        instruction: str,
        *,
        session_id: str,
        mission_designer_context: dict[str, Any] | None = None,
        coordinate_route: dict[str, Any] | None = None,
        route_hint: str | None = None,
        client_surface: str | None = None,
        robot_profile: str | None = None,
    ) -> dict[str, Any]:
        del coordinate_route
        self.requests.append(
            {
                "operator_instruction": instruction,
                "session_id": session_id,
                "mission_designer_context": mission_designer_context or {},
                "missionos_route_hint": route_hint or "",
                "missionos_client_surface": client_surface or "",
                "robot_profile": robot_profile or "",
            }
        )
        if route_hint == "approve":
            return self._payload(
                routed_action="approve",
                context_ref="mission_designer_context:approved",
                context_sha="sha-approved",
                session_id=session_id,
            )
        if route_hint == "execute":
            return self._payload(
                routed_action="execute",
                context_ref="mission_designer_context:prepared",
                context_sha="sha-prepared",
                session_id=session_id,
                task_id="task_prepare",
            )
        return self._payload(
            routed_action="mission_designer_plan",
            context_ref="mission_designer_context:plan",
            context_sha="sha-plan",
            session_id=session_id,
            approvable=True,
        )

    def start_sitl(self, *, task_id: str) -> dict[str, Any]:
        self.started.append(task_id)
        return {
            "summary": {
                "task_id": task_id,
                "startup_status": "started",
                "readiness_status": "ready",
                "container_name": "fixture",
            },
            "px4_gazebo_sitl_execution_readiness": {
                "readiness_status": "ready",
                "mavlink_endpoint_observed": True,
            },
        }

    @staticmethod
    def _payload(
        *,
        routed_action: str,
        context_ref: str,
        context_sha: str,
        session_id: str,
        approvable: bool = False,
        task_id: str = "",
    ) -> dict[str, Any]:
        mission_designer: dict[str, Any] = {
            "mission_designer_context_ref": context_ref,
            "mission_designer_context_sha256": context_sha,
            "mission_designer_context_session_id": session_id,
            "summary": {},
        }
        if approvable:
            mission_designer["scenario_proposal"] = {"proposal_id": "proposal_back"}
            mission_designer["validation_result"] = {"validation_status": "passed"}
        if task_id:
            mission_designer["summary"]["sitl_execution_task_id"] = task_id
        return {
            "schema_version": "missionos_autonomy_conversation_response.v1",
            "message": "handled",
            "routed_action": routed_action,
            "routing_source": "test",
            "progress_counted": False,
            "mission_designer": mission_designer,
        }


def _chat_ctx(tmp_path: Path) -> click.Context:
    ctx = click.Context(missionos_cli.missionos)
    ctx.obj = {
        "missionos_client": None,
        "missionos_gateway_url": "http://127.0.0.1:18881",
        "missionos_json_output": False,
        "missionos_state_path": tmp_path / "state.json",
    }
    return ctx


def test_run_command_sends_execute_route_hint(monkeypatch: Any, tmp_path: Path) -> None:
    client = RecordingMissionOSClient()
    monkeypatch.setattr(missionos_cli, "make_client", lambda *_args, **_kwargs: client)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.requests[-1]["operator_instruction"] == missionos_cli.INTENT_INSTRUCTIONS["run"]
    assert client.requests[-1]["missionos_route_hint"] == "execute"
    assert "approved" not in client.requests[-1]["operator_instruction"].lower()


def test_chat_robot_turtlebot3_dry_run_prints_sim_entrypoint(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(missionos_cli, "_gateway_reachable", lambda _client: False)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "turtlebot3",
            "--turtlebot3-dry-run",
            "TurtleBot3で屋内配送ルートを走って",
        ],
    )

    assert result.exit_code == 0, result.output
    compact_output = result.output.replace("\n", "")
    assert "TurtleBot3 MissionOS Gateway" in result.output
    assert "TurtleBot3で屋内配送ルートを走って" in result.output
    assert "start_ros2_nav2_turtlebot3_gateway_docker.sh" in compact_output
    assert "surfaces=chat + operate + watch + map" in result.output
    assert "claim_scope=sim_action" in result.output
    assert "world_profile=house" in result.output


def test_turtlebot3_gateway_launcher_preserves_runtime_import_and_failure_evidence() -> None:
    launcher = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "start_ros2_nav2_turtlebot3_gateway_docker.sh"
    ).read_text(encoding="utf-8")

    assert "docker run -d" in launcher
    assert "docker run --rm -d" not in launcher
    assert 'GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-false}' in launcher
    assert 'gateway_llm_backend="${MISSIONOS_LLM_BACKEND:-deepseek}"' in launcher
    assert "gateway_default_model_id=deepseek-v4-flash" in launcher
    assert "gateway_default_model_id=gemini-3.1-flash-lite" in launcher
    assert 'gateway_model_id="${AGENT_MODEL:-${gateway_default_model_id}}"' in launcher
    assert "vertex_location=global" in launcher
    assert '-e "AGENT_MODEL=${gateway_model_id}"' in launcher
    assert "missing_deterministic_fallback" in launcher
    assert "MISSIONOS_GEMINI_CREDENTIAL_STATUS" in launcher
    assert (
        "MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_M:-1.10"
        in launcher
    )
    assert 'MISSIONOS_TB3_GAZEBO_WAIT_SECONDS=${MISSIONOS_TB3_GAZEBO_WAIT_SECONDS:-30}' in launcher
    assert 'MISSIONOS_TB3_NAV2_WAIT_SECONDS=${MISSIONOS_TB3_NAV2_WAIT_SECONDS:-55}' in launcher
    assert (
        'MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL='
        '${MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL:-1}'
        in launcher
    )
    assert "sys.path[:0]" in launcher
    assert "/work/missionos/packages/missionos-gateway/src" in launcher


def test_turtlebot3_gateway_launcher_forwards_cli_api_key(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    class Result:
        returncode = 0

    def fake_run(*args: Any, **kwargs: Any) -> Result:
        calls.append({"args": args, "kwargs": kwargs})
        return Result()

    monkeypatch.setattr(missionos_cli.subprocess, "run", fake_run)

    started = missionos_cli._start_turtlebot3_gateway_container(
        gateway_url="http://127.0.0.1:18792",
        instruction="TurtleBot3で屋内配送して",
        build_image=False,
        dry_run=False,
        gateway_api_key="test-container-api-key",
    )

    assert started is True
    assert calls[-1]["kwargs"]["env"]["GATEWAY_API_KEY"] == (
        "test-container-api-key"
    )


def test_chat_robot_nova_carter_passes_robot_profile_to_gateway(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    monkeypatch.setattr(missionos_cli, "make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_build_chat_session",
        lambda _history_path: object(),
    )

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "nova-carter",
            "短いNav2ルートを走って",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.requests[-1]["robot_profile"] == "nova_carter"
    assert client.requests[-1]["session_id"] == "missionos-cli-nova-carter"
    assert client.requests[-1]["missionos_client_surface"] == "chat"
    assert client.requests[-1]["operator_instruction"] == "短いNav2ルートを走って"


def test_chat_robot_turtlebot4_uses_turtlebot4_default_instruction(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    monkeypatch.setattr(missionos_cli, "make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_build_chat_session",
        lambda _history_path: object(),
    )

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "turtlebot4",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.requests[-1]["operator_instruction"] == (
        missionos_cli.DEFAULT_TURTLEBOT4_CHAT_INSTRUCTION
    )
    assert client.requests[-1]["robot_profile"] == "turtlebot4"
    assert client.requests[-1]["session_id"] == "missionos-cli-turtlebot4"
    assert client.requests[-1]["missionos_client_surface"] == "chat"


def test_chat_robot_turtlebot4_rejects_turtlebot3_only_runtime_flags(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    for option in ("--turtlebot3-smoke", "--turtlebot3-dry-run"):
        result = runner.invoke(
            missionos_cli.missionos,
            [
                "--state-path",
                str(tmp_path / f"{option}.json"),
                "chat",
                "--robot",
                "turtlebot4",
                option,
            ],
        )

        assert result.exit_code == 2, result.output
        assert "can only be used with --robot turtlebot3" in result.output


def test_turtlebot4_execution_opens_home_robot_companions(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    launched: list[str] = []
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    missionos_cli._maybe_open_turtlebot3_companion_terminals(
        ctx,
        {
            "task_id": "task_stale_parent_payload",
            "operation_result": {
                "task_id": "task_turtlebot4",
                "summary": {
                    "task_id": "task_turtlebot4",
                    "execution_target": "ros2_nav2_turtlebot4_sim",
                    "robot_profile": "turtlebot4",
                },
            }
        },
    )

    assert launched == ["task_turtlebot4"]


def test_chat_robot_turtlebot3_dry_run_can_select_house_profile(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(missionos_cli, "_gateway_reachable", lambda _client: False)
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "turtlebot3",
            "--turtlebot3-dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "TurtleBot3 MissionOS Gateway" in result.output
    assert "world_profile=house" in result.output
    assert "MISSIONOS_TURTLEBOT3_WORLD_PROFILE=house" in result.output


def test_chat_robot_turtlebot3_mid_recovery_dry_run_sets_env(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(missionos_cli, "_gateway_reachable", lambda _client: False)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "turtlebot3",
            "--turtlebot3-mid-recovery",
            "--turtlebot3-dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    compact_output = result.output.replace("\n", "")
    assert "start_ros2_nav2_turtlebot3_gateway_docker.sh" in compact_output
    assert "physical_execution_invoked=false" in result.output


def test_chat_robot_turtlebot3_dry_run_retargets_default_busy_gateway(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(missionos_cli, "_gateway_reachable", lambda _client: True)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "turtlebot3",
            "--turtlebot3-dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "using TurtleBot3" in result.output
    assert "Gateway URL http://127.0.0.1:18792" in result.output
    assert "gateway_url=http://127.0.0.1:18792" in result.output


def test_chat_robot_turtlebot3_smoke_dry_run_keeps_noninteractive_path(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "chat",
            "--robot",
            "turtlebot3",
            "--turtlebot3-smoke",
            "--turtlebot3-mid-recovery",
            "--turtlebot3-dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "TurtleBot3 MissionOS Chat" in result.output
    assert "MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE=1" in result.output
    compact_output = result.output.replace("\n", "")
    assert "smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh" in compact_output


def test_turtlebot3_obstacle_delivery_text_is_mission_plan_not_recovery(
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = click.Context(missionos_cli.missionos)
    ctx.obj = {
        "missionos_client": client,
        "missionos_gateway_url": "http://127.0.0.1:18792",
        "missionos_json_output": False,
        "missionos_state_path": tmp_path / "state.json",
    }

    handled = missionos_cli._handle_chat_input(
        ctx,
        client,
        "TurtleBot3で屋内配送ルートを走って。障害物を避けて、目的地まで届けて。",
        session_id="missionos-cli-turtlebot3",
    )

    assert handled is True
    assert client.recovery_proposals == []
    assert client.requests[-1]["missionos_route_hint"] == "mission_designer_plan"
    assert client.requests[-1]["missionos_client_surface"] == "chat"


def test_payload_task_id_reads_turtlebot3_home_mission_task_id() -> None:
    assert (
        missionos_cli._payload_task_id(
            {
                "mission_designer": {
                    "summary": {
                        "execution_target": "ros2_nav2_turtlebot3_sim",
                        "turtlebot3_home_mission_task_id": "task_turtlebot3_delivery",
                    }
                }
            }
        )
        == "task_turtlebot3_delivery"
    )


def test_chat_enter_prepare_sends_execute_route_hint(tmp_path: Path) -> None:
    client = RecordingMissionOSClient()
    ctx = click.Context(missionos_cli.missionos)
    ctx.obj = {
        "missionos_client": client,
        "missionos_gateway_url": "http://127.0.0.1:18881",
        "missionos_json_output": False,
        "missionos_state_path": tmp_path / "state.json",
    }

    missionos_cli._set_chat_suggestion(ctx, raw="/run", label="prepare")

    assert missionos_cli._handle_chat_input(ctx, client, "", session_id="chat-session") is True
    assert client.requests[-1]["operator_instruction"] == missionos_cli.INTENT_INSTRUCTIONS["run"]
    assert client.requests[-1]["missionos_route_hint"] == "execute"
    assert client.requests[-1]["missionos_client_surface"] == "chat"
    assert "approved" not in client.requests[-1]["operator_instruction"].lower()
    assert missionos_cli._chat_suggestion(ctx) == {"raw": "/start-sitl", "label": "start"}


def test_chat_slash_avoid_dispatches_parameterized_recovery(tmp_path: Path) -> None:
    client = RecordingMissionOSClient()
    ctx = click.Context(missionos_cli.missionos)
    ctx.obj = {
        "missionos_client": client,
        "missionos_gateway_url": "http://127.0.0.1:18881",
        "missionos_json_output": False,
        "missionos_state_path": tmp_path / "state.json",
    }
    missionos_cli._remember_sitl_task_id(ctx, "task_chat_avoid")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "/avoid 40 20 45 --yes",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests[-1] == {
        "task_id": "task_chat_avoid",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {
            "target_x_m": 40.0,
            "target_y_m": 20.0,
            "target_altitude_m": 45.0,
        },
    }
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_chat_avoid",
        "label": "show status",
    }


def test_chat_approve_recovery_dispatches_pending_human_approval_proposal(
    tmp_path: Path,
) -> None:
    client = PendingRecoveryApprovalClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, client.task_id)

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "/approve-recovery task_pending_recovery",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests[-1] == {
        "task_id": "task_pending_recovery",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {
            "target_x_m": -0.2,
            "target_y_m": -1.4,
            "obstacle_avoidance_required": True,
        },
        "expected_recovery_checkpoint_id": REVIEWED_RECOVERY_CHECKPOINT_ID,
        "expected_recovery_checkpoint_hash": REVIEWED_RECOVERY_CHECKPOINT_HASH,
    }
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_pending_recovery",
        "label": "show status",
    }


def test_chat_natural_language_approval_uses_pending_recovery_proposal(
    tmp_path: Path,
) -> None:
    client = PendingRecoveryApprovalClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, client.task_id)

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "承認します",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests[-1]["task_id"] == "task_pending_recovery"
    assert client.requests[-1]["recovery_action"] == "avoid_obstacle"
    assert client.requests[-1]["recovery_parameters"] == {
        "target_x_m": -0.2,
        "target_y_m": -1.4,
        "obstacle_avoidance_required": True,
    }


def test_chat_cannot_approve_ask_human_checkpoint_and_enters_revision_mode(
    tmp_path: Path,
    capsys: Any,
) -> None:
    client = AskHumanPendingRecoveryClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, client.task_id)

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "承認します",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests == []
    pending = missionos_cli._pending_recovery_approval_from_task(
        client._task_payload()
    )
    assert pending is not None
    assert pending["operator_guidance_required"] is True
    assert pending["checkpoint_dispatch_supported"] is False
    assert pending["checkpoint_approval_supported"] is False
    assert missionos_cli._chat_recovery_revision_context(ctx) == {
        "task_id": client.task_id,
        "checkpoint_id": pending["checkpoint_id"],
        "checkpoint_hash": pending["checkpoint_hash"],
    }
    rendered = capsys.readouterr().out
    normalized = " ".join(rendered.split())
    assert "cannot be" in normalized
    assert "approved or dispatched" in normalized
    assert "No approval artifact or dispatch was" in normalized


def test_pending_turtlebot3_recovery_uses_authoritative_checkpoint_parameters() -> None:
    client = PendingRecoveryApprovalClient()
    payload = client._task_payload()
    summary = payload["task"]["artifacts"]["summary"]
    observations = summary["recovery_proposals"][0]["input_observations"]
    observations["recommended_avoidance_target_x_m"] = 9.0
    observations["recommended_avoidance_target_y_m"] = 9.0

    pending = missionos_cli._pending_recovery_approval_from_task(payload)

    assert pending is not None
    assert pending["recovery_parameters"] == {
        "target_x_m": -0.2,
        "target_y_m": -1.4,
        "obstacle_avoidance_required": True,
    }
    assert pending["checkpoint_id"] == REVIEWED_RECOVERY_CHECKPOINT_ID
    assert pending["checkpoint_hash"] == REVIEWED_RECOVERY_CHECKPOINT_HASH


def test_pending_px4_recovery_uses_current_runtime_proposal_parameters() -> None:
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v1",
        "proposal_id": "runtime_recovery_proposal_current",
        "proposal_status": "awaiting_operator_approval",
        "proposal_source": "deterministic_recompile_of_prior_llm_judgment",
        "runtime_recovery_agent_result": {
            "runtime_status": "proposal_guardrail_passed",
            "assessment": {
                "assessment_status": "proposal_guardrail_passed",
                "selected_bounded_action": "avoid_obstacle",
                "proposed_parameters": {
                    "target_x_m": -28.174,
                    "target_y_m": 436.426,
                    "target_altitude_m": 45.0,
                },
            },
            "agent_output": {"rationale": "avoid the current local conflict"},
            "agent_invocations": [],
        },
    }
    payload = {
        "task": {
            "task_id": "task_px4_runtime_recovery",
            "kind": "mission_designer_sitl_execution",
            "status": "running",
            "artifacts": {
                "missionos_runtime_recovery_last_proposal": proposal,
                "missionos_runtime_recovery_agent_live_bridge": {
                    "telemetry_snapshot": {"sample_index": 117}
                },
            },
        }
    }

    pending = missionos_cli._pending_recovery_approval_from_task(payload)

    assert pending is not None
    assert pending["recovery_action"] == "avoid_obstacle"
    assert pending["recovery_parameters"] == {
        "target_x_m": -28.174,
        "target_y_m": 436.426,
        "target_altitude_m": 45.0,
    }
    assert pending["recovery_proposal_id"] == (
        "runtime_recovery_proposal_current"
    )
    assert pending["input_observations"] == {"sample_index": 117}


def test_pending_px4_v2_recovery_requires_compiled_reachable_parameters() -> None:
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": "runtime_recovery_proposal_v2",
        "proposal_status": "awaiting_operator_approval",
        "runtime_recovery_agent_result": {
            "assessment": {
                "selected_bounded_action": "avoid_obstacle",
                "proposed_parameters": {"target_x_m": 999.0, "target_y_m": 999.0},
                "intent_compilation": {
                    "compilation_status": "compiled",
                    "compiled_action": "avoid_obstacle",
                    "compiled_parameters": {
                        "target_x_m": -49.0,
                        "target_y_m": 299.0,
                        "target_altitude_m": 45.0,
                        "source_obstacle_name": "missionos_route_obstacle_50pct",
                    },
                },
                "reachability_verification": {
                    "verification_status": "verified",
                    "reachability_verified": True,
                },
            }
        },
    }
    payload = {
        "task": {
            "task_id": "task_px4_runtime_recovery_v2",
            "kind": "px4_gazebo_mission_designer_sitl_execution_request",
            "status": "running",
            "artifacts": {"missionos_runtime_recovery_last_proposal": proposal},
        }
    }

    pending = missionos_cli._pending_recovery_approval_from_task(payload)

    assert pending is not None
    assert pending["runtime_proposal_approval_supported"] is True
    assert pending["recovery_parameters"] == {
        "target_x_m": -49.0,
        "target_y_m": 299.0,
        "target_altitude_m": 45.0,
        "source_obstacle_name": "missionos_route_obstacle_50pct",
    }
    console = Console(record=True, color_system=None, width=140)
    console.print(missionos_cli._render_chat_recovery_review(pending))
    rendered = console.export_text()
    assert "approve exact proposal" in rendered
    assert "proposal=runtime_recovery_proposal_v2" in rendered
    assert "approve exact checkpoint" not in rendered


def test_pending_px4_v2_recovery_hides_unverified_compilation() -> None:
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": "runtime_recovery_proposal_v2_unverified",
        "proposal_status": "awaiting_operator_approval",
        "runtime_recovery_agent_result": {
            "assessment": {
                "selected_bounded_action": "avoid_obstacle",
                "intent_compilation": {
                    "compilation_status": "compiled",
                    "compiled_action": "avoid_obstacle",
                    "compiled_parameters": {"target_x_m": -49.0, "target_y_m": 299.0},
                },
                "reachability_verification": {
                    "verification_status": "blocked",
                    "reachability_verified": False,
                },
            }
        },
    }
    payload = {
        "task": {
            "task_id": "task_px4_runtime_recovery_v2",
            "kind": "px4_gazebo_mission_designer_sitl_execution_request",
            "status": "running",
            "artifacts": {"missionos_runtime_recovery_last_proposal": proposal},
        }
    }

    assert missionos_cli._pending_recovery_approval_from_task(payload) is None


def test_pending_px4_v3_recovery_requires_verified_hazard_and_feasibility() -> None:
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v3",
        "proposal_id": "runtime_recovery_proposal_v3",
        "proposal_status": "awaiting_operator_approval",
        "proposal_source": "deterministic_recompile_of_prior_llm_judgment",
        "proposal_origin": {
            "origin_kind": "deterministic_recompile_of_prior_judgment",
            "provider": "google_adk_litellm_deepseek",
            "model_id": "deepseek-v4-flash",
            "source_proposal_id": "runtime_recovery_proposal_llm",
        },
        "source_obstacle_name": "missionos_route_obstacle_50pct",
        "runtime_recovery_agent_result": {
            "assessment": {
                "selected_bounded_action": "avoid_obstacle",
                "intent_compilation": {
                    "compilation_status": "compiled",
                    "compiled_action": "avoid_obstacle",
                    "compiled_parameters": {
                        "target_x_m": -49.0,
                        "target_y_m": 299.0,
                        "target_altitude_m": 45.0,
                        "source_obstacle_name": "missionos_route_obstacle_50pct",
                    },
                },
                "reachability_verification": {
                    "verification_status": "verified",
                    "reachability_verified": True,
                },
                "hazard_state": {"hazard_state_status": "verified"},
                "action_feasibility": {
                    "feasibility_status": "verified_feasible",
                    "obstacle_clearance_verification": {
                        "verification_status": "verified",
                        "minimum_clearance_m": 34.7,
                        "required_clearance_m": 20.0,
                    },
                    "maneuver_duration_model": {
                        "performance_envelope_status": "verified",
                    },
                    "blocking_reasons": [],
                },
            }
        },
    }
    payload = {
        "task": {
            "task_id": "task_px4_runtime_recovery_v3",
            "kind": "px4_gazebo_mission_designer_sitl_execution_request",
            "status": "running",
            "artifacts": {
                "missionos_runtime_recovery_last_proposal": proposal,
                "missionos_runtime_recovery_safety_hold_receipt": {
                    "hold_status": "observed"
                },
                "missionos_auto_mission_runtime_snapshot": {
                    "operator_recovery_action": "safety_hold",
                    "operator_recovery_assist_status": "safety_hold_observed",
                },
            },
        }
    }

    pending = missionos_cli._pending_recovery_approval_from_task(payload)

    assert pending is not None
    assert pending["runtime_proposal_approval_supported"] is True
    assert pending["recovery_parameters"]["source_obstacle_name"] == (
        "missionos_route_obstacle_50pct"
    )
    assert pending["llm_provider"] == "google_adk_litellm_deepseek"
    assert pending["llm_model_id"] == "deepseek-v4-flash"
    assert pending["source_obstacle_name"] == "missionos_route_obstacle_50pct"
    assert pending["action_feasibility"]["feasibility_status"] == (
        "verified_feasible"
    )

    panel = missionos_cli._render_recovery_agent_console(
        payload,
        proposal=None,
        show_proposal=False,
        status="running",
        task_id="task_px4_runtime_recovery_v3",
    )
    rendered = str(panel.renderable)
    assert "Proposal evidence:" in rendered
    assert "Aircraft held — recovery decision required" in rendered
    assert "google_adk_litellm_deepseek/deepseek-v4-flash" in rendered
    assert "source_proposal=runtime_recovery_proposal_llm" in rendered
    assert "obstacle=missionos_route_obstacle_50pct" in rendered
    assert "Action feasibility: verified_feasible" in rendered
    assert "clearance=verified (min=34.7m, required=20.0m)" in rendered
    assert "performance=verified" in rendered
    assert "not LLM approval" in rendered
    assert "not Gemini approval" not in rendered
    assert "Candidate validation: -" not in rendered
    assert "type a change in plain language" not in rendered


@pytest.mark.parametrize(
    ("hazard_status", "feasibility_status"),
    [
        ("unverified", "verified_feasible"),
        ("verified", "unverified"),
        ("verified", "blocked"),
    ],
)
def test_pending_px4_v3_recovery_hides_unverified_candidates(
    hazard_status: str,
    feasibility_status: str,
) -> None:
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v3",
        "proposal_id": "runtime_recovery_proposal_v3_unverified",
        "proposal_status": "awaiting_operator_approval",
        "runtime_recovery_agent_result": {
            "assessment": {
                "selected_bounded_action": "avoid_obstacle",
                "intent_compilation": {
                    "compilation_status": "compiled",
                    "compiled_action": "avoid_obstacle",
                    "compiled_parameters": {
                        "target_x_m": -49.0,
                        "target_y_m": 299.0,
                        "source_obstacle_name": "missionos_route_obstacle_50pct",
                    },
                },
                "reachability_verification": {
                    "verification_status": "verified",
                    "reachability_verified": True,
                },
                "hazard_state": {"hazard_state_status": hazard_status},
                "action_feasibility": {
                    "feasibility_status": feasibility_status,
                },
            }
        },
    }
    payload = {
        "task": {
            "task_id": "task_px4_runtime_recovery_v3",
            "kind": "px4_gazebo_mission_designer_sitl_execution_request",
            "status": "running",
            "artifacts": {"missionos_runtime_recovery_last_proposal": proposal},
        }
    }

    assert missionos_cli._pending_recovery_approval_from_task(payload) is None


def test_pending_px4_recovery_hides_proposal_after_matching_authority() -> None:
    payload = {
        "task": {
            "task_id": "task_px4_runtime_recovery",
            "kind": "mission_designer_sitl_execution",
            "status": "running",
            "artifacts": {
                "missionos_runtime_recovery_last_proposal": {
                    "schema_version": (
                        "missionos_runtime_recovery_proposal_evidence.v1"
                    ),
                    "proposal_id": "runtime_recovery_proposal_approved",
                    "proposal_status": "awaiting_operator_approval",
                    "runtime_recovery_agent_result": {
                        "assessment": {
                            "selected_bounded_action": "avoid_obstacle",
                            "proposed_parameters": {
                                "target_x_m": -28.0,
                                "target_y_m": 436.0,
                            },
                        }
                    },
                },
                "missionos_runtime_recovery_dispatch_receipt": {
                    "dispatch_authority_created": True,
                    "proposal_revalidation": {
                        "proposal_id": "runtime_recovery_proposal_approved"
                    },
                },
            },
        }
    }

    assert missionos_cli._pending_recovery_approval_from_task(payload) is None


def test_pending_turtlebot3_recovery_never_borrows_unmatched_proposal_evidence(
    capsys: Any,
) -> None:
    client = PendingRecoveryApprovalClient()
    payload = client._task_payload()
    checkpoint = payload["task"]["artifacts"]["turtlebot3_recovery_checkpoint"]
    checkpoint["recovery_proposal_id"] = "different-proposal"
    checkpoint["recovery_classification_id"] = "different-classification"
    payload["task"]["artifacts"]["summary"][
        "turtlebot3_recovery_checkpoint"
    ] = checkpoint

    pending = missionos_cli._pending_recovery_approval_from_task(payload)

    assert pending is not None
    assert pending["recovery_parameters"]["target_x_m"] == -0.2
    assert pending["proposal_reason"] == ""
    assert pending["input_observations"] == {}
    assert pending["llm_provider"] == ""
    assert pending["llm_model_id"] == ""
    assert pending["proposal_source"] == ""
    assert pending["rules_execution_class"] == ""

    missionos_cli.console.print(missionos_cli._render_chat_recovery_review(pending))
    rendered = capsys.readouterr().out
    assert "planner=-" in rendered
    assert "evidence=exact referenced evidence unavailable" in rendered
    assert "planner=llm" not in rendered
    assert "source-bound observations retained" not in rendered


def test_chat_recovery_review_escapes_llm_reason_markup(capsys: Any) -> None:
    client = PendingRecoveryApprovalClient()
    payload = client._task_payload()
    proposal = payload["task"]["artifacts"]["summary"]["recovery_proposals"][0]
    proposal["reason"] = "[bold red]APPROVE NOW[/bold red]"
    pending = missionos_cli._pending_recovery_approval_from_task(payload)
    assert pending is not None

    missionos_cli.console.print(missionos_cli._render_chat_recovery_review(pending))

    assert "[bold red]APPROVE NOW[/bold red]" in capsys.readouterr().out


def test_chat_enter_reviews_turtlebot3_recovery_with_default_defer(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    client = PendingRecoveryApprovalClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, client.task_id)
    task = client._task_payload()["task"]
    payload = {
        "routed_action": "execute",
        "operation_result": {
            "task_status": "pending",
            "summary_status": "incomplete",
            "summary": task["artifacts"]["summary"],
        },
    }
    missionos_cli._update_chat_suggestion_from_conversation(ctx, payload)
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review recovery",
    }
    rendered = capsys.readouterr().out
    assert "Recovery Agent Proposal Review" in rendered
    assert "action=avoid_obstacle" in rendered
    assert "target_x_m=-0.2" in rendered
    assert "dispatch_authority=False" in rendered

    prompts: list[dict[str, Any]] = []

    def default_defer_prompt(text: str, **kwargs: Any) -> str:
        prompts.append({"text": text, **kwargs})
        return str(kwargs["default"])

    monkeypatch.setattr(click, "prompt", default_defer_prompt)
    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "",
            session_id="chat-session",
        )
        is True
    )
    assert prompts == [
        {
            "text": "Recovery decision [y=approve, d/Enter=defer, c=change]",
            "default": "d",
            "show_default": False,
        }
    ]
    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {}
    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "",
            session_id="chat-session",
        )
        is True
    )
    assert client.requests == []


def test_chat_recovery_review_yes_dispatches_reviewed_checkpoint_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class StaleSummaryClient(PendingRecoveryApprovalClient):
        def _task_payload(self) -> dict[str, Any]:
            payload = super()._task_payload()
            observations = payload["task"]["artifacts"]["summary"][
                "recovery_proposals"
            ][0]["input_observations"]
            observations["recommended_avoidance_target_x_m"] = 9.0
            observations["recommended_avoidance_target_y_m"] = 9.0
            return payload

    client = StaleSummaryClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "y")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "/review-recovery task_pending_recovery",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests == [
        {
            "task_id": "task_pending_recovery",
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {
                "target_x_m": -0.2,
                "target_y_m": -1.4,
                "obstacle_avoidance_required": True,
            },
            "expected_recovery_checkpoint_id": REVIEWED_RECOVERY_CHECKPOINT_ID,
            "expected_recovery_checkpoint_hash": (
                REVIEWED_RECOVERY_CHECKPOINT_HASH
            ),
        }
    ]


@pytest.mark.parametrize("choice", ["n", "d", "defer"])
def test_chat_recovery_review_defer_never_dispatches(
    tmp_path: Path,
    monkeypatch: Any,
    choice: str,
) -> None:
    client = PendingRecoveryApprovalClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: choice)

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "/review-recovery task_pending_recovery",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {}
    assert missionos_cli._chat_recovery_revision_context(ctx) == {}


@pytest.mark.parametrize("choice", ["c", "修正", "revise"])
def test_chat_recovery_review_change_enters_checkpoint_bound_revision_mode(
    tmp_path: Path,
    monkeypatch: Any,
    choice: str,
) -> None:
    client = CheckpointRevisionClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: choice)

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {
        "task_id": "task_pending_recovery",
        "checkpoint_id": REVIEWED_RECOVERY_CHECKPOINT_ID,
        "checkpoint_hash": REVIEWED_RECOVERY_CHECKPOINT_HASH,
    }
    assert client.requests == []
    assert client.revision_requests == []
    assert client.generic_proposal_calls == 0
    assert missionos_cli._chat_suggestion(ctx) == {}


def test_chat_recovery_revision_enter_is_inert_and_keeps_exact_binding(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    client = CheckpointRevisionClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )
    binding = missionos_cli._chat_recovery_revision_context(ctx)
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == binding
    assert client.revision_requests == []
    assert client.requests == []
    assert "Enter does not approve" in capsys.readouterr().out


def test_chat_recovery_revision_slash_back_exits_without_authority(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    client = CheckpointRevisionClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/back",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review pending recovery",
    }
    assert client.revision_requests == []
    assert client.requests == []
    assert "Exited recovery revision mode" in capsys.readouterr().out


def test_chat_recovery_revision_is_not_offered_for_nova_carter_scope(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    client = CheckpointRevisionClient()
    artifacts = client.stored_task_payload["task"]["artifacts"]
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    checkpoint["robot_profile"] = "nova_carter"
    checkpoint["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    plan = artifacts["turtlebot3_home_mission_plan"]
    plan["robot_profile"] = "nova_carter"
    plan["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    summary = artifacts["summary"]
    summary["robot_profile"] = "nova_carter"
    summary["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    summary["turtlebot3_recovery_checkpoint"] = checkpoint
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    pending = missionos_cli._lookup_pending_recovery_approval(
        client,
        task_id=client.task_id,
    )
    assert pending is not None
    assert pending["checkpoint_approval_supported"] is False
    assert pending["checkpoint_revision_supported"] is False

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert client.revision_requests == []
    assert client.requests == []
    output = capsys.readouterr().out
    assert "only verified for TurtleBot3" in output
    assert "y=approve exact checkpoint" not in output


def test_chat_recovery_unsupported_robot_scope_does_not_offer_or_accept_approval(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    client = CheckpointRevisionClient()
    artifacts = client.stored_task_payload["task"]["artifacts"]
    for view_key in (
        "turtlebot3_home_mission_plan",
        "turtlebot3_recovery_checkpoint",
        "summary",
    ):
        artifacts[view_key]["robot_profile"] = "nova_carter"
        artifacts[view_key]["execution_target"] = (
            "isaac_ros_nav2_nova_carter_sim"
        )
    artifacts["summary"]["turtlebot3_recovery_checkpoint"] = artifacts[
        "turtlebot3_recovery_checkpoint"
    ]
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "y")

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )
    assert client.requests == []
    review_output = capsys.readouterr().out
    assert "y=approve exact checkpoint" not in review_output
    assert "Approval is unavailable for this robot scope" in review_output

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/approve-recovery task_pending_recovery",
        session_id="chat-session",
    )
    assert client.requests == []
    assert "Checkpoint approval is unavailable" in capsys.readouterr().out


def test_chat_recovery_capabilities_require_strict_plan_checkpoint_summary_scope() -> None:
    client = CheckpointRevisionClient()
    artifacts = client.stored_task_payload["task"]["artifacts"]
    artifacts["turtlebot3_home_mission_plan"]["robot_profile"] = "nova_carter"
    artifacts["turtlebot3_home_mission_plan"]["execution_target"] = (
        "isaac_ros_nav2_nova_carter_sim"
    )

    pending = missionos_cli._pending_recovery_approval_from_task(
        client.stored_task_payload
    )

    assert pending is not None
    assert pending["robot_profile"] == "turtlebot3"
    assert pending["execution_target"] == "ros2_nav2_turtlebot3_sim"
    assert pending["checkpoint_approval_supported"] is False
    assert pending["checkpoint_revision_supported"] is False


def test_chat_recovery_revision_uses_dedicated_route_not_generic_px4_path(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    client = CheckpointRevisionClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "左に大きく旋回してかわして",
        session_id="chat-session",
    )

    assert client.revision_requests == [
        {
            "task_id": "task_pending_recovery",
            "operator_instruction": "左に大きく旋回してかわして",
            "expected_recovery_checkpoint_id": (
                REVIEWED_RECOVERY_CHECKPOINT_ID
            ),
            "expected_recovery_checkpoint_hash": (
                REVIEWED_RECOVERY_CHECKPOINT_HASH
            ),
        }
    ]
    assert client.generic_proposal_calls == 0
    assert client.requests == []
    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review revised recovery",
    }
    pending = missionos_cli._lookup_pending_recovery_approval(
        client,
        task_id=client.task_id,
    )
    assert pending is not None
    assert pending["checkpoint_id"].startswith(
        "turtlebot3_recovery_checkpoint_"
    )
    assert pending["recovery_parameters"] == {
        "recovery_waypoints": [
            {"target_x_m": -0.8, "target_y_m": -1.6},
            {"target_x_m": -1.1, "target_y_m": -1.8},
        ],
        "obstacle_avoidance_required": True,
    }
    rendered = capsys.readouterr().out
    assert "A new checkpoint-bound recovery proposal" in rendered
    assert "Recovery Agent Proposal Review" in rendered

    monkeypatch.setattr(
        missionos_cli,
        "_wait_for_active_runner_recovery_observation",
        lambda _client, payload: payload,
    )
    assert missionos_cli._handle_chat_recovery_approval(
        ctx,
        client,
        explicit_task_id=client.task_id,
        expected_checkpoint_id=pending["checkpoint_id"],
        expected_checkpoint_hash=pending["checkpoint_hash"],
    )
    assert client.requests[-1] == {
        "task_id": client.task_id,
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {
            "recovery_waypoints": [
                {"target_x_m": -0.8, "target_y_m": -1.6},
                {"target_x_m": -1.1, "target_y_m": -1.8},
            ],
            "obstacle_avoidance_required": True,
        },
        "expected_recovery_checkpoint_id": pending["checkpoint_id"],
        "expected_recovery_checkpoint_hash": pending["checkpoint_hash"],
    }


def test_chat_recovery_revision_return_home_is_not_rewritten_to_px4_rtl(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    client = CheckpointRevisionClient(revision_mode="return_home")
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    # "戻って" is normally a chat-back synonym. Revision mode must bind it
    # to the reviewed TurtleBot3 checkpoint before generic/back routing.
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "出発地点へ戻って",
        session_id="chat-session",
    )

    assert client.revision_requests[-1]["operator_instruction"] == "出発地点へ戻って"
    pending = missionos_cli._lookup_pending_recovery_approval(
        client,
        task_id=client.task_id,
    )
    assert pending is not None
    assert pending["selected_action"] == "return_home"
    assert pending["recovery_action"] == "return_home"
    assert pending["recovery_parameters"] == {
        "target_x_m": 0.0,
        "target_y_m": 0.0,
        "return_home_required": True,
    }
    assert client.requests == []

    monkeypatch.setattr(
        missionos_cli,
        "_wait_for_active_runner_recovery_observation",
        lambda _client, payload: payload,
    )
    assert missionos_cli._handle_chat_recovery_approval(
        ctx,
        client,
        explicit_task_id=client.task_id,
        expected_checkpoint_id=pending["checkpoint_id"],
        expected_checkpoint_hash=pending["checkpoint_hash"],
    )
    assert client.requests[-1]["recovery_action"] == "return_home"
    assert client.requests[-1]["recovery_parameters"] == {
        "target_x_m": 0.0,
        "target_y_m": 0.0,
        "return_home_required": True,
    }


@pytest.mark.parametrize(
    ("revision_mode", "instruction"),
    [
        ("unsupported_altitude", "高度を45mに上げて"),
        ("stale", "左側を大きく迂回して"),
        ("malformed_success", "少し戻って別の通路を探して"),
    ],
)
def test_chat_recovery_revision_blocked_or_invalid_keeps_old_checkpoint_and_context(
    tmp_path: Path,
    monkeypatch: Any,
    revision_mode: str,
    instruction: str,
) -> None:
    client = CheckpointRevisionClient(revision_mode=revision_mode)
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )
    binding = missionos_cli._chat_recovery_revision_context(ctx)
    stored_before = copy.deepcopy(client.stored_task_payload)

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        instruction,
        session_id="chat-session",
    )

    assert client.stored_task_payload == stored_before
    assert missionos_cli._chat_recovery_revision_context(ctx) == binding
    assert client.generic_proposal_calls == 0
    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {}


def test_chat_recovery_revision_intercepts_natural_language_approval_without_dispatch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    client = CheckpointRevisionClient(revision_mode="malformed_success")
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "承認します",
        session_id="chat-session",
    )

    assert client.revision_requests[-1]["operator_instruction"] == "承認します"
    assert client.requests == []
    assert missionos_cli._chat_recovery_revision_context(ctx)


def test_chat_recovery_revision_transport_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class FailingRevisionClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **_kwargs: Any) -> dict[str, Any]:
            raise click.ClickException("revision endpoint unavailable")

    client = FailingRevisionClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )
    binding = missionos_cli._chat_recovery_revision_context(ctx)

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "左側を大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == binding
    assert client.requests == []
    assert "sent by this revision request" in capsys.readouterr().out


def test_chat_recovery_revision_clears_stale_mode_when_approval_wins_race(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class ApprovalWinsRaceClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **_kwargs: Any) -> dict[str, Any]:
            task = self.stored_task_payload["task"]
            artifacts = task["artifacts"]
            checkpoint = {
                **artifacts["turtlebot3_recovery_checkpoint"],
                "checkpoint_status": "dispatching",
                "dispatch_authority_created": True,
            }
            task["status"] = "running"
            artifacts["turtlebot3_recovery_checkpoint"] = checkpoint
            artifacts["turtlebot3_recovery_checkpoints"] = {
                checkpoint["checkpoint_id"]: checkpoint,
            }
            artifacts["summary"]["turtlebot3_recovery_checkpoint"] = checkpoint
            artifacts["turtlebot3_recovery_operator_approval"] = {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "checkpoint_hash": checkpoint["checkpoint_hash"],
                "operator_approved": True,
            }
            raise click.ClickException("approval won before revision")

    client = ApprovalWinsRaceClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "右に大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_pending_recovery",
        "label": "check recovery task status",
    }
    assert client.requests == []
    output = capsys.readouterr().out
    assert "reviewed checkpoint binding is no longer active" in output
    assert "task_status=running" in output
    assert "remains in revision mode" not in output


def test_chat_recovery_revision_recovers_committed_child_after_response_loss(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class CommittedThenLostResponseClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **kwargs: Any) -> dict[str, Any]:
            super().turtlebot3_recovery_revision(**kwargs)
            raise click.ClickException("response lost after durable commit")

    client = CommittedThenLostResponseClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "右に大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review revised recovery",
    }
    assert client.requests == []
    assert "durable task shows" in capsys.readouterr().out


def test_chat_recovery_revision_accepts_matching_durable_child_after_conflict_response(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class CommittedButConflictResponseClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().turtlebot3_recovery_revision(**kwargs)
            payload["summary"]["revision_status"] = "blocked"
            payload["summary"]["blocked_reasons"] = [
                "turtlebot3_recovery_checkpoint_revision_conflict"
            ]
            return payload

    client = CommittedButConflictResponseClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "右に大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review revised recovery",
    }
    assert "ready for review" in capsys.readouterr().out


def test_chat_recovery_revision_does_not_claim_different_concurrent_instruction(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class DifferentInstructionWinsClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **kwargs: Any) -> dict[str, Any]:
            concurrent_kwargs = {
                **kwargs,
                "operator_instruction": "左に大きく迂回して",
            }
            super().turtlebot3_recovery_revision(**concurrent_kwargs)
            raise click.ClickException("concurrent revision won")

    client = DifferentInstructionWinsClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "右に大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review latest recovery",
    }
    assert client.requests == []
    output = capsys.readouterr().out
    assert "different checkpoint-bound operator instruction" in output
    assert "Your instruction is not claimed as accepted" in output


def test_refetched_revision_ignores_authority_bound_to_superseded_checkpoint() -> None:
    client = CheckpointRevisionClient()
    old_checkpoint = client.stored_task_payload["task"]["artifacts"][
        "turtlebot3_recovery_checkpoint"
    ]
    client.turtlebot3_recovery_revision(
        task_id=client.task_id,
        operator_instruction="右に大きく迂回して",
        expected_recovery_checkpoint_id=str(old_checkpoint["checkpoint_id"]),
        expected_recovery_checkpoint_hash=str(old_checkpoint["checkpoint_hash"]),
    )
    old_binding = {
        "checkpoint_id": old_checkpoint["checkpoint_id"],
        "checkpoint_hash": old_checkpoint["checkpoint_hash"],
    }
    artifacts = client.stored_task_payload["task"]["artifacts"]
    artifacts["turtlebot3_recovery_operator_approval"] = {
        **old_binding,
        "operator_approved": True,
    }
    artifacts["turtlebot3_recovery_bounded_action"] = {
        **old_binding,
        "dispatch_authority_created": True,
    }
    artifacts["missionos_runtime_recovery_dispatch_receipt"] = {
        "operator_approved": True,
        "explicit_recovery_dispatch_approval": True,
        "turtlebot3_recovery_operator_approval": {
            **old_binding,
            "operator_approved": True,
        },
    }

    pending, no_authority, lineage_valid, revision_state = (
        missionos_cli._refetched_turtlebot3_revision_state(
            client,
            task_id=client.task_id,
        )
    )

    assert pending is not None
    assert pending["checkpoint_id"] != old_checkpoint["checkpoint_id"]
    assert no_authority is True
    assert lineage_valid is True
    assert revision_state["checkpoint_id"] == pending["checkpoint_id"]


def test_chat_recovery_revision_rejects_child_without_atomic_parent_lineage(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class BrokenLineageClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().turtlebot3_recovery_revision(**kwargs)
            checkpoints = self.stored_task_payload["task"]["artifacts"][
                "turtlebot3_recovery_checkpoints"
            ]
            checkpoints[REVIEWED_RECOVERY_CHECKPOINT_ID][
                "checkpoint_status"
            ] = "awaiting_operator_approval"
            return payload

    client = BrokenLineageClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "右に大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_pending_recovery",
        "label": "check recovery task status",
    }
    assert client.requests == []


@pytest.mark.parametrize(
    "corruption",
    [
        "content_hash",
        "embedded_copy",
        "parent_content",
        "parent_revision_link",
        "revision_record_collection",
        "revision_child_lineage",
    ],
)
def test_chat_recovery_revision_does_not_trust_corrupt_durable_child(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    corruption: str,
) -> None:
    class CorruptDurableChildClient(CheckpointRevisionClient):
        def turtlebot3_recovery_revision(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().turtlebot3_recovery_revision(**kwargs)
            artifacts = self.stored_task_payload["task"]["artifacts"]
            checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
            if corruption == "content_hash":
                corrupt_checkpoint = {
                    **checkpoint,
                    "selected_action": "return_home",
                }
                artifacts["turtlebot3_recovery_checkpoint"] = corrupt_checkpoint
                artifacts["turtlebot3_recovery_checkpoints"][
                    corrupt_checkpoint["checkpoint_id"]
                ] = corrupt_checkpoint
                artifacts["turtlebot3_home_mission_execution"][
                    "turtlebot3_recovery_checkpoint"
                ] = corrupt_checkpoint
                artifacts["summary"][
                    "turtlebot3_recovery_checkpoint"
                ] = corrupt_checkpoint
            elif corruption == "embedded_copy":
                artifacts["turtlebot3_home_mission_execution"][
                    "turtlebot3_recovery_checkpoint"
                ] = {
                    **checkpoint,
                    "checkpoint_status": "dispatching",
                }
            elif corruption == "parent_content":
                parent_id = checkpoint["parent_checkpoint_id"]
                parent = artifacts["turtlebot3_recovery_checkpoints"][parent_id]
                artifacts["turtlebot3_recovery_checkpoints"][parent_id] = {
                    **parent,
                    "selected_action": "return_home",
                }
            elif corruption == "parent_revision_link":
                parent_id = checkpoint["parent_checkpoint_id"]
                parent = {
                    **artifacts["turtlebot3_recovery_checkpoints"][parent_id],
                    "superseded_by_revision_id": "different_revision",
                    "superseded_by_revision_ref": "different_revision",
                }
                artifacts["turtlebot3_recovery_checkpoints"][parent_id] = parent
                for revision_record in (
                    artifacts["turtlebot3_recovery_revision"],
                    artifacts["turtlebot3_recovery_revisions"][
                        checkpoint["revision_id"]
                    ],
                ):
                    revision_record["superseded_checkpoint"] = copy.deepcopy(
                        parent
                    )
            elif corruption == "revision_record_collection":
                artifacts["turtlebot3_recovery_revisions"][
                    checkpoint["revision_id"]
                ]["parent_checkpoint_id"] = "different_parent"
            else:
                corrupt_lineage = {
                    **artifacts["turtlebot3_home_mission_execution"][
                        "recovery_checkpoint_revision"
                    ],
                    "child_checkpoint_id": "different_child",
                }
                artifacts["turtlebot3_home_mission_execution"][
                    "recovery_checkpoint_revision"
                ] = corrupt_lineage
                artifacts["summary"][
                    "recovery_checkpoint_revision"
                ] = corrupt_lineage
                for revision_record in (
                    artifacts["turtlebot3_recovery_revision"],
                    artifacts["turtlebot3_recovery_revisions"][
                        checkpoint["revision_id"]
                    ],
                ):
                    revision_record["turtlebot3_home_mission_execution"][
                        "recovery_checkpoint_revision"
                    ] = copy.deepcopy(corrupt_lineage)
                    revision_record["summary"][
                        "recovery_checkpoint_revision"
                    ] = copy.deepcopy(corrupt_lineage)
            return payload

    client = CorruptDurableChildClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "c")
    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/review-recovery task_pending_recovery",
        session_id="chat-session",
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "右に大きく迂回して",
        session_id="chat-session",
    )

    assert missionos_cli._chat_recovery_revision_context(ctx) == {}
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_pending_recovery",
        "label": "check recovery task status",
    }
    assert client.requests == []
    output = capsys.readouterr().out
    assert "revision is not claimed as accepted" in " ".join(output.split())
    assert "ready for review" not in output


def test_chat_recovery_review_aborts_when_checkpoint_changes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class ChangingCheckpointClient(PendingRecoveryApprovalClient):
        def __init__(self) -> None:
            super().__init__()
            self.task_reads = 0

        def get(self, path: str) -> dict[str, Any]:
            payload = super().get(path)
            if path.startswith(f"/tasks/{self.task_id}"):
                self.task_reads += 1
                if self.task_reads >= 2:
                    checkpoint = payload["task"]["artifacts"][
                        "turtlebot3_recovery_checkpoint"
                    ]
                    checkpoint["checkpoint_id"] = (
                        "turtlebot3_recovery_checkpoint_changed"
                    )
                    checkpoint["checkpoint_hash"] = "checkpoint-hash-changed"
                    payload["task"]["artifacts"]["summary"][
                        "turtlebot3_recovery_checkpoint"
                    ] = checkpoint
            return payload

    client = ChangingCheckpointClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "y")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "/review-recovery task_pending_recovery",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review latest recovery",
    }


def test_chat_recovery_review_restores_review_after_gateway_race_rejection(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class GatewayRaceClient(PendingRecoveryApprovalClient):
        def recovery_dispatch(
            self,
            *,
            task_id: str,
            recovery_action: str,
            recovery_parameters: dict[str, Any] | None = None,
            expected_recovery_checkpoint_id: str = "",
            expected_recovery_checkpoint_hash: str = "",
        ) -> dict[str, Any]:
            self.requests.append(
                {
                    "task_id": task_id,
                    "recovery_action": recovery_action,
                    "recovery_parameters": recovery_parameters or {},
                    "expected_recovery_checkpoint_id": (
                        expected_recovery_checkpoint_id
                    ),
                    "expected_recovery_checkpoint_hash": (
                        expected_recovery_checkpoint_hash
                    ),
                }
            )
            return {
                "task": self._task_payload()["task"],
                "summary": {
                    "task_id": task_id,
                    "task_status": "pending",
                    "dispatch_status": "blocked",
                    "blocked_reasons": [
                        "reviewed_turtlebot3_recovery_checkpoint_hash_changed"
                    ],
                    "active_runner_request_queued": False,
                    "physical_execution_invoked": False,
                },
            }

    client = GatewayRaceClient()
    ctx = _chat_ctx(tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: "y")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "/review-recovery task_pending_recovery",
            session_id="chat-session",
        )
        is True
    )

    assert len(client.requests) == 1
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/review-recovery task_pending_recovery",
        "label": "review latest recovery",
    }


def test_turtlebot3_recovery_summary_without_checkpoint_is_not_approvable() -> None:
    client = PendingRecoveryApprovalClient()
    payload = client._task_payload()
    artifacts = payload["task"]["artifacts"]
    artifacts.pop("turtlebot3_recovery_checkpoint")
    artifacts["summary"].pop("turtlebot3_recovery_checkpoint")

    assert missionos_cli._pending_recovery_approval_from_task(payload) is None


def test_operator_recovery_resolves_stored_turtlebot3_approval_checkpoint(
    tmp_path: Path,
) -> None:
    class PendingCheckpointClient(PendingRecoveryApprovalClient):
        def _task_payload(self) -> dict[str, Any]:
            payload = super()._task_payload()
            task = payload["task"]
            task["kind"] = "turtlebot3_home_mission_execution"
            task["status"] = "pending"
            task["artifacts"].pop(
                "missionos_auto_mission_gui_dispatch_running_receipt",
                None,
            )
            task["artifacts"]["turtlebot3_recovery_checkpoint"] = {
                "schema_version": (
                    "turtlebot3_recovery_checkpoint.v1"
                ),
                "checkpoint_status": "awaiting_operator_approval",
            }
            return payload

        def get(self, path: str) -> dict[str, Any]:
            if path.startswith("/tasks?page="):
                return {
                    "items": [
                        {
                            "task_id": "task_unrelated_px4_runner",
                            "status": "running",
                            "artifacts": {
                                "missionos_auto_mission_gui_dispatch_running_receipt": {
                                    "operator_recovery_request_container_path": (
                                        "/tmp/unrelated-request.json"
                                    )
                                }
                            },
                        }
                    ]
                }
            return self._task_payload()

    client = PendingCheckpointClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, client.task_id)

    assert (
        missionos_cli._resolve_operator_recovery_task_id(
            client,
            explicit_task_id="",
            stored_task_id=client.task_id,
        )
        == client.task_id
    )


def test_chat_natural_language_approval_falls_back_to_plan_approval(
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "承認します",
            session_id="chat-session",
        )
        is True
    )

    assert client.requests[-1]["missionos_route_hint"] == "approve"
    assert client.requests[-1]["missionos_client_surface"] == "chat"


def test_chat_natural_language_altitude_request_asks_recovery_agent_for_proposal(
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, "task_chat_avoid")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "高度を45mに上げて",
            session_id="chat-session",
        )
        is True
    )

    assert client.recovery_proposals[-1] == {
        "task_id": "task_chat_avoid",
        "operator_instruction": "高度を45mに上げて",
        "requested_action": "adjust_altitude",
        "requested_parameters": {"target_altitude_m": 45.0},
    }
    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/climb 45",
        "label": "review recovery",
    }


def test_chat_natural_language_obstacle_request_gets_avoidance_suggestion(
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, "task_chat_avoid")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "障害物を避けて迂回して",
            session_id="chat-session",
        )
        is True
    )

    assert client.recovery_proposals[-1]["requested_action"] == "avoid_obstacle"
    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/avoid 30 30 45",
        "label": "review recovery",
    }


def test_chat_route_plan_with_obstacle_stays_with_mission_designer(
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "東京駅から秋葉原駅まで。障害物あり",
            session_id="chat-session",
        )
        is True
    )

    assert client.recovery_proposals == []
    assert client.requests[-1] == {
        "operator_instruction": "東京駅から秋葉原駅まで。障害物あり",
        "session_id": "chat-session",
        "missionos_route_hint": "mission_designer_plan",
        "missionos_client_surface": "chat",
    }


def test_chat_natural_language_reroute_request_gets_reroute_suggestion(
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, "task_chat_avoid")

    assert (
        missionos_cli._handle_chat_input(
            ctx,
            client,
            "ルート変更して",
            session_id="chat-session",
        )
        is True
    )

    assert client.recovery_proposals[-1]["requested_action"] == "reroute"
    assert client.requests == []
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/reroute 80 30",
        "label": "review recovery",
    }


def test_chat_back_restores_previous_context_and_suggestion(tmp_path: Path) -> None:
    client = BackNavigationMissionOSClient()
    ctx = _chat_ctx(tmp_path)

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "tokyo station -> akihabara station",
        session_id="chat-back",
    )
    assert missionos_cli._chat_suggestion(ctx) == {"raw": "/approve", "label": "approve"}
    assert (
        missionos_cli._stored_mission_designer_context(ctx, "chat-back")[
            "mission_designer_context_sha256"
        ]
        == "sha-plan"
    )

    assert missionos_cli._handle_chat_input(ctx, client, "", session_id="chat-back")
    assert client.requests[-1]["missionos_route_hint"] == "approve"
    assert missionos_cli._chat_suggestion(ctx) == {"raw": "/run", "label": "prepare"}
    assert (
        missionos_cli._stored_mission_designer_context(ctx, "chat-back")[
            "mission_designer_context_sha256"
        ]
        == "sha-approved"
    )

    assert missionos_cli._handle_chat_input(ctx, client, "/back", session_id="chat-back")
    assert missionos_cli._chat_suggestion(ctx) == {"raw": "/approve", "label": "approve"}
    assert (
        missionos_cli._stored_mission_designer_context(ctx, "chat-back")[
            "mission_designer_context_sha256"
        ]
        == "sha-plan"
    )

    assert missionos_cli._handle_chat_input(ctx, client, "戻る", session_id="chat-back")
    assert missionos_cli._chat_suggestion(ctx) == {}
    assert missionos_cli._stored_mission_designer_context(ctx, "chat-back") == {}


def test_chat_back_does_not_cross_start_sitl_boundary(tmp_path: Path) -> None:
    client = BackNavigationMissionOSClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._save_state(
        tmp_path / "state.json",
        {
            "session_id": "chat-back",
            "missionos_gateway_url": "http://127.0.0.1:18881",
            "mission_designer_context": {
                "mission_designer_context_ref": "mission_designer_context:prepared",
                "mission_designer_context_sha256": "sha-prepared",
                "mission_designer_context_session_id": "chat-back",
            },
            "sitl_execution_task_id": "task_prepare",
        },
    )
    missionos_cli._set_chat_suggestion(ctx, raw="/start-sitl", label="start")
    missionos_cli._push_chat_back_state(ctx)

    assert missionos_cli._handle_chat_input(ctx, client, "", session_id="chat-back")
    assert client.started == ["task_prepare"]
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/execute-sitl task_prepare",
        "label": "fly",
    }

    assert missionos_cli._handle_chat_input(ctx, client, "/back", session_id="chat-back")
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/execute-sitl task_prepare",
        "label": "fly",
    }
    assert missionos_cli._stored_sitl_task_id(ctx) == "task_prepare"


def test_chat_execute_sitl_launches_companion_terminals(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)
    missionos_cli._remember_sitl_task_id(ctx, "task_fly")
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    launched: list[str] = []

    def fake_execute_sitl(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], None, None]:
        return (
            {
                "summary": {
                    "task_id": "task_fly",
                    "task_status": "running",
                    "upload_status": "uploaded",
                    "live_flight_status": "started",
                    "dropoff_verified": False,
                    "delivery_completion_claimed": False,
                    "physical_execution_invoked": False,
                }
            },
            None,
            None,
        )

    monkeypatch.setattr(missionos_cli, "_execute_sitl_with_task_polling", fake_execute_sitl)
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    assert missionos_cli._handle_chat_input(ctx, client, "fly", session_id="chat-fly")
    assert launched == ["task_fly"]
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_fly",
        "label": "show status",
    }


def test_chat_map_reuses_authenticated_live_companion_instead_of_snapshot(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    client = RecordingMissionOSClient()
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    ctx.obj["missionos_client"] = client
    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    missionos_cli._remember_sitl_task_id(ctx, "task_turtlebot3_live_map")
    launched: list[str] = []
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    assert missionos_cli._handle_chat_input(
        ctx,
        client,
        "/map",
        session_id="chat-live-map",
    )
    assert launched == ["task_turtlebot3_live_map"]
    assert missionos_cli._chat_suggestion(ctx) == {
        "raw": "/job-status task_turtlebot3_live_map",
        "label": "show status",
    }


def test_chat_companion_terminals_prepare_three_managed_scripts(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_session_id"] = "session/with spaces"
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    ctx.obj["missionos_client"] = missionos_cli.MissionOSGatewayClient(
        base_url="http://127.0.0.1:18792",
        api_key="ephemeral-test-key",
    )
    companion_root = tmp_path / "companions"
    fake_entrypoint = tmp_path / "missionos"
    fake_entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_entrypoint.chmod(0o755)
    launched_scripts: list[Path] = []

    monkeypatch.setattr(missionos_cli, "CHAT_COMPANION_TERMINAL_ROOT", companion_root)
    monkeypatch.setattr(missionos_cli.sys, "argv", [str(fake_entrypoint)])
    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_launch_macos_terminal_script",
        lambda script_path, *, title: launched_scripts.append(script_path) or bool(title),
    )
    monkeypatch.setattr(missionos_cli, "_close_macos_companion_terminal_titles", lambda _titles: None)
    monkeypatch.setattr(missionos_cli.time, "sleep", lambda _seconds: None)

    missionos_cli._ensure_chat_companion_terminals(ctx, "task_fly")

    assert [path.name for path in launched_scripts] == ["operate.sh", "watch.sh", "map.sh"]
    assert all(path.is_absolute() for path in launched_scripts)
    state = ctx.obj["missionos_chat_companion_terminals"]
    assert state["task_id"] == "task_fly"
    assert state["launched"] == ["operate", "watch", "map"]
    scripts = {path.name: path.read_text(encoding="utf-8") for path in launched_scripts}
    assert "operate --task-id task_fly" in scripts["operate.sh"]
    assert "--timeout 45.0" in scripts["operate.sh"]
    assert "watch --task-id task_fly" in scripts["watch.sh"]
    assert "map --task-id task_fly" in scripts["map.sh"]
    assert "map --task-id task_fly --serve-live" in scripts["map.sh"]
    assert "Waiting for missionos chat to close" in scripts["map.sh"]
    api_key_path = Path(state["gateway_api_key_path"])
    assert api_key_path.read_text(encoding="utf-8") == "ephemeral-test-key"
    assert api_key_path.stat().st_mode & 0o777 == 0o600
    assert all("ephemeral-test-key" not in script for script in scripts.values())
    assert all("GATEWAY_API_KEY_PATH=" in script for script in scripts.values())

    stop_path = Path(state["stop_path"])
    assert not stop_path.exists()
    missionos_cli._stop_chat_companion_terminals(ctx)
    assert stop_path.exists()
    assert not api_key_path.exists()
    assert "missionos_chat_companion_terminals" not in ctx.obj


def test_chat_companion_prefix_preserves_python_module_invocation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    module_entrypoint = (
        Path(missionos_cli.__file__).resolve().parent / "__main__.py"
    )
    assert module_entrypoint.is_file()
    assert not (module_entrypoint.stat().st_mode & 0o111)
    monkeypatch.setattr(missionos_cli.sys, "argv", [str(module_entrypoint)])

    prefix = missionos_cli._missionos_chat_companion_command_prefix(ctx)

    assert prefix.startswith(
        f"{missionos_cli.sys.executable} -m missionos_cli "
    )
    assert str(module_entrypoint) not in prefix
    assert "--gateway-url http://127.0.0.1:18881" in prefix


def test_turtlebot3_run_opens_companions_while_conversation_is_in_flight(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    launched: list[str] = []

    class InFlightTaskClient:
        list_count = 0

        def get(self, _path: str) -> dict[str, Any]:
            self.list_count += 1
            if self.list_count == 1:
                return {"items": []}
            return {
                "items": [
                    {
                        "task_id": "task_turtlebot3_in_flight",
                        "status": "running",
                        "artifacts": {
                            "summary": {
                                "execution_target": "ros2_nav2_turtlebot3_sim"
                            }
                        },
                    }
                ]
            }

    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    def slow_conversation() -> dict[str, Any]:
        time.sleep(0.7)
        return {"operation_result": {}}

    payload = missionos_cli._run_turtlebot3_conversation_with_companion_monitor(
        ctx,
        InFlightTaskClient(),  # type: ignore[arg-type]
        slow_conversation,
    )

    assert payload == {"operation_result": {}}
    assert launched == ["task_turtlebot3_in_flight"]
    assert missionos_cli._stored_sitl_task_id(ctx) == "task_turtlebot3_in_flight"


def test_turtlebot3_run_does_not_rebind_an_existing_home_robot_task(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    launched: list[str] = []

    class ExistingTaskClient:
        def get(self, _path: str) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "task_id": "task_existing_turtlebot3",
                        "status": "running",
                        "artifacts": {
                            "summary": {
                                "execution_target": "ros2_nav2_turtlebot3_sim"
                            }
                        },
                    }
                ]
            }

    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    def slow_conversation() -> dict[str, Any]:
        time.sleep(0.7)
        return {"operation_result": {}}

    payload = missionos_cli._run_turtlebot3_conversation_with_companion_monitor(
        ctx,
        ExistingTaskClient(),  # type: ignore[arg-type]
        slow_conversation,
    )

    assert payload == {"operation_result": {}}
    assert launched == []
    assert missionos_cli._stored_sitl_task_id(ctx) == ""


def test_turtlebot3_run_does_not_guess_between_concurrent_new_tasks(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    launched: list[str] = []

    class ConcurrentTaskClient:
        list_count = 0

        def get(self, _path: str) -> dict[str, Any]:
            self.list_count += 1
            if self.list_count == 1:
                return {"items": []}
            return {
                "items": [
                    {
                        "task_id": task_id,
                        "status": "running",
                        "artifacts": {
                            "summary": {
                                "execution_target": "ros2_nav2_turtlebot3_sim"
                            }
                        },
                    }
                    for task_id in ("task_concurrent_a", "task_concurrent_b")
                ]
            }

    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    def slow_conversation() -> dict[str, Any]:
        time.sleep(0.7)
        return {
            "operation_result": {
                "task_id": "task_concurrent_b",
                "summary": {"status": "running"},
            }
        }

    payload = missionos_cli._run_turtlebot3_conversation_with_companion_monitor(
        ctx,
        ConcurrentTaskClient(),  # type: ignore[arg-type]
        slow_conversation,
    )

    assert payload["operation_result"]["task_id"] == "task_concurrent_b"
    assert launched == []
    assert missionos_cli._stored_sitl_task_id(ctx) == ""


def test_turtlebot3_run_disables_early_binding_when_baseline_listing_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    launched: list[str] = []

    class BaselineFailureClient:
        list_count = 0

        def get(self, _path: str) -> dict[str, Any]:
            self.list_count += 1
            if self.list_count == 1:
                raise click.ClickException("temporary task listing failure")
            return {
                "items": [
                    {
                        "task_id": "task_preexisting",
                        "status": "running",
                        "artifacts": {
                            "summary": {
                                "execution_target": "ros2_nav2_turtlebot3_sim"
                            }
                        },
                    }
                ]
            }

    client = BaselineFailureClient()
    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    payload = missionos_cli._run_turtlebot3_conversation_with_companion_monitor(
        ctx,
        client,  # type: ignore[arg-type]
        lambda: {
            "operation_result": {
                "task_id": "task_exact_response",
                "summary": {
                    "task_id": "task_exact_response",
                    "status": "running",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                },
            }
        },
    )

    assert client.list_count == 1
    assert launched == []
    missionos_cli._maybe_open_turtlebot3_companion_terminals(ctx, payload)
    assert launched == ["task_exact_response"]


def test_home_robot_task_listing_distinguishes_invalid_payload_from_empty() -> None:
    class InvalidListingClient:
        def get(self, _path: str) -> dict[str, Any]:
            return {}

    class EmptyListingClient:
        def get(self, _path: str) -> dict[str, Any]:
            return {"items": []}

    assert (
        missionos_cli._listed_home_robot_task_ids(
            InvalidListingClient(),  # type: ignore[arg-type]
        )
        is None
    )
    assert missionos_cli._listed_home_robot_task_ids(
        EmptyListingClient(),  # type: ignore[arg-type]
    ) == set()


def test_turtlebot3_run_stops_listing_after_one_task_is_bound(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
    launched: list[str] = []

    class ChangingTaskWindowClient:
        list_count = 0

        def get(self, _path: str) -> dict[str, Any]:
            self.list_count += 1
            if self.list_count == 1:
                return {"items": []}
            task_ids = ["task_bound"]
            if self.list_count > 2:
                task_ids = ["task_later_window_entry"]
            return {
                "items": [
                    {
                        "task_id": task_id,
                        "status": "running",
                        "artifacts": {
                            "summary": {
                                "execution_target": "ros2_nav2_turtlebot3_sim"
                            }
                        },
                    }
                    for task_id in task_ids
                ]
            }

    client = ChangingTaskWindowClient()
    monkeypatch.setattr(
        missionos_cli,
        "_chat_companion_terminals_enabled",
        lambda _ctx: True,
    )
    monkeypatch.setattr(
        missionos_cli,
        "_ensure_chat_companion_terminals",
        lambda _ctx, task_id: launched.append(task_id),
    )

    def slow_conversation() -> dict[str, Any]:
        time.sleep(1.2)
        return {"operation_result": {"task_id": "task_bound"}}

    missionos_cli._run_turtlebot3_conversation_with_companion_monitor(
        ctx,
        client,  # type: ignore[arg-type]
        slow_conversation,
    )

    assert client.list_count == 2
    assert launched == ["task_bound"]


def test_turtlebot3_chat_task_monitor_prints_durable_terminal_update(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    terminal_payload = {
        "task": {
            "task_id": "task_chat_final_update",
            "status": "completed",
            "artifacts": {
                "summary": {
                    "status": "completed",
                    "completion_claimed": True,
                    "segment_completion_count": 6,
                    "planned_segment_count": 6,
                    "recovery_goal_status": "succeeded",
                    "recovery_verification_status": "verified",
                    "route_resume_status": "resumed",
                    "physical_execution_invoked": False,
                    "mission_delivery_completion_claimed": False,
                    "blocking_reasons": [],
                }
            },
        }
    }

    class TerminalTaskClient:
        def get(self, path: str) -> dict[str, Any]:
            assert path == "/tasks/task_chat_final_update"
            return copy.deepcopy(terminal_payload)

    printed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        missionos_cli,
        "_print_turtlebot3_chat_task_terminal_update",
        lambda payload: printed.append(payload),
    )

    missionos_cli._start_turtlebot3_chat_task_status_monitor(
        ctx,
        TerminalTaskClient(),  # type: ignore[arg-type]
        task_id="task_chat_final_update",
    )
    state = ctx.obj["missionos_turtlebot3_chat_task_status_monitor"]
    state["thread"].join(timeout=1.0)

    assert printed == [terminal_payload]
    assert state["thread"].is_alive() is False
    missionos_cli._stop_turtlebot3_chat_task_status_monitor(ctx)


def test_turtlebot3_chat_terminal_update_replaces_pending_truth_in_transcript(
    capsys: Any,
) -> None:
    missionos_cli._print_turtlebot3_chat_task_terminal_update(
        {
            "task": {
                "task_id": "task_chat_final_update",
                "status": "completed",
                "artifacts": {
                    "summary": {
                        "completion_claimed": True,
                        "segment_completion_count": 6,
                        "planned_segment_count": 6,
                        "recovery_goal_status": "succeeded",
                        "recovery_verification_status": "verified",
                        "route_resume_status": "resumed",
                        "physical_execution_invoked": False,
                        "mission_delivery_completion_claimed": False,
                    }
                },
            }
        }
    )

    rendered = capsys.readouterr().out
    assert "MissionOS task final update" in rendered
    assert "task_id=task_chat_final_update" in rendered
    assert "operation_status=completed" in rendered
    assert "recovery_goal=succeeded; verification=verified; route=resumed" in rendered
    assert "segments=6/6; completion_claimed=True" in rendered
    assert "physical_execution_invoked=False" in rendered


def test_pending_turtlebot3_conversation_starts_task_status_monitor(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    started: list[str] = []
    monkeypatch.setattr(
        missionos_cli,
        "_start_turtlebot3_chat_task_status_monitor",
        lambda _ctx, _client, *, task_id: started.append(task_id),
    )

    missionos_cli._maybe_start_turtlebot3_chat_task_status_monitor(
        ctx,
        RecordingMissionOSClient(),
        {
            "operation_result": {
                "summary": {
                    "status": "pending",
                    "task_id": "task_chat_pending",
                }
            }
        },
    )

    assert started == ["task_chat_pending"]


def test_terminal_turtlebot3_conversation_does_not_start_task_status_monitor(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    started: list[str] = []
    monkeypatch.setattr(
        missionos_cli,
        "_start_turtlebot3_chat_task_status_monitor",
        lambda _ctx, _client, *, task_id: started.append(task_id),
    )

    missionos_cli._maybe_start_turtlebot3_chat_task_status_monitor(
        ctx,
        RecordingMissionOSClient(),
        {
            "operation_result": {
                "summary": {
                    "status": "completed",
                    "task_id": "task_chat_completed",
                }
            }
        },
    )

    assert started == []


def test_chat_plan_without_source_bound_context_does_not_offer_approval(tmp_path: Path) -> None:
    ctx = click.Context(missionos_cli.missionos)
    ctx.obj = {
        "missionos_state_path": tmp_path / "state.json",
    }

    missionos_cli._update_chat_suggestion_from_conversation(
        ctx,
        {
            "schema_version": "missionos_autonomy_conversation_response.v1",
            "routed_action": "plan",
            "message": "I asked the planner for a bounded plan from your instruction.",
            "operation_result": {},
            "progress_counted": False,
        },
    )

    assert missionos_cli._chat_suggestion(ctx) == {}


def test_chat_mission_designer_plan_with_source_bound_context_offers_approval(
    tmp_path: Path,
) -> None:
    ctx = click.Context(missionos_cli.missionos)
    ctx.obj = {
        "missionos_state_path": tmp_path / "state.json",
    }

    missionos_cli._update_chat_suggestion_from_conversation(
        ctx,
        {
            "schema_version": "missionos_autonomy_conversation_response.v1",
            "routed_action": "mission_designer_plan",
            "message": "I built a bounded PX4/Gazebo mission proposal.",
            "mission_designer": {
                "mission_designer_context_ref": "mission_designer_context:test",
                "mission_designer_context_sha256": "sha",
                "scenario_proposal": {"proposal_id": "proposal_1"},
                "validation_result": {"validation_status": "passed"},
                "summary": {},
            },
            "progress_counted": False,
        },
    )

    assert missionos_cli._chat_suggestion(ctx) == {"raw": "/approve", "label": "approve"}
