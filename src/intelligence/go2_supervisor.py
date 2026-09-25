"""A bounded MissionOS ADK judge, invoked only on the Gateway host."""

import os

from src.runtime.go2_supervision import digest


AGENT = "missionos_go2_supervisor_agent"
MODEL = "deepseek-flash"


def configuration():
    from src.agents.model_config import agent_model_label, llm_provider_label

    if os.getenv("RUN_MISSIONOS_GO2_SUPERVISOR") != "1":
        raise ValueError("Go2のAgent管制が有効になっていません。")
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise ValueError("管制Agentの接続情報がありません。")
    if (
        agent_model_label(agent_name=AGENT) != MODEL
        or llm_provider_label(AGENT) != "google_adk_litellm_deepseek"
    ):
        raise ValueError("Go2管制には明示的に選択したdeepseek-flash接続が必要です。")
    return {
        "agent_name": AGENT,
        "provider": "google_adk_litellm_deepseek",
        "model_id": MODEL,
        "max_output_tokens": 768,
        "timeout_seconds": 25,
        "max_decisions": 3,
    }


def judge(request, expected_configuration):
    if configuration() != expected_configuration:
        raise ValueError("承認後に管制モデルの設定が変わりました。")
    from src.intelligence.missionos_agent_runtime import _run_agent_once

    evidence = _run_agent_once(
        agent_name=AGENT,
        agent_role="Go2 delivery exception supervisor",
        prompt_payload=request,
        validate_intent=False,
        timeout_seconds=25,
    )
    guard = evidence["guardrail_result"]
    return {
        "request_sha256": digest(request),
        "judge_status": "valid" if guard["guardrail_passed"] else "invalid",
        "proposal": guard.get("validated_output", {}),
        "invocation": evidence,
    }
