import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import types

import pytest

from missionos_cli import cli as missionos_cli

GATEWAY_LLM_ADK_ENV_KEYS = (
    "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED",
    "MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED",
    "MISSIONOS_LLM_DIALOGUE_ROUTER_ADK_ENABLED",
    "MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED",
    "MISSIONOS_LLM_RESPONSE_PLANNER_ADK_ENABLED",
    "MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED",
)


class _FixtureHealthClient:
    def health(self) -> dict:
        return {
            "status": "ok",
            "session_backend": "fixture",
            "version": "missionos-gateway-fixture.v1",
        }


@pytest.fixture
def isolated_gateway_factory(
    monkeypatch,
    tmp_path,
) -> Callable[..., object]:
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    def factory(
        *,
        api_key: str = "",
        cors_origins: str = "",
        legacy_agent_routes_enabled: bool = False,
    ) -> object:
        monkeypatch.setenv("GATEWAY_API_KEY", api_key)
        monkeypatch.setenv("GATEWAY_CORS_ALLOWED_ORIGINS", cors_origins)
        monkeypatch.setenv(
            "GATEWAY_LEGACY_AGENT_ROUTES_ENABLED",
            "1" if legacy_agent_routes_enabled else "0",
        )
        reset_settings()
        reset_task_store()
        audit._audit_logger = None

        from src.gateway.server import create_gateway

        return create_gateway()

    yield factory
    reset_task_store()
    reset_settings()
    audit._audit_logger = None


def test_live_sitl_gateway_env_selects_production_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSIONOS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("BOILED_CLAW_LLM_BACKEND", raising=False)
    env = missionos_cli._gateway_process_env(enable_live_sitl=True)

    assert env["MISSIONOS_GATEWAY_BACKEND"] == "production"
    assert env["MISSIONOS_LLM_BACKEND"] == "gemini"
    for key in GATEWAY_LLM_ADK_ENV_KEYS:
        assert env[key] == "1"
    assert "GOOGLE_API_KEY" not in env
    assert env["RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION"] == "1"
    assert env["RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT"] == "1"
    assert env["RUN_MISSIONOS_SITL_DISPATCH_RUNTIME"] == "1"


def test_planning_gateway_env_keeps_fixture_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSIONOS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("BOILED_CLAW_LLM_BACKEND", raising=False)
    env = missionos_cli._gateway_process_env(enable_live_sitl=False)

    assert env["MISSIONOS_LLM_BACKEND"] == "gemini"
    for key in GATEWAY_LLM_ADK_ENV_KEYS:
        assert env[key] == "1"
    assert "GOOGLE_API_KEY" not in env
    assert "MISSIONOS_GATEWAY_BACKEND" not in env
    assert "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION" not in env


def test_turtlebot3_recovery_parameters_allow_bounded_arrival_yaw() -> None:
    from src.gateway.server import _bounded_turtlebot3_operator_recovery_parameters

    bounded = _bounded_turtlebot3_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": {
                "target_x_m": 0.2,
                "target_y_m": -2.11,
                "target_yaw_rad": -0.715745,
                "obstacle_avoidance_required": True,
            }
        },
    )

    assert bounded == {
        "target_x_m": 0.2,
        "target_y_m": -2.11,
        "target_yaw_rad": -0.715745,
        "obstacle_avoidance_required": True,
    }


def test_turtlebot3_recovery_parameters_allow_one_source_bound_route_retry() -> None:
    from src.gateway.server import (
        MISSIONOS_TURTLEBOT3_RUNTIME_RECOVERY_ACTIONS,
        _bounded_turtlebot3_operator_recovery_parameters,
    )

    bounded = _bounded_turtlebot3_operator_recovery_parameters(
        recovery_action="reroute",
        body={
            "recovery_parameters": {
                "target_x_m": -0.55,
                "target_y_m": -1.55,
                "retry_failed_segment_required": True,
                "retry_count": 1,
            }
        },
    )

    assert bounded == {
        "target_x_m": -0.55,
        "target_y_m": -1.55,
        "retry_failed_segment_required": True,
        "retry_count": 1,
    }
    assert MISSIONOS_TURTLEBOT3_RUNTIME_RECOVERY_ACTIONS == {
        "avoid_obstacle",
        "reroute",
        "return_home",
    }


def test_gateway_env_loads_dotenv_backend_without_export(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join([
            "GOOGLE_API_KEY=must-not-propagate",
            "MISSIONOS_LLM_BACKEND=ollama",
            "MISSIONOS_OLLAMA_MODEL=gemma4:26b",
            "MISSIONOS_CHIEF_ROUTE_SEMANTIC_TIMEOUT_SECONDS=240",
            "MISSIONOS_AGENT_RUNTIME_TIMEOUT_SECONDS=240",
        ]),
        encoding="utf-8",
    )

    env = missionos_cli._gateway_process_env(enable_live_sitl=False)

    assert env["MISSIONOS_LLM_BACKEND"] == "ollama"
    assert env["MISSIONOS_OLLAMA_MODEL"] == "gemma4:26b"
    assert env["MISSIONOS_CHIEF_ROUTE_SEMANTIC_TIMEOUT_SECONDS"] == "240"
    assert env["MISSIONOS_AGENT_RUNTIME_TIMEOUT_SECONDS"] == "240"
    assert "GOOGLE_API_KEY" not in env


