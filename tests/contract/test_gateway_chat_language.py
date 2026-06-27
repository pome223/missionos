from __future__ import annotations

import re
from typing import Any, Mapping

import src.gateway.server as gateway_server


JAPANESE_TEXT = re.compile(r"[ぁ-んァ-ン一-龥]")


def _install_quiet_conversation_dependencies(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **_kwargs: {
            "runtime_status": "not_configured",
            "agent_invocations": [],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(
        gateway_server,
        "run_llm_dialogue_router",
        lambda *_args, **_kwargs: {"router_status": "not_configured"},
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})


def _registered_plan_context(session_id: str) -> dict[str, Any]:
    return gateway_server._missionos_register_mission_designer_context(
        {
            "scenario_proposal": {
                "proposal_id": "proposal_language_test",
                "mission_objective": "Source-backed route language test",
            },
            "validation_result": {"validation_status": "accepted"},
            "summary": {"mission_objective": "Source-backed route language test"},
        },
        session_id=session_id,
    )


def _fake_approval_result(
    *,
    proposal: Mapping[str, Any],
    validation: Mapping[str, Any],
    now: Any | None = None,
) -> dict[str, Any]:
    del proposal, validation, now
    return {
        "scenario_approval": {"approval_status": "approved"},
        "scenario_compile_result": {"compile_status": "compiled"},
        "bounded_simulation_request": {"request_status": "prepared_for_operator"},
        "summary": {
            "approval_status": "approved",
            "operator_approved": True,
            "approved_for_bounded_simulation": True,
            "gazebo_execution_invoked": False,
            "progress_counted": False,
        },
    }


def test_chat_approve_and_prepare_messages_are_english(monkeypatch: Any) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    monkeypatch.setattr(
        gateway_server,
        "approve_px4_gazebo_mission_scenario_for_bounded_simulation",
        _fake_approval_result,
    )

    session_id = "chat-language-session"
    plan_context = _registered_plan_context(session_id)

    approval = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )

    assert approval["routed_action"] == "approve"
    assert "Approval recorded." in approval["message"]
    assert not JAPANESE_TEXT.search(approval["message"])

    def fake_prepare(context: Mapping[str, Any]) -> dict[str, Any]:
        summary = context.get("summary") if isinstance(context.get("summary"), Mapping) else {}
        return gateway_server._missionos_register_mission_designer_context(
            {
                **dict(context),
                "sitl_execution_request": {"request_status": "prepared"},
                "summary": {
                    **dict(summary),
                    "sitl_execution_task_id": "task_chat_language",
                    "progress_counted": False,
                },
            },
            session_id=session_id,
        )

    monkeypatch.setattr(
        gateway_server,
        "_missionos_prepare_mission_designer_sitl_context",
        fake_prepare,
    )

    prepared = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "execute",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approval["mission_designer"],
        }
    )

    assert prepared["routed_action"] == "execute"
    assert "SITL execution request prepared" in prepared["message"]
    assert not JAPANESE_TEXT.search(prepared["message"])


def test_chat_status_prompts_are_english(monkeypatch: Any) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    monkeypatch.setattr(
        gateway_server,
        "approve_px4_gazebo_mission_scenario_for_bounded_simulation",
        _fake_approval_result,
    )

    session_id = "chat-language-status-session"
    plan_context = _registered_plan_context(session_id)

    waiting_for_approval = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "status",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )

    assert "Type `approve`" in waiting_for_approval["message"]
    assert not JAPANESE_TEXT.search(waiting_for_approval["message"])

    approved = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )
    waiting_for_prepare = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "status",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approved["mission_designer"],
        }
    )

    assert "Type `prepare`" in waiting_for_prepare["message"]
    assert not JAPANESE_TEXT.search(waiting_for_prepare["message"])


def test_chat_repair_intent_hands_off_to_repair_capability(monkeypatch: Any) -> None:
    repair_contexts: list[Mapping[str, Any]] = []

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **_kwargs: {
            "runtime_status": "proposal_guardrail_passed",
            "proposal": {
                "intent": "repair",
                "operator_instruction": "diagnose the blocked mission",
                "specialist_agent": "missionos_repair_planner_agent",
            },
            "agent_invocations": [
                {
                    "agent_name": "missionos_chief_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/chief.json",
                },
                {
                    "agent_name": "missionos_repair_planner_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/repair.json",
                },
                {
                    "agent_name": "missionos_safety_critic_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/critic.json",
                },
            ],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(
        gateway_server,
        "run_llm_dialogue_router",
        lambda *_args, **_kwargs: {"router_status": "not_configured"},
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})

    def fake_repair_planner(
        *,
        capability_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        repair_contexts.append(capability_context)
        return {
            "summary_status": "proposal_guardrail_passed",
            "planner_status": "proposal_guardrail_passed",
            "proposal": {"repair_target": "collect_more_runtime_evidence"},
        }

    monkeypatch.setattr(
        gateway_server,
        "run_llm_repair_planner_from_latest_evidence",
        fake_repair_planner,
    )

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "repair this blocked mission",
            "missionos_client_surface": "chat",
            "session_id": "repair-handoff-session",
        }
    )

    assert response["routed_action"] == "repair"
    assert "Repair Agent" in response["message"]
    assert response["operation_result"]["capability_surface"]["capability_id"] == (
        "llm_repair_planning"
    )
    assert response["operation_result"]["capability_surface"]["coordinating_agent"] == (
        "missionos_repair_planner_agent"
    )
    assert response["operation_result"]["repair_agent_handoff"]["repair_phase"] == (
        "post_block_or_next_run_planning"
    )
    assert response["operation_result"]["repair_agent_handoff"]["input_scope"] == (
        "latest_blocked_or_failed_evidence"
    )
    assert response["missionos_repair_agent_capability_handoff"]["capability_id"] == (
        "llm_repair_planning"
    )
    assert response["repair"]["repair_agent_handoff"]["dispatch_authority_created"] is False

    assert len(repair_contexts) == 1
    context = repair_contexts[0]
    assert context["requested_by"] == "missionos_chief_agent"
    assert context["specialist_agent_invocation_ref"].endswith(
        "missionos_agent_invocations/repair.json"
    )


