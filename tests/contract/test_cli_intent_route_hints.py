from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner

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
    ) -> dict[str, Any]:
        self.requests.append(
            {
                "task_id": task_id,
                "recovery_action": recovery_action,
                "recovery_parameters": recovery_parameters or {},
            }
        )
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


class PendingRecoveryApprovalClient(RecordingMissionOSClient):
    def __init__(self) -> None:
        super().__init__()
        self.task_id = "task_pending_recovery"

    def _task_payload(self) -> dict[str, Any]:
        recovery_proposal = {
            "proposal_source": "llm",
            "selected_action": "avoid_obstacle",
            "input_observations": {
                "runtime_obstacle_observed": True,
                "recommended_avoidance_target_x_m": -0.2,
                "recommended_avoidance_target_y_m": -1.4,
            },
        }
        recovery_classification = {
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
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "runtime_recovery_triggered": True,
            "runtime_recovery_action_kind": "avoid_obstacle",
            "recovery_dispatch_request_sent": False,
            "recovery_proposals": [recovery_proposal],
            "recovery_proposal_classifications": [recovery_classification],
        }
        artifacts = {
            "summary": summary,
            "turtlebot3_recovery_decision_summary": decision_summary,
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": "/tmp/recovery_request.json"
            },
        }
        return {
            "task": {
                "task_id": self.task_id,
                "status": "running",
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
    monkeypatch.setattr(missionos_cli, "_gateway_reachable", lambda _client: True)

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
    assert "stacle_delivery_docker.sh" in result.output


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
        },
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
    }


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


def test_chat_companion_terminals_prepare_three_managed_scripts(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    ctx = _chat_ctx(tmp_path)
    ctx.obj["missionos_chat_session_id"] = "session/with spaces"
    ctx.obj["missionos_chat_companion_terminals_enabled"] = True
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
    assert "watch --task-id task_fly" in scripts["watch.sh"]
    assert "map --task-id task_fly" in scripts["map.sh"]
    assert "Waiting for missionos chat to close" in scripts["map.sh"]

    stop_path = Path(state["stop_path"])
    assert not stop_path.exists()
    missionos_cli._stop_chat_companion_terminals(ctx)
    assert stop_path.exists()
    assert "missionos_chat_companion_terminals" not in ctx.obj


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