def test_gateway_env_respects_explicit_adk_disable(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", "0")
    monkeypatch.setenv("MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED", "0")

    env = missionos_cli._gateway_process_env(enable_live_sitl=False)

    for key in GATEWAY_LLM_ADK_ENV_KEYS:
        if key in {
            "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED",
            "MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED",
        }:
            assert env[key] == "0"


def test_gateway_env_can_disable_llm_backend(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "off")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-must-not-propagate")

    env = missionos_cli._gateway_process_env(enable_live_sitl=False)

    for key in GATEWAY_LLM_ADK_ENV_KEYS:
        assert env[key] == "0"
    assert "GOOGLE_API_KEY" not in env


def test_default_model_backend_uses_gemini(monkeypatch) -> None:
    from src.agents import model_config

    monkeypatch.delenv("MISSIONOS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("BOILED_CLAW_LLM_BACKEND", raising=False)

    assert model_config.llm_backend_disabled() is False
    assert model_config.local_llm_backend_enabled() is False
    assert model_config.google_llm_backend_enabled() is True


def test_default_agent_model_uses_stable_gemini_id(monkeypatch) -> None:
    from src.config.settings import Settings

    monkeypatch.delenv("AGENT_MODEL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.agent_model == "gemini-3.1-flash-lite"


def test_stable_gemini_planners_normalize_vertex_location(monkeypatch) -> None:
    from src.config.settings import reset_settings
    from src.intelligence import (
        llm_dialogue_router,
        llm_repair_planner,
        llm_response_planner,
        real_hardware_arm_disarm_planner,
        turtlebot3_recovery_planner,
    )

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "gemini")
    monkeypatch.setenv("AGENT_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")

    for planner in (
        llm_dialogue_router,
        llm_repair_planner,
        llm_response_planner,
        real_hardware_arm_disarm_planner,
        turtlebot3_recovery_planner,
    ):
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        reset_settings()

        planner._configure_google_adk_environment()

        assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


def test_gateway_env_ollama_backend_keeps_adk_but_removes_google_key(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("MISSIONOS_OLLAMA_MODEL", "gemma4:26b")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-must-not-propagate")

    env = missionos_cli._gateway_process_env(enable_live_sitl=True)

    for key in GATEWAY_LLM_ADK_ENV_KEYS:
        assert env[key] == "1"
    assert env["MISSIONOS_LLM_BACKEND"] == "ollama"
    assert env["MISSIONOS_OLLAMA_MODEL"] == "gemma4:26b"
    assert "GOOGLE_API_KEY" not in env


def test_gateway_env_deepseek_backend_keeps_only_deepseek_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-test-key-must-not-propagate")

    env = missionos_cli._gateway_process_env(enable_live_sitl=False)

    for key in GATEWAY_LLM_ADK_ENV_KEYS:
        assert env[key] == "1"
    assert env["MISSIONOS_LLM_BACKEND"] == "deepseek"
    assert env["DEEPSEEK_API_KEY"] == "deepseek-test-key"
    assert "GOOGLE_API_KEY" not in env


def test_gateway_env_non_deepseek_backend_removes_deepseek_key(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "off")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key-must-not-propagate")

    env = missionos_cli._gateway_process_env(enable_live_sitl=False)

    assert "DEEPSEEK_API_KEY" not in env


def test_ollama_backend_uses_local_model_label(monkeypatch) -> None:
    from src.agents import model_config

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("MISSIONOS_OLLAMA_MODEL", "gemma4:26b")

    assert model_config.local_llm_backend_enabled() is True
    assert model_config.agent_model_label() == "ollama_chat/gemma4:26b"


def test_deepseek_backend_uses_official_model_and_litellm_adapter(monkeypatch) -> None:
    from src.agents import model_config

    class FakeLiteLlm:
        def __init__(self, model: str, **kwargs: object) -> None:
            self.model = model
            self._additional_args = kwargs

    fake_module = types.ModuleType("google.adk.models.lite_llm")
    fake_module.LiteLlm = FakeLiteLlm
    monkeypatch.setitem(sys.modules, "google.adk.models.lite_llm", fake_module)

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "deepseek")
    monkeypatch.setenv("MISSIONOS_DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("MISSIONOS_DEEPSEEK_API_BASE", "https://api.deepseek.com/")

    resolved = model_config.resolve_agent_model()

    assert model_config.local_llm_backend_enabled() is False
    assert model_config.litellm_backend_enabled() is True
    assert model_config.deepseek_llm_backend_enabled() is True
    assert model_config.agent_model_label() == "deepseek-v4-flash"
    assert model_config.llm_provider_label() == "google_adk_litellm_deepseek"
    assert resolved.model == "deepseek/deepseek-v4-flash"
    assert resolved._additional_args["api_base"] == "https://api.deepseek.com"
    assert resolved._additional_args["thinking"] == {"type": "disabled"}


def test_deepseek_backend_requires_its_own_key(monkeypatch) -> None:
    from src.intelligence import missionos_agent_runtime

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key-does-not-count")

    assert missionos_agent_runtime._adk_llm_credentials_available() is False
    assert (
        missionos_agent_runtime._llm_credentials_blocking_reason()
        == "DEEPSEEK_API_KEY_not_configured"
    )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    assert missionos_agent_runtime._adk_llm_credentials_available() is True


def test_agent_specific_model_override(monkeypatch) -> None:
    from src.agents import model_config

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("MISSIONOS_OLLAMA_MODEL", "gemma4:26b")
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_LLM_BACKEND",
        "gemini",
    )
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_MODEL_ID",
        "gemini-test-model",
    )

    assert model_config.agent_model_label() == "ollama_chat/gemma4:26b"
    assert (
        model_config.agent_model_label(agent_name="missionos_runtime_recovery_agent")
        == "gemini-test-model"
    )
    assert (
        model_config.llm_provider_label(agent_name="missionos_runtime_recovery_agent")
        == "google_adk_gemini"
    )


def test_local_llm_backend_does_not_require_google_api_key(monkeypatch) -> None:
    from src.intelligence import missionos_agent_runtime

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    assert missionos_agent_runtime._google_adk_credentials_available() is True


def test_copied_production_gateway_imports() -> None:
    from src.gateway.server import create_gateway

    gateway = create_gateway()

    assert gateway.app is not None


def test_production_gateway_does_not_serve_legacy_control_ui() -> None:
    from fastapi.testclient import TestClient

    from src.gateway.server import create_gateway

    gateway = create_gateway()
    client = TestClient(gateway.app)

    assert client.get("/chat").status_code == 404
    assert client.get("/chat-static/index.html").status_code == 404


def test_task_get_exposes_read_only_turtlebot3_live_telemetry(
    isolated_gateway_factory,
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    telemetry_path = tmp_path / "turtlebot3-live.jsonl"
    telemetry_path.write_text(
        json.dumps(
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "task_id": "task_turtlebot3_live_telemetry",
                "captured_at": "2026-07-12T01:37:46+00:00",
                "frame_id": "odom",
                "child_frame_id": "base_footprint",
                "position": {"x_m": 0.75, "y_m": 1.25},
                "twist": {
                    "linear_x_mps": 0.12,
                    "linear_y_mps": 0.0,
                    "angular_z_radps": 0.2,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL",
        str(telemetry_path),
    )
    gateway = isolated_gateway_factory()
    gateway.task_store.create(
        task_id="task_turtlebot3_live_telemetry",
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 live telemetry",
        status="running",
        artifacts={"summary": {"status": "running"}},
    )

    response = TestClient(gateway.app).get(
        "/tasks/task_turtlebot3_live_telemetry"
    )

    assert response.status_code == 200
    live = response.json()["task"]["artifacts"]["turtlebot3_live_telemetry"]
    assert live["task_id"] == "task_turtlebot3_live_telemetry"
    assert live["raw_odom_position"] == {"x_m": 0.75, "y_m": 1.25}
    assert live["twist"]["linear_x_mps"] == 0.12
    assert live["display_only"] is True
    assert live["dispatch_authority_created"] is False
    assert live["completion_claimed"] is False


def test_task_get_does_not_attach_other_turtlebot3_task_telemetry(
    isolated_gateway_factory,
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    telemetry_path = tmp_path / "turtlebot3-live-other-task.jsonl"
    telemetry_path.write_text(
        json.dumps(
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "task_id": "task_new_robot_run",
                "captured_at": "2026-07-12T01:37:46+00:00",
                "frame_id": "odom",
                "position": {"x_m": 0.75, "y_m": 1.25},
                "twist": {"linear_x_mps": 0.12},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL",
        str(telemetry_path),
    )
    gateway = isolated_gateway_factory()
    gateway.task_store.create(
        task_id="task_old_robot_run",
        kind="turtlebot3_home_mission_execution",
        title="Old TurtleBot3 task",
        status="running",
        artifacts={"summary": {"status": "running"}},
    )

    response = TestClient(gateway.app).get("/tasks/task_old_robot_run")

    assert response.status_code == 200
    assert "turtlebot3_live_telemetry" not in response.json()["task"]["artifacts"]


def test_production_gateway_rejects_unlisted_browser_origin(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory()
    client = TestClient(gateway.app)

    response = client.get(
        "/missionos/current-milestone",
        headers={"Origin": "https://untrusted.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Browser origin is not allowed"


def test_production_gateway_requires_key_for_configured_browser_origin(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory(cors_origins="https://operator.example")
    client = TestClient(gateway.app)

    response = client.get(
        "/missionos/current-milestone",
        headers={"Origin": "https://operator.example"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Gateway API key is required for browser requests"


def test_production_gateway_accepts_configured_origin_with_api_key(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory(
        api_key="test-gateway-key",
        cors_origins="https://operator.example",
    )
    client = TestClient(gateway.app)

    response = client.get(
        "/missionos/current-milestone",
        headers={
            "Origin": "https://operator.example",
            "X-API-Key": "test-gateway-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://operator.example"


def test_production_gateway_hides_legacy_general_agent_routes(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory()
    client = TestClient(gateway.app)

    assert client.post("/agent/run", json={"text": "run shell"}).status_code == 404
    assert client.get("/tools/policy").status_code == 404

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/operator"):
            pass
    assert exc_info.value.code == 4404


def test_legacy_agent_websocket_requires_allowed_origin_and_key(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    gateway = isolated_gateway_factory(
        api_key="test-gateway-key",
        cors_origins="https://operator.example",
        legacy_agent_routes_enabled=True,
    )
    client = TestClient(gateway.app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/operator?token=test-gateway-key",
            headers={"Origin": "https://untrusted.example"},
        ):
            pass
    assert exc_info.value.code == 4403

    with client.websocket_connect(
        "/ws/operator?token=test-gateway-key",
        headers={"Origin": "https://operator.example"},
    ) as websocket:
        connected = websocket.receive_json()

    assert connected["event"] == "connected"


def test_execute_sitl_boolean_cannot_replace_stored_authority(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory()
    gateway.task_store.create(
        task_id="task_missing_execution_authority",
        kind="px4_gazebo_mission_designer_sitl_execution",
        title="Missing stored execution authority",
        status="pending",
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/execute-sitl",
        json={
            "task_id": "task_missing_execution_authority",
            "explicit_execution_approval": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "execution_approval_id is required"

    approval_response = client.post(
        "/px4-gazebo/mission-scenarios/approve-sitl-execution",
        json={
            "task_id": "task_missing_execution_authority",
            "explicit_execution_approval": True,
        },
    )

    assert approval_response.status_code == 409
    assert approval_response.json()["detail"] == (
        "stored SITL execution request is required"
    )


def _pending_turtlebot3_recovery_task_artifacts() -> dict:
    from src.runtime.turtlebot3_home_mission import (
        _planned_segment_goals_from_proposal,
        _planned_segments_sha256,
        _recovery_checkpoint_hash,
    )

    planned_segments = [
        {
            "frame_id": "map",
            "x_m": 0.0,
            "y_m": 0.0,
            "yaw_rad": 0.0,
            "label": "completed_test_segment",
        },
        {
            "frame_id": "map",
            "x_m": 1.0,
            "y_m": 0.0,
            "yaw_rad": 0.0,
            "label": "remaining_test_segment",
        },
    ]
    planned_segments_sha256 = _planned_segments_sha256(
        _planned_segment_goals_from_proposal(
            {"planned_segments": planned_segments}
        )
    )

    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_status": "awaiting_operator_approval",
        "proposal_id": "turtlebot3_home_test",
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
        "recovery_proposal_id": "recovery_proposal_test",
        "recovery_classification_id": "recovery_classification_test",
        "selected_action": "avoid_obstacle",
        "approved_parameters": {
            "target_x_m": -0.2,
            "target_y_m": -1.4,
            "obstacle_avoidance_required": True,
        },
        "completed_segment_count": 1,
        "next_segment_index": 2,
        "remaining_segment_count": 1,
        "planned_segments_sha256": planned_segments_sha256,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    checkpoint_hash = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    return {
        "turtlebot3_home_mission_plan": {
            "proposal_id": "turtlebot3_home_test",
            "robot_profile": "turtlebot3",
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "planned_segments": planned_segments,
        },
        "turtlebot3_home_mission_approval": {
            "operator_approved": True,
            "operator_approval_ref": "mission_approval:test",
        },
        "turtlebot3_home_mission_execution": {
            "schema_version": "missionos_turtlebot3_home_mission_execution.v1",
            "status": "incomplete",
            "segment_results": [{"completion_claimed": True}],
            "turtlebot3_recovery_checkpoint": checkpoint,
        },
        "turtlebot3_recovery_checkpoint": checkpoint,
        "turtlebot3_recovery_checkpoints": {
            checkpoint["checkpoint_id"]: checkpoint,
        },
        "summary": {
            "status": "incomplete",
            "robot_profile": "turtlebot3",
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "runtime_recovery_triggered": True,
            "recovery_dispatch_request_sent": False,
            "turtlebot3_recovery_checkpoint": checkpoint,
        },
    }


def _pending_nova_carter_recovery_task_artifacts() -> dict:
    from src.runtime.turtlebot3_home_mission import _recovery_checkpoint_hash

    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    proposal = artifacts["turtlebot3_home_mission_plan"]
    proposal["robot_profile"] = "nova_carter"
    proposal["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    checkpoint["robot_profile"] = "nova_carter"
    checkpoint["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    checkpoint_hash = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    artifacts["turtlebot3_recovery_checkpoints"] = {
        checkpoint["checkpoint_id"]: checkpoint
    }
    execution = artifacts["turtlebot3_home_mission_execution"]
    execution["robot_profile"] = "nova_carter"
    execution["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    summary = artifacts["summary"]
    summary["robot_profile"] = "nova_carter"
    summary["execution_target"] = "isaac_ros_nav2_nova_carter_sim"
    return artifacts


def _persist_dispatching_turtlebot3_recovery_task(
    gateway,
    *,
    task_id: str,
    gateway_process_owner_id: str,
) -> dict:
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    created = gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 dispatch crash-window fixture",
        status="pending",
        artifacts=artifacts,
    )
    approval_ref = "turtlebot3_recovery_operator_approval:restart_test"
    dispatch_attempt_id = f"dispatch_attempt_{task_id}"
    dispatching_checkpoint = {
        **checkpoint,
        "checkpoint_status": "dispatching",
        "claimed_at": "2026-07-11T00:00:00+00:00",
        "claimed_by_approval_ref": approval_ref,
    }
    execution = {
        **artifacts["turtlebot3_home_mission_execution"],
        "turtlebot3_recovery_checkpoint": dispatching_checkpoint,
    }
    summary = {
        **artifacts["summary"],
        "turtlebot3_recovery_checkpoint": dispatching_checkpoint,
    }
    dispatch_attempt = {
        "schema_version": "missionos_turtlebot3_recovery_dispatch_attempt.v1",
        "dispatch_attempt_id": dispatch_attempt_id,
        "attempt_status": "dispatching",
        "task_id": task_id,
        "checkpoint_id": checkpoint["checkpoint_id"],
        "checkpoint_hash": checkpoint["checkpoint_hash"],
        "operator_approval_ref": approval_ref,
        "gateway_process_owner_id": gateway_process_owner_id,
        "outcome_known": False,
        "automatic_redispatch_performed": False,
        "started_at": "2026-07-11T00:00:00+00:00",
    }
    claimed = gateway.task_store.claim_nested_artifact(
        task_id,
        collection_key="turtlebot3_recovery_checkpoints",
        artifact_id=checkpoint["checkpoint_id"],
        expected={
            "checkpoint_status": "awaiting_operator_approval",
            "checkpoint_hash": checkpoint["checkpoint_hash"],
        },
        updates={
            "checkpoint_status": "dispatching",
            "claimed_at": dispatching_checkpoint["claimed_at"],
            "claimed_by_approval_ref": approval_ref,
        },
        expected_task_status="pending",
        expected_updated_at=created["updated_at"],
        replace_artifacts={
            "turtlebot3_recovery_checkpoint": dispatching_checkpoint,
            "turtlebot3_home_mission_execution": execution,
            "summary": summary,
            "turtlebot3_recovery_operator_approval": {
                "schema_version": (
                    "missionos_turtlebot3_recovery_operator_approval.v1"
                ),
                "operator_approval_ref": approval_ref,
                "operator_approved": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "checkpoint_hash": checkpoint["checkpoint_hash"],
                "dispatch_attempt_id": dispatch_attempt_id,
            },
            "turtlebot3_recovery_bounded_action": {
                "schema_version": (
                    "missionos_turtlebot3_recovery_bounded_action.v1"
                ),
                "checkpoint_id": checkpoint["checkpoint_id"],
                "checkpoint_hash": checkpoint["checkpoint_hash"],
                "dispatch_attempt_id": dispatch_attempt_id,
            },
            "turtlebot3_recovery_dispatch_attempt": dispatch_attempt,
        },
        metadata={
            "turtlebot3_recovery_lifecycle": "dispatching",
            "turtlebot3_recovery_dispatch_attempt_id": dispatch_attempt_id,
            "turtlebot3_recovery_dispatch_owner_id": (
                gateway_process_owner_id
            ),
        },
        next_task_status="running",
    )
    assert claimed is not None
    return claimed


def _proposed_turtlebot3_recovery_revision(
    *,
    resume_execution: dict,
    proposal: dict,
    operator_instruction: str,
) -> dict:
    from src.runtime.turtlebot3_home_mission import (
        _planned_segment_goals_from_proposal,
        _planned_segments_sha256,
        _recovery_checkpoint_hash,
        _recovery_resume_state_hash,
        _revision_floor_plan_geometry_sha256,
    )

    checkpoint = dict(resume_execution["turtlebot3_recovery_checkpoint"])
    revision_id = "turtlebot3_recovery_revision_test"
    instruction_hash = hashlib.sha256(
        operator_instruction.encode("utf-8")
    ).hexdigest()
    revised_proposal = {
        "schema_version": "missionos_mission_autonomy_recovery_proposal.v1",
        "proposal_id": "recovery_proposal_revised_right",
        "mission_ref": "turtlebot3_home_test",
        "proposal_source": "operator",
        "selected_action": "avoid_obstacle",
        "reason": "Operator requested a wider right-side avoidance candidate.",
        "input_observations": {"operator_requested_avoidance_side": "right"},
        "llm_invocation_evidence": {},
        "llm_judgment_recorded": False,
        "proposal_allowed_to_be_recorded": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    revised_classification = {
        "schema_version": "missionos_mission_autonomy_proposal_classification.v1",
        "classification_id": "recovery_classification_revised_right",
        "envelope_ref": "revision_envelope_test",
        "proposal_ref": revised_proposal["proposal_id"],
        "selected_action": "avoid_obstacle",
        "proposal_recorded": True,
        "proposal_allowed": True,
        "execution_class": "requires_human_approval",
        "execution_permitted_by_envelope": False,
        "requires_new_human_approval": True,
        "blocked_reasons": ["action_requires_new_human_approval"],
        "classification_reason": "Revised maneuver requires a fresh approval.",
        "proposal_first_classification": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    revised_planner_result = {
        "schema_version": "missionos_turtlebot3_recovery_revision_planner_result.v1",
        "planner_status": "proposal_guardrail_passed",
        "revision_id": revision_id,
        "revision_intent": "avoid_right_wide",
        "proposal": revised_proposal,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    floor_plan = {
        "floor_plan_id": "turtlebot3_revision_test.v1",
        "source": "contract_test_floor_plan",
        "bounds": {
            "min_x_m": -3.0,
            "max_x_m": 3.0,
            "min_y_m": -3.0,
            "max_y_m": 3.0,
        },
        "wall_polygon": [],
        "walls": [],
        "furniture": [],
        "pillars": [],
    }
    source_obstacle = {
        "x_m": 0.0,
        "y_m": 0.0,
        "size_x_m": 0.32,
        "size_y_m": 0.32,
        "scene_ref": "contract_test_obstacle",
        "observation_source": "contract_test_costmap",
        "geometry_source": "contract_test_obstacle_geometry",
    }
    revised_execution = {
        **resume_execution,
        "recovery_proposals": [revised_proposal],
        "recovery_proposal_classifications": [revised_classification],
        "recovery_planner_result": revised_planner_result,
        "recovery_action_suggested": "avoid_obstacle",
        "runtime_recovery_action_kind": "avoid_obstacle",
        "recovery_dispatch_request_sent": False,
        "physical_execution_invoked": False,
        "runtime_recovery_obstacle_scenario": {
            "runtime_obstacle_observed": True,
            "runtime_obstacle_x_m": source_obstacle["x_m"],
            "runtime_obstacle_y_m": source_obstacle["y_m"],
            "runtime_obstacle_size_x_m": source_obstacle["size_x_m"],
            "runtime_obstacle_size_y_m": source_obstacle["size_y_m"],
            "runtime_obstacle_scene_ref": source_obstacle["scene_ref"],
            "runtime_obstacle_source": source_obstacle["observation_source"],
            "runtime_obstacle_geometry_source": source_obstacle[
                "geometry_source"
            ],
        },
        "turtlebot3_indoor_map_model": {
            "floor_plan": floor_plan,
            "obstacles": [],
        },
    }
    recovery_goal_poses = [
        {
            "frame_id": "map",
            "x_m": -0.25,
            "y_m": -0.15,
            "yaw_rad": 0.0,
            "tolerance_m": 0.25,
            "max_speed_mps": 0.25,
            "max_distance_m": 3.0,
            "label": "operator_revision_right_wide_avoidance_entry",
        },
        {
            "frame_id": "map",
            "x_m": 0.1,
            "y_m": -0.2,
            "yaw_rad": 0.0,
            "tolerance_m": 0.25,
            "max_speed_mps": 0.25,
            "max_distance_m": 3.0,
            "label": "operator_revision_right_wide_avoidance_exit",
        },
    ]
    revision_geometry = {
        "requested_direction": "right",
        "floor_plan_id": floor_plan["floor_plan_id"],
        "floor_plan_geometry_source": floor_plan["source"],
        "floor_plan_geometry_sha256": (
            _revision_floor_plan_geometry_sha256(floor_plan)
        ),
        "obstacle": source_obstacle,
        "recovery_goal_poses": recovery_goal_poses,
    }
    revised_proposal["input_observations"] = {
        "revision_id": revision_id,
        "revision_intent": "avoid_right_wide",
        "operator_instruction_sha256": instruction_hash,
        "parent_checkpoint_id": checkpoint["checkpoint_id"],
        "parent_checkpoint_hash": checkpoint["checkpoint_hash"],
        "recovery_revision_geometry": revision_geometry,
    }
    revised_checkpoint = {
        **checkpoint,
        "checkpoint_status": "awaiting_operator_approval",
        "parent_checkpoint_id": checkpoint["checkpoint_id"],
        "parent_checkpoint_hash": checkpoint["checkpoint_hash"],
        "recovery_proposal_id": revised_proposal["proposal_id"],
        "recovery_classification_id": revised_classification[
            "classification_id"
        ],
        "revision_id": revision_id,
        "revision_intent": "avoid_right_wide",
        "operator_instruction_sha256": instruction_hash,
        "recovery_revision_geometry": revision_geometry,
        "approved_parameters": {
            "recovery_waypoints": [
                {"target_x_m": -0.25, "target_y_m": -0.15},
                {"target_x_m": 0.1, "target_y_m": -0.2},
            ],
            "obstacle_avoidance_required": True,
        },
        "recovery_goal_poses": recovery_goal_poses,
        "resume_state_hash": _recovery_resume_state_hash(revised_execution),
        "planned_segments_sha256": _planned_segments_sha256(
            _planned_segment_goals_from_proposal(proposal)
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    revised_checkpoint["checkpoint_hash"] = _recovery_checkpoint_hash(
        revised_checkpoint
    )
    revised_checkpoint["checkpoint_id"] = (
        "turtlebot3_recovery_checkpoint_"
        + revised_checkpoint["checkpoint_hash"][:12]
    )
    revision_lineage = {
        "revision_id": revision_id,
        "parent_checkpoint_id": checkpoint["checkpoint_id"],
        "child_checkpoint_id": revised_checkpoint["checkpoint_id"],
        "revision_intent": "avoid_right_wide",
        "operator_approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    revised_execution["turtlebot3_recovery_checkpoint"] = revised_checkpoint
    revised_execution["recovery_checkpoint_revision"] = revision_lineage
    superseded_checkpoint = {
        **checkpoint,
        "checkpoint_status": "superseded",
        "superseded_at": "2026-07-11T00:00:00+00:00",
        "superseded_by_checkpoint_id": revised_checkpoint["checkpoint_id"],
        "superseded_by_checkpoint_hash": revised_checkpoint["checkpoint_hash"],
        "superseded_by_revision_id": revision_id,
    }
    revised_summary = {
        "status": "incomplete",
        "runtime_recovery_triggered": True,
        "runtime_recovery_action_kind": "avoid_obstacle",
        "recovery_action_suggested": "avoid_obstacle",
        "recovery_dispatch_request_sent": False,
        "recovery_completion_claimed": False,
        "route_resumed_after_recovery": False,
        "route_completed_after_recovery": False,
        "fresh_recovery_operator_approval_count": 0,
        "fresh_recovery_operator_approvals": [],
        "recovery_proposals": [revised_proposal],
        "recovery_proposal_classifications": [revised_classification],
        "recovery_planner_result": revised_planner_result,
        "turtlebot3_recovery_checkpoint": revised_checkpoint,
        "recovery_checkpoint_revision": revision_lineage,
        "completion_claimed": False,
        "completion_scope": "none",
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
    }
    return {
        "schema_version": "missionos_turtlebot3_recovery_checkpoint_revision.v1",
        "revision_id": revision_id,
        "revision_status": "proposed",
        "blocking_reasons": [],
        "parent_checkpoint_id": checkpoint["checkpoint_id"],
        "parent_checkpoint_hash": checkpoint["checkpoint_hash"],
        "superseded_checkpoint": superseded_checkpoint,
        "turtlebot3_recovery_checkpoint": revised_checkpoint,
        "turtlebot3_home_mission_execution": revised_execution,
        "summary": revised_summary,
        "recovery_proposal": revised_proposal,
        "recovery_proposal_classification": revised_classification,
        "recovery_planner_result": revised_planner_result,
        "dispatch_authority_created": False,
        "operator_approval_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _rebind_proposed_turtlebot3_recovery_revision(revision: dict) -> None:
    """Recompute child bindings after a malicious fixture mutation."""

    from src.runtime.turtlebot3_home_mission import (
        _recovery_checkpoint_hash,
        _recovery_resume_state_hash,
    )

    execution = revision["turtlebot3_home_mission_execution"]
    checkpoint = revision["turtlebot3_recovery_checkpoint"]
    checkpoint["resume_state_hash"] = _recovery_resume_state_hash(execution)
    checkpoint["checkpoint_hash"] = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_id"] = (
        "turtlebot3_recovery_checkpoint_" + checkpoint["checkpoint_hash"][:12]
    )
    execution["turtlebot3_recovery_checkpoint"] = checkpoint
    execution["recovery_checkpoint_revision"]["child_checkpoint_id"] = checkpoint[
        "checkpoint_id"
    ]
    revision["summary"]["turtlebot3_recovery_checkpoint"] = checkpoint
    revision["summary"]["recovery_checkpoint_revision"][
        "child_checkpoint_id"
    ] = checkpoint["checkpoint_id"]
    revision["superseded_checkpoint"]["superseded_by_checkpoint_id"] = checkpoint[
        "checkpoint_id"
    ]
    revision["superseded_checkpoint"]["superseded_by_checkpoint_hash"] = checkpoint[
        "checkpoint_hash"
    ]


def test_turtlebot3_recovery_revision_atomically_supersedes_checkpoint(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_revise_recovery"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent_checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 pending recovery",
        status="pending",
        artifacts=task_artifacts,
    )
    calls: list[dict] = []

    def fake_revision(**kwargs):
        calls.append(kwargs)
        return _proposed_turtlebot3_recovery_revision(
            resume_execution=kwargs["resume_execution"],
            proposal=kwargs["proposal"],
            operator_instruction=kwargs["operator_instruction"],
        )

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        fake_revision,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って障害物を避けて",
            "expected_recovery_checkpoint_id": parent_checkpoint[
                "checkpoint_id"
            ],
            "expected_recovery_checkpoint_hash": parent_checkpoint[
                "checkpoint_hash"
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["revision_status"] == "revised"
    assert payload["summary"]["checkpoint_unchanged"] is False
    assert payload["summary"]["operator_approval_created"] is False
    assert payload["summary"]["dispatch_authority_created"] is False
    assert payload["summary"]["dispatch_request_sent"] is False
    assert len(calls) == 1
    assert calls[0]["operator_instruction"] == (
        "右に大きく回って障害物を避けて"
    )

    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "pending"
    artifacts = stored["artifacts"]
    current = artifacts["turtlebot3_recovery_checkpoint"]
    assert current["checkpoint_id"] == payload["summary"]["current_checkpoint_id"]
    assert current["checkpoint_id"] == (
        f"turtlebot3_recovery_checkpoint_{current['checkpoint_hash'][:12]}"
    )
    assert current["checkpoint_status"] == "awaiting_operator_approval"
    checkpoints = artifacts["turtlebot3_recovery_checkpoints"]
    old = checkpoints[parent_checkpoint["checkpoint_id"]]
    assert old["checkpoint_status"] == "superseded"
    assert old["superseded_by_checkpoint_id"] == current["checkpoint_id"]
    assert checkpoints[current["checkpoint_id"]] == current
    assert artifacts["turtlebot3_home_mission_execution"][
        "turtlebot3_recovery_checkpoint"
    ] == current
    assert artifacts["summary"]["turtlebot3_recovery_checkpoint"] == current
    assert artifacts["turtlebot3_recovery_revision"]["revision_status"] == (
        "proposed"
    )
    assert artifacts["turtlebot3_recovery_decision_summary"][
        "decision_summary_creates_dispatch_authority"
    ] is False
    assert "turtlebot3_recovery_operator_approval" not in artifacts
    assert "turtlebot3_recovery_bounded_action" not in artifacts
    assert stored["metadata"]["turtlebot3_recovery_lifecycle"] == (
        "awaiting_operator_approval"
    )


def test_turtlebot3_recovery_revision_rejects_stale_full_task_snapshot(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_revise_stale_task_snapshot"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent_checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 concurrent source evidence",
        status="pending",
        artifacts=task_artifacts,
    )

    def revision_after_concurrent_source_update(**kwargs):
        gateway.task_store.update(
            task_id,
            artifacts={
                "turtlebot3_home_mission_execution": {
                    "concurrent_source_evidence": {"generation": 2}
                }
            },
        )
        return _proposed_turtlebot3_recovery_revision(
            resume_execution=kwargs["resume_execution"],
            proposal=kwargs["proposal"],
            operator_instruction=kwargs["operator_instruction"],
        )

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        revision_after_concurrent_source_update,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って障害物を避けて",
            "expected_recovery_checkpoint_id": parent_checkpoint[
                "checkpoint_id"
            ],
            "expected_recovery_checkpoint_hash": parent_checkpoint[
                "checkpoint_hash"
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["summary"]["blocked_reasons"] == [
        "turtlebot3_recovery_checkpoint_revision_conflict"
    ]
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["artifacts"]["turtlebot3_home_mission_execution"][
        "concurrent_source_evidence"
    ] == {"generation": 2}
    current = stored["artifacts"]["turtlebot3_recovery_checkpoint"]
    assert current == parent_checkpoint
    assert current["checkpoint_status"] == "awaiting_operator_approval"
    assert set(stored["artifacts"]["turtlebot3_recovery_checkpoints"]) == {
        parent_checkpoint["checkpoint_id"]
    }
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]


def test_turtlebot3_recovery_revision_stale_and_unsupported_leave_task_unchanged(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_revise_recovery_blocked"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent_checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 pending recovery",
        status="pending",
        artifacts=task_artifacts,
    )
    initial = gateway.task_store.get(task_id)
    assert initial is not None
    calls: list[dict] = []

    def unsupported_revision(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": (
                "missionos_turtlebot3_recovery_checkpoint_revision.v1"
            ),
            "revision_status": "unsupported",
            "blocking_reasons": [
                "unsupported_for_ground_robot:adjust_altitude"
            ],
            "parent_checkpoint_id": parent_checkpoint["checkpoint_id"],
            "parent_checkpoint_hash": parent_checkpoint["checkpoint_hash"],
            "dispatch_authority_created": False,
            "operator_approval_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        unsupported_revision,
    )
    client = TestClient(gateway.app)
    stale = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "左を回って",
            "expected_recovery_checkpoint_id": "stale-checkpoint",
            "expected_recovery_checkpoint_hash": "stale-hash",
        },
    )

    assert stale.status_code == 409
    assert stale.json()["summary"]["blocked_reasons"] == [
        "reviewed_turtlebot3_recovery_checkpoint_id_changed",
        "reviewed_turtlebot3_recovery_checkpoint_hash_changed",
    ]
    assert calls == []
    assert gateway.task_store.get(task_id)["artifacts"] == initial["artifacts"]

    unsupported = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "高度を上げて上を通って",
            "expected_recovery_checkpoint_id": parent_checkpoint[
                "checkpoint_id"
            ],
            "expected_recovery_checkpoint_hash": parent_checkpoint[
                "checkpoint_hash"
            ],
        },
    )

    assert unsupported.status_code == 409
    assert unsupported.json()["summary"]["blocked_reasons"] == [
        "unsupported_for_ground_robot:adjust_altitude"
    ]
    assert len(calls) == 1
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["artifacts"] == initial["artifacts"]
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]


def test_nova_carter_same_kind_cannot_enter_turtlebot3_revision_path(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_nova_carter_recovery_revision_scope"
    artifacts = _pending_nova_carter_recovery_task_artifacts()
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="Nova Carter pending recovery",
        status="pending",
        artifacts=artifacts,
    )
    initial = gateway.task_store.get(task_id)
    assert initial is not None
    planner_calls: list[dict] = []

    def should_not_plan(**kwargs):
        planner_calls.append(kwargs)
        raise AssertionError("Nova Carter must not use TurtleBot3 geometry")

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        should_not_plan,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って障害物を避けて",
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 409
    assert response.json()["summary"]["blocked_reasons"] == [
        "turtlebot3_recovery_revision_robot_scope_not_supported"
    ]
    assert planner_calls == []
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["artifacts"] == initial["artifacts"]
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ("top", "turtlebot3_recovery_checkpoint_hash_mismatch"),
        ("collection", "turtlebot3_recovery_checkpoint_collection_mismatch"),
        ("execution", "turtlebot3_recovery_execution_checkpoint_mismatch"),
        ("summary", "turtlebot3_recovery_summary_checkpoint_mismatch"),
    ],
)
def test_turtlebot3_recovery_revision_rejects_corrupt_parent_before_builder(
    isolated_gateway_factory,
    monkeypatch,
    corruption: str,
    expected_reason: str,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = f"task_turtlebot3_corrupt_parent_{corruption}"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    corrupted_checkpoint = {
        **checkpoint,
        "selected_action": "return_home",
    }
    if corruption == "top":
        artifacts["turtlebot3_recovery_checkpoint"] = corrupted_checkpoint
    elif corruption == "collection":
        artifacts["turtlebot3_recovery_checkpoints"] = {
            checkpoint["checkpoint_id"]: corrupted_checkpoint
        }
    elif corruption == "execution":
        artifacts["turtlebot3_home_mission_execution"] = {
            **artifacts["turtlebot3_home_mission_execution"],
            "turtlebot3_recovery_checkpoint": corrupted_checkpoint,
        }
    else:
        artifacts["summary"] = {
            **artifacts["summary"],
            "turtlebot3_recovery_checkpoint": corrupted_checkpoint,
        }
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 corrupt parent recovery checkpoint",
        status="pending",
        artifacts=artifacts,
    )
    initial = gateway.task_store.get(task_id)
    assert initial is not None
    planner_calls: list[dict] = []

    def should_not_plan(**kwargs):
        planner_calls.append(kwargs)
        raise AssertionError("corrupt parent must fail before revision builder")

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        should_not_plan,
    )
    client = TestClient(gateway.app)
    reviewed = artifacts["turtlebot3_recovery_checkpoint"]
    response = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って障害物を避けて",
            "expected_recovery_checkpoint_id": reviewed["checkpoint_id"],
            "expected_recovery_checkpoint_hash": reviewed["checkpoint_hash"],
        },
    )

    assert response.status_code == 409
    assert expected_reason in response.json()["summary"]["blocked_reasons"]
    assert planner_calls == []
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["artifacts"] == initial["artifacts"]
    assert set(stored["artifacts"]["turtlebot3_recovery_checkpoints"]) == {
        checkpoint["checkpoint_id"]
    }


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        (
            "checkpoint_hash",
            "turtlebot3_recovery_revised_checkpoint_hash_mismatch",
        ),
        (
            "resume_state",
            "turtlebot3_recovery_revision_resume_state_hash_mismatch",
        ),
        (
            "planned_segments_hash",
            "turtlebot3_recovery_revision_planned_segments_hash_mismatch",
        ),
        (
            "floor_plan",
            "turtlebot3_recovery_revision_floor_plan_geometry_changed",
        ),
    ],
)
def test_turtlebot3_recovery_revision_rejects_corrupt_child_before_supersede(
    isolated_gateway_factory,
    monkeypatch,
    corruption: str,
    expected_reason: str,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = f"task_turtlebot3_corrupt_revision_{corruption}"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent_checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 corrupt recovery revision",
        status="pending",
        artifacts=task_artifacts,
    )
    initial = gateway.task_store.get(task_id)
    assert initial is not None

    def corrupt_revision(**kwargs):
        revision = _proposed_turtlebot3_recovery_revision(
            resume_execution=kwargs["resume_execution"],
            proposal=kwargs["proposal"],
            operator_instruction=kwargs["operator_instruction"],
        )
        if corruption == "checkpoint_hash":
            revision["turtlebot3_recovery_checkpoint"]["checkpoint_hash"] = (
                "corrupt-checkpoint-hash"
            )
        elif corruption == "resume_state":
            revision["turtlebot3_home_mission_execution"][
                "runtime_recovery_motion_context"
            ] = {"tampered_after_checkpoint_hash": True}
        elif corruption == "planned_segments_hash":
            revision["turtlebot3_recovery_checkpoint"][
                "planned_segments_sha256"
            ] = ""
        else:
            revision["turtlebot3_home_mission_execution"][
                "turtlebot3_indoor_map_model"
            ]["floor_plan"]["walls"] = [
                {
                    "x_m": 0.5,
                    "y_m": 0.5,
                    "size_x_m": 0.2,
                    "size_y_m": 0.2,
                }
            ]
        return revision

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        corrupt_revision,
    )
    client = TestClient(gateway.app)
    response = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って障害物を避けて",
            "expected_recovery_checkpoint_id": parent_checkpoint[
                "checkpoint_id"
            ],
            "expected_recovery_checkpoint_hash": parent_checkpoint[
                "checkpoint_hash"
            ],
        },
    )

    assert response.status_code == 409
    assert expected_reason in response.json()["summary"]["blocked_reasons"]
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["artifacts"] == initial["artifacts"]

    def authority_creating_revision(**kwargs):
        revision = _proposed_turtlebot3_recovery_revision(
            resume_execution=kwargs["resume_execution"],
            proposal=kwargs["proposal"],
            operator_instruction=kwargs["operator_instruction"],
        )
        revision["operator_approval_created"] = True
        return revision

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        authority_creating_revision,
    )
    authority_attempt = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って",
            "expected_recovery_checkpoint_id": parent_checkpoint[
                "checkpoint_id"
            ],
            "expected_recovery_checkpoint_hash": parent_checkpoint[
                "checkpoint_hash"
            ],
        },
    )

    assert authority_attempt.status_code == 409
    assert authority_attempt.json()["summary"]["blocked_reasons"] == [
        "turtlebot3_recovery_revision_created_operator_approval"
    ]
    assert gateway.task_store.get(task_id)["artifacts"] == initial["artifacts"]


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        (
            "classification_authority",
            "turtlebot3_recovery_revision_classification_authority_invalid",
        ),
        ("action", "turtlebot3_recovery_revision_action_mismatch"),
        (
            "proposal_ref",
            "turtlebot3_recovery_revision_classification_proposal_ref_mismatch",
        ),
        (
            "execution_copy",
            "turtlebot3_recovery_revision_execution_proposal_mismatch",
        ),
        (
            "instruction_hash",
            "turtlebot3_recovery_revision_operator_instruction_hash_mismatch",
        ),
        (
            "intent_direction",
            "turtlebot3_recovery_revision_operator_intent_mismatch",
        ),
        (
            "proposal_observations",
            "turtlebot3_recovery_revision_proposal_observations_mismatch",
        ),
        (
            "planner_lineage",
            "turtlebot3_recovery_revision_planner_lineage_mismatch",
        ),
    ],
)
def test_turtlebot3_recovery_revision_rejects_rehashed_semantic_contradiction(
    isolated_gateway_factory,
    monkeypatch,
    corruption: str,
    expected_reason: str,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = f"task_turtlebot3_semantic_revision_{corruption}"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent_checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 contradictory recovery revision",
        status="pending",
        artifacts=task_artifacts,
    )
    initial = gateway.task_store.get(task_id)
    assert initial is not None

    def contradictory_revision(**kwargs):
        revision = _proposed_turtlebot3_recovery_revision(
            resume_execution=kwargs["resume_execution"],
            proposal=kwargs["proposal"],
            operator_instruction=kwargs["operator_instruction"],
        )
        if corruption == "classification_authority":
            classification = dict(
                revision["recovery_proposal_classification"]
            )
            classification.update(
                execution_class="auto_executable",
                execution_permitted_by_envelope=True,
                requires_new_human_approval=False,
                blocked_reasons=[],
            )
            revision["recovery_proposal_classification"] = classification
            revision["turtlebot3_home_mission_execution"][
                "recovery_proposal_classifications"
            ] = [classification]
            revision["summary"]["recovery_proposal_classifications"] = [
                classification
            ]
        elif corruption == "action":
            revision["recovery_proposal"]["selected_action"] = "return_home"
        elif corruption == "proposal_ref":
            revision["recovery_proposal_classification"][
                "proposal_ref"
            ] = "different_recovery_proposal"
        elif corruption == "execution_copy":
            revision["turtlebot3_home_mission_execution"][
                "recovery_proposals"
            ] = [
                {
                    **revision["recovery_proposal"],
                    "reason": "contradictory execution copy",
                }
            ]
        elif corruption == "instruction_hash":
            revision["turtlebot3_recovery_checkpoint"][
                "operator_instruction_sha256"
            ] = "0" * 64
        elif corruption == "proposal_observations":
            revision["recovery_proposal"]["input_observations"][
                "parent_checkpoint_hash"
            ] = "different_parent_hash"
        elif corruption == "planner_lineage":
            revision["recovery_planner_result"][
                "revision_id"
            ] = "different_revision_id"
        else:
            checkpoint = revision["turtlebot3_recovery_checkpoint"]
            checkpoint["revision_intent"] = "avoid_left_wide"
            checkpoint["recovery_revision_geometry"][
                "requested_direction"
            ] = "left"
        _rebind_proposed_turtlebot3_recovery_revision(revision)
        return revision

    monkeypatch.setattr(
        gateway_server,
        "build_turtlebot3_recovery_checkpoint_revision",
        contradictory_revision,
    )
    client = TestClient(gateway.app)
    response = client.post(
        "/missionos/turtlebot3/recovery-agent/revise-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "右に大きく回って障害物を避けて",
            "expected_recovery_checkpoint_id": parent_checkpoint[
                "checkpoint_id"
            ],
            "expected_recovery_checkpoint_hash": parent_checkpoint[
                "checkpoint_hash"
            ],
        },
    )

    assert response.status_code == 409
    assert expected_reason in response.json()["summary"]["blocked_reasons"]
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["artifacts"] == initial["artifacts"]
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]


def test_turtlebot3_recovery_approval_resumes_without_px4_runner_receipt(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_pending_recovery"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 pending recovery",
        status="pending",
        artifacts=task_artifacts,
    )
    calls: list[dict] = []

    def fake_resume(**kwargs):
        calls.append(kwargs)
        checkpoint = dict(
            kwargs["resume_execution"]["turtlebot3_recovery_checkpoint"]
        )
        checkpoint["checkpoint_status"] = "consumed"
        return {
            "turtlebot3_home_mission_execution": {
                "status": "completed",
                "segment_results": [
                    {"completion_claimed": True},
                    {"completion_claimed": True},
                ],
                "turtlebot3_recovery_checkpoint": checkpoint,
            },
            "turtlebot3_recovery_checkpoint": checkpoint,
            "summary": {
                "status": "completed",
                "completion_claimed": True,
                "completion_scope": "sim_action",
                "runtime_recovery_triggered": True,
                "runtime_recovery_action_kind": "avoid_obstacle",
                "recovery_dispatch_request_sent": True,
                "recovery_completion_claimed": True,
                "route_resumed_after_recovery": True,
                "route_completed_after_recovery": True,
                "fresh_recovery_operator_approval_count": 1,
                "fresh_recovery_operator_approvals": [
                    kwargs["recovery_operator_approval"]
                ],
                "recovery_execution_permitted_by_operator_approval": True,
                "recovery_dispatch_authority_source": "fresh_operator_approval",
                "recovery_proposals": [],
                "recovery_proposal_classifications": [],
                "mission_delivery_completion_claimed": False,
                "physical_execution_invoked": False,
                "turtlebot3_recovery_checkpoint": checkpoint,
            },
        }

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        fake_resume,
    )
    client = TestClient(gateway.app)
    request_body = {
        "task_id": task_id,
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {"target_x_m": -0.2, "target_y_m": -1.4},
        "explicit_recovery_dispatch_approval": True,
        "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
        "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
    }

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json=request_body,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["dispatch_status"] == "recovery_completed"
    assert payload["summary"]["route_completed_after_recovery"] is True
    assert len(calls) == 1
    approval = calls[0]["recovery_operator_approval"]
    assert approval["schema_version"] == (
        "missionos_turtlebot3_recovery_operator_approval.v1"
    )
    assert approval["checkpoint_hash"] == checkpoint["checkpoint_hash"]
    assert approval["execution_target"] == "ros2_nav2_turtlebot3_sim"
    assert approval["approved_parameters"] == {
        "target_x_m": -0.2,
        "target_y_m": -1.4,
        "obstacle_avoidance_required": True,
    }
    receipt = payload["missionos_runtime_recovery_dispatch_receipt"]
    assert receipt["active_runner_request_queued"] is False
    assert receipt["dispatch_attempt_id"] == approval["dispatch_attempt_id"]
    assert receipt["outcome_known"] is True
    assert receipt["automatic_redispatch_performed"] is False
    assert "maneuver_allowlist" not in receipt
    assert "allowed_mavlink_message_ids" not in str(receipt)
    stored_task = gateway.task_store.get(task_id)
    assert stored_task is not None
    claimed_checkpoint = stored_task["artifacts"][
        "turtlebot3_recovery_checkpoints"
    ][checkpoint["checkpoint_id"]]
    assert claimed_checkpoint["checkpoint_status"] == "consumed"
    assert claimed_checkpoint["claimed_by_approval_ref"] == approval[
        "operator_approval_ref"
    ]
    assert stored_task["artifacts"]["turtlebot3_recovery_checkpoint"] == (
        claimed_checkpoint
    )
    assert stored_task["artifacts"]["turtlebot3_home_mission_execution"][
        "turtlebot3_recovery_checkpoint"
    ] == claimed_checkpoint
    assert stored_task["artifacts"]["summary"][
        "turtlebot3_recovery_checkpoint"
    ] == claimed_checkpoint
    dispatch_attempt = stored_task["artifacts"][
        "turtlebot3_recovery_dispatch_attempt"
    ]
    assert dispatch_attempt["dispatch_attempt_id"] == approval[
        "dispatch_attempt_id"
    ]
    assert dispatch_attempt["attempt_status"] == "recovery_completed"
    assert dispatch_attempt["outcome_known"] is True

    duplicate = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json=request_body,
    )

    assert duplicate.status_code == 200, duplicate.json()
    assert duplicate.json()["summary"]["dispatch_status"] == "already_consumed"
    assert len(calls) == 1

    changed_replay = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            **request_body,
            "recovery_parameters": {"target_x_m": 9.0, "target_y_m": 9.0},
        },
    )

    assert changed_replay.status_code == 409
    assert changed_replay.json()["summary"]["dispatch_status"] == "blocked"
    assert changed_replay.json()["summary"]["idempotent_replay"] is False
    assert changed_replay.json()["summary"]["blocked_reasons"] == [
        "recovery_parameters_must_match_consumed_turtlebot3_checkpoint"
    ]
    assert len(calls) == 1


def test_failed_turtlebot3_recovery_persists_fresh_unapproved_repair_child(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_recovery_repair_child"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent = artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 recovery repair child",
        status="pending",
        artifacts=artifacts,
    )

    def fake_resume(**kwargs):
        failed_parent = {
            **kwargs["resume_execution"]["turtlebot3_recovery_checkpoint"],
            "checkpoint_status": "failed",
            "failure_reasons": ["nav2_recovery_orbit_detected"],
        }
        child = {
            **parent,
            "checkpoint_status": "awaiting_operator_approval",
            "approved_parameters": {
                "target_x_m": 0.8,
                "target_y_m": -1.8,
                "obstacle_avoidance_required": True,
            },
            "parent_checkpoint_id": parent["checkpoint_id"],
            "parent_checkpoint_hash": parent["checkpoint_hash"],
            "repair_attempt": 1,
            "requires_new_human_approval": True,
            "automatic_redispatch_performed": False,
        }
        child.pop("checkpoint_id", None)
        child.pop("checkpoint_hash", None)
        child_hash = gateway_server._recovery_checkpoint_hash(child)
        child["checkpoint_hash"] = child_hash
        child["checkpoint_id"] = (
            f"turtlebot3_recovery_checkpoint_{child_hash[:12]}"
        )
        summary = {
            "status": "pending",
            "runtime_recovery_triggered": True,
            "recovery_dispatch_request_sent": True,
            "recovery_completion_claimed": False,
            "route_resumed_after_recovery": False,
            "completion_claimed": False,
            "turtlebot3_recovery_checkpoint": child,
        }
        return {
            "turtlebot3_home_mission_execution": {
                **summary,
                "turtlebot3_recovery_checkpoint": child,
            },
            "turtlebot3_recovery_checkpoint": child,
            "turtlebot3_recovery_repair_parent_checkpoint": failed_parent,
            "summary": summary,
        }

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        fake_resume,
    )
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {"target_x_m": -0.2, "target_y_m": -1.4},
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": parent["checkpoint_id"],
            "expected_recovery_checkpoint_hash": parent["checkpoint_hash"],
        },
    )

    assert response.status_code == 200, response.json()
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "pending"
    current = stored["artifacts"]["turtlebot3_recovery_checkpoint"]
    assert current["checkpoint_status"] == "awaiting_operator_approval"
    assert current["requires_new_human_approval"] is True
    collection = stored["artifacts"]["turtlebot3_recovery_checkpoints"]
    assert collection[parent["checkpoint_id"]]["checkpoint_status"] == "failed"
    assert collection[current["checkpoint_id"]] == current
    assert current["automatic_redispatch_performed"] is False
    parent_approval = stored["artifacts"][
        "turtlebot3_recovery_operator_approval"
    ]
    assert parent_approval["checkpoint_id"] == parent["checkpoint_id"]
    assert parent_approval["checkpoint_id"] != current["checkpoint_id"]


