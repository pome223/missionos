from __future__ import annotations

import pytest

from src.intelligence import llm_dialogue_router
from src.intelligence import missionos_agent_runtime

pytestmark = pytest.mark.contract


def _router_json(intent: str = "mission_designer_plan") -> str:
    return (
        "{"
        f'"intent":"{intent}",'
        '"operator_instruction":"東京駅から秋葉原駅までのドローン配送ミッション",'
        '"reason":"The operator asked to plan a drone mission.",'
        '"requires_human_approval":true'
        "}"
    )


def test_dialogue_router_salvages_json_object_from_local_model_preamble(monkeypatch) -> None:
    async def fake_invoke_adk_gemini_async(**_kwargs: object) -> str:
        return "Thinking...\nI will route this safely.\n```json\n" + _router_json() + "\n```"

    monkeypatch.setenv(llm_dialogue_router.LLM_DIALOGUE_ROUTER_ADK_ENABLED_ENV, "1")
    monkeypatch.setattr(
        llm_dialogue_router,
        "_invoke_adk_gemini_async",
        fake_invoke_adk_gemini_async,
    )

    result = llm_dialogue_router.run_llm_dialogue_router(
        "東京駅から秋葉原駅まで飛んで",
        missionos_state={},
    )

    assert result["router_status"] == "proposal_guardrail_passed"
    assert result["proposal"]["intent"] == "mission_designer_plan"
    assert result["json_parse"]["strategy"] == "salvaged_json_object"
    assert result["json_parse"]["salvaged"] is True


def test_dialogue_router_salvage_still_blocks_forbidden_keys(monkeypatch) -> None:
    async def fake_invoke_adk_gemini_async(**_kwargs: object) -> str:
        return (
            "Here is the JSON:\n"
            "{"
            '"intent":"execute",'
            '"operator_instruction":"run it",'
            '"reason":"bad output",'
            '"requires_human_approval":true,'
            '"approved":true'
            "}"
        )

    monkeypatch.setenv(llm_dialogue_router.LLM_DIALOGUE_ROUTER_ADK_ENABLED_ENV, "1")
    monkeypatch.setattr(
        llm_dialogue_router,
        "_invoke_adk_gemini_async",
        fake_invoke_adk_gemini_async,
    )

    result = llm_dialogue_router.run_llm_dialogue_router(
        "fly",
        missionos_state={},
    )

    assert result["router_status"] == "guardrail_blocked"
    assert "forbidden_key_present:approved" in result["blocking_reasons"]
    assert result["json_parse"]["strategy"] == "salvaged_json_object"


def test_dialogue_router_json_salvage_ignores_braces_inside_strings() -> None:
    raw_output, parse = llm_dialogue_router._load_dialogue_router_json_object(
        'Thinking...\n{"intent":"mission_designer_plan",'
        '"operator_instruction":"drop at gate }B and keep planning",'
        '"reason":"The brace in the string is not an object terminator.",'
        '"requires_human_approval":true}\nDone.'
    )

    assert raw_output is not None
    assert raw_output["operator_instruction"] == "drop at gate }B and keep planning"
    assert parse["strategy"] == "salvaged_json_object"


def test_dialogue_router_json_salvage_handles_escaped_quotes_before_braces() -> None:
    raw_output, parse = llm_dialogue_router._load_dialogue_router_json_object(
        'Preamble {"intent":"mission_designer_plan",'
        '"operator_instruction":"operator said \\"avoid } here\\" before route",'
        '"reason":"escaped quote keeps the parser inside the string",'
        '"requires_human_approval":true} trailing text'
    )

    assert raw_output is not None
    assert raw_output["operator_instruction"] == 'operator said "avoid } here" before route'
    assert parse["strategy"] == "salvaged_json_object"


def test_dialogue_router_json_salvage_stops_at_first_fenced_object() -> None:
    raw_output, parse = llm_dialogue_router._load_dialogue_router_json_object(
        "Thinking...\n"
        "```json\n"
        + _router_json()
        + "\n```\n"
        "Another object follows: "
        '{"intent":"status","operator_instruction":"second",'
        '"reason":"should not be selected","requires_human_approval":false}'
    )

    assert raw_output is not None
    assert raw_output["intent"] == "mission_designer_plan"
    assert raw_output["operator_instruction"] == "東京駅から秋葉原駅までのドローン配送ミッション"
    assert parse["strategy"] == "salvaged_json_object"


def test_dialogue_router_reports_unparseable_json_without_jsondecodeerror(monkeypatch) -> None:
    async def fake_invoke_adk_gemini_async(**_kwargs: object) -> str:
        return "Thinking...\nNo object here."

    monkeypatch.setenv(llm_dialogue_router.LLM_DIALOGUE_ROUTER_ADK_ENABLED_ENV, "1")
    monkeypatch.setattr(
        llm_dialogue_router,
        "_invoke_adk_gemini_async",
        fake_invoke_adk_gemini_async,
    )

    result = llm_dialogue_router.run_llm_dialogue_router(
        "status",
        missionos_state={},
    )

    assert result["router_status"] == "not_configured"
    assert result["blocking_reasons"][0].startswith(
        "adk_error:dialogue_router_json_unparseable:"
    )
    assert result["json_parse"]["strategy"] == "failed"


def test_agent_runtime_error_label_uses_configured_provider(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setattr(
        missionos_agent_runtime,
        "_invoke_adk_agent_text",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    evidence = missionos_agent_runtime._run_agent_once(
        agent_name="missionos_chief_agent",
        agent_role="MissionOS chief coordinator agent",
        prompt_payload={},
    )

    assert evidence["guardrail_result"]["blocking_reasons"] == [
        "google_adk_litellm_ollama_agent_invocation_failed:TimeoutError"
    ]