def test_chat_repair_uses_current_mission_designer_context(monkeypatch: Any) -> None:
    evidence_payloads: list[Mapping[str, Any]] = []

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **_kwargs: {
            "runtime_status": "proposal_guardrail_passed",
            "proposal": {
                "intent": "repair",
                "operator_instruction": "repair the heavy wind plan",
                "specialist_agent": "missionos_repair_planner_agent",
            },
            "agent_invocations": [
                {
                    "agent_name": "missionos_chief_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/chief.json",
                },
                {
                    "agent_name": "missionos_repair_planner_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/repair.json",
                },
                {
                    "agent_name": "missionos_safety_critic_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/critic.json",
                },
            ],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(
        gateway_server,
        "run_llm_dialogue_router",
        lambda *_args, **_kwargs: {"router_status": "not_configured"},
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})
    monkeypatch.setattr(
        gateway_server,
        "run_llm_repair_planner_from_latest_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("latest evidence path should not be used")
        ),
    )

    def fake_context_repair_planner(
        *,
        evidence_artifact: Mapping[str, Any],
        evidence_label: str,
        capability_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        del capability_context
        evidence_payloads.append(evidence_artifact)
        return {
            "summary_status": "blocked",
            "repair_proposal": {
                "planner_status": "not_configured",
                "repair_target": "",
            },
            "input_evidence": {"artifact_path": "missionos_repair_input_evidence.json"},
            "evidence_label": evidence_label,
        }

    monkeypatch.setattr(
        gateway_server,
        "run_llm_repair_planner_from_evidence_payload",
        fake_context_repair_planner,
    )

    registered_context = gateway_server._missionos_register_mission_designer_context(
        {
            "scenario_proposal": {"proposal_id": "proposal_heavy_wind"},
            "validation_result": {"validation_status": "accepted"},
            "mission_designer_coordinate_pair_route": {
                "takeoff_label": "Tokyo Station",
                "dropoff_label": "Akihabara",
                "wind_speed_mps": 14.0,
                "payload_weight_kg": 4.0,
                "requested_total_payload_weight_kg": 8.0,
            },
            "missionos_payload_split_plan": {
                "plan_status": "split_required",
                "requested_payload_weight_kg": 8.0,
                "sortie_count": 2,
            },
            "summary": {"sitl_execution_task_id": "task_heavy_wind"},
        },
        session_id="repair-context-session",
    )

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "repair this heavy payload and wind case",
            "missionos_client_surface": "chat",
            "session_id": "repair-context-session",
            "mission_designer_context": registered_context,
        }
    )

    assert response["routed_action"] == "repair"
    assert "current Mission Designer evidence" in response["message"]
    assert response["operation_result"]["repair_agent_handoff"]["input_scope"] == (
        "mission_designer_context"
    )
    assert len(evidence_payloads) == 1
    evidence = evidence_payloads[0]
    assert evidence["evidence_label"] == "mission_designer_context"
    assert evidence["task_id"] == "task_heavy_wind"
    assert "payload_split_required" in evidence["blocking_reasons"]
    assert "wind_over_live_sitl_contract" in evidence["blocking_reasons"]
    assert evidence["source_boundary"]["context_ref_verified_server_side"] is True
    repair_warnings = response["operation_result"]["repair_followup_warnings"]
    assert any("Live SITL remains blocked" in warning for warning in repair_warnings)
    assert any("Payload split remains" in warning for warning in repair_warnings)


def test_blocked_mission_designer_context_offers_repair_prompt() -> None:
    context = gateway_server._missionos_register_mission_designer_context(
        {
            "scenario_proposal": {"proposal_id": "proposal_repair_prompt"},
            "validation_result": {"validation_status": "accepted"},
            "mission_designer_coordinate_pair_route": {
                "takeoff_label": "Tokyo Station",
                "dropoff_label": "Akihabara",
                "wind_speed_mps": 14.0,
                "payload_weight_kg": 4.0,
                "requested_total_payload_weight_kg": 8.0,
            },
            "missionos_payload_split_plan": {
                "plan_status": "split_required",
                "requested_payload_weight_kg": 8.0,
                "sortie_count": 2,
            },
        },
        session_id="repair-prompt-session",
    )

    evidence = gateway_server._missionos_repair_evidence_from_mission_designer_context(
        context,
        operator_instruction="Tokyo Station -> Akihabara with heavy payload and strong wind",
    )
    prompt = gateway_server._missionos_repair_prompt_from_evidence(evidence)

    assert evidence["summary_status"] == "blocked"
    assert evidence["blocking_reasons"] == [
        "wind_over_live_sitl_contract",
        "payload_split_required",
    ]
    assert prompt["prompt_status"] == "repair_available"
    assert prompt["suggested_command"] == "/repair"
    assert "Type `/repair`" in prompt["operator_prompt"]
    assert prompt["dispatch_authority_created"] is False
    assert prompt["progress_counted"] is False