def test_resumed_route_failure_persists_fresh_unapproved_followup_checkpoint(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_recovery_followup_child"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    parent = artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 recovery followup child",
        status="pending",
        artifacts=artifacts,
    )

    def fake_resume(**kwargs):
        consumed_parent = {
            **kwargs["resume_execution"]["turtlebot3_recovery_checkpoint"],
            "checkpoint_status": "consumed",
            "consumed_at": "2026-07-13T00:00:01+00:00",
        }
        child = {
            key: value
            for key, value in parent.items()
            if key
            not in {
                "checkpoint_id",
                "checkpoint_hash",
                "recovery_candidate_binding",
            }
        }
        child.update(
            {
                "checkpoint_status": "awaiting_operator_approval",
                "selected_action": "ask_human",
                "approved_parameters": {},
                "operator_guidance_required": True,
                "parent_checkpoint_id": parent["checkpoint_id"],
                "parent_checkpoint_hash": parent["checkpoint_hash"],
                "followup_trigger": (
                    "route_segment_failed_after_verified_recovery"
                ),
                "requires_new_human_approval": True,
                "automatic_redispatch_performed": False,
            }
        )
        child_hash = gateway_server._recovery_checkpoint_hash(child)
        child["checkpoint_hash"] = child_hash
        child["checkpoint_id"] = (
            f"turtlebot3_recovery_checkpoint_{child_hash[:12]}"
        )
        summary = {
            "status": "pending",
            "runtime_recovery_triggered": True,
            "recovery_dispatch_request_sent": True,
            "recovery_completion_claimed": True,
            "route_resumed_after_recovery": True,
            "route_completed_after_recovery": False,
            "completion_claimed": False,
            "blocking_reasons": ["nav2_goal_result_not_succeeded"],
            "turtlebot3_recovery_checkpoint": child,
        }
        return {
            "turtlebot3_home_mission_execution": {
                **summary,
                "turtlebot3_recovery_checkpoint": child,
            },
            "turtlebot3_recovery_checkpoint": child,
            "turtlebot3_recovery_followup_parent_checkpoint": consumed_parent,
            "summary": summary,
        }

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        fake_resume,
    )
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {"target_x_m": -0.2, "target_y_m": -1.4},
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": parent["checkpoint_id"],
            "expected_recovery_checkpoint_hash": parent["checkpoint_hash"],
        },
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["summary"]["dispatch_status"] == (
        "followup_recovery_proposed"
    )
    assert payload["summary"]["requires_new_human_approval"] is True
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "pending"
    current = stored["artifacts"]["turtlebot3_recovery_checkpoint"]
    assert current["checkpoint_status"] == "awaiting_operator_approval"
    assert current["checkpoint_id"] != parent["checkpoint_id"]
    assert current["selected_action"] == "ask_human"
    assert current["operator_guidance_required"] is True
    assert current["approved_parameters"] == {}
    assert "claimed_at" not in current
    assert "recovery_candidate_binding" not in current
    collection = stored["artifacts"]["turtlebot3_recovery_checkpoints"]
    assert collection[parent["checkpoint_id"]]["checkpoint_status"] == "consumed"
    assert collection[current["checkpoint_id"]] == current
    approval = stored["artifacts"]["turtlebot3_recovery_operator_approval"]
    assert approval["checkpoint_id"] == parent["checkpoint_id"]
    assert approval["checkpoint_id"] != current["checkpoint_id"]


