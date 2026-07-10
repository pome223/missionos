from collections.abc import Callable

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


def test_ollama_backend_uses_local_model_label(monkeypatch) -> None:
    from src.agents import model_config

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("MISSIONOS_OLLAMA_MODEL", "gemma4:26b")

    assert model_config.local_llm_backend_enabled() is True
    assert model_config.agent_model_label() == "ollama_chat/gemma4:26b"


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
