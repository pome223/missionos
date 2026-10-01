"""Bounded MissionOS DeepSeek agents for Yokohama delivery, invoked only on the Gateway host.

The planner interprets a chat request against a one-route catalog; it cannot add
destinations or approve. The pad judge sees only facts about a pad the Rules
already found clear, and its answer can only add a bounded wait. Both outputs are
validated here and again by their consumers. The simulator never receives keys.
"""

import os

from src.runtime.yokohama_payload import digest

PLANNER = "missionos_yokohama_delivery_planner_agent"
JUDGE = "missionos_yokohama_pad_judge_agent"
PROVIDER = "google_adk_litellm_deepseek"
DESTINATION = "yokohama_harbour_pad"


def configuration():
    from src.agents.model_config import agent_model_label, llm_provider_label

    if os.getenv("RUN_MISSIONOS_YOKOHAMA_AGENTS") != "1":
        raise ValueError("横浜配送のAgentが有効になっていません。")
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise ValueError("DeepSeekの接続情報がありません。")
    backend = os.environ.get("MISSIONOS_YOKOHAMA_JUDGE_BACKEND", "deepseek")
    if backend not in {"deepseek", "jev"}:
        raise ValueError("Unknown pad judge backend")
    names = (PLANNER, JUDGE) if backend == "deepseek" else (PLANNER,)
    if any(llm_provider_label(name) != PROVIDER for name in names):
        raise ValueError("横浜配送のAgentにはDeepSeek接続が必要です。")
    from src.intelligence.yokohama_pad_jev import configuration as pad_configuration

    selected_judge = (
        pad_configuration()
        if backend == "jev"
        else dict(agent_name=JUDGE, model_id=agent_model_label(agent_name=JUDGE))
    )
    return dict(
        provider=PROVIDER,
        planner=dict(agent_name=PLANNER, model_id=agent_model_label(agent_name=PLANNER)),
        judge=selected_judge,
        planner_timeout_seconds=30,
        judge_timeout_seconds=20,
    )


def _run(agent_name, role, payload, timeout):
    from src.intelligence.missionos_agent_runtime import _run_agent_once

    return _run_agent_once(
        agent_name=agent_name,
        agent_role=role,
        prompt_payload=payload,
        validate_intent=False,
        timeout_seconds=timeout,
    )


def valid_plan(output):
    return bool(
        isinstance(output, dict)
        and set(output) == {"supported", "destination_id", "summary", "reason"}
        and isinstance(output["supported"], bool)
        and all(isinstance(output[k], str) for k in ("destination_id", "summary", "reason"))
        and (
            (output["supported"] and output["destination_id"] == DESTINATION and output["summary"])
            or (not output["supported"] and output["destination_id"] == "" and output["reason"])
        )
    )


def plan(text, expected):
    """Interpret one chat request; raise unless DeepSeek returns a valid catalog reading."""
    if configuration() != expected:
        raise ValueError("横浜配送のAgent設定が変わりました。もう一度依頼してください。")
    evidence = _run(
        PLANNER,
        "Yokohama harbour delivery request interpreter",
        dict(operator_request=text[:2000], catalog_destination_id=DESTINATION),
        expected["planner_timeout_seconds"],
    )
    guard = evidence["guardrail_result"]
    output = guard.get("validated_output", {}) if guard["guardrail_passed"] else {}
    if not valid_plan(output):
        raise ValueError("DeepSeekの計画応答を確認できませんでした。もう一度依頼してください。")
    return dict(output=output, invocation=evidence)


def valid_decision(request, output):
    if not isinstance(output, dict) or set(output) != {
        "observation_id",
        "action",
        "wait_seconds",
        "rationale",
    }:
        return False
    seconds = output["wait_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return False
    return (
        output["observation_id"] == request["observation_id"]
        and isinstance(output["rationale"], str)
        and bool(output["rationale"].strip())
        and (
            (output["action"] == "enter" and seconds == 0)
            or (output["action"] == "wait" and 1 <= seconds <= request["remaining_wait_seconds"])
        )
    )


def judge(request, expected):
    """Answer one pad-judge request; any failure is reported, never turned into entry."""
    pad_config = expected.get("judge", expected)
    if pad_config.get("backend") == "jev":
        from src.intelligence.yokohama_pad_jev import judge as pad_judge

        return pad_judge(request, pad_config)
    base = dict(judge_request_id=request["judge_request_id"], request_sha256=digest(request))
    try:
        if configuration() != expected:
            raise ValueError("configuration changed after approval")
        evidence = _run(
            JUDGE,
            "Delivery pad entry judge",
            dict(
                observation_id=request["observation_id"],
                situation=request["situation"],
                remaining_wait_seconds=request["remaining_wait_seconds"],
                decisions_remaining=request["decisions_remaining"],
                allowed_actions=request["allowed_actions"],
            ),
            expected["judge_timeout_seconds"],
        )
    except Exception as exc:  # noqa: BLE001 - provider failures are recorded, not raised.
        return dict(base, judge_status="unavailable", error_type=type(exc).__name__)
    guard = evidence["guardrail_result"]
    output = guard.get("validated_output", {}) if guard["guardrail_passed"] else {}
    if not valid_decision(request, output):
        return dict(base, judge_status="invalid", invocation=evidence)
    return dict(base, judge_status="valid", decision=output, invocation=evidence)