def test_ask_human_checkpoint_cannot_mint_approval_or_dispatch(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_ask_human_proposal_only"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = {
        key: value
        for key, value in artifacts["turtlebot3_recovery_checkpoint"].items()
        if key
        not in {
            "checkpoint_id",
            "checkpoint_hash",
            "recovery_candidate_binding",
            "recovery_goal_poses",
        }
    }
    checkpoint.update(
        {
            "selected_action": "ask_human",
            "approved_parameters": {},
            "operator_guidance_required": True,
            "requires_new_human_approval": True,
            "automatic_redispatch_performed": False,
        }
    )
    checkpoint_hash = gateway_server._recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    artifacts["turtlebot3_recovery_checkpoint"] = checkpoint
    artifacts["turtlebot3_recovery_checkpoints"] = {
        checkpoint["checkpoint_id"]: checkpoint
    }
    artifacts["turtlebot3_home_mission_execution"] = {
        **artifacts["turtlebot3_home_mission_execution"],
        "turtlebot3_recovery_checkpoint": checkpoint,
    }
    artifacts["summary"] = {
        **artifacts["summary"],
        "turtlebot3_recovery_checkpoint": checkpoint,
    }
    artifacts.pop("turtlebot3_recovery_operator_approval", None)
    artifacts.pop("turtlebot3_recovery_bounded_action", None)
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 proposal-only ask human checkpoint",
        status="pending",
        artifacts=artifacts,
    )

    client = TestClient(gateway.app)
    unsupported_response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "ask_human",
            "recovery_parameters": {},
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert unsupported_response.status_code == 400
    assert "avoid_obstacle, reroute, return_home" in unsupported_response.json()[
        "detail"
    ]

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {
                "target_x_m": -0.2,
                "target_y_m": -1.4,
                "obstacle_avoidance_required": True,
            },
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 409, response.json()
    assert "turtlebot3_recovery_checkpoint_not_dispatchable" in response.json()[
        "summary"
    ]["blocked_reasons"]
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["artifacts"]["turtlebot3_recovery_checkpoint"] == checkpoint
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]


