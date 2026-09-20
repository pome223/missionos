"""Active topology and fail-closed production routing contracts."""

import pytest

from src.agents.missionos_agents import build_missionos_agent, MISSIONOS_OMITTED_AGENTS
from src.gateway import server
from src.intelligence.missionos_agent_topology import describe_agent_runtime


@pytest.mark.parametrize("name", MISSIONOS_OMITTED_AGENTS)
def test_omitted_roles_cannot_be_built_by_runtime(name):
    with pytest.raises(KeyError):
        build_missionos_agent(name)


def test_graph_failure_never_invokes_legacy_router(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", "1")
    monkeypatch.setenv("MISSIONOS_ADK_V2_GRAPH_PRIMARY", "1")
    monkeypatch.setenv("MISSIONOS_ADK_V2_GRAPH_ROLLBACK", "0")
    monkeypatch.setattr(
        server,
        "run_missionos_agent_runtime",
        lambda **kwargs: {
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": ["test_model_failure"],
            "agent_invocations": [],
            "workflow_execution_mode": "adk_v2_graph_primary",
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("failed graph fell through to legacy routing")

    monkeypatch.setattr(server, "run_llm_dialogue_router", forbidden)
    for name in (
        "build_form2a_response_selection_summary",
        "build_form2a_operator_review_summary",
        "build_form2a_action_consumption_summary",
    ):
        monkeypatch.setattr(server, name, lambda: {})
    result = server.run_missionos_autonomy_conversation({"text": "現在の状態を確認してください"})
    assert result["routed_action"] == "agent_graph_blocked"
    assert result["operation_result"] == {}
    assert result["progress_counted"] is False


def test_topology_distinguishes_configuration_from_model_health(monkeypatch):
    monkeypatch.setenv("MISSIONOS_ADK_V2_GRAPH_PRIMARY", "1")
    monkeypatch.setenv("MISSIONOS_ADK_V2_GRAPH_ROLLBACK", "0")
    result = describe_agent_runtime()
    assert result["agent_count"] == 7
    assert not set(result["agents"]) & set(result["omitted_agents"])
    assert set(result["specialist_by_intent"].values()) <= set(result["agents"])
    assert result["llm_health"] == "not_probed"
