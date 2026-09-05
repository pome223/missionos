from __future__ import annotations

from src.agents.missionos_agents import _json_config
from src.intelligence.missionos_agent_runtime import _runtime_recovery_output_schema


def test_deepseek_agents_use_prompt_json_without_response_format(monkeypatch) -> None:
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_LLM_BACKEND",
        "deepseek",
    )

    config = _json_config("missionos_runtime_recovery_agent")

    assert config.response_mime_type is None
    assert _runtime_recovery_output_schema() is None


def test_non_deepseek_agents_keep_structured_json_contract(monkeypatch) -> None:
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_LLM_BACKEND",
        "ollama",
    )

    config = _json_config("missionos_runtime_recovery_agent")

    assert config.response_mime_type == "application/json"
    assert _runtime_recovery_output_schema() is not None