def test_turtlebot3_recovery_finalization_conflict_is_durably_unknown(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_recovery_finalization_conflict"
    task_artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = task_artifacts["turtlebot3_recovery_checkpoint"]
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 finalization conflict",
        status="pending",
        artifacts=task_artifacts,
    )
    runtime_calls: list[dict] = []

    def fake_resume(**kwargs):
        runtime_calls.append(kwargs)
        resumed_checkpoint = {
            **kwargs["resume_execution"]["turtlebot3_recovery_checkpoint"],
            "checkpoint_status": "consumed",
        }
        return {
            "turtlebot3_home_mission_execution": {
                "status": "completed",
                "segment_results": [{"completion_claimed": True}],
                "turtlebot3_recovery_checkpoint": resumed_checkpoint,
            },
            "turtlebot3_recovery_checkpoint": resumed_checkpoint,
            "summary": {
                "status": "completed",
                "completion_claimed": True,
                "completion_scope": "sim_action",
                "runtime_recovery_triggered": True,
                "runtime_recovery_action_kind": "avoid_obstacle",
                "recovery_dispatch_request_sent": True,
                "recovery_completion_claimed": True,
                "route_resumed_after_recovery": True,
                "route_completed_after_recovery": True,
                "fresh_recovery_operator_approval_count": 1,
                "fresh_recovery_operator_approvals": [
                    kwargs["recovery_operator_approval"]
                ],
                "recovery_execution_permitted_by_operator_approval": True,
                "recovery_dispatch_authority_source": (
                    "fresh_operator_approval"
                ),
                "recovery_proposals": [],
                "recovery_proposal_classifications": [],
                "mission_delivery_completion_claimed": False,
                "physical_execution_invoked": False,
                "turtlebot3_recovery_checkpoint": resumed_checkpoint,
            },
        }

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        fake_resume,
    )
    original_claim = gateway.task_store.claim_nested_artifact
    finalization_attempts: list[dict] = []

    def conflict_once(*args, **kwargs):
        if kwargs.get("next_task_status") == "completed":
            finalization_attempts.append(dict(kwargs))
            return None
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(
        gateway.task_store,
        "claim_nested_artifact",
        conflict_once,
    )
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {
                "target_x_m": -0.2,
                "target_y_m": -1.4,
            },
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 409
    assert len(runtime_calls) == 1
    assert len(finalization_attempts) == 1
    assert "expected_updated_at" not in finalization_attempts[0]
    assert {
        "turtlebot3_recovery_checkpoint",
        "turtlebot3_home_mission_execution",
        "summary",
        "missionos_runtime_recovery_dispatch_receipt",
        "turtlebot3_recovery_dispatch_attempt",
    }.issubset(finalization_attempts[0]["replace_artifacts"])
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "blocked"
    artifacts = stored["artifacts"]
    unknown_checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    assert unknown_checkpoint["checkpoint_status"] == "dispatch_unknown"
    assert artifacts["turtlebot3_recovery_checkpoints"][
        unknown_checkpoint["checkpoint_id"]
    ] == unknown_checkpoint
    assert artifacts["turtlebot3_home_mission_execution"][
        "turtlebot3_recovery_checkpoint"
    ] == unknown_checkpoint
    assert artifacts["summary"][
        "turtlebot3_recovery_checkpoint"
    ] == unknown_checkpoint
    attempt = artifacts["turtlebot3_recovery_dispatch_attempt"]
    assert attempt["attempt_status"] == "dispatch_unknown"
    assert attempt["outcome_known"] is False
    assert attempt["runtime_result_observed"] is True
    assert attempt["runtime_result_atomically_committed"] is False
    receipt = artifacts["missionos_runtime_recovery_dispatch_receipt"]
    assert receipt["dispatch_status"] == "dispatch_unknown"
    assert receipt["operator_approved"] is True
    assert receipt["dispatch_authority_created"] is True
    assert receipt["recovery_dispatch_request_sent"] is None
    assert receipt["runtime_result_observed"] is True
    assert response.json()["task"]["status"] == "blocked"


@pytest.mark.parametrize(
    "checkpoint_status",
    ["awaiting_operator_approval", "consumed"],
)
def test_turtlebot3_recovery_dispatch_rejects_tampered_checkpoint_hash_before_authority(
    isolated_gateway_factory,
    monkeypatch,
    checkpoint_status: str,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = f"task_turtlebot3_tampered_{checkpoint_status}"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    checkpoint["checkpoint_status"] = checkpoint_status
    if checkpoint_status == "consumed":
        checkpoint["consumed_at"] = "2026-07-11T00:00:00+00:00"
        checkpoint["consumed_by_approval_ref"] = "approval:previous"
        artifacts["missionos_runtime_recovery_dispatch_receipt"] = {
            "schema_version": (
                "missionos_runtime_recovery_dispatch_receipt.v1"
            ),
            "dispatch_status": "recovery_completed",
        }
    # This field is hash-covered. Keep every durable copy equal so the
    # regression proves that the Gateway recomputes the hash instead of merely
    # comparing the caller's binding to the stored string.
    checkpoint["tampered_source_binding"] = True
    runner_calls: list[dict] = []

    def should_not_run(**kwargs):
        runner_calls.append(kwargs)
        raise AssertionError("tampered checkpoint must fail before runtime")

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        should_not_run,
    )
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 tampered recovery checkpoint",
        status=(
            "pending"
            if checkpoint_status == "awaiting_operator_approval"
            else "completed"
        ),
        artifacts=artifacts,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {
                "target_x_m": -0.2,
                "target_y_m": -1.4,
            },
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 409
    summary = response.json()["summary"]
    assert summary["dispatch_status"] == "blocked"
    assert "turtlebot3_recovery_checkpoint_hash_mismatch" in summary[
        "blocked_reasons"
    ]
    assert runner_calls == []
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]
    assert stored["artifacts"]["turtlebot3_recovery_checkpoint"][
        "checkpoint_status"
    ] == checkpoint_status


def test_turtlebot3_recovery_dispatch_rejects_changed_planned_route_before_authority(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_changed_planned_route"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    artifacts["turtlebot3_home_mission_plan"]["planned_segments"][1][
        "x_m"
    ] = 9.0
    runner_calls: list[dict] = []

    def should_not_run(**kwargs):
        runner_calls.append(kwargs)
        raise AssertionError("changed route must fail before runtime")

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        should_not_run,
    )
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 changed planned route",
        status="pending",
        artifacts=artifacts,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {
                "target_x_m": -0.2,
                "target_y_m": -1.4,
            },
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 409
    assert (
        "turtlebot3_recovery_checkpoint_planned_segments_hash_mismatch"
        in response.json()["summary"]["blocked_reasons"]
    )
    assert runner_calls == []
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]
    assert stored["artifacts"]["turtlebot3_recovery_checkpoint"] == checkpoint


def test_turtlebot3_recovery_dispatch_rejects_divergent_summary_checkpoint(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_divergent_summary_checkpoint"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    artifacts["summary"]["turtlebot3_recovery_checkpoint"] = {
        **checkpoint,
        "checkpoint_status": "superseded",
    }
    runner_calls: list[dict] = []

    def should_not_run(**kwargs):
        runner_calls.append(kwargs)
        raise AssertionError("divergent summary must fail before runtime")

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        should_not_run,
    )
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 divergent summary checkpoint",
        status="pending",
        artifacts=artifacts,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {
                "target_x_m": -0.2,
                "target_y_m": -1.4,
            },
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 409
    assert "turtlebot3_recovery_summary_checkpoint_mismatch" in response.json()[
        "summary"
    ]["blocked_reasons"]
    assert runner_calls == []
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]


def test_gateway_restart_reconciles_only_prior_owner_dispatch_to_unknown(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from src.gateway import server as gateway_server
    from src.runtime.turtlebot3_home_mission import _recovery_checkpoint_hash

    prior_gateway = isolated_gateway_factory()
    prior_gateway._acquire_gateway_process_lock()
    prior_task_id = "task_turtlebot3_prior_process_dispatch"
    try:
        prior = _persist_dispatching_turtlebot3_recovery_task(
            prior_gateway,
            task_id=prior_task_id,
            gateway_process_owner_id=prior_gateway._gateway_process_owner_id,
        )
        prior_owner_id = prior_gateway._gateway_process_owner_id
    finally:
        # Releasing the OS lock without reconciling the claimed task simulates
        # abrupt process death after the durable dispatching transition.
        prior_gateway._release_gateway_process_lock()

    restarted_gateway = isolated_gateway_factory()
    # Both Gateway objects live in this pytest process. Override only the
    # process-owner token to simulate a real process restart/module reload.
    restarted_gateway._gateway_process_owner_id = (
        f"{prior_owner_id}_simulated_restart"
    )
    assert restarted_gateway._gateway_process_owner_id != prior_owner_id
    current_task_id = "task_turtlebot3_current_process_dispatch"
    _persist_dispatching_turtlebot3_recovery_task(
        restarted_gateway,
        task_id=current_task_id,
        gateway_process_owner_id=(
            restarted_gateway._gateway_process_owner_id
        ),
    )
    legacy_live_task_id = "task_turtlebot3_legacy_live_process_dispatch"
    legacy_live_owner_id = (
        f"missionos_gateway_process_{os.getpid()}_legacy_owner"
    )
    _persist_dispatching_turtlebot3_recovery_task(
        restarted_gateway,
        task_id=legacy_live_task_id,
        gateway_process_owner_id=legacy_live_owner_id,
    )
    runtime_calls: list[dict] = []

    def should_not_redispatch(**kwargs):
        runtime_calls.append(kwargs)
        raise AssertionError("startup reconciliation must never redispatch")

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        should_not_redispatch,
    )

    restarted_gateway._acquire_gateway_process_lock()
    try:
        assert restarted_gateway._gateway_previous_lock_owner_id == prior_owner_id
        assert (
            restarted_gateway._reconcile_abandoned_turtlebot3_recovery_dispatches()
            == 1
        )
        assert (
            restarted_gateway._reconcile_abandoned_turtlebot3_recovery_dispatches()
            == 0
        )
    finally:
        restarted_gateway._release_gateway_process_lock()
    assert runtime_calls == []
    reconciled = restarted_gateway.task_store.get(prior_task_id)
    assert reconciled is not None
    assert reconciled["status"] == "blocked"
    artifacts = reconciled["artifacts"]
    checkpoint = artifacts["turtlebot3_recovery_checkpoint"]
    assert checkpoint["checkpoint_status"] == "dispatch_unknown"
    assert checkpoint["checkpoint_hash"] == _recovery_checkpoint_hash(
        checkpoint
    )
    checkpoint_copies = [
        artifacts["turtlebot3_recovery_checkpoints"][
            checkpoint["checkpoint_id"]
        ],
        artifacts["turtlebot3_home_mission_execution"][
            "turtlebot3_recovery_checkpoint"
        ],
        artifacts["summary"]["turtlebot3_recovery_checkpoint"],
    ]
    assert all(item == checkpoint for item in checkpoint_copies)
    assert checkpoint["failure_reasons"] == [
        "gateway_restarted_with_unresolved_turtlebot3_recovery_dispatch"
    ]
    receipt = artifacts["missionos_runtime_recovery_dispatch_receipt"]
    assert receipt["dispatch_status"] == "dispatch_unknown"
    assert receipt["outcome_known"] is False
    assert receipt["recovery_dispatch_request_sent"] is None
    assert receipt["automatic_redispatch_performed"] is False
    assert receipt["operator_approved"] is True
    assert receipt["explicit_recovery_dispatch_approval"] is True
    assert receipt["dispatch_authority_created"] is True
    assert receipt["reconciliation_created_dispatch_authority"] is False
    assert receipt["turtlebot3_continuation_invoked"] is None
    attempt = artifacts["turtlebot3_recovery_dispatch_attempt"]
    assert attempt["attempt_status"] == "dispatch_unknown"
    assert attempt["outcome_known"] is False
    assert attempt["operator_approved"] is True
    assert attempt["dispatch_authority_created"] is True
    assert attempt["reconciliation_created_dispatch_authority"] is False
    assert attempt["turtlebot3_continuation_invoked"] is None
    assert attempt["previous_gateway_process_owner_id"] == prior_owner_id
    assert attempt["reconciled_by_gateway_process_owner_id"] == (
        restarted_gateway._gateway_process_owner_id
    )
    assert artifacts["turtlebot3_recovery_operator_approval"] == prior[
        "artifacts"
    ]["turtlebot3_recovery_operator_approval"]
    assert reconciled["metadata"]["turtlebot3_recovery_lifecycle"] == (
        "dispatch_unknown"
    )

    current = restarted_gateway.task_store.get(current_task_id)
    assert current is not None
    assert current["status"] == "running"
    assert current["artifacts"]["turtlebot3_recovery_checkpoint"][
        "checkpoint_status"
    ] == "dispatching"
    legacy_live = restarted_gateway.task_store.get(legacy_live_task_id)
    assert legacy_live is not None
    assert legacy_live["status"] == "running"
    assert legacy_live["artifacts"]["turtlebot3_recovery_dispatch_attempt"][
        "gateway_process_owner_id"
    ] == legacy_live_owner_id


def test_gateway_process_lock_excludes_same_store_and_allows_different_store(
    isolated_gateway_factory,
    tmp_path,
) -> None:
    from src.runtime.task_store import TaskStore

    first = isolated_gateway_factory()
    same_store = isolated_gateway_factory()
    different_store = isolated_gateway_factory()
    different_store.task_store = TaskStore(str(tmp_path / "different_tasks.db"))

    first._acquire_gateway_process_lock()
    try:
        with pytest.raises(
            RuntimeError,
            match="another MissionOS Gateway process owns the task-store lock",
        ):
            same_store._acquire_gateway_process_lock()
        different_store._acquire_gateway_process_lock()
        different_store._release_gateway_process_lock()
    finally:
        first._release_gateway_process_lock()

    same_store._acquire_gateway_process_lock()
    same_store._release_gateway_process_lock()


def test_gateway_shutdown_marks_current_owner_dispatch_unknown(
    isolated_gateway_factory,
) -> None:
    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_current_owner_shutdown"
    gateway._acquire_gateway_process_lock()
    try:
        _persist_dispatching_turtlebot3_recovery_task(
            gateway,
            task_id=task_id,
            gateway_process_owner_id=gateway._gateway_process_owner_id,
        )
        assert (
            gateway._reconcile_abandoned_turtlebot3_recovery_dispatches(
                include_current_owner=True
            )
            == 1
        )
    finally:
        gateway._release_gateway_process_lock()

    reconciled = gateway.task_store.get(task_id)
    assert reconciled is not None
    assert reconciled["status"] == "blocked"
    artifacts = reconciled["artifacts"]
    assert artifacts["turtlebot3_recovery_checkpoint"]["checkpoint_status"] == (
        "dispatch_unknown"
    )
    assert artifacts["turtlebot3_recovery_checkpoint"]["failure_reasons"] == [
        "gateway_shutdown_with_unresolved_turtlebot3_recovery_dispatch"
    ]
    assert artifacts["turtlebot3_recovery_dispatch_attempt"]["outcome_known"] is False
    assert artifacts["turtlebot3_recovery_dispatch_attempt"][
        "automatic_redispatch_performed"
    ] is False

def test_gateway_lifespan_releases_process_lock_when_startup_fails(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    failing_gateway = isolated_gateway_factory()
    replacement_gateway = isolated_gateway_factory()

    async def fail_startup() -> None:
        raise RuntimeError("fixture startup failure")

    monkeypatch.setattr(failing_gateway, "_startup_gateway", fail_startup)

    async def exercise_failure() -> None:
        with pytest.raises(RuntimeError, match="fixture startup failure"):
            async with failing_gateway._lifespan(failing_gateway.app):
                raise AssertionError("startup failure must prevent lifespan body")

    asyncio.run(exercise_failure())
    assert failing_gateway._gateway_process_lock_handle is None
    assert failing_gateway._gateway_previous_lock_owner_id is None

    replacement_gateway._acquire_gateway_process_lock()
    replacement_gateway._release_gateway_process_lock()


def test_turtlebot3_recovery_dispatch_exception_updates_all_checkpoint_copies(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server
    from src.runtime.turtlebot3_home_mission import _recovery_checkpoint_hash

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_recovery_dispatch_exception"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = dict(artifacts["turtlebot3_recovery_checkpoint"])
    checkpoint_hash = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    checkpoint_id = checkpoint["checkpoint_id"]
    artifacts["turtlebot3_recovery_checkpoint"] = checkpoint
    artifacts["turtlebot3_recovery_checkpoints"] = {
        checkpoint_id: checkpoint,
    }
    artifacts["turtlebot3_home_mission_execution"][
        "turtlebot3_recovery_checkpoint"
    ] = checkpoint
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 recovery dispatch exception",
        status="pending",
        artifacts=artifacts,
    )

    def raise_after_dispatch_claim(**_kwargs):
        raise RuntimeError("bridge response lost after dispatch claim")

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        raise_after_dispatch_claim,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {"target_x_m": -0.2, "target_y_m": -1.4},
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint_id,
            "expected_recovery_checkpoint_hash": checkpoint_hash,
        },
    )

    assert response.status_code == 409
    assert response.json()["summary"]["blocked_reasons"] == [
        "turtlebot3_recovery_dispatch_outcome_unknown"
    ]
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "blocked"
    stored_artifacts = stored["artifacts"]
    failed_checkpoint = stored_artifacts["turtlebot3_recovery_checkpoint"]
    checkpoint_copies = [
        stored_artifacts["turtlebot3_recovery_checkpoints"][checkpoint_id],
        stored_artifacts["turtlebot3_home_mission_execution"][
            "turtlebot3_recovery_checkpoint"
        ],
        stored_artifacts["summary"]["turtlebot3_recovery_checkpoint"],
    ]
    assert all(copy == failed_checkpoint for copy in checkpoint_copies)
    assert failed_checkpoint["checkpoint_status"] == "dispatch_unknown"
    assert failed_checkpoint["failure_reasons"] == [
        "RuntimeError: bridge response lost after dispatch claim"
    ]
    assert "failure" not in failed_checkpoint
    assert failed_checkpoint["checkpoint_hash"] == _recovery_checkpoint_hash(
        failed_checkpoint
    )
    receipt = stored_artifacts["missionos_runtime_recovery_dispatch_receipt"]
    assert receipt["blocked_reasons"] == [
        "turtlebot3_recovery_dispatch_outcome_unknown"
    ]
    assert receipt["operator_approved"] is True
    assert receipt["explicit_recovery_dispatch_approval"] is True


def test_turtlebot3_recovery_return_home_uses_ground_action_without_route_resume(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server
    from src.runtime.turtlebot3_home_mission import _recovery_checkpoint_hash

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_pending_return_home"
    artifacts = _pending_turtlebot3_recovery_task_artifacts()
    checkpoint = {
        **artifacts["turtlebot3_recovery_checkpoint"],
        "selected_action": "return_home",
        "approved_parameters": {
            "target_x_m": -2.0,
            "target_y_m": -0.5,
            "return_home_required": True,
        },
    }
    checkpoint_hash = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    artifacts["turtlebot3_recovery_checkpoint"] = checkpoint
    artifacts["turtlebot3_recovery_checkpoints"] = {
        checkpoint["checkpoint_id"]: checkpoint
    }
    artifacts["turtlebot3_home_mission_execution"][
        "turtlebot3_recovery_checkpoint"
    ] = checkpoint
    artifacts["summary"]["turtlebot3_recovery_checkpoint"] = checkpoint
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 pending return home",
        status="pending",
        artifacts=artifacts,
    )
    calls: list[dict] = []

    def fake_return_home_resume(**kwargs):
        calls.append(kwargs)
        consumed = {
            **kwargs["resume_execution"]["turtlebot3_recovery_checkpoint"],
            "checkpoint_status": "consumed",
        }
        return {
            "turtlebot3_home_mission_execution": {
                **kwargs["resume_execution"],
                "status": "recovered",
                "turtlebot3_recovery_checkpoint": consumed,
            },
            "turtlebot3_recovery_checkpoint": consumed,
            "summary": {
                "status": "recovered",
                "completion_claimed": False,
                "completion_scope": "none",
                "runtime_recovery_triggered": True,
                "runtime_recovery_action_kind": "return_home",
                "recovery_dispatch_request_sent": True,
                "recovery_completion_claimed": True,
                "route_resumed_after_recovery": False,
                "route_completed_after_recovery": False,
                "fresh_recovery_operator_approval_count": 1,
                "fresh_recovery_operator_approvals": [
                    kwargs["recovery_operator_approval"]
                ],
                "recovery_proposals": [],
                "recovery_proposal_classifications": [],
                "mission_delivery_completion_claimed": False,
                "physical_execution_invoked": False,
                "turtlebot3_recovery_checkpoint": consumed,
            },
        }

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        fake_return_home_resume,
    )
    client = TestClient(gateway.app)
    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "return_home",
            "recovery_parameters": checkpoint["approved_parameters"],
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint["checkpoint_id"],
            "expected_recovery_checkpoint_hash": checkpoint["checkpoint_hash"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["recovery_action"] == "return_home"
    assert payload["summary"]["recovery_completion_claimed"] is True
    assert payload["summary"]["route_resumed_after_recovery"] is False
    assert payload["summary"]["route_completed_after_recovery"] is False
    assert len(calls) == 1
    approval = calls[0]["recovery_operator_approval"]
    assert approval["approved_action"] == "return_home"
    assert approval["approved_parameters"] == checkpoint["approved_parameters"]
    assert payload["task"]["status"] == "recovered"
    assert payload["summary"]["physical_execution_invoked"] is False
    assert payload["summary"]["delivery_completion_claimed"] is False


def test_turtlebot3_return_home_parameters_preserve_explicit_route_resume() -> None:
    from src.gateway import server as gateway_server

    parameters = gateway_server._bounded_turtlebot3_operator_recovery_parameters(
        recovery_action="return_home",
        body={
            "recovery_parameters": {
                "target_x_m": -2.0,
                "target_y_m": -0.5,
                "return_home_required": True,
                "resume_route_after_recovery": True,
            }
        },
    )

    assert parameters == {
        "target_x_m": -2.0,
        "target_y_m": -0.5,
        "return_home_required": True,
        "resume_route_after_recovery": True,
    }


def test_turtlebot3_gateway_maps_return_home_then_resume_without_new_action() -> None:
    from src.gateway import server as gateway_server

    action, direction = (
        gateway_server._turtlebot3_recovery_revision_action_direction(
            "return_home_then_resume"
        )
    )

    assert action == "return_home"
    assert direction == "return_home"


def test_turtlebot3_gateway_maps_failed_segment_retry_to_bounded_reroute() -> None:
    from src.gateway import server as gateway_server

    action, direction = (
        gateway_server._turtlebot3_recovery_revision_action_direction(
            "retry_failed_segment"
        )
    )

    assert action == "reroute"
    assert direction == "route_retry"


def test_nova_carter_same_kind_cannot_enter_turtlebot3_dispatch_path(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory()
    task_id = "task_nova_carter_recovery_dispatch_scope"
    artifacts = _pending_nova_carter_recovery_task_artifacts()
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="Nova Carter pending recovery",
        status="pending",
        artifacts=artifacts,
    )
    initial = gateway.task_store.get(task_id)
    assert initial is not None
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "return_home",
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith(
        "TurtleBot3 recovery dispatch requires robot_profile=turtlebot3"
    )
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["artifacts"] == initial["artifacts"]
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert "turtlebot3_recovery_bounded_action" not in stored["artifacts"]
    assert "missionos_runtime_recovery_dispatch_receipt" not in stored[
        "artifacts"
    ]


def test_px4_recovery_action_allowlist_does_not_accept_ground_return_home(
    isolated_gateway_factory,
) -> None:
    from fastapi.testclient import TestClient

    gateway = isolated_gateway_factory()
    task_id = "task_px4_recovery_action_allowlist"
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="PX4 recovery action allowlist",
        status="running",
        artifacts={},
    )
    client = TestClient(gateway.app)

    ground_action = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "return_home",
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert ground_action.status_code == 400
    assert "return_home" not in ground_action.json()["detail"].split("one of ", 1)[1]
    assert "return_to_launch" in ground_action.json()["detail"]

    existing_px4_action = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "return_to_launch",
            "explicit_recovery_dispatch_approval": False,
        },
    )

    assert existing_px4_action.status_code == 400
    assert existing_px4_action.json()["detail"] == (
        "explicit_recovery_dispatch_approval is required"
    )


def test_px4_stale_agent_recovery_proposal_cannot_mint_or_queue_authority(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_px4_stale_agent_recovery_proposal"
    proposed_parameters = {
        "target_x_m": 30.0,
        "target_y_m": 40.0,
        "target_altitude_m": 45.0,
    }
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="PX4 stale agent recovery proposal",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_auto_operator_recovery_request_stale.json"
                ),
            },
            "missionos_runtime_recovery_last_proposal": {
                "schema_version": "missionos_runtime_recovery_proposal_evidence.v1",
                "proposal_id": "runtime_recovery_proposal_stale",
                "proposal_status": "awaiting_operator_approval",
                "observed_at": "2026-01-01T00:00:00+00:00",
                "valid_until": "2026-01-01T00:01:00+00:00",
                "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
                "max_origin_drift_m": 5.0,
                "runtime_recovery_agent_result": {
                    "assessment": {
                        "recovery_planner_tool_candidate": {
                            "selected_bounded_action": "avoid_obstacle",
                            "proposed_parameters": proposed_parameters,
                        }
                    }
                },
                "dispatch_authority_created": False,
            },
            "missionos_auto_mission_runtime_snapshot": {
                "sample_index": 22,
                "elapsed_seconds": 132.0,
                "local_x_m": 20.0,
                "local_y_m": 0.0,
                "local_z_m": -30.0,
                "altitude_above_home_m": 30.0,
                "heartbeat_observed": True,
                "landed": False,
            },
        },
    )
    queue_calls: list[dict] = []

    def should_not_queue(**kwargs):
        queue_calls.append(kwargs)
        raise AssertionError("stale proposal must fail before active-runner queue")

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        should_not_queue,
    )
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": proposed_parameters,
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 409, response.json()
    payload = response.json()
    reasons = payload["summary"]["blocked_reasons"]
    assert "runtime_recovery_proposal_stale" in reasons
    assert "runtime_recovery_proposal_origin_drift_exceeded" in reasons
    assert queue_calls == []
    receipt = payload["missionos_runtime_recovery_dispatch_receipt"]
    assert receipt["operator_approved"] is False
    assert receipt["dispatch_authority_created"] is False
    assert receipt["maneuver_approval"] == {}
    assert receipt["maneuver_allowlist"] == {}
    assert receipt["active_runner_request_queued"] is False
    assert receipt["proposal_revalidation"]["validation_status"] == "blocked"


def test_px4_fresh_bound_agent_recovery_proposal_can_queue_after_approval(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_px4_fresh_agent_recovery_proposal"
    proposed_parameters = {
        "target_x_m": 30.0,
        "target_y_m": 40.0,
        "target_altitude_m": 45.0,
    }
    proposal_origin_without_hash = {
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
    proposal_origin_sha256 = hashlib.sha256(
        json.dumps(
            proposal_origin_without_hash,
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    proposal_origin = {
        **proposal_origin_without_hash,
        "origin_sha256": proposal_origin_sha256,
    }
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="PX4 fresh agent recovery proposal",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_auto_operator_recovery_request_fresh.json"
                ),
            },
            "missionos_runtime_recovery_last_proposal": {
                "schema_version": "missionos_runtime_recovery_proposal_evidence.v1",
                "proposal_id": "runtime_recovery_proposal_fresh",
                "proposal_status": "awaiting_operator_approval",
                "observed_at": "2026-07-16T00:00:00+00:00",
                "valid_until": "9999-01-01T00:00:00+00:00",
                "origin_position": {"local_x_m": 1.0, "local_y_m": 2.0},
                "max_origin_drift_m": 5.0,
                "proposal_origin": proposal_origin,
                "proposal_origin_sha256": proposal_origin_sha256,
                "runtime_recovery_agent_result": {
                    "assessment": {
                        "recovery_planner_tool_candidate": {
                            "selected_bounded_action": "avoid_obstacle",
                            "proposed_parameters": proposed_parameters,
                        }
                    }
                },
                "dispatch_authority_created": False,
            },
            "missionos_auto_mission_runtime_snapshot": {
                "sample_index": 2,
                "elapsed_seconds": 10.0,
                "local_x_m": 1.5,
                "local_y_m": 2.5,
                "local_z_m": -30.0,
                "altitude_above_home_m": 30.0,
                # This low-level snapshot may not carry heartbeat evidence even
                # while the task-correlated recovery bridge is live.
                "heartbeat_observed": False,
                "landed": False,
            },
            "missionos_runtime_recovery_agent_live_bridge": {
                "schema_version": "missionos_runtime_recovery_agent_live_bridge.v1",
                "bridge_status": "live",
                "telemetry_snapshot": {
                    "source": "missionos_auto_mission_runtime_snapshot",
                    "sample_index": 2,
                    "position": {
                        "local_x_m": 1.5,
                        "local_y_m": 2.5,
                        "local_z_m": -30.0,
                        "altitude_above_home_m": 30.0,
                    },
                    "telemetry": {"stale": False, "dropout": False},
                },
            },
        },
    )
    queue_calls: list[dict] = []

    def queue_request(**kwargs):
        queue_calls.append(kwargs)
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo",
            "container_path": kwargs["container_path"],
            "bytes_written": 123,
        }

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        queue_request,
    )
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": proposed_parameters,
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["summary"]["dispatch_status"] == "queued_for_active_runner"
    assert payload["summary"]["dispatch_authority_created"] is True
    assert payload["summary"]["proposal_revalidation"]["validation_status"] == "valid"
    assert len(queue_calls) == 1
    queued_request = queue_calls[0]["request_payload"]
    assert queued_request["operator_approved"] is True
    assert queued_request["proposal_origin"] == proposal_origin
    assert queued_request["proposal_origin_sha256"] == proposal_origin_sha256
    assert queued_request["recovery_parameters"] == {
        **proposed_parameters,
        "obstacle_avoidance_required": True,
    }
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    stored_artifacts = stored["artifacts"]
    bound_proposal = stored_artifacts[
        "missionos_runtime_recovery_last_proposal"
    ]
    assert bound_proposal["proposal_status"] == "dispatch_authority_bound"
    assert bound_proposal["dispatch_authority_created"] is True
    assert bound_proposal["proposal_origin"] == proposal_origin
    assert bound_proposal["claimed_by_approval_ref"]
    assert stored_artifacts["missionos_runtime_recovery_proposals"][
        bound_proposal["proposal_id"]
    ] == bound_proposal


def test_px4_recovery_proposal_origin_hash_mismatch_fails_closed() -> None:
    from src.gateway import server as gateway_server

    proposed_parameters = {
        "target_x_m": 30.0,
        "target_y_m": 40.0,
        "target_altitude_m": 45.0,
    }
    evidence = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": {
                "proposal_id": "runtime_recovery_proposal_tampered_origin",
                "proposal_status": "awaiting_operator_approval",
                "observed_at": "2026-07-16T00:00:00+00:00",
                "valid_until": "9999-01-01T00:00:00+00:00",
                "origin_position": {"local_x_m": 1.0, "local_y_m": 2.0},
                "max_origin_drift_m": 5.0,
                "proposal_origin": {
                    "schema_version": (
                        "missionos_runtime_recovery_proposal_origin.v1"
                    ),
                    "origin_kind": "hosted_llm",
                    "provider": "tampered_provider",
                    "origin_sha256": "a" * 64,
                },
                "proposal_origin_sha256": "a" * 64,
                "runtime_recovery_agent_result": {
                    "assessment": {
                        "recovery_planner_tool_candidate": {
                            "selected_bounded_action": "avoid_obstacle",
                            "proposed_parameters": proposed_parameters,
                        }
                    }
                },
            },
            "missionos_auto_mission_runtime_snapshot": {
                "sample_index": 3,
                "elapsed_seconds": 11.0,
                "local_x_m": 1.0,
                "local_y_m": 2.0,
                "heartbeat_observed": True,
                "landed": False,
            },
        },
        recovery_action="avoid_obstacle",
        recovery_parameters=proposed_parameters,
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert evidence["validation_status"] == "blocked"
    assert "runtime_recovery_proposal_origin_hash_mismatch" in evidence["reasons"]


def test_turtlebot3_recovery_approval_rejects_changed_coordinates(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_changed_recovery"
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 pending recovery",
        status="pending",
        artifacts=_pending_turtlebot3_recovery_task_artifacts(),
    )
    called = False

    def should_not_resume(**kwargs):
        nonlocal called
        called = True
        raise AssertionError(kwargs)

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        should_not_resume,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {"target_x_m": 9.0, "target_y_m": 9.0},
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["summary"]["blocked_reasons"] == [
        "recovery_parameters_must_match_turtlebot3_checkpoint"
    ]
    assert called is False


def test_turtlebot3_recovery_approval_rejects_changed_reviewed_checkpoint(
    isolated_gateway_factory,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.gateway import server as gateway_server

    gateway = isolated_gateway_factory()
    task_id = "task_turtlebot3_changed_review_checkpoint"
    gateway.task_store.create(
        task_id=task_id,
        kind="turtlebot3_home_mission_execution",
        title="TurtleBot3 pending recovery",
        status="pending",
        artifacts=_pending_turtlebot3_recovery_task_artifacts(),
    )
    called = False

    def should_not_resume(**kwargs):
        nonlocal called
        called = True
        raise AssertionError(kwargs)

    monkeypatch.setattr(
        gateway_server,
        "run_turtlebot3_home_mission_dispatch",
        should_not_resume,
    )
    client = TestClient(gateway.app)

    response = client.post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": {"target_x_m": -0.2, "target_y_m": -1.4},
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": "stale-checkpoint-id",
            "expected_recovery_checkpoint_hash": "stale-checkpoint-hash",
        },
    )

    assert response.status_code == 409
    assert response.json()["summary"]["blocked_reasons"] == [
        "reviewed_turtlebot3_recovery_checkpoint_id_changed",
        "reviewed_turtlebot3_recovery_checkpoint_hash_changed",
    ]
    receipt = response.json()["missionos_runtime_recovery_dispatch_receipt"]
    assert receipt["operator_approval_attempted"] is True
    assert receipt["operator_approved"] is False
    assert receipt["operator_approved_for_current_checkpoint"] is False
    assert receipt["explicit_recovery_dispatch_approval"] is False
    assert receipt["dispatch_authority_created"] is False
    assert receipt["reviewed_recovery_checkpoint_id"] == "stale-checkpoint-id"
    assert receipt["reviewed_recovery_checkpoint_hash"] == (
        "stale-checkpoint-hash"
    )
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    assert "turtlebot3_recovery_operator_approval" not in stored["artifacts"]
    assert called is False


def test_form2a_operator_review_summary_handles_empty_public_repo(tmp_path) -> None:
    from src.gateway.missionos_knowledge_sharing import (
        build_form2a_operator_review_summary,
    )

    summary = build_form2a_operator_review_summary(artifact_root=tmp_path)

    assert summary["summary_status"] == "missing"


def test_form2a_operator_review_reports_directory_artifact_paths(tmp_path) -> None:
    from src.gateway.missionos_knowledge_sharing import (
        _form2a_human_operator_review_check,
    )

    (tmp_path / "selection_dir").mkdir()
    (tmp_path / "token_dir").mkdir()

    check = _form2a_human_operator_review_check(
        root=tmp_path,
        selection_path="selection_dir",
        selection={"response_selection_id": "selection_1"},
        token_path="token_dir",
        token={"approval_ref": "approval_1"},
        review_path="review.json",
        review={
            "schema_version": "missionos_form2a_human_operator_review.v1",
            "review_status": "approved",
            "human_operator_approval_granted_in_artifact": True,
            "response_selection_ref": "missionos_form2a_response_selection:selection_1",
            "response_selection_artifact_path": "selection_dir",
            "response_selection_artifact_sha256": "not-a-real-file-hash",
            "operator_approval_token_ref": "approval_1",
            "operator_approval_token_artifact_path": "token_dir",
            "operator_approval_token_artifact_sha256": "not-a-real-file-hash",
            "llm_judgment_in_gate": False,
        },
    )

    assert check["approved"] is False
    assert "form2a_human_operator_review_selection_artifact_not_file" in check["blocking_reasons"]
    assert "form2a_human_operator_review_token_artifact_not_file" in check["blocking_reasons"]


def test_agent_runtime_without_api_key_falls_back_without_adk_invocation(monkeypatch) -> None:
    from src.intelligence import missionos_agent_runtime

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "gemini")
    monkeypatch.setenv(
        missionos_agent_runtime.MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV,
        "1",
    )
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    monkeypatch.setattr(
        missionos_agent_runtime,
        "_configure_google_adk_environment",
        lambda _agent_name=None: None,
    )

    result = missionos_agent_runtime.run_missionos_agent_runtime(
        utterance="New York Public Library -> Brooklyn Bridge",
        missionos_state={},
    )

    assert result["runtime_status"] == "not_configured"
    assert result["blocking_reasons"] == ["GOOGLE_API_KEY_not_configured"]
    assert result["agent_invocations"] == []


def test_live_sitl_autostart_refuses_existing_fixture_backend() -> None:
    try:
        missionos_cli._ensure_gateway(
            _FixtureHealthClient(),  # type: ignore[arg-type]
            "http://127.0.0.1:18791",
            autostart=True,
            enable_live_sitl=True,
        )
    except Exception as exc:
        assert "fixture Gateway is already running" in str(exc)
        assert "gateway restart --enable-live-sitl" in str(exc)
    else:
        raise AssertionError("fixture backend was reused for live SITL")
